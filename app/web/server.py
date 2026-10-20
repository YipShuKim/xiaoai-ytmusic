"""统一 HTTP 服务：Web 控制台 + 小爱控制 API + YouTube Music 登录与桥接 API + Range 音频流 + DLNA 端点"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from app import __version__
from app.config import AppConfig
from app.engine.dlna import DLNAReceiver
from app.engine.player import PlayerManager
from app.engine.voice import ConversationMonitor, VoiceCommandEngine
from app.xiaomi.auth import XiaomiAuthManager
from app.xiaomi.client import XiaomiClient
from app.ytmusic.client import Track, YTMusicClient
from app.ytmusic.cookies import inspect_cookie_file, save_cookies_any_format
from app.ytmusic.downloader import AudioDownloader, validate_video_id
from app.ytmusic.matching import match_score, normalize
from app.ytmusic.playlists import PlaylistStore

log = logging.getLogger("xiaoai-ytmusic.web")

STATIC_DIR = Path(__file__).parent / "static"
SERVER_KEY = web.AppKey("server", "ApplicationServer")


class SearchSessionStore:
    """兼容 Xeltra233/youtube-music-api 的 /search -> /download 会话缓存"""

    def __init__(self, ttl_sec: int = 600):
        self.ttl_sec = ttl_sec
        self._store: dict[str, tuple[float, list[Track]]] = {}

    def put(self, tracks: list[Track]) -> str:
        self.cleanup()
        sid = secrets.token_urlsafe(12)
        self._store[sid] = (time.time() + self.ttl_sec, tracks)
        return sid

    def get(self, sid: str) -> list[Track] | None:
        self.cleanup()
        item = self._store.get(sid)
        if not item:
            return None
        return item[1]

    def cleanup(self) -> None:
        now = time.time()
        for k, (exp, _) in list(self._store.items()):
            if exp <= now:
                self._store.pop(k, None)


class ApplicationServer:
    """全栈整合服务容器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.yt_client = YTMusicClient(
            proxy=config.ytmusic_proxy,
            cookies_file=config.cookies_file,
            hl=config.ytmusic_hl,
            gl=config.ytmusic_gl,
        )
        self.downloader = AudioDownloader(config, self.yt_client)
        self.playlist_store = PlaylistStore(config, self.yt_client)
        self.xiaomi_auth = XiaomiAuthManager(config)
        self.xiaomi_client = XiaomiClient(config, self.xiaomi_auth)
        self.players = PlayerManager(config, self.xiaomi_client, self.yt_client, self.downloader)
        self.voice_engine = VoiceCommandEngine(
            config, self.xiaomi_client, self.players, self.playlist_store
        )
        self.monitor = ConversationMonitor(
            config, self.xiaomi_client, self.voice_engine, self.players
        )
        self.dlna = DLNAReceiver(config, self.xiaomi_client, self.players)
        self.search_sessions = SearchSessionStore()

    def create_web_app(self) -> web.Application:
        app = web.Application(
            client_max_size=10 * 1024 * 1024,
            middlewares=[self._auth_middleware, self._error_middleware],
        )
        app[SERVER_KEY] = self
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)

        # 静态首页与图标
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/favicon.ico", self.handle_favicon)

        # 健康检查（兼容 ytmusic-bridge /healthz 与 miair-next /api/v1/health）
        app.router.add_get("/healthz", self.handle_health)
        app.router.add_get("/api/health", self.handle_health)
        app.router.add_get("/api/v1/health", self.handle_health)

        # 兼容 Xeltra233/youtube-music-api 的 Bot 接口 + 局域网音频直链
        app.router.add_post("/search", self.handle_ytmusic_search)
        app.router.add_post("/api/ytmusic/search", self.handle_ytmusic_search)
        app.router.add_post("/download", self.handle_ytmusic_download)
        app.router.add_post("/api/ytmusic/download", self.handle_ytmusic_download)
        app.router.add_get("/file/{token}", self.handle_serve_token)
        app.router.add_get("/stream/{filename}", self.handle_serve_stream)

        # YouTube Music 账号登录、状态检测与连通性诊断
        app.router.add_get("/api/ytmusic/auth-status", self.handle_ytmusic_auth_status)
        app.router.add_post("/api/ytmusic/login", self.handle_upload_cookies)
        app.router.add_post("/api/ytmusic/logout", self.handle_ytmusic_logout)
        app.router.add_post("/api/ytmusic/diagnose", self.handle_ytmusic_diagnose)
        app.router.add_post("/api/cookies", self.handle_upload_cookies)

        # 播放列表（歌单）同步与管理
        app.router.add_get("/api/playlists", self.handle_list_playlists)
        app.router.add_get("/api/playlists/{playlist_id}", self.handle_get_playlist)
        app.router.add_post("/api/playlists/sync", self.handle_sync_playlists)
        app.router.add_post("/api/playlists/import", self.handle_import_playlist)
        app.router.add_post("/api/playlists/save-queue", self.handle_save_queue_playlist)
        app.router.add_post("/api/playlists/play", self.handle_play_playlist)
        app.router.add_delete("/api/playlists/{playlist_id}", self.handle_delete_playlist)

        # 小米账号认证与音箱设备管理
        app.router.add_post("/api/xiaomi/qr/start", self.handle_qr_start)
        app.router.add_get("/api/xiaomi/qr/poll", self.handle_qr_poll)
        app.router.add_post("/api/xiaomi/login/password", self.handle_login_password)
        app.router.add_post("/api/xiaomi/login/cookie", self.handle_login_cookie)
        app.router.add_get("/api/xiaomi/devices", self.handle_get_devices)
        app.router.add_post("/api/xiaomi/devices/select", self.handle_select_devices)

        # 播放器与音箱控制
        app.router.add_get("/api/player/status", self.handle_player_status)
        app.router.add_post("/api/player/search-and-play", self.handle_search_and_play)
        app.router.add_post("/api/player/play-track", self.handle_play_track)
        app.router.add_post("/api/player/control", self.handle_player_control)

        # 语音口令测试与对话记录
        app.router.add_get("/api/voice/logs", self.handle_voice_logs)
        app.router.add_post("/api/voice/test", self.handle_voice_test)

        # 全局配置与缓存管理
        app.router.add_get("/api/config", self.handle_get_config)
        app.router.add_put("/api/config", self.handle_update_config)
        app.router.add_post("/api/cache/clear", self.handle_clear_cache)

        # DLNA UPnP 路由
        app.router.add_get("/dlna/device.xml", self.dlna.handle_device_xml)
        app.router.add_get("/dlna/{service}.xml", self.dlna.handle_scpd_xml)
        app.router.add_route("*", "/dlna/{service}/event", self.dlna.handle_event_sub)
        app.router.add_post("/dlna/{service}/control", self.dlna.handle_control)

        return app

    async def _on_startup(self, app: web.Application) -> None:
        if self.xiaomi_auth.is_authenticated() or (
            self.config.mi_user_id and self.config.mi_pass_token
        ):
            try:
                await self.xiaomi_client.get_devices(force_refresh=True)
            except Exception as e:
                log.warning(f"启动时拉取小爱设备列表失败: {e}")

        await self.monitor.start()
        if self.config.dlna_enabled:
            await self.dlna.start()
        # 若已有登录 Cookie 且尚未同步过歌单，启动后台自动同步
        if inspect_cookie_file(self.config.cookies_file).logged_in and not self.playlist_store.list_summaries():
            asyncio.create_task(self.playlist_store.sync_from_ytmusic())

    async def _on_cleanup(self, app: web.Application) -> None:
        await self.monitor.stop()
        await self.dlna.stop()
        await self.players.stop_all()
        await self.xiaomi_client.close()
        await self.xiaomi_auth.close()
        await self.yt_client.close()

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            log.warning(f"Request error on {request.method} {request.path}: {e}")
            return web.json_response(
                {"code": "INTERNAL_ERROR", "error": str(e), "message": str(e)},
                status=500,
            )

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler) -> web.StreamResponse:
        path = request.path
        if (
            path in ("/", "/favicon.ico", "/healthz", "/api/health", "/api/v1/health")
            or path.startswith("/stream/")
            or path.startswith("/file/")
            or path.startswith("/dlna/")
        ):
            return await handler(request)

        if path in ("/search", "/download") and self.config.api_key:
            key = request.headers.get("X-API-Key", "")
            if key != self.config.api_key:
                return web.json_response({"code": "UNAUTHORIZED", "message": "Invalid API Key"}, status=401)
            return await handler(request)

        if path.startswith("/api/") and self.config.admin_password:
            token = (
                request.headers.get("X-Admin-Password", "")
                or request.query.get("admin_password", "")
            )
            if token != self.config.admin_password:
                return web.json_response(
                    {"code": "UNAUTHORIZED", "error": "需要管理员密码"},
                    status=401,
                )

        return await handler(request)

    def _resolve_device_id(self, requested: str = "") -> str:
        if requested and requested.strip():
            return requested.strip()
        if self.config.default_device_id:
            return self.config.default_device_id
        if self.config.enabled_device_ids:
            return self.config.enabled_device_ids[0]
        return ""

    # ---- Handlers ----

    async def handle_index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def handle_favicon(self, request: web.Request) -> web.Response:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="8" fill="#ef4444"/>'
            '<polygon points="12,9 24,16 12,23" fill="#fff"/></svg>'
        )
        return web.Response(text=svg, content_type="image/svg+xml")

    async def handle_health(self, request: web.Request) -> web.Response:
        yt_status = inspect_cookie_file(self.config.cookies_file)
        return web.json_response(
            {
                "status": "ok",
                "version": __version__,
                "xiaomi_authenticated": self.xiaomi_auth.is_authenticated(),
                "ytmusic_logged_in": yt_status.logged_in,
                "voice_monitor_running": self.monitor.get_status()["running"],
                "lan_base_url": self.config.base_url(),
            }
        )

    async def handle_ytmusic_search(self, request: web.Request) -> web.Response:
        body = await request.json() if request.body_exists else {}
        query = str(body.get("query") or "").strip()
        if not query:
            return web.json_response({"code": "BAD_REQUEST", "message": "query is required"}, status=400)

        limit = int(body.get("limit") or self.config.default_search_limit)
        if limit < 1:
            return web.json_response({"code": "BAD_REQUEST", "message": "limit must be >= 1"}, status=400)
        limit = min(limit, self.config.max_search_limit)
        min_score = float(body.get("min_score") if body.get("min_score") is not None else self.config.min_match_score)

        try:
            tracks = await self.yt_client.search(query, limit=limit, min_score=min_score)
        except Exception as e:
            return web.json_response(
                {
                    "code": "UPSTREAM_ERROR",
                    "error": f"YouTube Music 搜索失败，请检查「系统设置」中的代理地址或网络连接: {e}",
                    "message": str(e),
                },
                status=502,
            )

        session_id = self.search_sessions.put(tracks)
        return web.json_response(
            {
                "session_id": session_id,
                "query": query,
                "limit_requested": limit,
                "limit_used": limit,
                "min_score_used": min_score,
                "total": len(tracks),
                "expires_in": 600,
                "results": [t.to_dict() for t in tracks],
            }
        )

    async def handle_ytmusic_download(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.body_exists else {}
        video_id = str(body.get("video_id") or "").strip()
        selected_track: Track | None = None

        if not video_id:
            session_id = str(body.get("session_id") or "").strip()
            candidates = self.search_sessions.get(session_id) if session_id else None
            if not candidates:
                return web.json_response(
                    {"code": "SESSION_EXPIRED", "message": "session_id not found or expired"},
                    status=410,
                )
            idx = body.get("index")
            name = str(body.get("name") or "").strip()
            if idx is not None:
                i = int(idx)
                if 1 <= i <= len(candidates):
                    selected_track = candidates[i - 1]
            elif name:
                norm_name = normalize(name)
                for c in candidates:
                    if c.display_name == name or normalize(c.display_name) == norm_name:
                        selected_track = c
                        break
                if not selected_track:
                    best = max(candidates, key=lambda c: match_score(name, c.display_name))
                    if match_score(name, best.display_name) >= 0.6:
                        selected_track = best

            if not selected_track:
                return web.json_response({"code": "NOT_FOUND", "message": "track selection not matched"}, status=404)
            video_id = selected_track.video_id

        entry = await self.downloader.ensure_downloaded(video_id, selected_track)
        mode = request.query.get("mode", "").lower()
        if mode == "json" or body.get("mode") == "json":
            return web.json_response(
                {
                    "video_id": entry.video_id,
                    "title": entry.title,
                    "artist": entry.artist,
                    "display_name": entry.display_name,
                    "format": entry.ext,
                    "size": entry.size,
                    "cached": entry.cached,
                    "duration_seconds": entry.duration_seconds,
                    "token": entry.token,
                    "file_url": f"{self.config.base_url()}/file/{entry.token}",
                    "stream_url": f"{self.config.base_url()}/stream/{entry.video_id}.{entry.ext}",
                }
            )

        return web.FileResponse(
            Path(entry.path),
            headers={
                "Content-Type": entry.content_type,
                "X-Track-Video-Id": entry.video_id,
            },
        )

    async def handle_serve_token(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info.get("token", "").strip()
        entry = self.downloader.get_by_token(token)
        if not entry:
            raise web.HTTPNotFound(text="Audio token not found or expired")
        return web.FileResponse(
            Path(entry.path),
            headers={"Content-Type": entry.content_type},
        )

    async def handle_serve_stream(self, request: web.Request) -> web.StreamResponse:
        filename = request.match_info.get("filename", "").strip()
        video_id = filename.rsplit(".", 1)[0] if "." in filename else filename
        video_id = validate_video_id(video_id)
        entry = self.downloader.get_by_video_id(video_id)
        if entry is None:
            entry = await self.downloader.ensure_downloaded(video_id)
        return web.FileResponse(
            Path(entry.path),
            headers={"Content-Type": entry.content_type},
        )

    # ---- YouTube Music Auth & Diagnostics ----

    async def handle_ytmusic_auth_status(self, request: web.Request) -> web.Response:
        status = inspect_cookie_file(self.config.cookies_file)
        return web.json_response(
            {
                **status.to_dict(),
                "proxy": self.config.ytmusic_proxy,
                "has_quickjs": bool(shutil.which("qjs")),
            }
        )

    async def handle_upload_cookies(self, request: web.Request) -> web.Response:
        body = await request.json()
        content = str(body.get("cookies_text") or "").strip()
        if not content:
            self.config.cookies_file.unlink(missing_ok=True)
            return web.json_response(
                {
                    "status": "ok",
                    "has_cookies_file": False,
                    "cookie_status": inspect_cookie_file(self.config.cookies_file).to_dict(),
                    "message": "已清空 YouTube 登录 Cookie",
                }
            )

        try:
            c_status = save_cookies_any_format(self.config.cookies_file, content)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        # 登录成功后自动在后台拉取并保存用户已有的 YouTube Music 播放列表
        if c_status.valid:
            asyncio.create_task(self.playlist_store.sync_from_ytmusic())

        msg = (
            f"YouTube Music 登录成功！正在后台自动同步你的播放列表（识别格式: {c_status.source_format}，"
            f"共 {c_status.cookie_count} 条 Cookie，含 {c_status.auth_cookie_count} 项核心认证凭证）"
            if c_status.logged_in
            else f"Cookie 已保存（共 {c_status.cookie_count} 条，但未检测到 SID/SAPISID/LOGIN_INFO 登录凭证，可能仅为匿名访客 Cookie）"
        )
        return web.json_response(
            {
                "status": "ok",
                "has_cookies_file": True,
                "size": c_status.size_bytes,
                "cookie_status": c_status.to_dict(),
                "auto_sync_playlists": bool(c_status.valid),
                "message": msg,
            }
        )

    async def handle_ytmusic_logout(self, request: web.Request) -> web.Response:
        self.config.cookies_file.unlink(missing_ok=True)
        return web.json_response(
            {
                "status": "ok",
                "cookie_status": inspect_cookie_file(self.config.cookies_file).to_dict(),
                "message": "已退出并清除 YouTube Music 登录凭证",
            }
        )

    async def handle_ytmusic_diagnose(self, request: web.Request) -> web.Response:
        body = await request.json() if request.body_exists else {}
        query = str(body.get("query") or "周杰伦 晴天").strip()
        test_download = bool(body.get("test_download", True))

        c_status = inspect_cookie_file(self.config.cookies_file)
        report: dict[str, Any] = {
            "cookie_status": c_status.to_dict(),
            "proxy": self.config.ytmusic_proxy or "(直连/透明网关)",
            "has_quickjs": bool(shutil.which("qjs")),
            "search_ok": False,
            "search_ms": 0,
            "search_count": 0,
            "first_track": None,
            "search_error": "",
            "download_ok": False,
            "download_ms": 0,
            "file_size": 0,
            "stream_url": "",
            "download_error": "",
        }

        t0 = time.time()
        try:
            tracks = await self.yt_client.search(query, limit=5)
            report["search_ms"] = int((time.time() - t0) * 1000)
            report["search_count"] = len(tracks)
            if tracks:
                report["search_ok"] = True
                report["first_track"] = tracks[0].to_dict()
            else:
                report["search_error"] = "搜索返回 0 条结果"
        except Exception as e:
            report["search_ms"] = int((time.time() - t0) * 1000)
            report["search_error"] = str(e)
            return web.json_response(report)

        if test_download and report["first_track"]:
            vid = report["first_track"]["video_id"]
            # 若已有缓存则先移除以测试真实提取链路
            self.downloader._remove_entry(vid)
            t1 = time.time()
            try:
                entry = await self.downloader.ensure_downloaded(vid, tracks[0])
                report["download_ms"] = int((time.time() - t1) * 1000)
                report["download_ok"] = True
                report["file_size"] = entry.size
                report["stream_url"] = f"{self.config.base_url()}/stream/{entry.video_id}.{entry.ext}"
            except Exception as e:
                report["download_ms"] = int((time.time() - t1) * 1000)
                report["download_error"] = str(e)

        return web.json_response(report)

    # ---- Xiaomi Account & Devices ----

    async def handle_qr_start(self, request: web.Request) -> web.Response:
        info = await self.xiaomi_auth.start_qr_login()
        return web.json_response(info)

    async def handle_qr_poll(self, request: web.Request) -> web.Response:
        session_id = request.query.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"state": "failed", "message": "缺少 session_id"}, status=400)
        res = await self.xiaomi_auth.poll_qr_login(session_id)
        if res.get("state") == "confirmed":
            await self.xiaomi_client.get_devices(force_refresh=True)
        return web.json_response(res)

    async def handle_login_password(self, request: web.Request) -> web.Response:
        body = await request.json()
        account = str(body.get("account") or "").strip()
        password = str(body.get("password") or "")
        captcha = str(body.get("captcha") or "").strip()
        if not account or not password:
            return web.json_response({"state": "failed", "error": "请输入小米账号与密码"}, status=400)
        res = await self.xiaomi_auth.login_password(account, password, captcha)
        if res.get("state") == "success":
            await self.xiaomi_client.get_devices(force_refresh=True)
        return web.json_response(res)

    async def handle_login_cookie(self, request: web.Request) -> web.Response:
        body = await request.json()
        cookie = str(body.get("cookie") or "").strip()
        if not cookie:
            return web.json_response({"state": "failed", "error": "请输入 Cookie"}, status=400)
        res = await self.xiaomi_auth.login_cookie(cookie)
        if res.get("state") == "success":
            await self.xiaomi_client.get_devices(force_refresh=True)
        return web.json_response(res)

    async def handle_get_devices(self, request: web.Request) -> web.Response:
        force = request.query.get("refresh") in ("1", "true", "yes")
        devices = await self.xiaomi_client.get_devices(force_refresh=force)
        return web.json_response(
            {
                "authenticated": self.xiaomi_auth.is_authenticated(),
                "user_id": self.config.mi_user_id,
                "enabled_device_ids": self.config.enabled_device_ids,
                "default_device_id": self.config.default_device_id,
                "devices": devices,
            }
        )

    async def handle_select_devices(self, request: web.Request) -> web.Response:
        body = await request.json()
        enabled_ids = body.get("enabled_device_ids")
        default_id = str(body.get("default_device_id") or "").strip()
        if isinstance(enabled_ids, list):
            self.config.enabled_device_ids = [str(x).strip() for x in enabled_ids if str(x).strip()]
        if default_id:
            self.config.default_device_id = default_id
        elif self.config.enabled_device_ids:
            self.config.default_device_id = self.config.enabled_device_ids[0]
        self.config.save()
        devices = await self.xiaomi_client.get_devices(force_refresh=False)
        for d in devices:
            d["enabled"] = d["deviceID"] in self.config.enabled_device_ids
        return web.json_response({"status": "ok", "devices": devices})

    # ---- Player & Controls ----

    async def handle_player_status(self, request: web.Request) -> web.Response:
        yt_status = inspect_cookie_file(self.config.cookies_file)
        return web.json_response(
            {
                "players": self.players.get_all_statuses(),
                "voice_monitor": self.monitor.get_status(),
                "cache": self.downloader.stats(),
                "xiaomi_authenticated": self.xiaomi_auth.is_authenticated(),
                "user_id": self.config.mi_user_id,
                "ytmusic_auth": yt_status.to_dict(),
                "playlists": {
                    "count": len(self.playlist_store.list_summaries()),
                    "syncing": self.playlist_store.syncing,
                    "last_sync_at": self.playlist_store.last_sync_at,
                },
                "lan_base_url": self.config.base_url(),
            }
        )

    # ---- Playlists (歌单同步与管理) ----

    async def handle_list_playlists(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "syncing": self.playlist_store.syncing,
                "last_sync_at": self.playlist_store.last_sync_at,
                "last_sync_error": self.playlist_store.last_sync_error,
                "playlists": self.playlist_store.list_summaries(),
            }
        )

    async def handle_get_playlist(self, request: web.Request) -> web.Response:
        pl_id = request.match_info.get("playlist_id", "").strip()
        pl = self.playlist_store.get(pl_id)
        if not pl:
            return web.json_response({"error": "未找到该播放列表"}, status=404)
        return web.json_response(pl.to_full_dict())

    async def handle_sync_playlists(self, request: web.Request) -> web.Response:
        res = await self.playlist_store.sync_from_ytmusic()
        return web.json_response(res)

    async def handle_import_playlist(self, request: web.Request) -> web.Response:
        body = await request.json()
        url_or_id = str(body.get("url_or_id") or "").strip()
        custom_title = str(body.get("title") or "").strip()
        if not url_or_id:
            return web.json_response({"error": "请输入播放列表链接或 Playlist ID"}, status=400)
        pl = await self.playlist_store.import_playlist(url_or_id, custom_title)
        return web.json_response(
            {
                "status": "ok",
                "playlist": pl.to_summary_dict(),
                "playlists": self.playlist_store.list_summaries(),
            }
        )

    async def handle_save_queue_playlist(self, request: web.Request) -> web.Response:
        body = await request.json()
        device_id = self._resolve_device_id(str(body.get("device_id") or ""))
        title = str(body.get("title") or "").strip()
        if not device_id:
            return web.json_response({"error": "请先选择音箱设备"}, status=400)
        player = self.players.get_or_create(device_id)
        if not player.queue:
            return web.json_response({"error": "当前播放队列为空，无法保存为歌单"}, status=400)
        pl = self.playlist_store.save_custom_playlist(title or player.last_query or "我的收藏队列", player.queue)
        return web.json_response(
            {
                "status": "ok",
                "playlist": pl.to_summary_dict(),
                "playlists": self.playlist_store.list_summaries(),
            }
        )

    async def handle_play_playlist(self, request: web.Request) -> web.Response:
        body = await request.json()
        device_id = self._resolve_device_id(str(body.get("device_id") or ""))
        playlist_id = str(body.get("playlist_id") or "").strip()
        start_index = int(body.get("start_index") or 0)
        if not device_id:
            return web.json_response({"error": "请先登录小米账号并选择小爱音箱"}, status=400)
        pl = self.playlist_store.get(playlist_id)
        if not pl or not pl.tracks:
            return web.json_response({"error": "歌单不存在或曲目为空"}, status=404)
        player = self.players.get_or_create(device_id)
        track = await player.play_playlist_tracks(pl.tracks, playlist_title=pl.title, start_index=start_index)
        return web.json_response(
            {
                "status": "ok" if track else "failed",
                "error": player.last_error if not track else "",
                "track": track.to_dict() if track else None,
                "player": player.to_status_dict(),
            }
        )

    async def handle_delete_playlist(self, request: web.Request) -> web.Response:
        pl_id = request.match_info.get("playlist_id", "").strip()
        self.playlist_store.delete(pl_id)
        return web.json_response({"status": "ok", "playlists": self.playlist_store.list_summaries()})

    async def handle_search_and_play(self, request: web.Request) -> web.Response:
        body = await request.json()
        query = str(body.get("query") or "").strip()
        device_id = self._resolve_device_id(str(body.get("device_id") or ""))
        if not query:
            return web.json_response({"error": "搜索关键词不能为空"}, status=400)
        if not device_id:
            return web.json_response({"error": "请先登录小米账号并选择一台小爱音箱"}, status=400)

        player = self.players.get_or_create(device_id)
        track = await player.search_and_play(query, interrupt_first=True)
        return web.json_response(
            {
                "status": "ok" if track else "failed",
                "error": player.last_error if not track else "",
                "track": track.to_dict() if track else None,
                "player": player.to_status_dict(),
            }
        )

    async def handle_play_track(self, request: web.Request) -> web.Response:
        body = await request.json()
        device_id = self._resolve_device_id(str(body.get("device_id") or ""))
        if not device_id:
            return web.json_response({"error": "请先选择小爱音箱设备"}, status=400)

        t_data = body.get("track") or {}
        vid = str(t_data.get("video_id") or body.get("video_id") or "").strip()
        if not vid:
            return web.json_response({"error": "缺少 video_id"}, status=400)

        track = Track(
            video_id=vid,
            title=str(t_data.get("title") or vid),
            artists=list(t_data.get("artists") or []),
            album=str(t_data.get("album") or ""),
            duration=str(t_data.get("duration") or ""),
            duration_seconds=int(t_data.get("duration_seconds") or 0),
            thumbnail=str(t_data.get("thumbnail") or ""),
            display_name=str(t_data.get("display_name") or t_data.get("title") or vid),
        )
        append_only = bool(body.get("append"))
        player = self.players.get_or_create(device_id)
        ok = await player.play_single_track(track, append_only=append_only)
        return web.json_response(
            {
                "status": "ok" if ok else "failed",
                "error": player.last_error if not ok else "",
                "player": player.to_status_dict(),
            }
        )

    async def handle_player_control(self, request: web.Request) -> web.Response:
        body = await request.json()
        device_id = self._resolve_device_id(str(body.get("device_id") or ""))
        action = str(body.get("action") or "").strip()
        value = body.get("value")

        if not device_id:
            return web.json_response({"error": "请先选择小爱音箱设备"}, status=400)

        player = self.players.get_or_create(device_id)
        ok = True

        if action == "play" or action == "resume":
            ok = await player.resume()
        elif action == "pause":
            ok = await player.pause()
        elif action == "stop":
            ok = await player.stop()
        elif action == "next":
            ok = await player.next()
        elif action == "previous":
            ok = await player.previous()
        elif action == "play_index":
            ok = await player.play_index(int(value or 0))
        elif action == "mode":
            player.set_play_mode(str(value or "loop"))
        elif action == "volume":
            ok = await self.xiaomi_client.set_volume(device_id, int(value or 50))
        elif action == "tts":
            ok = await self.xiaomi_client.text_to_speech(device_id, str(value or ""))
        else:
            return web.json_response({"error": f"未知操作: {action}"}, status=400)

        return web.json_response({"status": "ok" if ok else "failed", "player": player.to_status_dict()})

    # ---- Voice Logs & Test ----

    async def handle_voice_logs(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit") or 50)
        return web.json_response(
            {
                "monitor": self.monitor.get_status(),
                "logs": self.monitor.get_history(limit),
            }
        )

    async def handle_voice_test(self, request: web.Request) -> web.Response:
        body = await request.json()
        query = str(body.get("query") or "").strip()
        execute = bool(body.get("execute", False))
        device_id = self._resolve_device_id(str(body.get("device_id") or ""))

        match = self.voice_engine.match_command(query)
        if not match:
            return web.json_response(
                {
                    "matched": False,
                    "query": query,
                    "note": "未命中任何音乐拦截口令（将保持小爱原生回答）",
                }
            )

        played = ""
        error_msg = ""
        if execute and device_id:
            played = await self.voice_engine.execute_match(device_id, match)
            player = self.players.get_or_create(device_id)
            if player.last_error:
                error_msg = player.last_error

        return web.json_response(
            {
                "matched": True,
                "query": query,
                "command_type": match.command_type,
                "keyword": match.keyword,
                "argument": match.argument,
                "source": match.source,
                "executed": bool(execute and device_id),
                "played": played,
                "error": error_msg,
            }
        )

    # ---- Config & Cache ----

    async def handle_get_config(self, request: web.Request) -> web.Response:
        return web.json_response(self.config.to_public_dict())

    async def handle_update_config(self, request: web.Request) -> web.Response:
        body = await request.json()
        for secret_key in ("mi_password", "mi_pass_token", "mi_service_token", "mi_ssecurity", "admin_password"):
            if body.get(secret_key) == "******":
                body.pop(secret_key, None)

        self.config.update_from_dict(body, save_now=True)
        self.yt_client.proxy = self.config.ytmusic_proxy.strip()
        self.yt_client.hl = self.config.ytmusic_hl
        self.yt_client.gl = self.config.ytmusic_gl
        return web.json_response({"status": "ok", "config": self.config.to_public_dict()})

    async def handle_clear_cache(self, request: web.Request) -> web.Response:
        removed = self.downloader.clear_cache()
        return web.json_response({"status": "ok", "removed": removed, "cache": self.downloader.stats()})

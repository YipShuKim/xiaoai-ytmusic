"""YouTube Music 音频下载与 LRU 磁盘缓存管理
（整合自 Xeltra233/youtube-music-api/internal/download/downloader.go & ytdlp.go & cache.go）

核心升级：
1. 快照保护 (snapshot_for_ytdlp)：传给 yt-dlp 的永远是临时副本，杜绝 yt-dlp 退出时覆写破坏真实登录 Cookie。
2. QuickJS + yt-dlp-ejs 引擎：支持 2026 新版 YouTube 的 JS 签名解密（tv / web_embedded / web_music 客户端）。
3. 静默重试与连接优化：消除无意义的直连超时回退与未捕获 Future 异常警告。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiohttp

from app.config import AppConfig
from app.ytmusic.client import ANDROID_VR_UA, Track, YTMusicClient
from app.ytmusic.cookies import inspect_cookie_file, snapshot_for_ytdlp

log = logging.getLogger("xiaoai-ytmusic.downloader")

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

CONTENT_TYPES = {
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "webm": "audio/webm",
    "opus": "audio/ogg",
}


class _SilentYtdlpLogger:
    """拦截 yt-dlp 内部策略尝试期间的中间报错与进度刷屏，统一由 Downloader 输出简洁日志"""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        log.debug(f"[yt-dlp] {msg}")


def validate_video_id(video_id: str) -> str:
    vid = (video_id or "").strip()
    if not _VIDEO_ID_RE.match(vid):
        raise ValueError(f"无效的 video_id: {video_id!r}")
    return vid


@dataclass
class CacheEntry:
    video_id: str
    token: str
    path: str
    size: int
    ext: str
    content_type: str
    title: str = ""
    artist: str = ""
    display_name: str = ""
    duration_seconds: int = 0
    created_at: float = 0.0
    last_accessed: float = 0.0
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AudioDownloader:
    """支持 Singleflight 去重、Cookie 快照保护、QuickJS 解密与磁盘 LRU 缓存的音频管理器"""

    def __init__(self, config: AppConfig, yt_client: YTMusicClient):
        self.config = config
        self.yt_client = yt_client
        self._sem = asyncio.Semaphore(max(1, config.max_concurrent_downloads))
        self._inflight: dict[str, asyncio.Task[CacheEntry]] = {}
        self._entries_by_vid: dict[str, CacheEntry] = {}
        self._entries_by_token: dict[str, CacheEntry] = {}
        self._scan_existing_cache()

    def _make_token(self, video_id: str) -> str:
        digest = hashlib.sha1(f"xiaoai-ytm:{video_id}".encode("utf-8")).hexdigest()[:20]
        return f"{video_id}_{digest}"

    def _scan_existing_cache(self) -> None:
        cache_dir = self.config.cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        now = time.time()
        for f in cache_dir.iterdir():
            if not f.is_file() or f.name.endswith(".part") or f.name.endswith(".json"):
                continue
            ext = f.suffix.lstrip(".").lower()
            if ext not in CONTENT_TYPES:
                continue
            vid = f.stem
            if not _VIDEO_ID_RE.match(vid):
                continue
            try:
                st = f.stat()
                if st.st_size <= 0:
                    continue
                token = self._make_token(vid)
                meta = self._load_sidecar_meta(vid)
                entry = CacheEntry(
                    video_id=vid,
                    token=token,
                    path=str(f),
                    size=st.st_size,
                    ext=ext,
                    content_type=CONTENT_TYPES.get(ext, "audio/mp4"),
                    title=meta.get("title", vid),
                    artist=meta.get("artist", ""),
                    display_name=meta.get("display_name", vid),
                    duration_seconds=int(meta.get("duration_seconds") or 0),
                    created_at=st.st_mtime,
                    last_accessed=now,
                    cached=True,
                )
                self._entries_by_vid[vid] = entry
                self._entries_by_token[token] = entry
            except OSError:
                continue

    def _sidecar_path(self, video_id: str) -> Path:
        return self.config.cache_dir / f"{video_id}.meta.json"

    def _save_sidecar_meta(self, entry: CacheEntry) -> None:
        try:
            p = self._sidecar_path(entry.video_id)
            p.write_text(
                json.dumps(
                    {
                        "title": entry.title,
                        "artist": entry.artist,
                        "display_name": entry.display_name,
                        "duration_seconds": entry.duration_seconds,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_sidecar_meta(self, video_id: str) -> dict[str, Any]:
        try:
            p = self._sidecar_path(video_id)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def get_by_video_id(self, video_id: str) -> CacheEntry | None:
        entry = self._entries_by_vid.get(video_id)
        if not entry:
            return None
        p = Path(entry.path)
        if not p.exists() or p.stat().st_size <= 0:
            self._entries_by_vid.pop(video_id, None)
            self._entries_by_token.pop(entry.token, None)
            return None
        entry.last_accessed = time.time()
        entry.cached = True
        return entry

    def get_by_token(self, token: str) -> CacheEntry | None:
        entry = self._entries_by_token.get(token)
        if not entry:
            return None
        return self.get_by_video_id(entry.video_id)

    async def ensure_downloaded(
        self,
        video_id: str,
        track: Track | None = None,
    ) -> CacheEntry:
        """获取或下载指定 video_id 的音频文件（Singleflight 并发合并，无悬空 Future）"""
        video_id = validate_video_id(video_id)

        cached = self.get_by_video_id(video_id)
        if cached is not None:
            if track:
                if track.title and not cached.title:
                    cached.title = track.title
                if track.artists and not cached.artist:
                    cached.artist = ", ".join(track.artists)
                if track.display_name and not cached.display_name:
                    cached.display_name = track.display_name
                if track.duration_seconds and not cached.duration_seconds:
                    cached.duration_seconds = track.duration_seconds
            return cached

        task = self._inflight.get(video_id)
        if task is None or task.done():
            task = asyncio.create_task(self._run_download_task(video_id, track))
            self._inflight[video_id] = task
            # 防止调用方取消后 Task 异常未消费触发 asyncio warning
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        return await asyncio.shield(task)

    async def _run_download_task(self, video_id: str, track: Track | None) -> CacheEntry:
        try:
            async with self._sem:
                cached = self.get_by_video_id(video_id)
                if cached is not None:
                    return cached

                entry = await self._do_download(video_id, track)
                self._entries_by_vid[video_id] = entry
                self._entries_by_token[entry.token] = entry
                self._save_sidecar_meta(entry)
                self.evict_if_needed()
                return entry
        finally:
            self._inflight.pop(video_id, None)

    async def _do_download(self, video_id: str, track: Track | None) -> CacheEntry:
        os.makedirs(self.config.cache_dir, exist_ok=True)
        title = track.title if track else ""
        artist = ", ".join(track.artists) if (track and track.artists) else ""
        display_name = track.display_name if track else ""
        duration_seconds = track.duration_seconds if track else 0

        # 引擎 1：尝试纯 HTTP InnerTube 直链下载（仅未登录时快速尝试）
        direct = await self.yt_client.extract_direct_audio_url(video_id)
        if direct and direct.get("url"):
            try:
                out_path = await self._download_direct_http(
                    video_id,
                    direct["url"],
                    direct.get("ext", "m4a"),
                    direct.get("user_agent", ANDROID_VR_UA),
                )
                if out_path and out_path.exists() and out_path.stat().st_size > 0:
                    if not title and direct.get("title"):
                        title = direct["title"]
                    if not artist and direct.get("author"):
                        artist = direct["author"]
                    if not duration_seconds and direct.get("duration_seconds"):
                        duration_seconds = int(direct["duration_seconds"])
                    if not display_name:
                        display_name = f"{title} - {artist}" if artist else title
                    ext = out_path.suffix.lstrip(".").lower() or "m4a"
                    now = time.time()
                    log.info(f"[Downloader] 直链下载完成: {video_id} ({out_path.stat().st_size} bytes)")
                    return CacheEntry(
                        video_id=video_id,
                        token=self._make_token(video_id),
                        path=str(out_path),
                        size=out_path.stat().st_size,
                        ext=ext,
                        content_type=CONTENT_TYPES.get(ext, "audio/mp4"),
                        title=title or video_id,
                        artist=artist,
                        display_name=display_name or title or video_id,
                        duration_seconds=duration_seconds,
                        created_at=now,
                        last_accessed=now,
                        cached=False,
                    )
            except Exception as e:
                log.debug(f"[Downloader] 直链下载未命中 ({video_id}), 转用 yt-dlp: {e}")

        # 引擎 2：使用 yt-dlp + QuickJS + Cookie 快照多客户端策略下载
        out_path, info = await asyncio.wait_for(
            asyncio.to_thread(self._run_ytdlp_sync, video_id),
            timeout=max(45, self.config.download_timeout_sec),
        )
        if not title and info.get("title"):
            title = str(info["title"])
        if not artist and info.get("uploader"):
            artist = str(info["uploader"])
        if not duration_seconds and info.get("duration"):
            duration_seconds = int(info["duration"])
        if not display_name:
            display_name = f"{title} - {artist}" if artist else (title or video_id)

        ext = out_path.suffix.lstrip(".").lower() or "m4a"
        now = time.time()
        st = out_path.stat()
        log.info(f"[Downloader] yt-dlp 下载完成: {video_id} -> {out_path.name} ({st.st_size} bytes)")
        return CacheEntry(
            video_id=video_id,
            token=self._make_token(video_id),
            path=str(out_path),
            size=st.st_size,
            ext=ext,
            content_type=CONTENT_TYPES.get(ext, "audio/mp4"),
            title=title or video_id,
            artist=artist,
            display_name=display_name,
            duration_seconds=duration_seconds,
            created_at=now,
            last_accessed=now,
            cached=False,
        )

    async def _download_direct_http(
        self,
        video_id: str,
        url: str,
        ext: str = "m4a",
        user_agent: str = ANDROID_VR_UA,
    ) -> Path:
        part_path = self.config.cache_dir / f"{video_id}.{ext}.part"
        final_path = self.config.cache_dir / f"{video_id}.{ext}"
        headers = {"User-Agent": user_agent}
        proxy_url = self.config.ytmusic_proxy.strip() or None

        connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False, force_close=True)
        timeout = aiohttp.ClientTimeout(total=self.config.download_timeout_sec, connect=8)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url, headers=headers, proxy=proxy_url) as resp:
                    if resp.status not in (200, 206):
                        raise RuntimeError(f"Upstream stream returned HTTP {resp.status}")
                    with open(part_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            f.write(chunk)
            if part_path.exists() and part_path.stat().st_size > 0:
                part_path.replace(final_path)
                return final_path
        except Exception:
            part_path.unlink(missing_ok=True)
            raise
        raise RuntimeError("Direct HTTP stream produced empty file")

    def _run_ytdlp_sync(self, video_id: str) -> tuple[Path, dict[str, Any]]:
        """在后台线程中通过 yt_dlp + QuickJS + Cookie 快照执行多客户端策略下载"""
        import yt_dlp

        # 若别的任务已经完成了该文件的下载，直接返回
        existing = self._find_produced_file(video_id)
        if existing is not None:
            return existing, {}

        url = f"https://www.youtube.com/watch?v={video_id}"
        out_tmpl = str(self.config.cache_dir / f"{video_id}.%(ext)s")

        cookie_status = inspect_cookie_file(self.config.cookies_file)
        if cookie_status.valid:
            client_strategies = [
                ["tv", "web_embedded"],
                ["tv_downgraded", "web_music", "mweb"],
                ["web", "web_safari"],
                ["visionos", "android_vr"],
            ]
        else:
            client_strategies = [
                ["visionos", "tv", "web_embedded"],
                ["android_vr", "ios"],
                ["mweb", "web_safari"],
            ]

        has_ffmpeg = bool(shutil.which("ffmpeg"))
        has_qjs = bool(shutil.which("qjs"))
        want_fmt = (self.config.audio_format or "m4a").lower()
        proxy_url = self.config.ytmusic_proxy.strip() or None

        last_err: Exception | None = None
        for clients in client_strategies:
            existing = self._find_produced_file(video_id)
            if existing is not None:
                return existing, {}

            with snapshot_for_ytdlp(self.config.cookies_file) as cookie_snap:
                ydl_opts: dict[str, Any] = {
                    "format": "ba[ext=m4a]/ba/bestaudio/best",
                    "outtmpl": out_tmpl,
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                    "logger": _SilentYtdlpLogger(),
                    "nocheckcertificate": True,
                    "geo_bypass": True,
                    "retries": 2,
                    "fragment_retries": 2,
                    "socket_timeout": 10,
                    "cachedir": False,
                    "extractor_args": {"youtube": {"player_client": clients}},
                }
                if has_qjs:
                    ydl_opts["js_runtimes"] = {"quickjs": {}}
                if proxy_url:
                    ydl_opts["proxy"] = proxy_url
                if cookie_snap:
                    ydl_opts["cookiefile"] = cookie_snap

                if want_fmt == "mp3" and has_ffmpeg:
                    ydl_opts["postprocessors"] = [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
                    ]

                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True) or {}
                    produced = self._find_produced_file(video_id)
                    if produced is not None:
                        return produced, info
                except Exception as e:
                    last_err = e
                    continue

        err_msg = str(last_err)
        if "confirm you’re not a bot" in err_msg or "confirm you're not a bot" in err_msg:
            if not cookie_status.logged_in:
                raise RuntimeError(
                    "YouTube 触发了防机器人验证 (Sign in to confirm you're not a bot)！"
                    "请点击顶部「🎬 YouTube Music 登录」标签页导入登录态 Cookie 后重试。"
                )
            raise RuntimeError(
                f"当前 YouTube Cookie 登录态可能已过期或节点被风控，请在「🎬 YouTube Music 登录」页更新登录态: {err_msg}"
            )
        raise RuntimeError(f"yt-dlp 下载失败 ({video_id}): {err_msg}")

    def _find_produced_file(self, video_id: str) -> Path | None:
        for ext in ("m4a", "mp3", "webm", "opus", "mp4"):
            p = self.config.cache_dir / f"{video_id}.{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
        for p in self.config.cache_dir.glob(f"{video_id}.*"):
            if p.suffix not in (".part", ".json") and p.stat().st_size > 0:
                return p
        return None

    def evict_if_needed(self) -> None:
        """按 TTL 与最大缓存容量（MB）执行 LRU 淘汰"""
        now = time.time()
        ttl_sec = max(1, self.config.cache_ttl_hours) * 3600
        max_bytes = max(64, self.config.max_cache_mb) * 1024 * 1024

        for vid, entry in list(self._entries_by_vid.items()):
            if now - entry.created_at > ttl_sec:
                self._remove_entry(vid)

        total_bytes = sum(e.size for e in self._entries_by_vid.values())
        if total_bytes > max_bytes:
            sorted_entries = sorted(self._entries_by_vid.values(), key=lambda x: x.last_accessed)
            for entry in sorted_entries:
                if total_bytes <= max_bytes:
                    break
                total_bytes -= entry.size
                self._remove_entry(entry.video_id)

    def _remove_entry(self, video_id: str) -> None:
        entry = self._entries_by_vid.pop(video_id, None)
        if entry:
            self._entries_by_token.pop(entry.token, None)
            Path(entry.path).unlink(missing_ok=True)
            self._sidecar_path(video_id).unlink(missing_ok=True)

    def clear_cache(self) -> int:
        count = len(self._entries_by_vid)
        for vid in list(self._entries_by_vid.keys()):
            self._remove_entry(vid)
        return count

    def stats(self) -> dict[str, Any]:
        total_bytes = sum(e.size for e in self._entries_by_vid.values())
        return {
            "count": len(self._entries_by_vid),
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "max_cache_mb": self.config.max_cache_mb,
            "cache_ttl_hours": self.config.cache_ttl_hours,
        }

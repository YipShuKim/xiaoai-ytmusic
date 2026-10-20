"""YouTube Music InnerTube 搜索、歌单同步与快速流提取客户端
（整合自 Xeltra233/youtube-music-api/internal/ytmusic/client.go & parse.go）

核心增强：
1. 采用与 yt-dlp 同源的 urllib3/requests 传输栈执行 InnerTube 请求，彻底解决 Python 3.12 asyncio start_tls 经代理连接时的超时问题。
2. 仅在未配置代理时直连；配置了代理时优先走代理（仅当代理端口拒绝连接时才回退直连），消除无意义的 12s 直连超时等待。
3. 支持已登录账号的 YouTube Music 歌单库（FEmusic_liked_playlists + LM 我喜欢的音乐）及歌单曲目拉取。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests
import urllib3

from app.ytmusic.cookies import (
    build_ytmusic_auth_headers,
    inspect_cookie_file,
    snapshot_for_ytdlp,
)
from app.ytmusic.matching import build_display_name, match_score

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("xiaoai-ytmusic.ytmusic")

DEFAULT_SEARCH_URL = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false"
DEFAULT_BROWSE_URL = "https://music.youtube.com/youtubei/v1/browse?prettyPrint=false"
WEB_SEARCH_URL = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
DEFAULT_PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"

SONGS_FILTER_PARAMS = "EgWKAQIIAWoMEA4QChADEAQQCRAF"
VIDEOS_FILTER_PARAMS = "EgWKAQIQAWoMEA4QChADEAQQCRAF"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
VISIONOS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
)
ANDROID_VR_UA = (
    "com.google.android.apps.youtube.vr.oculus/1.65.10 "
    "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
)

_DURATION_RE = re.compile(r"^(\d+:)*\d+:\d+$")
_TYPE_LABELS = {
    "song",
    "songs",
    "single",
    "album",
    "video",
    "videos",
    "ep",
    "playlist",
    "歌曲",
    "单曲",
    "专辑",
    "视频",
}


@dataclass
class Track:
    video_id: str
    title: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration: str = ""
    duration_seconds: int = 0
    thumbnail: str = ""
    display_name: str = ""
    match_score: float = 0.0
    music_video_type: str = ""
    index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dig(obj: Any, *path: str | int) -> Any:
    cur = obj
    for key in path:
        if isinstance(key, str) and isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(key, int) and isinstance(cur, list) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return None
        if cur is None:
            return None
    return cur


def _walk_json(node: Any, fn) -> None:
    if isinstance(node, dict):
        fn(node)
        for v in node.values():
            _walk_json(v, fn)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, fn)


def _join_run_texts(runs: Any) -> str:
    if not isinstance(runs, list):
        return ""
    parts = [str(r.get("text", "")) for r in runs if isinstance(r, dict) and r.get("text")]
    return "".join(parts).strip()


def _parse_duration_seconds(duration: str) -> int:
    duration = (duration or "").strip()
    if not duration or not _DURATION_RE.match(duration):
        return 0
    total = 0
    for part in duration.split(":"):
        try:
            total = total * 60 + int(part)
        except ValueError:
            return 0
    return total


def _format_duration_seconds(seconds: int) -> str:
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _pick_best_thumbnail(renderer: dict[str, Any]) -> str:
    thumbs = (
        _dig(renderer, "thumbnail", "musicThumbnailRenderer", "thumbnail", "thumbnails")
        or _dig(renderer, "thumbnailRenderer", "musicThumbnailRenderer", "thumbnail", "thumbnails")
        or _dig(renderer, "thumbnail", "thumbnails")
    )
    if not isinstance(thumbs, list):
        return ""
    best_url = ""
    best_area = -1
    for t in thumbs:
        if not isinstance(t, dict):
            continue
        url = str(t.get("url") or "").strip()
        if not url:
            continue
        area = int(t.get("width") or 0) * int(t.get("height") or 0)
        if area >= best_area:
            best_area = area
            best_url = url
    return best_url


def _looks_like_views_only(runs: list[Any]) -> bool:
    for r in runs:
        if not isinstance(r, dict):
            continue
        text = str(r.get("text") or "").strip()
        if not text or text in ("•", "·"):
            continue
        low = text.lower()
        if "play" in low or "view" in low or "次" in text or "播放" in text or "观看" in text:
            return True
        if not _DURATION_RE.match(text):
            return True
    return False


def _collect_meta_runs(renderer: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx in (1, 2):
        runs = _dig(
            renderer,
            "flexColumns",
            idx,
            "musicResponsiveListItemFlexColumnRenderer",
            "text",
            "runs",
        )
        if not isinstance(runs, list) or not runs:
            continue
        if idx == 2 and _looks_like_views_only(runs):
            continue
        if out:
            out.append({"text": " • "})
        out.extend(r for r in runs if isinstance(r, dict))
    # 播放列表详情页的时长有时放在 fixedColumns[0]
    fixed_runs = _dig(
        renderer,
        "fixedColumns",
        0,
        "musicResponsiveListItemFixedColumnRenderer",
        "text",
        "runs",
    )
    if isinstance(fixed_runs, list) and fixed_runs:
        if out:
            out.append({"text": " • "})
        out.extend(r for r in fixed_runs if isinstance(r, dict))
    return out


def _parse_song_runs(runs: list[dict[str, Any]]) -> tuple[list[str], str, str]:
    artists: list[str] = []
    album = ""
    duration = ""
    for i, run in enumerate(runs):
        text = str(run.get("text") or "").strip()
        if not text or text in ("•", "·", "-", "—"):
            continue
        if _DURATION_RE.match(text):
            if duration == "":
                duration = text
            continue
        if len(text) == 4 and text.isdigit():
            continue
        low = text.lower()
        if "play" in low or "view" in low or "次" in text or "播放" in text or "观看" in text:
            continue
        browse_id = str(_dig(run, "navigationEndpoint", "browseEndpoint", "browseId") or "")
        if browse_id and (browse_id.startswith("MPRE") or "release_detail" in browse_id):
            if not album:
                album = text
            continue
        if low in _TYPE_LABELS:
            continue
        if text not in artists:
            artists.append(text)
    return artists, album, duration


def _extract_music_video_type(renderer: dict[str, Any]) -> str:
    found = ""

    def check(obj: dict[str, Any]) -> None:
        nonlocal found
        if found:
            return
        cfg = obj.get("watchEndpointMusicConfig")
        if isinstance(cfg, dict):
            typ = str(cfg.get("musicVideoType") or "").strip()
            if typ:
                found = typ

    _walk_json(renderer, check)
    return found


def _parse_list_item(renderer: dict[str, Any]) -> Track | None:
    vid = (
        _dig(renderer, "playlistItemData", "videoId")
        or _dig(
            renderer,
            "overlay",
            "musicItemThumbnailOverlayRenderer",
            "content",
            "musicPlayButtonRenderer",
            "playNavigationEndpoint",
            "watchEndpoint",
            "videoId",
        )
        or _dig(
            renderer,
            "flexColumns",
            0,
            "musicResponsiveListItemFlexColumnRenderer",
            "text",
            "runs",
            0,
            "navigationEndpoint",
            "watchEndpoint",
            "videoId",
        )
        or _dig(renderer, "onTap", "watchEndpoint", "videoId")
    )
    video_id = str(vid or "").strip()
    if not video_id:
        return None

    title_runs = _dig(
        renderer,
        "flexColumns",
        0,
        "musicResponsiveListItemFlexColumnRenderer",
        "text",
        "runs",
    )
    title = _join_run_texts(title_runs)
    if not title:
        return None
    meta_runs = _collect_meta_runs(renderer)
    artists, album, duration = _parse_song_runs(meta_runs)
    display_name = build_display_name(title, artists)

    return Track(
        video_id=video_id,
        title=title,
        artists=artists,
        album=album,
        duration=duration,
        duration_seconds=_parse_duration_seconds(duration),
        thumbnail=_pick_best_thumbnail(renderer),
        display_name=display_name,
        music_video_type=_extract_music_video_type(renderer),
    )


def _parse_web_video_renderer(renderer: dict[str, Any]) -> Track | None:
    """解析 www.youtube.com 的 videoRenderer / playlistVideoRenderer 节点"""
    video_id = str(renderer.get("videoId") or "").strip()
    if not video_id:
        return None
    title = (
        _join_run_texts(_dig(renderer, "title", "runs"))
        or str(_dig(renderer, "title", "simpleText") or "").strip()
    )
    if not title:
        return None
    artist = (
        _join_run_texts(_dig(renderer, "ownerText", "runs"))
        or _join_run_texts(_dig(renderer, "shortBylineText", "runs"))
        or _join_run_texts(_dig(renderer, "longBylineText", "runs"))
        or str(_dig(renderer, "ownerText", "simpleText") or "").strip()
    )
    artist = re.sub(r"\s*-\s*Topic$", "", artist, flags=re.IGNORECASE).strip()
    artists = [artist] if artist else []
    duration = (
        str(_dig(renderer, "lengthText", "simpleText") or "").strip()
        or _join_run_texts(_dig(renderer, "lengthText", "runs"))
    )
    dur_sec = _parse_duration_seconds(duration) or int(renderer.get("lengthSeconds") or 0)
    if not duration and dur_sec > 0:
        duration = _format_duration_seconds(dur_sec)
    return Track(
        video_id=video_id,
        title=title,
        artists=artists,
        album="",
        duration=duration,
        duration_seconds=dur_sec,
        thumbnail=_pick_best_thumbnail(renderer),
        display_name=build_display_name(title, artists),
    )


def parse_search_response(body: bytes | str | dict[str, Any]) -> list[Track]:
    """从 YouTube Music 或 YouTube Web InnerTube JSON 中提取所有音轨"""
    if isinstance(body, (bytes, str)):
        root = json.loads(body)
    else:
        root = body

    tracks: list[Track] = []
    seen: set[str] = set()

    def visitor(obj: dict[str, Any]) -> None:
        m_renderer = obj.get("musicResponsiveListItemRenderer")
        if isinstance(m_renderer, dict):
            track = _parse_list_item(m_renderer)
            if track is not None and track.video_id not in seen:
                seen.add(track.video_id)
                tracks.append(track)
            return
        for key in ("videoRenderer", "playlistVideoRenderer"):
            v_renderer = obj.get(key)
            if isinstance(v_renderer, dict):
                track = _parse_web_video_renderer(v_renderer)
                if track is not None and track.video_id not in seen:
                    seen.add(track.video_id)
                    tracks.append(track)
                return

    _walk_json(root, visitor)
    return tracks


def parse_library_playlists(body: bytes | str | dict[str, Any]) -> list[dict[str, Any]]:
    """从 FEmusic_liked_playlists 响应中解析用户的播放列表元数据"""
    if isinstance(body, (bytes, str)):
        root = json.loads(body)
    else:
        root = body

    playlists: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def visitor(obj: dict[str, Any]) -> None:
        two_row = obj.get("musicTwoRowItemRenderer")
        if isinstance(two_row, dict):
            title = _join_run_texts(_dig(two_row, "title", "runs"))
            browse_id = str(_dig(two_row, "navigationEndpoint", "browseEndpoint", "browseId") or "").strip()
            pl_id = str(
                _dig(
                    two_row,
                    "thumbnailOverlay",
                    "musicItemThumbnailOverlayRenderer",
                    "content",
                    "musicPlayButtonRenderer",
                    "playNavigationEndpoint",
                    "watchPlaylistEndpoint",
                    "playlistId",
                )
                or ""
            ).strip()
            if not pl_id and browse_id.startswith("VL"):
                pl_id = browse_id[2:]
            elif not pl_id and browse_id:
                pl_id = browse_id

            # 过滤非歌单导航项（如“新建播放列表”或订阅艺人）
            if not pl_id or not title or pl_id in ("FEmusic_liked_playlists", "SPunlimited"):
                return
            if pl_id.startswith("UC") or pl_id.startswith("FEmusic_"):
                return
            if pl_id in seen_ids:
                return
            seen_ids.add(pl_id)
            subtitle = _join_run_texts(_dig(two_row, "subtitle", "runs"))
            playlists.append(
                {
                    "playlist_id": pl_id,
                    "title": title,
                    "subtitle": subtitle,
                    "thumbnail": _pick_best_thumbnail(two_row),
                }
            )

    _walk_json(root, visitor)
    return playlists


def cookie_header_from_netscape(cookies_path: Path | str | None) -> str:
    """兼容旧接口：读取 cookies.txt 并返回 Cookie Header 字符串"""
    return build_ytmusic_auth_headers(cookies_path).get("Cookie", "SOCS=CAI")


class YTMusicClient:
    """YouTube Music 搜索、歌单同步与快速音频直链解析器"""

    def __init__(
        self,
        proxy: str = "",
        cookies_file: Path | str | None = None,
        hl: str = "zh-CN",
        gl: str = "US",
    ):
        self.proxy = proxy.strip()
        self.cookies_file = Path(cookies_file) if cookies_file else None
        self.hl = hl or "zh-CN"
        self.gl = gl or "US"

    async def close(self) -> None:
        pass

    def _post_innertube_sync(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_sec: float = 8.0,
    ) -> bytes:
        """使用与 yt-dlp 相同的 urllib3/requests 引擎发送 POST 请求（避免 asyncio start_tls 代理卡死）"""
        p = (self.proxy or "").strip()
        proxies = {"http": p, "https": p} if p else None

        try:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                proxies=proxies,
                timeout=(5.0, timeout_sec),
                verify=False,
            )
            if 200 <= resp.status_code < 300:
                return resp.content
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:180]}")
        except requests.exceptions.ProxyError:
            # 仅当代理服务器本身不可达（如填错局域网代理 IP）时，才回退尝试一次无代理直连
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                proxies=None,
                timeout=(4.0, timeout_sec),
                verify=False,
            )
            if 200 <= resp.status_code < 300:
                return resp.content
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:180]}")

    async def _post_innertube(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_sec: float = 8.0,
    ) -> bytes:
        return await asyncio.to_thread(
            self._post_innertube_sync,
            url,
            payload,
            headers,
            timeout_sec,
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        min_score: float = 0.15,
        filter_type: str = "songs",
    ) -> list[Track]:
        """三级容灾搜索 YouTube Music 歌曲并按相似度打分排序"""
        query = (query or "").strip()
        if not query:
            return []

        raw_tracks: list[Track] = []
        now_ver = "1." + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d") + ".01.00"
        params_token = VIDEOS_FILTER_PARAMS if filter_type == "videos" else SONGS_FILTER_PARAMS

        # ---- 一级：music.youtube.com (WEB_REMIX) ----
        try:
            auth_headers = build_ytmusic_auth_headers(self.cookies_file, origin="https://music.youtube.com")
            headers = {
                "User-Agent": DEFAULT_UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Language": self.hl,
                **auth_headers,
            }
            payload = {
                "context": {
                    "client": {
                        "clientName": "WEB_REMIX",
                        "clientVersion": now_ver,
                        "hl": self.hl,
                        "gl": self.gl,
                    }
                },
                "query": query,
                "params": params_token,
            }
            body = await self._post_innertube(DEFAULT_SEARCH_URL, payload, headers, timeout_sec=8.0)
            raw_tracks = parse_search_response(body)
        except Exception as e1:
            log.debug(f"[YTMusic] 一级搜索未命中，切换二级搜索: {e1}")

        # ---- 二级：www.youtube.com (WEB InnerTube) ----
        if not raw_tracks:
            try:
                auth_headers = build_ytmusic_auth_headers(self.cookies_file, origin="https://www.youtube.com")
                headers = {
                    "User-Agent": DEFAULT_UA,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Language": self.hl,
                    **auth_headers,
                }
                web_ver = "2." + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d") + ".01.00"
                payload = {
                    "context": {
                        "client": {
                            "clientName": "WEB",
                            "clientVersion": web_ver,
                            "hl": self.hl,
                            "gl": self.gl,
                        }
                    },
                    "query": f"{query} Audio",
                }
                body = await self._post_innertube(WEB_SEARCH_URL, payload, headers, timeout_sec=8.0)
                raw_tracks = parse_search_response(body)
            except Exception as e2:
                log.debug(f"[YTMusic] 二级搜索未命中，切换 yt-dlp 检索: {e2}")

        # ---- 三级：yt-dlp ytsearch 扁平检索兜底 ----
        if not raw_tracks:
            raw_tracks = await asyncio.to_thread(self._search_via_ytdlp_sync, query, limit)

        scored: list[Track] = []
        seen_ids: set[str] = set()

        for t in raw_tracks:
            if t.video_id in seen_ids:
                continue
            seen_ids.add(t.video_id)
            score_disp = match_score(query, t.display_name)
            score_title = match_score(query, t.title)
            t.match_score = max(score_disp, score_title)
            if t.match_score >= min_score:
                scored.append(t)

        if not scored and raw_tracks:
            scored = sorted(raw_tracks, key=lambda x: x.match_score, reverse=True)[:3]

        scored.sort(key=lambda x: x.match_score, reverse=True)
        limit = max(1, min(20, limit))
        result = scored[:limit]
        for idx, item in enumerate(result, 1):
            item.index = idx
        return result

    def _search_via_ytdlp_sync(self, query: str, limit: int = 10) -> list[Track]:
        import yt_dlp

        count = max(1, min(15, limit))
        proxy_url = (self.proxy or "").strip() or None
        with snapshot_for_ytdlp(self.cookies_file) as cookie_snap:
            ydl_opts: dict[str, Any] = {
                "extract_flat": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "nocheckcertificate": True,
                "socket_timeout": 8,
                "cachedir": False,
            }
            if proxy_url:
                ydl_opts["proxy"] = proxy_url
            if cookie_snap:
                ydl_opts["cookiefile"] = cookie_snap
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"ytsearch{count}:{query}", download=False) or {}
                return self._entries_to_tracks(info.get("entries") or [])
            except Exception as err:
                log.debug(f"[YTMusic] yt-dlp ytsearch 失败: {err}")
                return []

    @staticmethod
    def _entries_to_tracks(entries: list[Any]) -> list[Track]:
        out: list[Track] = []
        for idx, e in enumerate(entries, 1):
            if not isinstance(e, dict) or not e.get("id"):
                continue
            vid = str(e["id"]).strip()
            if len(vid) > 20 or not vid:
                continue
            title = str(e.get("title") or vid).strip()
            if title in ("[Deleted video]", "[Private video]"):
                continue
            uploader = str(e.get("uploader") or e.get("channel") or e.get("artist") or "").strip()
            uploader = re.sub(r"\s*-\s*Topic$", "", uploader, flags=re.IGNORECASE).strip()
            artists = [uploader] if uploader else []
            dur_sec = int(e.get("duration") or 0)
            thumbs = e.get("thumbnails") or []
            thumb_url = thumbs[-1].get("url", "") if (isinstance(thumbs, list) and thumbs) else ""
            out.append(
                Track(
                    video_id=vid,
                    title=title,
                    artists=artists,
                    duration=_format_duration_seconds(dur_sec) if dur_sec else "",
                    duration_seconds=dur_sec,
                    thumbnail=thumb_url,
                    display_name=build_display_name(title, artists),
                    index=idx,
                )
            )
        return out

    async def fetch_library_playlists(self) -> list[dict[str, Any]]:
        """拉取已登录用户的 YouTube Music 播放列表库（FEmusic_liked_playlists + 默认 LM 喜欢列表）"""
        now_ver = "1." + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d") + ".01.00"
        auth_headers = build_ytmusic_auth_headers(self.cookies_file, origin="https://music.youtube.com")
        headers = {
            "User-Agent": DEFAULT_UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": self.hl,
            **auth_headers,
        }
        payload = {
            "context": {
                "client": {
                    "clientName": "WEB_REMIX",
                    "clientVersion": now_ver,
                    "hl": self.hl,
                    "gl": self.gl,
                }
            },
            "browseId": "FEmusic_liked_playlists",
        }
        playlists: list[dict[str, Any]] = []
        try:
            body = await self._post_innertube(DEFAULT_BROWSE_URL, payload, headers, timeout_sec=10.0)
            playlists = parse_library_playlists(body)
        except Exception as e:
            log.warning(f"[YTMusic] 拉取 FEmusic_liked_playlists 异常: {e}")

        # 确保始终包含「我喜欢的音乐」(LM)
        if not any(p.get("playlist_id") in ("LM", "VLLM") for p in playlists):
            playlists.insert(
                0,
                {
                    "playlist_id": "LM",
                    "title": "我喜欢的音乐",
                    "subtitle": "YouTube Music 自动同步",
                    "thumbnail": "",
                },
            )
        return playlists

    async def fetch_playlist_tracks(self, playlist_id: str, max_tracks: int = 100) -> tuple[str, list[Track]]:
        """获取指定 YouTube Music 播放列表的标题与歌曲列表（优先 InnerTube browse，自动回退 yt-dlp）"""
        pl_id = (playlist_id or "").strip()
        if pl_id.startswith("VL"):
            pl_id = pl_id[2:]
        if not pl_id:
            return "", []

        browse_id = f"VL{pl_id}"
        now_ver = "1." + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d") + ".01.00"
        auth_headers = build_ytmusic_auth_headers(self.cookies_file, origin="https://music.youtube.com")
        headers = {
            "User-Agent": DEFAULT_UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": self.hl,
            **auth_headers,
        }
        payload = {
            "context": {
                "client": {
                    "clientName": "WEB_REMIX",
                    "clientVersion": now_ver,
                    "hl": self.hl,
                    "gl": self.gl,
                }
            },
            "browseId": browse_id,
        }

        title = ""
        tracks: list[Track] = []
        try:
            body = await self._post_innertube(DEFAULT_BROWSE_URL, payload, headers, timeout_sec=10.0)
            root = json.loads(body)
            # 尝试提取歌单标题
            def find_title(obj: dict[str, Any]) -> None:
                nonlocal title
                if title:
                    return
                for r_key in ("musicDetailHeaderRenderer", "musicEditablePlaylistDetailHeaderRenderer", " musicResponsiveHeaderRenderer"):
                    hdr = obj.get(r_key.strip())
                    if isinstance(hdr, dict):
                        t = _join_run_texts(_dig(hdr, "title", "runs")) or _join_run_texts(
                            _dig(hdr, "header", "musicDetailHeaderRenderer", "title", "runs")
                        )
                        if t:
                            title = t
                            return

            _walk_json(root, find_title)
            tracks = parse_search_response(root)
        except Exception as e:
            log.debug(f"[YTMusic] InnerTube 歌单详情拉取未命中 ({pl_id})，切换 yt-dlp: {e}")

        if not tracks:
            yt_title, yt_tracks = await asyncio.to_thread(
                self._fetch_playlist_via_ytdlp_sync, pl_id, max_tracks
            )
            if yt_title and not title:
                title = yt_title
            tracks = yt_tracks

        tracks = tracks[:max_tracks]
        for idx, t in enumerate(tracks, 1):
            t.index = idx
        return title, tracks

    def _fetch_playlist_via_ytdlp_sync(self, playlist_id: str, max_tracks: int = 100) -> tuple[str, list[Track]]:
        import yt_dlp

        url = f"https://music.youtube.com/playlist?list={playlist_id}"
        proxy_url = (self.proxy or "").strip() or None
        with snapshot_for_ytdlp(self.cookies_file) as cookie_snap:
            ydl_opts: dict[str, Any] = {
                "extract_flat": "in_playlist",
                "playlistend": max_tracks,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "nocheckcertificate": True,
                "socket_timeout": 10,
                "cachedir": False,
            }
            if proxy_url:
                ydl_opts["proxy"] = proxy_url
            if cookie_snap:
                ydl_opts["cookiefile"] = cookie_snap
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False) or {}
                pl_title = str(info.get("title") or "").strip()
                entries = info.get("entries") or []
                return pl_title, self._entries_to_tracks(entries)
            except Exception as e:
                log.debug(f"[YTMusic] yt-dlp 拉取歌单 ({playlist_id}) 异常: {e}")
                return "", []

    async def extract_direct_audio_url(self, video_id: str) -> dict[str, Any] | None:
        """快速路径：通过 InnerTube VISIONOS / ANDROID_VR 端点提取免 JS/免 PO Token 的原生 M4A 音频流"""
        video_id = (video_id or "").strip()
        if not video_id:
            return None

        cookie_status = inspect_cookie_file(self.cookies_file)
        if cookie_status.logged_in:
            return None

        clients = [
            {
                "name": "VISIONOS",
                "context": {
                    "client": {
                        "clientName": "VISIONOS",
                        "clientVersion": "1.02",
                        "deviceMake": "Apple",
                        "deviceModel": "RealityDevice17,1",
                        "osName": "visionOS",
                        "osVersion": "26.5.23O471",
                        "hl": "en",
                        "gl": self.gl or "US",
                    }
                },
                "headers": {
                    "User-Agent": VISIONOS_UA,
                    "Content-Type": "application/json",
                    "X-YouTube-Client-Name": "101",
                    "X-YouTube-Client-Version": "1.02",
                },
            },
            {
                "name": "ANDROID_VR",
                "context": {
                    "client": {
                        "clientName": "ANDROID_VR",
                        "clientVersion": "1.65.10",
                        "deviceMake": "Oculus",
                        "deviceModel": "Quest 3",
                        "androidSdkVersion": 32,
                        "osName": "Android",
                        "osVersion": "12L",
                        "hl": "en",
                        "gl": self.gl or "US",
                    }
                },
                "headers": {
                    "User-Agent": ANDROID_VR_UA,
                    "Content-Type": "application/json",
                    "X-YouTube-Client-Name": "28",
                    "X-YouTube-Client-Version": "1.65.10",
                },
            },
        ]

        for client_cfg in clients:
            payload = {
                "context": client_cfg["context"],
                "videoId": video_id,
                "contentCheckOk": True,
                "racyCheckOk": True,
            }
            try:
                raw = await self._post_innertube(
                    DEFAULT_PLAYER_URL,
                    payload,
                    client_cfg["headers"],
                    timeout_sec=6.0,
                )
                data = json.loads(raw)
                if _dig(data, "playabilityStatus", "status") != "OK":
                    continue
                formats = _dig(data, "streamingData", "adaptiveFormats")
                if not isinstance(formats, list):
                    continue

                best_fmt = None
                for fmt in formats:
                    if not isinstance(fmt, dict) or not fmt.get("url"):
                        continue
                    mime = str(fmt.get("mimeType") or "")
                    if not mime.startswith("audio/"):
                        continue
                    if fmt.get("itag") == 140:
                        best_fmt = fmt
                        break
                    if best_fmt is None and "audio/mp4" in mime:
                        best_fmt = fmt

                if best_fmt is None:
                    continue

                duration_sec = int(_dig(data, "videoDetails", "lengthSeconds") or 0)
                return {
                    "url": best_fmt["url"],
                    "ext": "m4a",
                    "mime_type": "audio/mp4",
                    "user_agent": client_cfg["headers"]["User-Agent"],
                    "content_length": int(best_fmt.get("contentLength") or 0),
                    "duration_seconds": duration_sec,
                    "title": str(_dig(data, "videoDetails", "title") or ""),
                    "author": str(_dig(data, "videoDetails", "author") or ""),
                }
            except Exception as e:
                log.debug(f"InnerTube {client_cfg['name']} 直链未命中 ({video_id}): {e}")
                continue
        return None

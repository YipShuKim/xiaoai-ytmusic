"""YouTube Music 播放列表自动同步与本地持久化管理模块

核心功能：
1. 登录成功后自动拉取用户 YouTube Music 账号下的所有歌单（含「我喜欢的音乐」LM 与自建/收藏歌单）并保存至 /app/data/playlists.json。
2. 支持按歌单链接或 Playlist ID 手动导入歌单，或将当前播放队列一键保存为本地歌单。
3. 支持小爱语音口令（「播放歌单 XXX」）模糊匹配已保存的歌单并整单推送播放。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import AppConfig
from app.ytmusic.client import Track, YTMusicClient
from app.ytmusic.cookies import inspect_cookie_file
from app.ytmusic.matching import match_score, normalize

log = logging.getLogger("xiaoai-ytmusic.playlists")


@dataclass
class SavedPlaylist:
    playlist_id: str
    title: str
    subtitle: str = ""
    thumbnail: str = ""
    source: str = "ytmusic"  # ytmusic | custom
    updated_at: float = 0.0
    tracks: list[Track] = field(default_factory=list)

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    def to_summary_dict(self) -> dict[str, Any]:
        thumb = self.thumbnail
        if not thumb and self.tracks:
            thumb = self.tracks[0].thumbnail
        return {
            "playlist_id": self.playlist_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "thumbnail": thumb,
            "source": self.source,
            "track_count": len(self.tracks),
            "updated_at": self.updated_at,
        }

    def to_full_dict(self) -> dict[str, Any]:
        d = self.to_summary_dict()
        d["tracks"] = [t.to_dict() for t in self.tracks]
        return d


def extract_playlist_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        return ""
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    if s.startswith("VL") and len(s) > 2:
        return s[2:]
    return s


class PlaylistStore:
    """管理已同步与自建的 YouTube Music 播放列表"""

    def __init__(self, config: AppConfig, yt_client: YTMusicClient):
        self.config = config
        self.yt_client = yt_client
        self._playlists: dict[str, SavedPlaylist] = {}
        self.syncing: bool = False
        self.last_sync_at: float = 0.0
        self.last_sync_error: str = ""
        self._load()

    @property
    def playlists_file(self) -> Path:
        return Path(self.config.data_dir) / "playlists.json"

    def _load(self) -> None:
        p = self.playlists_file
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            items = raw.get("playlists") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                return
            self._playlists.clear()
            for item in items:
                if not isinstance(item, dict) or not item.get("playlist_id"):
                    continue
                tracks: list[Track] = []
                for idx, td in enumerate(item.get("tracks") or [], 1):
                    if not isinstance(td, dict) or not td.get("video_id"):
                        continue
                    tracks.append(
                        Track(
                            video_id=str(td["video_id"]),
                            title=str(td.get("title") or td["video_id"]),
                            artists=list(td.get("artists") or []),
                            album=str(td.get("album") or ""),
                            duration=str(td.get("duration") or ""),
                            duration_seconds=int(td.get("duration_seconds") or 0),
                            thumbnail=str(td.get("thumbnail") or ""),
                            display_name=str(td.get("display_name") or td.get("title") or td["video_id"]),
                            index=idx,
                        )
                    )
                pl = SavedPlaylist(
                    playlist_id=str(item["playlist_id"]),
                    title=str(item.get("title") or item["playlist_id"]),
                    subtitle=str(item.get("subtitle") or ""),
                    thumbnail=str(item.get("thumbnail") or ""),
                    source=str(item.get("source") or "ytmusic"),
                    updated_at=float(item.get("updated_at") or 0.0),
                    tracks=tracks,
                )
                self._playlists[pl.playlist_id] = pl
            self.last_sync_at = float(raw.get("last_sync_at") or 0.0) if isinstance(raw, dict) else 0.0
        except Exception as e:
            log.warning(f"[Playlists] 读取歌单文件失败: {e}")

    def save(self) -> None:
        try:
            os.makedirs(self.config.data_dir, exist_ok=True)
            payload = {
                "last_sync_at": self.last_sync_at,
                "playlists": [pl.to_full_dict() for pl in self._playlists.values()],
            }
            tmp = self.playlists_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.playlists_file)
        except Exception as e:
            log.error(f"[Playlists] 保存歌单文件失败: {e}")

    def list_summaries(self) -> list[dict[str, Any]]:
        items = sorted(
            self._playlists.values(),
            key=lambda p: (0 if p.playlist_id == "LM" else 1, -p.updated_at),
        )
        return [p.to_summary_dict() for p in items]

    def get(self, playlist_id: str) -> SavedPlaylist | None:
        return self._playlists.get(playlist_id)

    def delete(self, playlist_id: str) -> bool:
        removed = self._playlists.pop(playlist_id, None)
        if removed:
            self.save()
            return True
        return False

    def save_custom_playlist(
        self,
        title: str,
        tracks: list[Track],
        playlist_id: str = "",
        source: str = "custom",
    ) -> SavedPlaylist:
        title = (title or "").strip() or "未命名歌单"
        pl_id = (playlist_id or "").strip() or f"custom_{secrets.token_hex(4)}"
        indexed_tracks: list[Track] = []
        for idx, t in enumerate(tracks, 1):
            copy_t = Track(**t.to_dict())
            copy_t.index = idx
            indexed_tracks.append(copy_t)
        pl = SavedPlaylist(
            playlist_id=pl_id,
            title=title,
            subtitle=f"共 {len(indexed_tracks)} 首",
            thumbnail=indexed_tracks[0].thumbnail if indexed_tracks else "",
            source=source,
            updated_at=time.time(),
            tracks=indexed_tracks,
        )
        self._playlists[pl_id] = pl
        self.save()
        return pl

    def find_by_name(self, query: str) -> SavedPlaylist | None:
        """按名称模糊匹配已保存的播放列表（供语音口令「播放歌单 XXX」调用）"""
        if not self._playlists:
            return None
        query = (query or "").strip()
        if not query:
            # 未指定歌单名时优先返回「我喜欢的音乐」或第一个非空歌单
            if "LM" in self._playlists and self._playlists["LM"].tracks:
                return self._playlists["LM"]
            for pl in self._playlists.values():
                if pl.tracks:
                    return pl
            return None

        nq = normalize(query)
        # 特殊别名匹配
        if any(alias in query for alias in ("喜欢", "红心", "收藏", "liked")):
            if "LM" in self._playlists and self._playlists["LM"].tracks:
                return self._playlists["LM"]

        best_pl: SavedPlaylist | None = None
        best_score = 0.0
        for pl in self._playlists.values():
            if not pl.tracks:
                continue
            if pl.title == query or normalize(pl.title) == nq:
                return pl
            sc = match_score(query, pl.title)
            if sc > best_score:
                best_score = sc
                best_pl = pl

        if best_pl and best_score >= 0.35:
            return best_pl
        return None

    async def sync_from_ytmusic(self, max_playlists: int = 20, max_tracks_per_list: int = 100) -> dict[str, Any]:
        """自动从已登录的 YouTube Music 账号拉取所有播放列表及曲目并落盘保存"""
        if self.syncing:
            return {"status": "syncing", "message": "歌单同步正在后台进行中"}

        cookie_status = inspect_cookie_file(self.config.cookies_file)
        if not cookie_status.valid:
            return {"status": "skipped", "message": "尚未导入 YouTube Music Cookie，无法同步账号歌单"}

        self.syncing = True
        self.last_sync_error = ""
        synced_count = 0
        total_tracks = 0
        try:
            metas = await self.yt_client.fetch_library_playlists()
            metas = metas[:max_playlists]
            now = time.time()

            for meta in metas:
                pl_id = str(meta.get("playlist_id") or "").strip()
                if not pl_id:
                    continue
                fetched_title, tracks = await self.yt_client.fetch_playlist_tracks(
                    pl_id, max_tracks=max_tracks_per_list
                )
                if not tracks:
                    continue
                title = str(meta.get("title") or fetched_title or pl_id).strip()
                if pl_id == "LM" and title in ("LM", "VLLM", "Liked Music"):
                    title = "我喜欢的音乐 (Liked Music)"
                thumb = str(meta.get("thumbnail") or (tracks[0].thumbnail if tracks else "")).strip()
                self._playlists[pl_id] = SavedPlaylist(
                    playlist_id=pl_id,
                    title=title,
                    subtitle=str(meta.get("subtitle") or f"共 {len(tracks)} 首"),
                    thumbnail=thumb,
                    source="ytmusic",
                    updated_at=now,
                    tracks=tracks,
                )
                synced_count += 1
                total_tracks += len(tracks)

            self.last_sync_at = time.time()
            self.save()
            log.info(f"[Playlists] 已自动同步保存 {synced_count} 个 YouTube Music 播放列表 (共 {total_tracks} 首曲目)")
            return {
                "status": "ok",
                "synced_playlists": synced_count,
                "total_tracks": total_tracks,
                "playlists": self.list_summaries(),
            }
        except Exception as e:
            self.last_sync_error = str(e)
            log.warning(f"[Playlists] 同步 YouTube Music 歌单异常: {e}")
            return {"status": "error", "error": str(e), "playlists": self.list_summaries()}
        finally:
            self.syncing = False

    async def import_playlist(self, url_or_id: str, custom_title: str = "") -> SavedPlaylist:
        """手动通过 YouTube Music 播放列表 URL 或 ID 拉取并保存歌单"""
        pl_id = extract_playlist_id(url_or_id)
        if not pl_id:
            raise ValueError("请输入有效的播放列表 URL 或 Playlist ID")

        fetched_title, tracks = await self.yt_client.fetch_playlist_tracks(pl_id, max_tracks=150)
        if not tracks:
            raise RuntimeError(f"未能从播放列表 ({pl_id}) 中提取到曲目，请检查歌单权限或登录状态")

        title = (custom_title or "").strip() or fetched_title or (
            "我喜欢的音乐 (Liked Music)" if pl_id == "LM" else f"歌单 {pl_id[:8]}"
        )
        pl = SavedPlaylist(
            playlist_id=pl_id,
            title=title,
            subtitle=f"共 {len(tracks)} 首",
            thumbnail=tracks[0].thumbnail if tracks else "",
            source="ytmusic",
            updated_at=time.time(),
            tracks=tracks,
        )
        self._playlists[pl_id] = pl
        self.save()
        return pl

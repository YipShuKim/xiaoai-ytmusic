"""小爱音箱 YouTube Music 播放队列与自动续播/预取管理器
（融合自 songloft-plugin-miot/src/player/manager.ts 与 voice_resume.ts）
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from app.config import AppConfig
from app.xiaomi.client import XiaomiClient
from app.ytmusic.client import Track, YTMusicClient
from app.ytmusic.downloader import AudioDownloader

log = logging.getLogger("xiaoai-ytmusic.player")


class SpeakerPlayer:
    """单台小爱音箱的 YouTube Music 播放状态机与队列控制器"""

    def __init__(
        self,
        device_id: str,
        config: AppConfig,
        xiaomi: XiaomiClient,
        yt_client: YTMusicClient,
        downloader: AudioDownloader,
    ):
        self.device_id = device_id
        self.config = config
        self.xiaomi = xiaomi
        self.yt_client = yt_client
        self.downloader = downloader

        self.queue: list[Track] = []
        self.current_index: int = -1
        self.state: str = "stopped"  # stopped | playing | paused | loading
        self.play_mode: str = config.play_mode or "loop"  # loop | single | random | order
        self.last_query: str = ""
        self.started_at: float = 0.0
        self.elapsed_before_pause: float = 0.0
        self.current_stream_url: str = ""
        self.last_error: str = ""

        self._generation: int = 0
        self._auto_next_task: asyncio.Task | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._resume_task: asyncio.Task | None = None

    @property
    def current_track(self) -> Track | None:
        if 0 <= self.current_index < len(self.queue):
            return self.queue[self.current_index]
        return None

    def _cancel_timers(self) -> None:
        for task in (self._auto_next_task, self._prefetch_task, self._resume_task):
            if task and not task.done():
                task.cancel()
        self._auto_next_task = None
        self._prefetch_task = None
        self._resume_task = None

    def get_position(self) -> int:
        if self.state == "playing" and self.started_at > 0:
            return int(self.elapsed_before_pause + (time.time() - self.started_at))
        return int(self.elapsed_before_pause)

    def to_status_dict(self) -> dict[str, Any]:
        track = self.current_track
        dev = self.xiaomi.get_device_info(self.device_id) or {}
        return {
            "device_id": self.device_id,
            "device_name": dev.get("name") or self.device_id,
            "hardware": dev.get("hardware") or "",
            "state": self.state,
            "play_mode": self.play_mode,
            "last_query": self.last_query,
            "current_index": self.current_index,
            "position": self.get_position(),
            "duration": track.duration_seconds if track else 0,
            "current_track": track.to_dict() if track else None,
            "current_stream_url": self.current_stream_url,
            "last_error": self.last_error,
            "queue": [t.to_dict() for t in self.queue],
        }

    async def search_and_play(self, query: str, interrupt_first: bool = True) -> Track | None:
        """在 YouTube Music 搜索关键词，建立播放队列并立即推送首曲到小爱音箱"""
        query = (query or "").strip()
        if not query:
            return None

        self._generation += 1
        gen = self._generation
        self._cancel_timers()
        self.state = "loading"
        self.last_query = query
        self.last_error = ""

        # 1. 立即打断小爱音箱原有的“需要会员/找不到”播报
        if interrupt_first:
            try:
                await self.xiaomi.stop(self.device_id)
                if self.config.interrupt_tts_enabled and self.config.interrupt_tts_text:
                    await asyncio.sleep(0.2)
                    await self.xiaomi.text_to_speech(self.device_id, self.config.interrupt_tts_text)
            except Exception as e:
                log.debug(f"[Player] 打断小爱原声异常 ({self.device_id}): {e}")

        if gen != self._generation:
            return None

        # 2. 搜索 YouTube Music
        try:
            tracks = await self.yt_client.search(
                query,
                limit=self.config.default_search_limit,
                min_score=self.config.min_match_score,
            )
        except Exception as e:
            self.state = "stopped"
            self.last_error = f"YouTube Music 搜索失败: {e}"
            log.error(f"[Player] {self.last_error}")
            return None

        if gen != self._generation:
            return None

        if not tracks:
            self.state = "stopped"
            self.last_error = f"未找到与「{query}」匹配的歌曲"
            await self.xiaomi.text_to_speech(self.device_id, f"未找到歌曲{query}")
            return None

        self.queue = tracks
        ok = await self.play_index(0, expected_gen=gen)
        return self.current_track if ok else None

    async def play_playlist_tracks(
        self,
        tracks: list[Track],
        playlist_title: str = "",
        start_index: int = 0,
        interrupt_first: bool = True,
    ) -> Track | None:
        """整单加载已保存的播放列表并开始播放"""
        if not tracks:
            return None
        self._generation += 1
        gen = self._generation
        self._cancel_timers()
        self.state = "loading"
        if playlist_title:
            self.last_query = f"歌单: {playlist_title}"
        self.last_error = ""

        if interrupt_first:
            try:
                await self.xiaomi.stop(self.device_id)
            except Exception:
                pass

        if gen != self._generation:
            return None

        self.queue = [Track(**t.to_dict()) for t in tracks]
        for idx, item in enumerate(self.queue, 1):
            item.index = idx
        idx = max(0, min(len(self.queue) - 1, start_index))
        ok = await self.play_index(idx, expected_gen=gen)
        return self.current_track if ok else None

    async def play_single_track(self, track: Track, append_only: bool = False) -> bool:
        """播放指定单曲（供 Web 端一键点播使用）"""
        if append_only and self.queue:
            self.queue.append(track)
            return True
        # 若已在队列中则直接定位，否则置于队首
        for idx, item in enumerate(self.queue):
            if item.video_id == track.video_id:
                return await self.play_index(idx)
        self.queue.insert(0, track)
        for i, item in enumerate(self.queue, 1):
            item.index = i
        return await self.play_index(0)

    async def play_index(self, index: int, expected_gen: int | None = None) -> bool:
        if not self.queue or index < 0 or index >= len(self.queue):
            return False

        if expected_gen is None:
            self._generation += 1
            gen = self._generation
        else:
            gen = expected_gen

        self._cancel_timers()
        self.current_index = index
        track = self.queue[index]
        self.state = "loading"
        self.last_error = ""

        try:
            entry = await self.downloader.ensure_downloaded(track.video_id, track)
        except Exception as e:
            if gen == self._generation:
                self.state = "stopped"
                self.last_error = f"音频拉取失败 ({track.title}): {e}"
            log.error(f"[Player] 下载音频失败 ({track.video_id}): {e}")
            return False

        if gen != self._generation:
            return False

        if entry.duration_seconds > 0 and track.duration_seconds <= 0:
            track.duration_seconds = entry.duration_seconds

        stream_url = f"{self.config.base_url()}/stream/{track.video_id}.{entry.ext}"
        self.current_stream_url = stream_url
        artist_str = ", ".join(track.artists) if track.artists else ""

        dev = self.xiaomi.get_device_info(self.device_id) or {}
        hardware = dev.get("hardware", "")

        ok = await self.xiaomi.play_url(
            device_id=self.device_id,
            audio_url=stream_url,
            hardware=hardware,
            title=track.title,
            artist=artist_str,
        )
        if gen != self._generation:
            return False

        if not ok:
            self.state = "stopped"
            self.last_error = "向小爱音箱下发播放指令失败"
            return False

        self.state = "playing"
        self.started_at = time.time()
        self.elapsed_before_pause = 0.0
        log.info(
            f"[Player] 开始播放 [{self.device_id}]: {track.display_name} -> {stream_url}"
        )

        # 启动自动切歌定时器与下一首后台预缓存
        self._schedule_auto_next(gen, track.duration_seconds or entry.duration_seconds or 240)
        if self.config.prefetch_next:
            self._schedule_prefetch(gen)
        return True

    def _next_index(self, manual: bool = False) -> int:
        n = len(self.queue)
        if n == 0:
            return -1
        if self.play_mode == "single" and not manual:
            return self.current_index
        if self.play_mode == "random":
            if n == 1:
                return 0
            candidates = [i for i in range(n) if i != self.current_index]
            return random.choice(candidates)
        next_idx = self.current_index + 1
        if next_idx >= n:
            if self.play_mode == "order" and not manual:
                return -1
            return 0
        return next_idx

    def _schedule_auto_next(self, gen: int, duration_sec: int) -> None:
        wait_sec = max(10, int(duration_sec) + 2)

        async def _timer() -> None:
            try:
                await asyncio.sleep(wait_sec)
                if gen != self._generation or self.state != "playing":
                    return
                nxt = self._next_index(manual=False)
                if nxt >= 0:
                    log.info(f"[Player] 单曲播放完毕，自动切换下一首 (index={nxt})")
                    await self.play_index(nxt)
                else:
                    self.state = "stopped"
            except asyncio.CancelledError:
                pass

        self._auto_next_task = asyncio.create_task(_timer())

    def _schedule_prefetch(self, gen: int) -> None:
        nxt = self._next_index(manual=True)
        if nxt < 0 or nxt >= len(self.queue) or nxt == self.current_index:
            return
        next_track = self.queue[nxt]

        async def _prefetch() -> None:
            try:
                await asyncio.sleep(2.0)
                if gen != self._generation:
                    return
                await self.downloader.ensure_downloaded(next_track.video_id, next_track)
                log.info(f"[Player] 已预缓存下一首: {next_track.display_name}")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.debug(f"[Player] 预缓存下一首忽略异常: {e}")

        self._prefetch_task = asyncio.create_task(_prefetch())

    async def next(self) -> bool:
        if not self.queue:
            return False
        nxt = self._next_index(manual=True)
        if nxt < 0:
            return False
        return await self.play_index(nxt)

    async def previous(self) -> bool:
        if not self.queue:
            return False
        prev_idx = (self.current_index - 1) % len(self.queue)
        return await self.play_index(prev_idx)

    async def pause(self) -> bool:
        self._generation += 1
        self._cancel_timers()
        if self.state == "playing" and self.started_at > 0:
            self.elapsed_before_pause += time.time() - self.started_at
            self.started_at = 0.0
        res = await self.xiaomi.pause(self.device_id)
        self.state = "paused" if res == "paused" else "stopped"
        return True

    async def resume(self) -> bool:
        if not self.queue:
            return False
        if self.state == "paused":
            ok = await self.xiaomi.resume(self.device_id)
            if ok:
                self._generation += 1
                gen = self._generation
                self.state = "playing"
                self.started_at = time.time()
                track = self.current_track
                rem = max(15, (track.duration_seconds if track else 240) - int(self.elapsed_before_pause))
                self._schedule_auto_next(gen, rem)
                return True
        # 若固件已丢失上下文（stopped），从头重播当前曲目
        idx = max(0, self.current_index)
        return await self.play_index(idx)

    async def stop(self) -> bool:
        self._generation += 1
        self._cancel_timers()
        self.state = "stopped"
        self.started_at = 0.0
        self.elapsed_before_pause = 0.0
        return await self.xiaomi.stop(self.device_id)

    def set_play_mode(self, mode: str) -> str:
        mode_map = {
            "loop": "loop",
            "列表循环": "loop",
            "循环": "loop",
            "single": "single",
            "单曲循环": "single",
            "单曲": "single",
            "random": "random",
            "随机": "random",
            "随机播放": "random",
            "order": "order",
            "顺序": "order",
            "顺序播放": "order",
        }
        resolved = mode_map.get(mode.strip(), "loop")
        self.play_mode = resolved
        self.config.play_mode = resolved
        self.config.save()
        return resolved

    def suspend_for_voice_And_schedule_resume(self) -> None:
        """当播放中途用户唤醒小爱询问非音乐问题时，挂起切歌定时器并在小爱回答结束后自动续播"""
        if not self.config.smart_resume_enabled or self.state != "playing" or not self.current_track:
            return
        if self._auto_next_task and not self._auto_next_task.done():
            self._auto_next_task.cancel()
        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()

        gen = self._generation

        async def _smart_resume() -> None:
            try:
                await asyncio.sleep(6.0)
                if gen != self._generation or self.state != "playing":
                    return
                st = await self.xiaomi.get_player_status(self.device_id)
                if st.get("status") != 1:
                    log.info(f"[Player] 检测到语音问答打断播放，自动恢复当前曲目 ({self.device_id})")
                    await self.play_index(self.current_index, expected_gen=gen)
            except asyncio.CancelledError:
                pass

        self._resume_task = asyncio.create_task(_smart_resume())


class PlayerManager:
    """管理所有音箱的播放器实例"""

    def __init__(
        self,
        config: AppConfig,
        xiaomi: XiaomiClient,
        yt_client: YTMusicClient,
        downloader: AudioDownloader,
    ):
        self.config = config
        self.xiaomi = xiaomi
        self.yt_client = yt_client
        self.downloader = downloader
        self._players: dict[str, SpeakerPlayer] = {}

    def get_or_create(self, device_id: str) -> SpeakerPlayer:
        if device_id not in self._players:
            self._players[device_id] = SpeakerPlayer(
                device_id=device_id,
                config=self.config,
                xiaomi=self.xiaomi,
                yt_client=self.yt_client,
                downloader=self.downloader,
            )
        return self._players[device_id]

    def get_all_statuses(self) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(self.config.enabled_device_ids + list(self._players.keys())))
        return [self.get_or_create(did).to_status_dict() for did in ids if did]

    async def stop_all(self) -> None:
        for p in self._players.values():
            await p.stop()

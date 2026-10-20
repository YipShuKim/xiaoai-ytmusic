"""小爱音箱对话监听与语音口令拦截引擎
（融合自 songloft-plugin-miot/src/conversation/monitor.ts 与 src/music/service.ts）

核心特性：
1. 服务端时间戳基线初始化（primed）：完全不依赖 NAS 本地时钟，避免时钟偏差导致历史指令重放或新指令漏检。
2. 跨优先级最长关键词匹配 + 有界跳字模糊子序列匹配（如“我今天想听晴天”命中“我想听”）。
3. 拦截后立即切断小爱原厂 VIP 提示并转交 YouTube Music 队列播放。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.config import AppConfig
from app.engine.player import PlayerManager
from app.xiaomi.client import XiaomiClient
from app.ytmusic.playlists import PlaylistStore

log = logging.getLogger("xiaoai-ytmusic.voice")

FUZZY_MAX_GAP = 4
FUZZY_MIN_KEYWORD_LEN = 3

COMMAND_PRIORITY: dict[str, int] = {
    "play_playlist": 1,
    "play_artist": 1,
    "play_song": 1,
    "set_play_mode": 2,
    "next": 3,
    "previous": 4,
    "resume": 5,
    "stop": 5,
}

SEARCH_COMMAND_TYPES = {"play_song", "play_artist", "play_playlist"}


@dataclass
class VoiceMatch:
    command_type: str
    keyword: str
    argument: str
    param: str = ""
    source: str = "rule"  # rule | fuzzy


@dataclass
class ConversationLogItem:
    timestamp_ms: int
    device_id: str
    device_name: str
    query: str
    xiaoai_answer: str
    intercepted: bool = False
    command_type: str = ""
    matched_keyword: str = ""
    argument: str = ""
    played_track: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fuzzy_subseq_match(query: str, keyword: str, max_gap: int = FUZZY_MAX_GAP) -> tuple[int, int] | None:
    """有界跳字子序列匹配：允许关键词中间插入不超过 max_gap 个字符

    返回 (last_idx, inserted_count) 或 None
    """
    kw_len = len(keyword)
    if kw_len < FUZZY_MIN_KEYWORD_LEN or len(query) < kw_len:
        return None

    best: tuple[int, int] | None = None
    for start in range(0, len(query) - kw_len + 1):
        if query[start] != keyword[0]:
            continue
        ki = 1
        qi = start + 1
        while qi < len(query) and ki < kw_len:
            if query[qi] == keyword[ki]:
                ki += 1
            qi += 1
        if ki < kw_len:
            continue
        last_idx = qi - 1
        inserted = (last_idx - start + 1) - kw_len
        if inserted > max_gap:
            continue
        if best is None or inserted < best[1]:
            best = (last_idx, inserted)
    return best


def clean_song_argument(arg: str, is_artist: bool = False) -> str:
    """清理语音识别末尾标点与口语助词"""
    s = (arg or "").strip()
    s = re.sub(r"^[，。！？、,.!?\s]+|[，。！？、,.!?\s]+$", "", s)
    if is_artist:
        s = re.sub(r"(的歌曲|的歌|的音乐)$", "", s).strip()
    else:
        s = re.sub(r"(这首歌|的歌|吧|呀|呢)$", "", s).strip()
    return s


class VoiceCommandEngine:
    """语音口令匹配与动作分发器"""

    def __init__(
        self,
        config: AppConfig,
        xiaomi: XiaomiClient,
        players: PlayerManager,
        playlists: PlaylistStore | None = None,
    ):
        self.config = config
        self.xiaomi = xiaomi
        self.players = players
        self.playlists = playlists
        self._ensure_playlist_command()

    def _ensure_playlist_command(self) -> None:
        """为旧配置文件自动补齐 play_playlist 口令规则"""
        if not any(isinstance(c, dict) and c.get("type") == "play_playlist" for c in self.config.voice_commands):
            self.config.voice_commands.insert(
                0,
                {
                    "type": "play_playlist",
                    "keywords": ["播放歌单", "放歌单", "播放列表", "播放我喜欢的音乐", "播放喜欢的音乐", "播放收藏的歌"],
                    "enabled": True,
                },
            )

    def match_command(self, query: str) -> VoiceMatch | None:
        query = (query or "").strip()
        if not query:
            return None

        commands = [c for c in self.config.voice_commands if isinstance(c, dict) and c.get("enabled", True)]
        if not commands:
            return None

        # 计算最早出现的搜索类口令边界，避免歌名内部包含“暂停/继续”等字被误判为控制命令
        search_start = len(query) + 1
        arg_start = len(query) + 1
        search_kw_len = 0

        for cmd in commands:
            ctype = str(cmd.get("type") or "")
            if ctype not in SEARCH_COMMAND_TYPES:
                continue
            for kw in cmd.get("keywords") or []:
                if not kw:
                    continue
                idx = query.find(kw)
                if idx >= 0 and (idx < search_start or (idx == search_start and len(kw) > search_kw_len)):
                    search_start = idx
                    arg_start = idx + len(kw)
                    search_kw_len = len(kw)

        best_match: VoiceMatch | None = None
        best_kw_len = 0
        best_prio = 99

        for cmd in commands:
            ctype = str(cmd.get("type") or "")
            prio = COMMAND_PRIORITY.get(ctype, 99)
            param = str(cmd.get("param") or "")
            for kw in cmd.get("keywords") or []:
                if not kw:
                    continue
                idx = query.find(kw)
                if 0 <= idx < arg_start:
                    raw_arg = query[idx + len(kw) :].strip()
                    cleaned_arg = clean_song_argument(raw_arg, is_artist=(ctype == "play_artist"))
                    # 若是“播放”等搜索口令但后面没有任何歌名参数（play_playlist 允许无参数播放默认喜欢歌单）
                    if ctype in ("play_song", "play_artist") and not cleaned_arg:
                        continue
                    if len(kw) > best_kw_len or (len(kw) == best_kw_len and prio < best_prio):
                        best_kw_len = len(kw)
                        best_prio = prio
                        best_match = VoiceMatch(
                            command_type=ctype,
                            keyword=kw,
                            argument=cleaned_arg,
                            param=param,
                            source="rule",
                        )

        if best_match is not None:
            return best_match

        # 第二趟：跳字模糊子序列匹配（如“我今天想听周杰伦的晴天” 命中 “我想听”）
        best_inserted = 999
        for cmd in commands:
            ctype = str(cmd.get("type") or "")
            prio = COMMAND_PRIORITY.get(ctype, 99)
            param = str(cmd.get("param") or "")
            for kw in cmd.get("keywords") or []:
                m = fuzzy_subseq_match(query, kw, FUZZY_MAX_GAP)
                if not m:
                    continue
                last_idx, inserted = m
                raw_arg = query[last_idx + 1 :].strip()
                cleaned_arg = clean_song_argument(raw_arg, is_artist=(ctype == "play_artist"))
                if ctype in ("play_song", "play_artist") and not cleaned_arg:
                    continue
                better = (
                    len(kw) > best_kw_len
                    or (len(kw) == best_kw_len and inserted < best_inserted)
                    or (len(kw) == best_kw_len and inserted == best_inserted and prio < best_prio)
                )
                if better:
                    best_kw_len = len(kw)
                    best_inserted = inserted
                    best_prio = prio
                    best_match = VoiceMatch(
                        command_type=ctype,
                        keyword=kw,
                        argument=cleaned_arg,
                        param=param,
                        source="fuzzy",
                    )

        return best_match

    async def execute_match(self, device_id: str, match: VoiceMatch) -> str:
        """执行匹配到的口令，返回执行结果描述"""
        player = self.players.get_or_create(device_id)
        ctype = match.command_type

        if ctype == "play_playlist":
            arg = match.argument
            if "喜欢" in match.keyword and not arg:
                arg = "我喜欢的音乐"
            if self.playlists:
                pl = self.playlists.find_by_name(arg)
                if pl and pl.tracks:
                    track = await player.play_playlist_tracks(pl.tracks, playlist_title=pl.title, interrupt_first=True)
                    return f"歌单《{pl.title}》({track.display_name if track else ''})"
            # 若本地未找到对应歌单但有名称，回退在线搜索
            if arg:
                track = await player.search_and_play(arg, interrupt_first=True)
                return track.display_name if track else f"未找到歌单: {arg}"
            return "未找到已保存的播放列表"

        if ctype in ("play_song", "play_artist"):
            track = await player.search_and_play(match.argument, interrupt_first=True)
            return track.display_name if track else f"未找到: {match.argument}"

        if ctype == "next":
            await self.xiaomi.stop(device_id)
            ok = await player.next()
            return (player.current_track.display_name if (ok and player.current_track) else "切歌")

        if ctype == "previous":
            await self.xiaomi.stop(device_id)
            ok = await player.previous()
            return (player.current_track.display_name if (ok and player.current_track) else "上一首")

        if ctype == "stop":
            await player.stop()
            return "已停止播放"

        if ctype == "resume":
            await self.xiaomi.stop(device_id)
            ok = await player.resume()
            return (player.current_track.display_name if (ok and player.current_track) else "恢复播放")

        if ctype == "set_play_mode":
            mode = player.set_play_mode(match.param or match.argument)
            player.suspend_for_voice_And_schedule_resume()
            return f"播放模式: {mode}"

        return ""


@dataclass
class _DevicePollState:
    device_id: str
    device_name: str
    hardware: str
    last_timestamp_ms: int = 0
    primed: bool = False


class ConversationMonitor:
    """定时轮询小爱音箱云端对话记录并触发口令拦截"""

    def __init__(
        self,
        config: AppConfig,
        xiaomi: XiaomiClient,
        voice_engine: VoiceCommandEngine,
        players: PlayerManager,
    ):
        self.config = config
        self.xiaomi = xiaomi
        self.voice_engine = voice_engine
        self.players = players

        self._states: dict[str, _DevicePollState] = {}
        self._history: list[ConversationLogItem] = []
        self._max_history = 100
        self._task: asyncio.Task | None = None
        self._running = False

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        items = self._history[-limit:] if limit > 0 else self._history
        return [item.to_dict() for item in reversed(items)]

    def get_status(self) -> dict[str, Any]:
        return {
            "running": self._running and self.config.voice_enabled,
            "poll_interval": self.config.poll_interval,
            "monitored_devices": [
                {
                    "device_id": s.device_id,
                    "device_name": s.device_name,
                    "hardware": s.hardware,
                    "primed": s.primed,
                    "last_timestamp_ms": s.last_timestamp_ms,
                }
                for s in self._states.values()
            ],
            "history_count": len(self._history),
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("[ConversationMonitor] 语音对话监听已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._states.clear()
        log.info("[ConversationMonitor] 语音对话监听已停止")

    async def _sync_devices(self) -> None:
        if not self.xiaomi.auth.is_authenticated():
            return
        devices = await self.xiaomi.get_devices(force_refresh=False)
        enabled_set = set(self.config.enabled_device_ids)

        for d in devices:
            did = d["deviceID"]
            if did in enabled_set and did not in self._states:
                self._states[did] = _DevicePollState(
                    device_id=did,
                    device_name=d.get("name") or did,
                    hardware=d.get("hardware") or "LX06",
                )
        for did in list(self._states.keys()):
            if did not in enabled_set:
                self._states.pop(did, None)

    async def _loop(self) -> None:
        while self._running:
            try:
                if not self.config.voice_enabled or not self.xiaomi.auth.is_authenticated():
                    await asyncio.sleep(3.0)
                    continue

                await self._sync_devices()
                if not self._states:
                    await asyncio.sleep(3.0)
                    continue

                await asyncio.gather(
                    *(self._poll_device(st) for st in list(self._states.values())),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"[ConversationMonitor] 轮询异常: {e}")

            interval = max(1.0, min(15.0, float(self.config.poll_interval or 2.0)))
            await asyncio.sleep(interval)

    async def _poll_device(self, st: _DevicePollState) -> None:
        records = await self.xiaomi.get_latest_ask(st.device_id, st.hardware, limit=5)
        # 取记录失败（None）时绝不更新或建立基线
        if records is None:
            return

        # 首轮仅建立服务端时间戳去重基线，不触发历史指令执行
        if not st.primed:
            st.last_timestamp_ms = max((r["timestamp_ms"] for r in records), default=0)
            st.primed = True
            log.info(
                f"[ConversationMonitor] 设备基线已建立: {st.device_name} "
                f"(last_ts={st.last_timestamp_ms})"
            )
            return

        new_records = [r for r in records if r["timestamp_ms"] > st.last_timestamp_ms]
        if not new_records:
            return

        new_records.sort(key=lambda r: r["timestamp_ms"])
        st.last_timestamp_ms = new_records[-1]["timestamp_ms"]

        for rec in new_records:
            await self._process_new_record(st, rec)

    async def _process_new_record(self, st: _DevicePollState, rec: dict[str, Any]) -> None:
        query = rec["query"]
        answer = rec.get("answer", "")
        ts = rec["timestamp_ms"]

        match = self.voice_engine.match_command(query)
        log_item = ConversationLogItem(
            timestamp_ms=ts,
            device_id=st.device_id,
            device_name=st.device_name,
            query=query,
            xiaoai_answer=answer,
            intercepted=match is not None,
            command_type=match.command_type if match else "",
            matched_keyword=match.keyword if match else "",
            argument=match.argument if match else "",
        )
        self._history.append(log_item)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        if match is not None:
            log.info(
                f"[Voice] 拦截语音指令 [{st.device_name}]: '{query}' -> "
                f"{match.command_type} (kw='{match.keyword}', arg='{match.argument}')"
            )
            played = await self.voice_engine.execute_match(st.device_id, match)
            log_item.played_track = played
        else:
            # 未匹配到音乐指令，若当前正在播放 YouTube Music，则触发智能打断续播
            player = self.players.get_or_create(st.device_id)
            player.suspend_for_voice_And_schedule_resume()

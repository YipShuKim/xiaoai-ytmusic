"""小爱音箱 MiNA 控制与对话记录客户端
（融合自 songloft-plugin-miot/src/mina/client.ts 与 miair-next/backend/app/engine/speaker.py）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from app.config import AppConfig
from app.xiaomi.auth import XiaomiAuthManager
from app.xiaomi.const import (
    DEFAULT_MUSIC_AUDIO_ID,
    LATEST_ASK_API_TEMPLATE,
    MINA_API_BASE_URL,
    MUSIC_CP_ID,
    need_use_play_music_api,
    should_use_mina_for_ask,
)

log = logging.getLogger("xiaoai-ytmusic.xiaomi.client")


def _generate_request_id() -> str:
    return "app_ios_" + secrets.token_hex(15)


class XiaomiClient:
    """小爱音箱云端 API 控制客户端"""

    def __init__(self, config: AppConfig, auth: XiaomiAuthManager):
        self.config = config
        self.auth = auth
        self._session: aiohttp.ClientSession | None = None
        self._devices_cache: list[dict[str, Any]] = []

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10, connect=5)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: str | None = None,
        extra_device_id: str = "",
        preserve_big_ids: bool = False,
    ) -> dict[str, Any] | None:
        if not self.auth.is_authenticated():
            ok = await self.auth.refresh_credentials(reason="missing_token")
            if not ok:
                return None

        session = await self._get_session()
        for attempt in range(2):
            headers = {
                "User-Agent": self.auth.user_agent(),
                "Cookie": "; ".join(
                    f"{k}={v}"
                    for k, v in self.auth.build_mina_cookies(extra_device_id).items()
                ),
            }
            if method.upper() == "POST":
                headers["Content-Type"] = "application/x-www-form-urlencoded"

            try:
                async with session.request(method, url, data=data, headers=headers) as resp:
                    if resp.status == 401 and attempt == 0:
                        refreshed = await self.auth.refresh_credentials(reason="http_401")
                        if refreshed:
                            continue
                        return None
                    if resp.status < 200 or resp.status >= 300:
                        return None
                    text = await resp.text()
                    if preserve_big_ids:
                        text = re.sub(r'"(audioID|songID)"\s*:\s*(-?\d+)', r'"\1":"\2"', text)
                    return json.loads(text)
            except Exception as e:
                if attempt == 1:
                    log.debug(f"[XiaomiClient] 请求失败 {method} {url}: {e}")
        return None

    async def get_devices(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        """获取账号名下所有小爱音箱列表"""
        if self._devices_cache and not force_refresh:
            return self._devices_cache
        if not (
            self.auth.is_authenticated()
            or (self.config.mi_user_id and self.config.mi_pass_token)
            or (self.config.mi_account and self.config.mi_password)
        ):
            return []

        url = f"{MINA_API_BASE_URL}/admin/v2/device_list?master=0"
        res = await self._request_json("GET", url)
        if not res or res.get("code") != 0 or not isinstance(res.get("data"), list):
            return self._devices_cache

        devices: list[dict[str, Any]] = []
        for d in res["data"]:
            if not isinstance(d, dict) or not d.get("deviceID"):
                continue
            dev_id = str(d.get("deviceID"))
            devices.append(
                {
                    "deviceID": dev_id,
                    "name": str(d.get("name") or d.get("alias") or dev_id),
                    "alias": str(d.get("alias") or d.get("name") or ""),
                    "miotDID": str(d.get("miotDID") or ""),
                    "model": str(d.get("model") or ""),
                    "hardware": str(d.get("hardware") or ""),
                    "presence": str(d.get("presence") or "online"),
                    "enabled": dev_id in self.config.enabled_device_ids,
                }
            )

        self._devices_cache = devices
        # 若用户尚未勾选任何音箱且列表非空，默认启用第一台在线音箱
        if devices and not self.config.enabled_device_ids:
            first_id = devices[0]["deviceID"]
            self.config.enabled_device_ids = [first_id]
            self.config.default_device_id = first_id
            devices[0]["enabled"] = True
            self.config.save()
        return devices

    def get_device_info(self, device_id: str) -> dict[str, Any] | None:
        for d in self._devices_cache:
            if d.get("deviceID") == device_id:
                return d
        return None

    async def ubus_request(
        self,
        device_id: str,
        method: str,
        path: str,
        message: dict[str, Any],
    ) -> dict[str, Any] | None:
        url = f"{MINA_API_BASE_URL}/remote/ubus"
        form = {
            "deviceId": device_id,
            "method": method,
            "path": path,
            "message": json.dumps(message, ensure_ascii=False),
            "requestId": _generate_request_id(),
        }
        res = await self._request_json("POST", url, data=urlencode(form))
        if not res or res.get("code") != 0:
            return None
        return res

    @staticmethod
    def _is_device_result_ok(res: dict[str, Any] | None) -> bool:
        if not res or res.get("code") != 0:
            return False
        data = res.get("data")
        if isinstance(data, dict) and "code" in data:
            try:
                if int(data["code"]) != 0:
                    return False
            except (TypeError, ValueError):
                pass
        return True

    async def search_audio_id(
        self,
        title: str,
        artist: str = "",
        fallback_audio_id: str = DEFAULT_MUSIC_AUDIO_ID,
    ) -> str:
        """搜索小米曲库获取真实 audioID（用于触屏音箱显示歌词与封面）"""
        title = (title or "").strip()
        if not title:
            return fallback_audio_id
        artist = (artist or "").strip()
        query = f"{title}-{artist}" if artist else title
        form = {
            "query": query,
            "queryType": "1",
            "offset": "0",
            "count": "6",
            "timestamp": str(int(time.time() * 1000)),
            "requestId": _generate_request_id(),
        }
        res = await self._request_json(
            "POST",
            f"{MINA_API_BASE_URL}/music/search",
            data=urlencode(form),
            preserve_big_ids=True,
        )
        songs = (res or {}).get("data", {}).get("songList")
        if not isinstance(songs, list) or not songs:
            return fallback_audio_id

        first_artist = re.split(r"[;；,，&、/·・—]", artist)[0].strip().lower() if artist else ""
        for s in songs:
            if not isinstance(s, dict):
                continue
            s_name = str(s.get("name") or "").strip()
            s_artist = str((s.get("artist") or {}).get("name") or "").strip()
            if s_name.lower() == title.lower():
                if not first_artist or first_artist in s_artist.lower():
                    aid = str(s.get("audioID") or "").strip()
                    if aid:
                        return aid

        first_aid = str(songs[0].get("audioID") or "").strip()
        return first_aid or fallback_audio_id

    async def play_url(
        self,
        device_id: str,
        audio_url: str,
        hardware: str = "",
        title: str = "",
        artist: str = "",
    ) -> bool:
        """根据音箱硬件型号自动选择 player_play_music 或 player_play_url 推送音频流"""
        if not hardware:
            dev = self.get_device_info(device_id)
            if dev:
                hardware = dev.get("hardware", "")

        use_music_api = need_use_play_music_api(hardware, self.config.disabled_music_api_models)
        if use_music_api:
            audio_id = DEFAULT_MUSIC_AUDIO_ID
            if self.config.touchscreen_lyrics and title:
                try:
                    audio_id = await asyncio.wait_for(
                        self.search_audio_id(title, artist, DEFAULT_MUSIC_AUDIO_ID),
                        timeout=3.5,
                    )
                except Exception:
                    audio_id = DEFAULT_MUSIC_AUDIO_ID

            ok = await self.play_by_music_url(device_id, audio_url, audio_id=audio_id)
            if ok:
                return True
            log.warning(f"[XiaomiClient] player_play_music 失败 ({device_id})，回退到 player_play_url")

        res = await self.ubus_request(
            device_id,
            "player_play_url",
            "mediaplayer",
            {"url": audio_url, "type": 1, "media": "app_ios"},
        )
        return self._is_device_result_ok(res)

    async def play_by_music_url(
        self,
        device_id: str,
        audio_url: str,
        audio_id: str = DEFAULT_MUSIC_AUDIO_ID,
    ) -> bool:
        music_payload = {
            "payload": {
                "audio_type": "MUSIC",
                "audio_items": [
                    {
                        "item_id": {
                            "audio_id": audio_id,
                            "cp": {
                                "album_id": "-1",
                                "episode_index": 0,
                                "id": MUSIC_CP_ID,
                                "name": "xiaowei",
                            },
                        },
                        "stream": {"url": audio_url},
                    }
                ],
                "list_params": {
                    "listId": "-1",
                    "loadmore_offset": 0,
                    "origin": "xiaowei",
                    "type": "MUSIC",
                },
            },
            "play_behavior": "REPLACE_ALL",
        }
        res = await self.ubus_request(
            device_id,
            "player_play_music",
            "mediaplayer",
            {"startaudioid": audio_id, "music": json.dumps(music_payload, ensure_ascii=False)},
        )
        return self._is_device_result_ok(res)

    async def player_operation(self, device_id: str, action: str) -> bool:
        res = await self.ubus_request(
            device_id,
            "player_play_operation",
            "mediaplayer",
            {"action": action, "media": "app_ios"},
        )
        return self._is_device_result_ok(res)

    async def pause(self, device_id: str) -> str:
        """暂停播放并核验状态，若设备忽略 pause 则自动升级为 stop（修复部分小爱固件 pause 无效问题）"""
        await self.player_operation(device_id, "pause")
        for _ in range(2):
            await asyncio.sleep(0.5)
            st = await self.get_player_status(device_id)
            if st.get("status") != 1:
                return "paused"
        if await self.player_operation(device_id, "stop"):
            return "stopped"
        return "paused"

    async def stop(self, device_id: str) -> bool:
        """强制停止播放（先 pause 再 stop，彻底打断小爱自带回复或残留缓冲）"""
        await self.player_operation(device_id, "pause")
        return await self.player_operation(device_id, "stop")

    async def resume(self, device_id: str) -> bool:
        return await self.player_operation(device_id, "play")

    async def set_volume(self, device_id: str, volume: int) -> bool:
        v = max(0, min(100, int(volume)))
        res = await self.ubus_request(
            device_id,
            "player_set_volume",
            "mediaplayer",
            {"volume": v},
        )
        return self._is_device_result_ok(res)

    async def get_player_status(self, device_id: str) -> dict[str, Any]:
        """获取音箱状态：{status: 0=stopped|1=playing|2=paused|-1=unknown, volume: int}"""
        res = await self.ubus_request(
            device_id,
            "player_get_play_status",
            "mediaplayer",
            {},
        )
        if not res or not isinstance(res.get("data"), dict):
            return {"status": -1, "volume": -1}
        info_str = res["data"].get("info")
        if not isinstance(info_str, str):
            return {"status": -1, "volume": -1}
        try:
            info = json.loads(info_str)
            return {
                "status": int(info.get("status", -1)),
                "volume": int(info.get("volume", -1)),
                "position": int(info.get("position", 0)),
                "duration": int(info.get("duration", 0)),
            }
        except Exception:
            return {"status": -1, "volume": -1}

    async def text_to_speech(self, device_id: str, text: str) -> bool:
        """TTS 语音播报（优先走 mibrain/text_to_speech，失败回退 mediaplayer/player_play_tts）"""
        text = (text or "").strip()
        if not text:
            return False
        res = await self.ubus_request(device_id, "text_to_speech", "mibrain", {"text": text})
        if self._is_device_result_ok(res):
            return True
        res2 = await self.ubus_request(device_id, "player_play_tts", "mediaplayer", {"text": text})
        return self._is_device_result_ok(res2)

    async def get_latest_ask(
        self,
        device_id: str,
        hardware: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]] | None:
        """拉取最新语音对话记录

        返回值约定（与 songloft-plugin-miot 一致）：
        - None: 网络或鉴权失败（调用方不可将其视为无记录）
        - []: 请求成功，但暂无对话记录
        - [{"timestamp_ms": int, "query": str, "answer": str}, ...]
        """
        if not hardware:
            dev = self.get_device_info(device_id)
            if dev:
                hardware = dev.get("hardware", "")

        if should_use_mina_for_ask(hardware):
            return await self._get_latest_ask_by_ubus(device_id)

        url = LATEST_ASK_API_TEMPLATE.format(
            hardware=hardware or "LX06",
            timestamp=int(time.time() * 1000),
            limit=limit,
        )
        res = await self._request_json("GET", url, extra_device_id=device_id)
        if res is None or res.get("code") != 0:
            return None

        data_str = res.get("data")
        if not data_str:
            return []
        try:
            data_obj = json.loads(data_str) if isinstance(data_str, str) else data_str
            records = data_obj.get("records") or []
            out: list[dict[str, Any]] = []
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                ts = int(rec.get("time") or 0)
                q = str(rec.get("query") or "").strip()
                if ts <= 0 or not q:
                    continue
                ans_text = ""
                for a in rec.get("answers") or []:
                    if isinstance(a, dict) and a.get("type") == "TTS":
                        ans_text = str((a.get("tts") or {}).get("text") or "").strip()
                        break
                out.append({"timestamp_ms": ts, "query": q, "answer": ans_text})
            return out
        except Exception:
            return None

    async def _get_latest_ask_by_ubus(self, device_id: str) -> list[dict[str, Any]] | None:
        res = await self.ubus_request(device_id, "nlp_result_get", "mibrain", {})
        if not res or not isinstance(res.get("data"), dict):
            return None
        data = res["data"]
        if data.get("code") != 0:
            return None
        info_str = data.get("info")
        if not info_str:
            return []
        try:
            info = json.loads(info_str)
            out: list[dict[str, Any]] = []
            for item in info.get("result") or []:
                nlp_str = item.get("nlp") if isinstance(item, dict) else None
                if not nlp_str:
                    continue
                nlp = json.loads(nlp_str)
                ts = int((nlp.get("meta") or {}).get("timestamp") or 0)
                answers = (nlp.get("response") or {}).get("answer") or []
                if ts <= 0 or not answers:
                    continue
                ans0 = answers[0]
                q = str((ans0.get("intention") or {}).get("query") or "").strip()
                a = str((ans0.get("content") or {}).get("to_speak") or "").strip()
                if q:
                    out.append({"timestamp_ms": ts, "query": q, "answer": a})
            return out
        except Exception:
            return None

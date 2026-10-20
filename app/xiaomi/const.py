"""小米账号与小爱音箱协议常量表"""

from __future__ import annotations

ACCOUNT_BASE_URL = "https://account.xiaomi.com"
LONG_POLLING_URL = "https://account.xiaomi.com/longPolling/loginUrl"
MINA_API_BASE_URL = "https://api2.mina.mi.com"

MINA_SID = "micoapi"
QR_LOGIN_SID = "mijia"
SDK_VERSION = "3.8.6"

USER_AGENT_TEMPLATE = (
    "Android-7.1.1-1.0.0-ONEPLUS A3010-136-%s APP/xiaomi.smarthome APPV/62830"
)

LATEST_ASK_API_TEMPLATE = (
    "https://userprofile.mina.mi.com/device_profile/v2/conversation"
    "?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit={limit}"
)

DEFAULT_MUSIC_AUDIO_ID = "1732418460076477549"
MUSIC_CP_ID = "355454500"

# 需要通过 Mina ubus (nlp_result_get) 获取对话记录的设备型号
GET_ASK_BY_MINA: set[str] = {"M01"}

# 默认使用 player_play_music 接口播放（支持触屏歌词与封面）的设备硬件型号
DEFAULT_MUSIC_API_MODELS: set[str] = {
    "X08C",
    "X08E",
    "X8F",
    "X4B",
    "LX05",
    "OH11",
    "OH2",
    "OH2P",
    "X6A",
    "LX04",
    "L05B",
    "L05C",
    "LX06",
    "L06A",
    "X08A",
    "X10A",
    "L15A",
    "L16A",
    "L17A",
}


def need_use_play_music_api(hardware: str, disabled_models: list[str] | None = None) -> bool:
    hw = (hardware or "").strip().upper()
    if not hw:
        return False
    if disabled_models and hw in {m.strip().upper() for m in disabled_models}:
        return False
    return hw in DEFAULT_MUSIC_API_MODELS


def should_use_mina_for_ask(hardware: str) -> bool:
    return (hardware or "").strip().upper() in GET_ASK_BY_MINA

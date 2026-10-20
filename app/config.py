"""配置管理与局域网 IP 自动探测模块"""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("xiaoai-ytmusic")


def detect_local_ip() -> str:
    """探测本机局域网 IPv4 地址（不发送实际数据包）"""
    for target in (("223.5.5.5", 80), ("114.114.114.114", 80), ("8.8.8.8", 80)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(1.0)
                s.connect(target)
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
        except Exception:
            continue
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


def default_voice_commands() -> list[dict[str, Any]]:
    """默认语音口令规则表（精简自 songloft-plugin-miot）"""
    return [
        {
            "type": "play_playlist",
            "keywords": ["播放歌单", "放歌单", "播放列表", "播放我喜欢的音乐", "播放喜欢的音乐", "播放收藏的歌"],
            "enabled": True,
        },
        {
            "type": "play_artist",
            "keywords": ["播放歌手", "放歌手", "听歌手"],
            "enabled": True,
        },
        {
            "type": "play_song",
            "keywords": ["播放歌曲", "播放音乐", "放歌曲", "放一首", "来一首", "我想听", "点播", "播放"],
            "enabled": True,
        },
        {
            "type": "next",
            "keywords": ["下一首", "切歌", "换一首", "下一曲"],
            "enabled": True,
        },
        {
            "type": "previous",
            "keywords": ["上一首", "上一曲"],
            "enabled": True,
        },
        {
            "type": "resume",
            "keywords": ["继续播放", "恢复播放", "继续", "接着播", "接着放"],
            "enabled": True,
        },
        {
            "type": "stop",
            "keywords": ["暂停播放", "停止播放", "暂停音乐", "停一下", "别播了", "关掉音乐", "暂停", "停止"],
            "enabled": True,
        },
        {
            "type": "set_play_mode",
            "param": "single",
            "keywords": ["单曲循环", "循环播放这首"],
            "enabled": True,
        },
        {
            "type": "set_play_mode",
            "param": "random",
            "keywords": ["随机播放", "随机模式"],
            "enabled": True,
        },
        {
            "type": "set_play_mode",
            "param": "loop",
            "keywords": ["列表循环", "循环播放", "顺序播放"],
            "enabled": True,
        },
    ]


@dataclass
class AppConfig:
    """全局运行配置（支持环境变量覆盖与 JSON 持久化）"""

    # 网络与服务设置
    host: str = "0.0.0.0"
    port: int = 8080
    lan_ip: str = ""
    admin_password: str = ""
    api_key: str = ""

    # 小米账号与认证凭据
    mi_account: str = ""
    mi_password: str = ""
    mi_user_id: str = ""
    mi_pass_token: str = ""
    mi_c_user_id: str = ""
    mi_device_id: str = ""
    mi_service_token: str = ""
    mi_ssecurity: str = ""
    mi_token_updated_at: float = 0.0

    # 小爱音箱设备控制设置
    enabled_device_ids: list[str] = field(default_factory=list)
    default_device_id: str = ""
    touchscreen_lyrics: bool = True
    disabled_music_api_models: list[str] = field(default_factory=list)
    default_volume: int = 0

    # 语音监听与拦截配置
    voice_enabled: bool = True
    poll_interval: float = 2.0
    interrupt_tts_enabled: bool = False
    interrupt_tts_text: str = "正在从 YouTube Music 搜索"
    smart_resume_enabled: bool = True
    play_mode: str = "loop"  # loop | single | random | order
    voice_commands: list[dict[str, Any]] = field(default_factory=default_voice_commands)

    # YouTube Music 搜索与下载配置
    ytmusic_proxy: str = ""
    ytmusic_hl: str = "zh-CN"
    ytmusic_gl: str = "US"
    audio_format: str = "m4a"  # 默认 m4a（原生 AAC 免 ffmpeg 转码，小爱音箱原生支持）
    min_match_score: float = 0.15
    default_search_limit: int = 10
    max_search_limit: int = 20
    prefetch_next: bool = True
    max_concurrent_downloads: int = 2
    download_timeout_sec: int = 90
    cache_ttl_hours: int = 72
    max_cache_mb: int = 1024

    # DLNA 投屏接收器配置
    dlna_enabled: bool = True
    dlna_friendly_name: str = "小爱音箱 (YouTube Music)"
    dlna_uuid: str = ""

    # 内部非序列化路径
    data_dir: str = field(default="./data", repr=False)

    @property
    def config_file(self) -> Path:
        return Path(self.data_dir) / "config.json"

    @property
    def cache_dir(self) -> Path:
        return Path(self.data_dir) / "cache"

    @property
    def cookies_file(self) -> Path:
        return Path(self.data_dir) / "cookies.txt"

    def effective_lan_ip(self) -> str:
        ip = (self.lan_ip or "").strip()
        if ip and not ip.startswith("127.") and ip != "0.0.0.0":
            return ip
        return detect_local_ip()

    def base_url(self) -> str:
        return f"http://{self.effective_lan_ip()}:{self.port}"

    @classmethod
    def load(cls, data_dir: str | None = None) -> "AppConfig":
        resolved_dir = data_dir or os.environ.get("DATA_DIR", "./data")
        os.makedirs(resolved_dir, exist_ok=True)
        cfg = cls(data_dir=resolved_dir)

        cfg_path = cfg.config_file
        if cfg_path.exists():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg.update_from_dict(raw, save_now=False)
            except Exception as e:
                log.warning(f"读取配置文件失败 ({cfg_path}): {e}")

        # 环境变量覆盖（首次或容器编排注入）
        if env_port := os.environ.get("PORT", "").strip():
            try:
                cfg.port = int(env_port)
            except ValueError:
                pass
        if env_lan := os.environ.get("LAN_IP", "").strip():
            cfg.lan_ip = env_lan
        if env_proxy := (
            os.environ.get("YTMUSIC_PROXY", "").strip()
            or os.environ.get("HTTPS_PROXY", "").strip()
            or os.environ.get("HTTP_PROXY", "").strip()
        ):
            if not cfg.ytmusic_proxy:
                cfg.ytmusic_proxy = env_proxy
        if env_pwd := os.environ.get("ADMIN_PASSWORD", "").strip():
            cfg.admin_password = env_pwd
        if env_key := os.environ.get("API_KEY", "").strip():
            cfg.api_key = env_key
        if env_cache_mb := os.environ.get("MAX_CACHE_MB", "").strip():
            try:
                cfg.max_cache_mb = max(64, int(env_cache_mb))
            except ValueError:
                pass
        if env_ttl := os.environ.get("CACHE_TTL_HOURS", "").strip():
            try:
                cfg.cache_ttl_hours = max(1, int(env_ttl))
            except ValueError:
                pass

        if not cfg.lan_ip:
            cfg.lan_ip = detect_local_ip()

        os.makedirs(cfg.cache_dir, exist_ok=True)
        cfg.save()
        return cfg

    def update_from_dict(self, data: dict[str, Any], save_now: bool = True) -> None:
        valid_fields = {f for f in self.__dataclass_fields__ if f != "data_dir"}
        for k, v in data.items():
            if k not in valid_fields or v is None:
                continue
            current = getattr(self, k)
            if isinstance(current, bool):
                setattr(self, k, bool(v))
            elif isinstance(current, int):
                try:
                    setattr(self, k, int(v))
                except (TypeError, ValueError):
                    pass
            elif isinstance(current, float):
                try:
                    setattr(self, k, float(v))
                except (TypeError, ValueError):
                    pass
            elif isinstance(current, str):
                setattr(self, k, str(v).strip())
            elif isinstance(current, list) and isinstance(v, list):
                setattr(self, k, v)
        if save_now:
            self.save()

    def save(self) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            payload = asdict(self)
            payload.pop("data_dir", None)
            tmp_path = self.config_file.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            tmp_path.replace(self.config_file)
        except Exception as e:
            log.error(f"保存配置失败: {e}")

    def to_public_dict(self) -> dict[str, Any]:
        """脱敏配置字典（供前端管理界面读取）"""
        d = asdict(self)
        d.pop("data_dir", None)
        d["mi_password"] = "******" if self.mi_password else ""
        d["mi_pass_token"] = "******" if self.mi_pass_token else ""
        d["mi_service_token"] = "******" if self.mi_service_token else ""
        d["mi_ssecurity"] = "******" if self.mi_ssecurity else ""
        d["admin_password"] = "******" if self.admin_password else ""
        d["effective_lan_ip"] = self.effective_lan_ip()
        d["base_url"] = self.base_url()
        d["has_mi_credentials"] = bool(
            (self.mi_user_id and (self.mi_pass_token or self.mi_service_token))
            or (self.mi_account and self.mi_password)
        )
        d["has_cookies_file"] = self.cookies_file.exists() and self.cookies_file.stat().st_size > 0
        return d

"""YouTube Music 登录凭证与 Cookie 智能管理模块
（整合自 Xeltra233/youtube-music-api/internal/cookies/store.go & ytdlp_guard.go 并增强多格式解析）

核心功能：
1. 多格式一键导入：自动识别并转换
   - 浏览器 F12 「Copy as cURL (bash / cmd)」或「Copy as fetch」
   - 原始 HTTP Request Header (`Cookie: ...`) 或 `SID=xxx; SAPISID=yyy` 键值对
   - Cookie-Editor 导出的 JSON 数组格式
   - Netscape `cookies.txt` 格式（自动修复缺失文件头、自动修复空格代替 Tab 的问题）
2. SAPISIDHASH 签名生成：为 YouTube Music InnerTube 搜索与播放接口生成 `Authorization: SAPISIDHASH` 请求头。
3. 快照保护机制（SnapshotForYtdlp）：每次调用 yt-dlp 时使用临时副本，防止 yt-dlp 退出时把真实登录态覆盖成匿名 Cookie。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

AUTH_COOKIE_KEYS = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
    "LOGIN_INFO",
}

SAPISID_KEYS = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")


@dataclass
class CookieItem:
    domain: str
    name: str
    value: str
    path: str = "/"
    secure: bool = True
    expires: int = 0
    http_only: bool = False


@dataclass
class CookieStatus:
    present: bool = False
    valid: bool = False
    logged_in: bool = False
    has_sapisid: bool = False
    cookie_count: int = 0
    auth_cookie_count: int = 0
    auth_cookie_names: list[str] = field(default_factory=list)
    score: int = 0
    size_bytes: int = 0
    modified_at: float = 0.0
    source_format: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_cmd_escapes(text: str) -> str:
    """清理 Windows Copy as cURL (cmd) 中的 ^ 转义符"""
    if "^" not in text:
        return text
    text = text.replace("^\r\n", " ").replace("^\n", " ")
    text = text.replace('^"', '"').replace("^%", "%").replace("^&", "&")
    text = text.replace("^|", "|").replace("^<", "<").replace("^>", ">")
    text = text.replace("^^", "^")
    return text


def _parse_kv_cookie_string(cookie_str: str, default_Exp: int) -> list[CookieItem]:
    items: list[CookieItem] = []
    seen: set[str] = set()
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k or not v or " " in k or "\t" in k or k in seen:
            continue
        seen.add(k)
        items.append(
            CookieItem(
                domain=".youtube.com",
                name=k,
                value=v,
                path="/",
                secure=True,
                expires=default_Exp,
                http_only=k in AUTH_COOKIE_KEYS,
            )
        )
    return items


def parse_any_cookies(raw_text: str) -> tuple[list[CookieItem], str]:
    """智能解析任意格式的 Cookie 输入，返回 (CookieItem列表, 识别出的格式名称)"""
    text = (raw_text or "").strip()
    if not text:
        return [], "empty"

    default_exp = int(time.time()) + 365 * 86400

    # 1. 检测 JSON 格式（如 Cookie-Editor 插件导出）
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "cookies" in data:
                data = data["cookies"]
            if isinstance(data, list):
                items: list[CookieItem] = []
                for obj in data:
                    if not isinstance(obj, dict):
                        continue
                    name = str(obj.get("name") or "").strip()
                    value = str(obj.get("value") or "").strip()
                    domain = str(obj.get("domain") or ".youtube.com").strip()
                    if not name or not value:
                        continue
                    if not domain.startswith(".") and "youtube.com" in domain:
                        domain = "." + domain
                    exp = int(float(obj.get("expirationDate") or obj.get("expires") or default_exp))
                    if exp <= 0:
                        exp = default_exp
                    items.append(
                        CookieItem(
                            domain=domain,
                            name=name,
                            value=value,
                            path=str(obj.get("path") or "/"),
                            secure=bool(obj.get("secure", True)),
                            expires=exp,
                            http_only=bool(obj.get("httpOnly", False)),
                        )
                    )
                if items:
                    return items, "json"
        except Exception:
            pass

    # 2. 检测 cURL 命令 / fetch 代码 / HTTP Headers
    cleaned = _clean_cmd_escapes(text)
    curl_cookies: list[str] = []
    # 匹配 -b '...' / --cookie "..." / -H 'cookie: ...' / "cookie": "..."
    for pattern in (
        r"(?:-b|--cookie)\s+['\"]([^'\"]+)['\"]",
        r"(?:-H|--header)\s+['\"]cookie\s*:\s*([^'\"]+)['\"]",
        r"['\"]cookie['\"]\s*:\s*['\"]([^'\"]+)['\"]",
        r"^cookie\s*:\s*(.+)$",
    ):
        for m in re.finditer(pattern, cleaned, flags=re.IGNORECASE | re.MULTILINE):
            val = m.group(1).strip()
            if "=" in val:
                curl_cookies.append(val)

    if curl_cookies:
        merged = "; ".join(curl_cookies)
        items = _parse_kv_cookie_string(merged, default_exp)
        if items:
            return items, "curl_or_headers"

    # 3. 检测 Netscape cookies.txt 格式（兼容标准 Tab 分隔与被转成空格的 7 列文本）
    netscape_items: list[CookieItem] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            # 兼容复制时 Tab 被替换为空格的情况
            parts = re.split(r"\s+", line, maxsplit=6)
        if len(parts) >= 7 and parts[1].upper() in ("TRUE", "FALSE") and parts[3].upper() in ("TRUE", "FALSE"):
            domain = parts[0].strip()
            path = parts[2].strip() or "/"
            secure = parts[3].upper() == "TRUE"
            try:
                exp = int(parts[4].strip())
            except ValueError:
                exp = default_exp
            if exp <= 0:
                exp = default_exp
            name = parts[5].strip()
            value = parts[6].strip()
            if domain and name and value:
                netscape_items.append(
                    CookieItem(
                        domain=domain,
                        name=name,
                        value=value,
                        path=path,
                        secure=secure,
                        expires=exp,
                        http_only=http_only,
                    )
                )

    if netscape_items:
        return netscape_items, "netscape"

    # 4. 检测纯 Cookie 键值对字符串（如 SID=xxx; HSID=yyy; SAPISID=zzz）
    if "=" in text and (";" in text or len(text.splitlines()) == 1):
        kv_items = _parse_kv_cookie_string(text.replace("\n", "; "), default_exp)
        if kv_items:
            return kv_items, "cookie_string"

    return [], "unknown"


def render_netscape_cookies(items: list[CookieItem]) -> str:
    """将 CookieItem 列表渲染为标准 7 列 Tab 分隔的 Netscape cookies.txt"""
    lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.haxx.se/rfc/cookie_spec.html",
        "# This is a generated file! Do not edit.",
        "",
    ]
    seen: set[tuple[str, str]] = set()
    future_exp = int(time.time()) + 365 * 86400

    for item in items:
        domain = item.domain.strip() or ".youtube.com"
        name = item.name.strip()
        value = item.value.strip()
        if not name or not value:
            continue

        # 确保主认证 Cookie 同时对 .youtube.com 生效
        domains_to_write = [domain]
        if "youtube.com" not in domain and "google.com" in domain and name in AUTH_COOKIE_KEYS:
            domains_to_write.append(".youtube.com")

        for dom in domains_to_write:
            if not dom.startswith(".") and dom.endswith(("youtube.com", "google.com")):
                dom = "." + dom
            key = (dom.lower(), name)
            if key in seen:
                continue
            seen.add(key)
            include_sub = "TRUE" if dom.startswith(".") else "FALSE"
            sec_str = "TRUE" if item.secure else "FALSE"
            exp = item.expires if item.expires > int(time.time()) else future_exp
            prefix = "#HttpOnly_" if item.http_only else ""
            lines.append(f"{prefix}{dom}\t{include_sub}\t{item.path or '/'}\t{sec_str}\t{exp}\t{name}\t{value}")

    return "\n".join(lines) + "\n"


def save_cookies_any_format(cookies_path: Path | str, raw_text: str) -> CookieStatus:
    """解析任意格式的输入，转换为标准 Netscape 文件原子落盘并返回质量评分"""
    p = Path(cookies_path)
    items, fmt = parse_any_cookies(raw_text)
    if not items:
        raise ValueError(
            "未能识别出有效的 Cookie 内容！请粘贴 Netscape cookies.txt、Cookie-Editor JSON、"
            "浏览器 F12 的 Copy as cURL 命令，或 Cookie: SID=... 字符串。"
        )

    netscape_text = render_netscape_cookies(items)
    os.makedirs(p.parent, exist_ok=True)
    tmp = p.with_suffix(".txt.tmp")
    tmp.write_text(netscape_text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)

    status = inspect_cookie_file(p)
    status.source_format = fmt
    return status


def inspect_cookie_file(cookies_path: Path | str | None) -> CookieStatus:
    """检查 cookies.txt 文件的完整性、登录态及质量分"""
    if not cookies_path:
        return CookieStatus()
    p = Path(cookies_path)
    if not p.exists() or not p.is_file():
        return CookieStatus()
    try:
        st = p.stat()
        if st.st_size == 0:
            return CookieStatus()
        raw = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return CookieStatus()

    items, fmt = parse_any_cookies(raw)
    if not items:
        return CookieStatus(present=True, size_bytes=st.st_size, modified_at=st.st_mtime)

    # 若原文件不是标准 Netscape（比如缺少 # Netscape HTTP Cookie File 头），自动标准化修复
    if not raw.lstrip().startswith("# Netscape HTTP Cookie File"):
        try:
            normalized = render_netscape_cookies(items)
            p.write_text(normalized, encoding="utf-8")
        except Exception:
            pass

    yt_items = [
        it for it in items if ("youtube.com" in it.domain.lower() or "google.com" in it.domain.lower())
    ]
    auth_names = sorted({it.name for it in yt_items if it.name in AUTH_COOKIE_KEYS})
    has_sid = any(k in auth_names for k in ("SID", "__Secure-1PSID", "__Secure-3PSID"))
    has_sapisid = any(k in auth_names for k in SAPISID_KEYS)
    has_login = "LOGIN_INFO" in auth_names

    logged_in = has_login or (has_sid and has_sapisid) or len(auth_names) >= 3
    score = len(yt_items) + len(auth_names) * 5 + (10 if logged_in else 0)

    return CookieStatus(
        present=True,
        valid=len(yt_items) > 0,
        logged_in=logged_in,
        has_sapisid=has_sapisid,
        cookie_count=len(yt_items),
        auth_cookie_count=len(auth_names),
        auth_cookie_names=auth_names,
        score=score,
        size_bytes=st.st_size,
        modified_at=st.st_mtime,
        source_format=fmt,
    )


def build_ytmusic_auth_headers(
    cookies_path: Path | str | None,
    origin: str = "https://music.youtube.com",
) -> dict[str, str]:
    """构建带 Cookie 与 SAPISIDHASH 签名的 YouTube Music 认证请求头"""
    headers: dict[str, str] = {
        "Cookie": "SOCS=CAI",
        "Origin": origin,
        "X-Origin": origin,
    }
    if not cookies_path:
        return headers
    p = Path(cookies_path)
    if not p.exists() or not p.is_file():
        return headers

    try:
        items, _ = parse_any_cookies(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return headers

    cookie_map: dict[str, str] = {}
    for it in items:
        dom = it.domain.lower()
        if "youtube.com" in dom or "google.com" in dom:
            cookie_map[it.name] = it.value

    if not cookie_map:
        return headers

    cookie_map.setdefault("SOCS", "CAI")
    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_map.items())

    sapisid = ""
    for key in SAPISID_KEYS:
        if cookie_map.get(key):
            sapisid = cookie_map[key]
            break

    if sapisid:
        ts = int(time.time())
        sha1_hex = hashlib.sha1(f"{ts} {sapisid} {origin}".encode("utf-8")).hexdigest()
        headers["Authorization"] = f"SAPISIDHASH {ts}_{sha1_hex}"
        headers["X-Goog-AuthUser"] = "0"

    return headers


@contextlib.contextmanager
def snapshot_for_ytdlp(cookies_path: Path | str | None) -> Iterator[str | None]:
    """为 yt-dlp 创建临时 cookies.txt 快照，防止 yt-dlp 原地覆写破坏真实登录 Cookie"""
    if not cookies_path:
        yield None
        return
    p = Path(cookies_path)
    if not p.exists() or not p.is_file() or p.stat().st_size == 0:
        yield None
        return

    # 确保快照文件具有合法的 # Netscape HTTP Cookie File 头部
    status = inspect_cookie_file(p)
    if not status.valid:
        yield None
        return

    tmp_path = p.parent / f".ytdlp-cookies-{secrets.token_hex(6)}.txt"
    try:
        shutil.copyfile(p, tmp_path)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        yield str(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

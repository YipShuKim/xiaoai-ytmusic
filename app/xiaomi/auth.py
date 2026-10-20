"""小米账号认证管理器（融合自 miair-next 的扫码登录 + 三级降级链 与 songloft-plugin-miot 的三步 STS 协议）

支持三种登录方式：
1. 米家 App 扫码登录（推荐，免验证码/免短信风控）
2. 账号密码登录（支持图形验证码续接）
3. Cookie / PassToken 手动导入登录
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp
from yarl import URL

from app.config import AppConfig
from app.xiaomi.const import (
    ACCOUNT_BASE_URL,
    LONG_POLLING_URL,
    MINA_SID,
    QR_LOGIN_SID,
    SDK_VERSION,
    USER_AGENT_TEMPLATE,
)

log = logging.getLogger("xiaoai-ytmusic.xiaomi.auth")

RELOGIN_COOLDOWN_SEC = 60
SERVICE_TOKEN_TTL_SEC = 12 * 3600
TOKEN_REFRESH_THRESHOLD_SEC = 9 * 3600


def strip_json_prefix(body: str) -> str:
    return (body or "").replace("&&&START&&&", "").strip()


def extract_bigint_field(json_str: str, field: str) -> str:
    m = re.search(rf'"{re.escape(field)}"\s*:\s*(\d+)', json_str)
    return m.group(1) if m else ""


def compute_client_sign(nonce: str | int, ssecurity: str) -> str:
    raw = f"nonce={nonce}&{ssecurity}".encode("utf-8")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def generate_device_id() -> str:
    return secrets.token_hex(8).upper()


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (cookie_str or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
    return out


class XiaomiAuthSession:
    """单次三步认证会话（支持密码登录、验证码、passToken 换取 serviceToken）"""

    def __init__(self, device_id: str = ""):
        self.device_id = device_id or generate_device_id()
        self.user_agent = USER_AGENT_TEMPLATE % self.device_id
        self.jar = aiohttp.CookieJar(unsafe=True)
        self._session: aiohttp.ClientSession | None = None
        self.login_data: dict[str, Any] | None = None
        self.captcha_ick: str = ""
        self.username: str = ""
        self.password: str = ""

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=self.jar,
                timeout=aiohttp.ClientTimeout(total=15, connect=6),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def get_cookie(self, name: str) -> str:
        for cookie in self.jar:
            if cookie.key == name and cookie.value:
                return cookie.value
        return ""

    def set_cookie(self, name: str, value: str, domain: str = "https://account.xiaomi.com") -> None:
        if value:
            self.jar.update_cookies({name: value}, response_url=URL(domain))

    async def login_with_password(
        self,
        username: str,
        password: str,
        captcha: str = "",
        sid: str = MINA_SID,
    ) -> dict[str, Any]:
        self.username = username.strip()
        self.password = password
        if not self.login_data or not captcha:
            step1 = await self._step1(sid)
            if not step1:
                return {"state": "failed", "error": "无法连接小米认证中心 (step1)"}
            self.login_data = step1
        return await self._step2_password(self.login_data, captcha, sid)

    async def exchange_pass_token(
        self,
        pass_token: str,
        user_id: str,
        c_user_id: str = "",
        sid: str = MINA_SID,
    ) -> dict[str, Any]:
        """用 passToken + userId 换取 micoapi 的 serviceToken 与 ssecurity"""
        url = f"{ACCOUNT_BASE_URL}/pass/serviceLogin?sid={sid}&_json=true"
        cookies = {
            "passToken": pass_token,
            "userId": str(user_id),
            "deviceId": self.device_id,
            "sdkVersion": SDK_VERSION,
        }
        if c_user_id:
            cookies["cUserId"] = c_user_id
        self.jar.update_cookies(cookies, response_url=URL(ACCOUNT_BASE_URL))

        headers = {"User-Agent": self.user_agent}
        async with self._client().get(url, headers=headers, allow_redirects=False) as resp:
            raw_text = await resp.text()

        json_str = strip_json_prefix(raw_text)
        try:
            data = json.loads(json_str)
        except Exception:
            return {"state": "failed", "error": f"解析 serviceLogin 响应失败: {json_str[:160]}"}

        code = int(data.get("code") or 0)
        if code != 0:
            return {
                "state": "failed",
                "error": f"passToken 换取凭证失败 (code={code}, desc={data.get('desc') or data.get('description')})",
            }

        location = str(data.get("location") or "")
        ssecurity = str(data.get("ssecurity") or "")
        nonce = extract_bigint_field(json_str, "nonce") or str(data.get("nonce") or "")
        uid = str(data.get("userId") or user_id)

        if not location or not ssecurity or not nonce:
            return {"state": "failed", "error": "serviceLogin 响应缺少 location/ssecurity/nonce"}

        return await self._step3_sts(location, nonce, ssecurity, uid, sid)

    async def _step1(self, sid: str) -> dict[str, Any] | None:
        url = f"{ACCOUNT_BASE_URL}/pass/serviceLogin?sid={sid}&_json=true"
        self.jar.update_cookies(
            {"sdkVersion": SDK_VERSION, "deviceId": self.device_id},
            response_url=URL(ACCOUNT_BASE_URL),
        )
        headers = {"User-Agent": self.user_agent}
        async with self._client().get(url, headers=headers, allow_redirects=False) as resp:
            text = await resp.text()
        try:
            return json.loads(strip_json_prefix(text))
        except Exception:
            return None

    async def _step2_password(
        self,
        step1: dict[str, Any],
        captcha: str,
        sid: str,
    ) -> dict[str, Any]:
        url = f"{ACCOUNT_BASE_URL}/pass/serviceLoginAuth2?_json=true"
        pwd_md5 = hashlib.md5(self.password.encode("utf-8")).hexdigest().upper()
        form = {
            "user": self.username,
            "hash": pwd_md5,
            "callback": str(step1.get("callback") or ""),
            "sid": str(step1.get("sid") or sid),
            "qs": str(step1.get("qs") or ""),
            "_sign": str(step1.get("_sign") or ""),
        }
        if captcha:
            form["captCode"] = captcha.strip()
            if self.captcha_ick:
                self.set_cookie("ick", self.captcha_ick)

        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with self._client().post(
            url,
            data=urlencode(form),
            headers=headers,
            allow_redirects=False,
        ) as resp:
            raw_text = await resp.text()

        json_str = strip_json_prefix(raw_text)
        try:
            result = json.loads(json_str)
        except Exception:
            return {"state": "failed", "error": f"解析登录响应失败: {json_str[:160]}"}

        # 检查是否触发图形验证码
        for cap_key in ("captchaUrl", "captchaURL", "captcha_url"):
            cap_val = str(result.get(cap_key) or "").strip()
            if cap_val:
                if not cap_val.startswith("http"):
                    cap_val = ACCOUNT_BASE_URL + cap_val
                cap_img = await self._fetch_captcha_base64(cap_val)
                return {
                    "state": "need_captcha",
                    "captcha_image": cap_img,
                    "error": "请输入图形验证码（或推荐使用扫码登录）",
                }

        # 检查是否触发手机/邮箱二次验证
        if result.get("notificationUrl"):
            return {
                "state": "need_verify",
                "error": "账号触发了安全二次验证，请直接使用上方「米家 App 扫码登录」免验证登录",
            }

        location = str(result.get("location") or "")
        if not location:
            return {
                "state": "failed",
                "error": f"登录失败 (code={result.get('code')}, desc={result.get('description') or result.get('desc')})",
            }

        ssecurity = str(result.get("ssecurity") or "")
        nonce = extract_bigint_field(json_str, "nonce") or str(result.get("nonce") or "")
        user_id = str(result.get("userId") or self.get_cookie("userId") or "")
        return await self._step3_sts(location, nonce, ssecurity, user_id, sid)

    async def _fetch_captcha_base64(self, captcha_url: str) -> str:
        try:
            async with self._client().get(captcha_url, headers={"User-Agent": self.user_agent}) as resp:
                data = await resp.read()
                ick = resp.cookies.get("ick")
                if ick and ick.value:
                    self.captcha_ick = ick.value
                else:
                    self.captcha_ick = self.get_cookie("ick")
                return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        except Exception:
            return ""

    async def _step3_sts(
        self,
        location: str,
        nonce: str,
        ssecurity: str,
        user_id: str,
        sid: str,
    ) -> dict[str, Any]:
        client_sign = compute_client_sign(nonce, ssecurity)
        sep = "&" if "?" in location else "?"
        sts_url = f"{location}{sep}{urlencode({'_userIdNeedEncrypt': 'true', 'clientSign': client_sign})}"

        headers = {"User-Agent": self.user_agent}
        async with self._client().get(sts_url, headers=headers, allow_redirects=True) as resp:
            status = resp.status
            resp_service_token = ""
            if "serviceToken" in resp.cookies and resp.cookies["serviceToken"].value:
                resp_service_token = resp.cookies["serviceToken"].value

        service_token = resp_service_token or self.get_cookie("serviceToken")
        if not service_token:
            return {"state": "failed", "error": f"STS 未返回 serviceToken (HTTP {status})"}

        pass_token = self.get_cookie("passToken")
        c_user_id = self.get_cookie("cUserId")
        final_user_id = user_id or self.get_cookie("userId")

        return {
            "state": "success",
            "user_id": str(final_user_id),
            "pass_token": pass_token,
            "c_user_id": c_user_id,
            "device_id": self.device_id,
            "service_token": service_token,
            "ssecurity": ssecurity,
            "sid": sid,
        }


class QRCodeSession:
    """米家 App 扫码登录单次会话（移植自 miair-next/backend/app/engine/qr_login.py）"""

    def __init__(self):
        self.device_id = generate_device_id()
        self.user_agent = USER_AGENT_TEMPLATE % self.device_id
        self.jar = aiohttp.CookieJar(unsafe=True)
        self._session = aiohttp.ClientSession(cookie_jar=self.jar)
        self.poll_url = ""
        self.poll_count = 0
        self.created_at = time.time()

    async def get_qrcode(self) -> dict[str, str] | None:
        try:
            url1 = f"{ACCOUNT_BASE_URL}/pass/serviceLogin?sid={QR_LOGIN_SID}&_json=true"
            headers1 = {
                "User-Agent": self.user_agent,
                "Cookie": f"sdkVersion={SDK_VERSION}; deviceId={self.device_id}",
            }
            async with self._session.get(url1, headers=headers1) as resp1:
                data1 = json.loads(strip_json_prefix(await resp1.text()))

            sign = str(data1.get("_sign") or "")
            qs = str(data1.get("qs") or "")
            callback = str(data1.get("callback") or "")
            if not (sign and qs and callback):
                return None

            params = "&".join(
                [
                    "_qrsize=240",
                    f"qs={quote(qs)}",
                    f"sid={QR_LOGIN_SID}",
                    f"_sign={quote(sign)}",
                    f"callback={quote(callback)}",
                    "_json=true",
                    f"_dc={int(time.time() * 1000)}",
                ]
            )
            url2 = f"{LONG_POLLING_URL}?{params}"
            headers2 = {
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            }
            async with self._session.get(url2, headers=headers2) as resp2:
                data2 = json.loads(strip_json_prefix(await resp2.text()))

            if int(data2.get("code") or 0) != 0:
                return None

            qr = str(data2.get("qr") or "")
            login_url = str(data2.get("loginUrl") or "")
            lp = str(data2.get("lp") or "")
            if not lp:
                return None

            self.poll_url = lp
            self.poll_count = 0
            return {"qrcode_url": qr or login_url, "login_url": login_url}
        except Exception as e:
            log.warning(f"[QRLogin] 获取二维码失败: {e}")
            return None

    async def poll(self) -> dict[str, Any]:
        if not self.poll_url:
            return {"state": "failed", "message": "尚未获取二维码"}
        self.poll_count += 1
        if self.poll_count > 20:
            return {"state": "expired", "message": "二维码已过期"}

        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with self._session.get(self.poll_url, headers=headers, timeout=timeout) as resp:
                if resp.status == 403:
                    return {"state": "expired", "message": "二维码已过期"}
                if resp.status >= 400:
                    return {"state": "failed", "message": f"轮询失败: HTTP {resp.status}"}
                body = strip_json_prefix(await resp.text())
        except asyncio.TimeoutError:
            return {"state": "waiting", "message": "等待米家 App 扫码确认..."}
        except Exception as e:
            return {"state": "failed", "message": f"轮询异常: {e}"}

        try:
            data = json.loads(body)
        except Exception:
            return {"state": "waiting", "message": "等待米家 App 扫码确认..."}

        if int(data.get("code") or 0) != 0:
            return {"state": "expired", "message": f"二维码失效: {data.get('desc')}"}

        pass_token = str(data.get("passToken") or "")
        user_id = str(data.get("userId") or "")
        c_user_id = str(data.get("cUserId") or "")
        if not (pass_token and user_id):
            return {"state": "waiting", "message": "已扫码，请在手机上点击确认"}

        # 立即换取 micoapi serviceToken
        auth_sess = XiaomiAuthSession(device_id=self.device_id)
        try:
            exchanged = await auth_sess.exchange_pass_token(
                pass_token=pass_token,
                user_id=user_id,
                c_user_id=c_user_id,
                sid=MINA_SID,
            )
        finally:
            await auth_sess.close()

        return {
            "state": "confirmed",
            "message": "扫码登录成功",
            "user_id": user_id,
            "pass_token": pass_token,
            "c_user_id": c_user_id,
            "device_id": self.device_id,
            "service_token": exchanged.get("service_token", ""),
            "ssecurity": exchanged.get("ssecurity", ""),
        }

    def is_expired(self) -> bool:
        return time.time() - self.created_at > 300

    async def close(self) -> None:
        if not self._session.closed:
            await self._session.close()


class XiaomiAuthManager:
    """统一管理小米账号凭证、扫码会话、密码会话与 401 自动续期"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._last_relogin_at: float = 0.0
        self._qr_sessions: dict[str, QRCodeSession] = {}
        self._pwd_session: XiaomiAuthSession | None = None

    def is_authenticated(self) -> bool:
        return bool(self.config.mi_user_id and self.config.mi_service_token)

    def build_mina_cookies(self, extra_device_id: str = "") -> dict[str, str]:
        cookies = {
            "userId": str(self.config.mi_user_id),
            "serviceToken": str(self.config.mi_service_token),
            "channel": "MI_APP_STORE",
        }
        if extra_device_id:
            cookies["deviceId"] = extra_device_id
        return cookies

    def user_agent(self) -> str:
        dev_id = self.config.mi_device_id or generate_device_id()
        return USER_AGENT_TEMPLATE % dev_id

    async def start_qr_login(self) -> dict[str, Any]:
        # 清理过期会话
        for sid, qr in list(self._qr_sessions.items()):
            if qr.is_expired():
                self._qr_sessions.pop(sid, None)
                await qr.close()

        qr = QRCodeSession()
        info = await qr.get_qrcode()
        if not info:
            await qr.close()
            raise RuntimeError("向小米服务器申请登录二维码失败")
        session_id = secrets.token_urlsafe(12)
        self._qr_sessions[session_id] = qr
        return {"session_id": session_id, **info}

    async def poll_qr_login(self, session_id: str) -> dict[str, Any]:
        qr = self._qr_sessions.get(session_id)
        if not qr:
            return {"state": "failed", "message": "扫码会话不存在或已结束，请重新获取二维码"}
        result = await qr.poll()
        if result["state"] in ("confirmed", "expired", "failed"):
            self._qr_sessions.pop(session_id, None)
            await qr.close()
        if result["state"] == "confirmed":
            self._apply_tokens(result)
        return result

    async def login_password(self, account: str, password: str, captcha: str = "") -> dict[str, Any]:
        async with self._lock:
            if self._pwd_session is None or not captcha:
                if self._pwd_session:
                    await self._pwd_session.close()
                self._pwd_session = XiaomiAuthSession(self.config.mi_device_id)
            res = await self._pwd_session.login_with_password(account, password, captcha)
            if res.get("state") == "success":
                self.config.mi_account = account.strip()
                self.config.mi_password = password
                self._apply_tokens(res)
                await self._pwd_session.close()
                self._pwd_session = None
            return res

    async def login_cookie(self, cookie_str: str) -> dict[str, Any]:
        parsed = parse_cookie_string(cookie_str)
        user_id = parsed.get("userId", "").strip()
        pass_token = parsed.get("passToken", "").strip()
        service_token = parsed.get("serviceToken", "").strip()
        c_user_id = parsed.get("cUserId", "").strip()

        if not user_id:
            return {"state": "failed", "error": "Cookie 中缺少 userId=..."}

        if pass_token:
            sess = XiaomiAuthSession(self.config.mi_device_id)
            try:
                res = await sess.exchange_pass_token(
                    pass_token=pass_token,
                    user_id=user_id,
                    c_user_id=c_user_id,
                    sid=MINA_SID,
                )
            finally:
                await sess.close()
            if res.get("state") == "success":
                res["pass_token"] = pass_token
                res["c_user_id"] = c_user_id
                self._apply_tokens(res)
                return res
            if not service_token:
                return res

        if service_token:
            self._apply_tokens(
                {
                    "user_id": user_id,
                    "pass_token": pass_token,
                    "c_user_id": c_user_id,
                    "device_id": parsed.get("deviceId") or self.config.mi_device_id or generate_device_id(),
                    "service_token": service_token,
                    "ssecurity": parsed.get("ssecurity", ""),
                }
            )
            return {"state": "success", "user_id": user_id}

        return {"state": "failed", "error": "Cookie 中需包含 passToken 或 serviceToken"}

    def _apply_tokens(self, data: dict[str, Any]) -> None:
        if data.get("user_id"):
            self.config.mi_user_id = str(data["user_id"])
        if data.get("pass_token"):
            self.config.mi_pass_token = str(data["pass_token"])
        if data.get("c_user_id"):
            self.config.mi_c_user_id = str(data["c_user_id"])
        if data.get("device_id"):
            self.config.mi_device_id = str(data["device_id"])
        if data.get("service_token"):
            self.config.mi_service_token = str(data["service_token"])
        if data.get("ssecurity"):
            self.config.mi_ssecurity = str(data["ssecurity"])
        self.config.mi_token_updated_at = time.time()
        self.config.save()
        log.info(f"[XiaomiAuth] 凭证已更新并持久化 (userId={self.config.mi_user_id})")

    async def refresh_credentials(self, reason: str = "auto") -> bool:
        """三级凭证自愈链（passToken 换发 → 账号密码重登），带 60s 冷却防小米 70016 风控"""
        async with self._lock:
            now = time.time()
            if now - self._last_relogin_at < RELOGIN_COOLDOWN_SEC and self.is_authenticated():
                log.info(f"[XiaomiAuth] 距上次凭证刷新不足 {RELOGIN_COOLDOWN_SEC}s，跳过重复换发 ({reason})")
                return True

            self._last_relogin_at = now
            log.info(f"[XiaomiAuth] 触发凭证自愈刷新 (reason={reason})")

            # 1. 优先使用 passToken 换取新的 serviceToken
            if self.config.mi_user_id and self.config.mi_pass_token:
                sess = XiaomiAuthSession(self.config.mi_device_id)
                try:
                    res = await sess.exchange_pass_token(
                        pass_token=self.config.mi_pass_token,
                        user_id=self.config.mi_user_id,
                        c_user_id=self.config.mi_c_user_id,
                        sid=MINA_SID,
                    )
                    if res.get("state") == "success":
                        self._apply_tokens(res)
                        return True
                    log.warning(f"[XiaomiAuth] passToken 换发失败: {res.get('error')}")
                finally:
                    await sess.close()

            # 2. 退回账号密码重登
            if self.config.mi_account and self.config.mi_password:
                sess = XiaomiAuthSession(self.config.mi_device_id)
                try:
                    res = await sess.login_with_password(
                        self.config.mi_account,
                        self.config.mi_password,
                    )
                    if res.get("state") == "success":
                        self._apply_tokens(res)
                        return True
                    log.warning(f"[XiaomiAuth] 账号密码自动重登失败: {res.get('error')}")
                finally:
                    await sess.close()

            return False

    async def close(self) -> None:
        for qr in self._qr_sessions.values():
            await qr.close()
        self._qr_sessions.clear()
        if self._pwd_session:
            await self._pwd_session.close()
            self._pwd_session = None

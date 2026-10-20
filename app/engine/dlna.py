"""轻量级 UPnP / DLNA MediaRenderer 投屏接收器
（精简整合自 miair-next/backend/app/engine/dlna 与 songloft-plugin-miot/src/receiver）

复用主 HTTP 端口提供 /dlna/* 描述与 SOAP 控制端点，无需额外占用独立 TCP 端口；
通过 UDP 1900 多播响应局域网手机/电脑 App（如 YouTube Music / 网易云 / QQ音乐 / BubbleUPnP）的投屏发现。
"""

from __future__ import annotations

import asyncio
import html
import logging
import socket
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any

from aiohttp import web

from app.config import AppConfig
from app.engine.player import PlayerManager
from app.xiaomi.client import XiaomiClient

log = logging.getLogger("xiaoai-ytmusic.dlna")

SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900

DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
SERVICE_TYPES = {
    "AVTransport": "urn:schemas-upnp-org:service:AVTransport:1",
    "RenderingControl": "urn:schemas-upnp-org:service:RenderingControl:1",
    "ConnectionManager": "urn:schemas-upnp-org:service:ConnectionManager:1",
}
PROTOCOL_INFO = (
    "http-get:*:audio/mpeg:*,"
    "http-get:*:audio/mp3:*,"
    "http-get:*:audio/mp4:*,"
    "http-get:*:audio/x-m4a:*,"
    "http-get:*:audio/aac:*,"
    "http-get:*:audio/flac:*,"
    "http-get:*:audio/wav:*,"
    "http-get:*:audio/ogg:*"
)


def _format_clock(seconds: float | int) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}"


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


class _SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, receiver: "DLNAReceiver"):
        self.receiver = receiver
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            return
        if not text.startswith("M-SEARCH * HTTP/1.1"):
            return
        headers: dict[str, str] = {}
        for line in text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if headers.get("man") != '"ssdp:discover"':
            return
        st = headers.get("st", "")
        self.receiver.on_msearch(st, addr)


class DLNAReceiver:
    """DLNA MediaRenderer 协议栈（SSDP + UPnP AVTransport / RenderingControl）"""

    def __init__(self, config: AppConfig, xiaomi: XiaomiClient, players: PlayerManager):
        self.config = config
        self.xiaomi = xiaomi
        self.players = players

        if not self.config.dlna_uuid or not self.config.dlna_uuid.startswith("uuid:"):
            self.config.dlna_uuid = f"uuid:{uuid.uuid4()}"
            self.config.save()

        self._transport: asyncio.DatagramTransport | None = None
        self._announce_task: asyncio.Task | None = None
        self._running = False

        # DLNA 传输状态
        self.transport_state = "NO_MEDIA_PRESENT"
        self.current_uri = ""
        self.current_metadata = ""
        self.current_title = ""
        self.duration = 0
        self.play_started_at = 0.0
        self.volume = 50

    @property
    def uuid(self) -> str:
        return self.config.dlna_uuid

    def target_device_id(self) -> str:
        if self.config.default_device_id:
            return self.config.default_device_id
        if self.config.enabled_device_ids:
            return self.config.enabled_device_ids[0]
        return ""

    def location_url(self) -> str:
        return f"{self.config.base_url()}/dlna/device.xml"

    def _advertised_types(self) -> list[str]:
        return ["upnp:rootdevice", self.uuid, DEVICE_TYPE, *SERVICE_TYPES.values()]

    def _usn(self, nt: str) -> str:
        return self.uuid if nt == self.uuid else f"{self.uuid}::{nt}"

    async def start(self) -> None:
        if self._running or not self.config.dlna_enabled:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("0.0.0.0", SSDP_PORT))
            mreq = struct.pack("4s4s", socket.inet_aton(SSDP_GROUP), socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setblocking(False)

            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _SSDPProtocol(self),
                sock=sock,
            )
            self._transport = transport
            self._running = True
            self._send_notify("ssdp:alive")
            self._announce_task = asyncio.create_task(self._periodic_announce())
            log.info(f"[DLNA] 渲染器已启动: {self.config.dlna_friendly_name} ({self.location_url()})")
        except Exception as e:
            log.warning(f"[DLNA] 启动 SSDP UDP 1900 失败（若非 host 网络模式可忽略）: {e}")

    async def stop(self) -> None:
        self._running = False
        if self._announce_task and not self._announce_task.done():
            self._announce_task.cancel()
            try:
                await self._announce_task
            except asyncio.CancelledError:
                pass
        self._announce_task = None
        if self._transport:
            try:
                self._send_notify("ssdp:byebye")
            except Exception:
                pass
            self._transport.close()
            self._transport = None

    async def _periodic_announce(self) -> None:
        while self._running:
            await asyncio.sleep(600)
            if self._running:
                self._send_notify("ssdp:alive")

    def _send_notify(self, nts: str) -> None:
        if not self._transport:
            return
        loc = self.location_url()
        for nt in self._advertised_types():
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
                f"NT: {nt}\r\n"
                f"NTS: {nts}\r\n"
                f"USN: {self._usn(nt)}\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                f"LOCATION: {loc}\r\n"
                "SERVER: Linux/5.0 UPnP/1.0 XiaoAi-YTMusic/1.0\r\n\r\n"
            )
            try:
                self._transport.sendto(msg.encode("utf-8"), (SSDP_GROUP, SSDP_PORT))
            except Exception:
                pass

    def on_msearch(self, st: str, addr: tuple[str, int]) -> None:
        if not self._transport or not self._running:
            return
        targets = [t for t in self._advertised_types() if st == "ssdp:all" or st == t]
        if not targets:
            return
        loc = self.location_url()
        for t in targets:
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                "EXT:\r\n"
                f"LOCATION: {loc}\r\n"
                "SERVER: Linux/5.0 UPnP/1.0 XiaoAi-YTMusic/1.0\r\n"
                f"ST: {t}\r\n"
                f"USN: {self._usn(t)}\r\n\r\n"
            )
            try:
                self._transport.sendto(resp.encode("utf-8"), addr)
            except Exception:
                pass

    # ---- HTTP / SOAP 路由处理 ----

    async def handle_device_xml(self, request: web.Request) -> web.Response:
        services_xml = "".join(
            f"<service>"
            f"<serviceType>{stype}</serviceType>"
            f"<serviceId>urn:upnp-org:serviceId:{name}</serviceId>"
            f"<SCPDURL>/dlna/{name}.xml</SCPDURL>"
            f"<controlURL>/dlna/{name}/control</controlURL>"
            f"<eventSubURL>/dlna/{name}/event</eventSubURL>"
            f"</service>"
            for name, stype in SERVICE_TYPES.items()
        )
        xml_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<root xmlns="urn:schemas-upnp-org:device-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            "<device>"
            f"<deviceType>{DEVICE_TYPE}</deviceType>"
            f"<friendlyName>{html.escape(self.config.dlna_friendly_name)}</friendlyName>"
            "<manufacturer>Xiaomi / XiaoAi-YTMusic</manufacturer>"
            "<modelName>XiaoAi DLNA &amp; YouTube Music Renderer</modelName>"
            f"<UDN>{html.escape(self.uuid)}</UDN>"
            f"<serviceList>{services_xml}</serviceList>"
            "</device></root>"
        )
        return web.Response(text=xml_body, content_type="text/xml", charset="utf-8")

    async def handle_scpd_xml(self, request: web.Request) -> web.Response:
        xml_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            "<actionList></actionList><serviceStateTable></serviceStateTable>"
            "</scpd>"
        )
        return web.Response(text=xml_body, content_type="text/xml", charset="utf-8")

    async def handle_event_sub(self, request: web.Request) -> web.Response:
        sid = request.headers.get("SID") or f"uuid:{uuid.uuid4()}"
        return web.Response(status=200, headers={"SID": sid, "TIMEOUT": "Second-1800"})

    async def handle_control(self, request: web.Request) -> web.Response:
        service = request.match_info.get("service", "AVTransport")
        soap_action = (request.headers.get("SOAPAction") or "").strip('"')
        action = soap_action.split("#", 1)[1] if "#" in soap_action else ""
        body_text = await request.text()

        args: dict[str, str] = {}
        try:
            root = ET.fromstring(body_text)
            for elem in root.iter():
                if _local_name(elem.tag) == action and action:
                    for child in elem:
                        args[_local_name(child.tag)] = (child.text or "").strip()
                    break
        except Exception:
            pass

        result_fields = await self._dispatch_soap_action(service, action, args)
        stype = SERVICE_TYPES.get(service, SERVICE_TYPES["AVTransport"])
        fields_xml = "".join(
            f"<{k}>{html.escape(str(v))}</{k}>" for k, v in result_fields.items()
        )
        resp_xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action}Response xmlns:u="{stype}">'
            f"{fields_xml}"
            f"</u:{action}Response></s:Body></s:Envelope>"
        )
        return web.Response(text=resp_xml, content_type="text/xml", charset="utf-8")

    async def _dispatch_soap_action(
        self,
        service: str,
        action: str,
        args: dict[str, str],
    ) -> dict[str, Any]:
        dev_id = self.target_device_id()

        if service == "ConnectionManager":
            if action == "GetProtocolInfo":
                return {"Source": "", "Sink": PROTOCOL_INFO}
            if action == "GetCurrentConnectionIDs":
                return {"ConnectionIDs": "0"}
            return {
                "RcsID": 0,
                "AVTransportID": 0,
                "ProtocolInfo": PROTOCOL_INFO,
                "PeerConnectionManager": "",
                "PeerConnectionID": -1,
                "Direction": "Input",
                "Status": "OK",
            }

        if service == "RenderingControl":
            if action == "GetVolume":
                if dev_id:
                    st = await self.xiaomi.get_player_status(dev_id)
                    if st.get("volume", -1) >= 0:
                        self.volume = st["volume"]
                return {"CurrentVolume": self.volume}
            if action == "SetVolume":
                vol = max(0, min(100, int(args.get("DesiredVolume") or 50)))
                self.volume = vol
                if dev_id:
                    await self.xiaomi.set_volume(dev_id, vol)
                return {}
            return {}

        # AVTransport
        if action == "SetAVTransportURI":
            self.current_uri = args.get("CurrentURI", "").strip()
            self.current_metadata = args.get("CurrentURIMetaData", "")
            self.transport_state = "STOPPED" if self.current_uri else "NO_MEDIA_PRESENT"
            if self.current_uri and dev_id:
                await self.xiaomi.play_url(dev_id, self.current_uri)
                self.transport_state = "PLAYING"
                self.play_started_at = time.time()
            return {}

        if action == "Play":
            if self.current_uri and dev_id:
                await self.xiaomi.play_url(dev_id, self.current_uri)
                self.transport_state = "PLAYING"
                self.play_started_at = time.time()
            return {}

        if action == "Pause":
            if dev_id:
                await self.xiaomi.pause(dev_id)
            self.transport_state = "PAUSED_PLAYBACK"
            return {}

        if action == "Stop":
            if dev_id:
                await self.xiaomi.stop(dev_id)
            self.transport_state = "STOPPED"
            return {}

        if action == "GetTransportInfo":
            return {
                "CurrentTransportState": self.transport_state,
                "CurrentTransportStatus": "OK",
                "CurrentSpeed": "1",
            }

        if action == "GetPositionInfo":
            pos = int(time.time() - self.play_started_at) if (self.transport_state == "PLAYING" and self.play_started_at > 0) else 0
            return {
                "Track": 1 if self.current_uri else 0,
                "TrackDuration": _format_clock(self.duration or 240),
                "TrackMetaData": self.current_metadata,
                "TrackURI": self.current_uri,
                "RelTime": _format_clock(pos),
                "AbsTime": _format_clock(pos),
                "RelCount": 2147483647,
                "AbsCount": 2147483647,
            }

        if action == "GetMediaInfo":
            return {
                "NrTracks": 1 if self.current_uri else 0,
                "MediaDuration": _format_clock(self.duration or 240),
                "CurrentURI": self.current_uri,
                "CurrentURIMetaData": self.current_metadata,
                "NextURI": "",
                "NextURIMetaData": "",
                "PlayMedium": "NETWORK" if self.current_uri else "NONE",
                "RecordMedium": "NOT_IMPLEMENTED",
                "WriteStatus": "NOT_IMPLEMENTED",
            }

        return {}

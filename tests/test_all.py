"""全模块单元与集成测试套件"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from app.config import AppConfig
from app.engine.player import PlayerManager
from app.engine.voice import (
    ConversationMonitor,
    VoiceCommandEngine,
    clean_song_argument,
    fuzzy_subseq_match,
)
from app.web.server import ApplicationServer
from app.xiaomi.auth import (
    XiaomiAuthManager,
    compute_client_sign,
    extract_bigint_field,
    parse_cookie_string,
    strip_json_prefix,
)
from app.xiaomi.client import XiaomiClient
from app.xiaomi.const import need_use_play_music_api, should_use_mina_for_ask
from app.ytmusic.client import Track, cookie_header_from_netscape, parse_library_playlists, parse_search_response
from app.ytmusic.cookies import (
    build_ytmusic_auth_headers,
    inspect_cookie_file,
    parse_any_cookies,
    save_cookies_any_format,
    snapshot_for_ytdlp,
)
from app.ytmusic.downloader import AudioDownloader, validate_video_id
from app.ytmusic.matching import build_display_name, match_score, normalize, tokenize
from app.ytmusic.playlists import PlaylistStore, extract_playlist_id


class TestMatchingAndYTMusic(unittest.TestCase):
    def test_normalize_and_tokenize(self) -> None:
        self.assertEqual(normalize("　Ｌｅｍｏｎ（Ｏｆｆｉｃｉａｌ）"), "lemon official")
        tokens = tokenize(normalize("周杰伦 晴天 Jay Chou"))
        self.assertEqual(tokens, ["周", "杰", "伦", "晴", "天", "jay", "chou"])

    def test_build_display_name_and_match_score(self) -> None:
        disp = build_display_name("晴天", ["周杰伦"])
        self.assertEqual(disp, "晴天 - 周杰伦")
        self.assertGreater(match_score("周杰伦 晴天", disp), 0.85)
        self.assertGreater(match_score("晴天", disp), 0.75)
        self.assertLess(match_score("完全无关的乱码xyz", disp), 0.2)

    def test_parse_innertube_search_response(self) -> None:
        sample = {
            "contents": {
                "tabbedSearchResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "sectionListRenderer": {
                                        "contents": [
                                            {
                                                "musicShelfRenderer": {
                                                    "contents": [
                                                        {
                                                            "musicResponsiveListItemRenderer": {
                                                                "playlistItemData": {"videoId": "dQw4w9WgXcQ"},
                                                                "flexColumns": [
                                                                    {
                                                                        "musicResponsiveListItemFlexColumnRenderer": {
                                                                            "text": {"runs": [{"text": "晴天"}]}
                                                                        }
                                                                    },
                                                                    {
                                                                        "musicResponsiveListItemFlexColumnRenderer": {
                                                                            "text": {
                                                                                "runs": [
                                                                                    {"text": "Song"},
                                                                                    {"text": " • "},
                                                                                    {"text": "周杰伦"},
                                                                                    {"text": " • "},
                                                                                    {
                                                                                        "text": "叶惠美",
                                                                                        "navigationEndpoint": {
                                                                                            "browseEndpoint": {
                                                                                                "browseId": "MPREb_123"
                                                                                            }
                                                                                        },
                                                                                    },
                                                                                    {"text": " • "},
                                                                                    {"text": "4:29"},
                                                                                ]
                                                                            }
                                                                        }
                                                                    },
                                                                ],
                                                                "thumbnail": {
                                                                    "musicThumbnailRenderer": {
                                                                        "thumbnail": {
                                                                            "thumbnails": [
                                                                                {"url": "https://img/1.jpg", "width": 120, "height": 120}
                                                                            ]
                                                                        }
                                                                    }
                                                                },
                                                            }
                                                        }
                                                    ]
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
        tracks = parse_search_response(sample)
        self.assertEqual(len(tracks), 1)
        t = tracks[0]
        self.assertEqual(t.video_id, "dQw4w9WgXcQ")
        self.assertEqual(t.title, "晴天")
        self.assertEqual(t.artists, ["周杰伦"])
        self.assertEqual(t.album, "叶惠美")
        self.assertEqual(t.duration, "4:29")
        self.assertEqual(t.duration_seconds, 269)
        self.assertEqual(t.display_name, "晴天 - 周杰伦")

    def test_cookie_header_from_netscape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cookies.txt"
            p.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t1890000000\tSID\tabc123\n"
                "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1890000000\tSAPISID\txyz789\n"
                ".youtube.com\tTRUE\t/\tTRUE\t1890000000\tLOGIN_INFO\tlogin_ok\n",
                encoding="utf-8",
            )
            hdr = cookie_header_from_netscape(p)
            self.assertIn("SID=abc123", hdr)
            self.assertIn("SAPISID=xyz789", hdr)

            auth_hdrs = build_ytmusic_auth_headers(p)
            self.assertIn("Authorization", auth_hdrs)
            self.assertTrue(auth_hdrs["Authorization"].startswith("SAPISIDHASH "))

            # 测试 cURL 格式自动解析与快照防擦除保护
            curl_text = (
                "curl 'https://music.youtube.com/youtubei/v1/browse' "
                "-H 'cookie: SID=sid_val; HSID=hsid_val; SAPISID=sapi_val; LOGIN_INFO=li_val'"
            )
            status = save_cookies_any_format(p, curl_text)
            self.assertTrue(status.logged_in)
            self.assertTrue(status.has_sapisid)
            self.assertEqual(status.source_format, "curl_or_headers")

            with snapshot_for_ytdlp(p) as snap_path:
                self.assertIsNotNone(snap_path)
                assert snap_path is not None
                Path(snap_path).write_text("corrupted by ytdlp", encoding="utf-8")
            # 原文件必须完好无损
            after_status = inspect_cookie_file(p)
            self.assertTrue(after_status.logged_in)


class TestXiaomiAndVoice(unittest.TestCase):
    def test_xiaomi_crypto_and_bigint_nonce(self) -> None:
        raw = '&&&START&&&{"code":0,"nonce":1610098522385872896,"ssecurity":"sec=="}'
        clean = strip_json_prefix(raw)
        nonce = extract_bigint_field(clean, "nonce")
        self.assertEqual(nonce, "1610098522385872896")
        sign = compute_client_sign(nonce, "sec==")
        self.assertTrue(len(sign) > 10)

        cookies = parse_cookie_string("userId=10086; passToken=pt_abc; serviceToken=st_xyz")
        self.assertEqual(cookies["userId"], "10086")
        self.assertEqual(cookies["passToken"], "pt_abc")

        self.assertTrue(need_use_play_music_api("LX06"))
        self.assertFalse(need_use_play_music_api("LX06", ["LX06"]))
        self.assertTrue(should_use_mina_for_ask("M01"))

    def test_voice_command_matching_exact_and_fuzzy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig.load(tmp)
            auth = XiaomiAuthManager(cfg)
            xiaomi = XiaomiClient(cfg, auth)
            yt = AsyncMock()
            dl = MagicMockDownloader(cfg, yt)
            players = PlayerManager(cfg, xiaomi, yt, dl)
            engine = VoiceCommandEngine(cfg, xiaomi, players)

            # 1. 精确规则匹配
            m1 = engine.match_command("播放歌曲周杰伦的晴天")
            self.assertIsNotNone(m1)
            assert m1 is not None
            self.assertEqual(m1.command_type, "play_song")
            self.assertEqual(m1.keyword, "播放歌曲")
            self.assertEqual(m1.argument, "周杰伦的晴天")

            # 2. 歌手匹配优于普通播放
            m2 = engine.match_command("播放歌手陈奕迅的歌")
            self.assertIsNotNone(m2)
            assert m2 is not None
            self.assertEqual(m2.command_type, "play_artist")
            self.assertEqual(m2.argument, "陈奕迅")

            # 3. 跳字模糊子序列匹配 ("我今天想听" 命中 "我想听")
            m3 = engine.match_command("我今天想听七里香吧。")
            self.assertIsNotNone(m3)
            assert m3 is not None
            self.assertEqual(m3.command_type, "play_song")
            self.assertEqual(m3.source, "fuzzy")
            self.assertEqual(m3.argument, "七里香")

            # 4. 歌名里包含控制词（如“播放别怕我伤心”或“播放歌曲继续”）不应被误截断为控制命令
            m4 = engine.match_command("播放歌曲继续宠爱")
            self.assertIsNotNone(m4)
            assert m4 is not None
            self.assertEqual(m4.command_type, "play_song")
            self.assertEqual(m4.argument, "继续宠爱")

            # 5. 控制命令
            m5 = engine.match_command("下一首")
            self.assertIsNotNone(m5)
            assert m5 is not None
            self.assertEqual(m5.command_type, "next")

            # 6. 播放歌单命令
            m6 = engine.match_command("播放歌单我喜欢的音乐")
            self.assertIsNotNone(m6)
            assert m6 is not None
            self.assertEqual(m6.command_type, "play_playlist")
            self.assertEqual(m6.argument, "我喜欢的音乐")

            # 7. PlaylistStore 保存与模糊查找
            pl_store = PlaylistStore(cfg, yt)
            pl_store.save_custom_playlist(
                "我喜欢的音乐 (Liked Music)",
                [Track(video_id="lT6PsoqdTsg", title="晴天", artists=["周杰伦"], display_name="晴天 - 周杰伦")],
                playlist_id="LM",
                source="ytmusic",
            )
            found_pl = pl_store.find_by_name("我喜欢的音乐")
            self.assertIsNotNone(found_pl)
            assert found_pl is not None
            self.assertEqual(found_pl.playlist_id, "LM")
            self.assertEqual(extract_playlist_id("https://music.youtube.com/playlist?list=PL123456"), "PL123456")


class MagicMockDownloader(AudioDownloader):
    def __init__(self, cfg: AppConfig, yt: Any):
        super().__init__(cfg, yt)


class TestAsyncIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_monitor_priming_and_interception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig.load(tmp)
            cfg.mi_user_id = "12345"
            cfg.mi_service_token = "tok_abc"
            cfg.enabled_device_ids = ["dev_1"]

            auth = XiaomiAuthManager(cfg)
            xiaomi = XiaomiClient(cfg, auth)
            xiaomi._devices_cache = [
                {"deviceID": "dev_1", "name": "客厅小爱", "hardware": "LX06", "enabled": True}
            ]

            yt = AsyncMock()
            yt.search = AsyncMock(
                return_value=[
                    Track(
                        video_id="dQw4w9WgXcQ",
                        title="晴天",
                        artists=["周杰伦"],
                        duration="4:29",
                        duration_seconds=269,
                        display_name="晴天 - 周杰伦",
                        match_score=0.98,
                    )
                ]
            )

            dl = AudioDownloader(cfg, yt)
            fake_audio = cfg.cache_dir / "dQw4w9WgXcQ.m4a"
            fake_audio.write_bytes(b"FAKE_M4A_AUDIO_DATA_1234567890")
            dl._scan_existing_cache()

            xiaomi.stop = AsyncMock(return_value=True)
            xiaomi.play_url = AsyncMock(return_value=True)

            players = PlayerManager(cfg, xiaomi, yt, dl)
            engine = VoiceCommandEngine(cfg, xiaomi, players)
            monitor = ConversationMonitor(cfg, xiaomi, engine, players)

            await monitor._sync_devices()
            state = monitor._states["dev_1"]

            # 第 1 轮轮询：仅建立服务端时间戳基线 (ts=1000)，绝不触发播放
            xiaomi.get_latest_ask = AsyncMock(
                return_value=[{"timestamp_ms": 1000, "query": "播放旧历史歌曲", "answer": "好的"}]
            )
            await monitor._poll_device(state)
            self.assertTrue(state.primed)
            self.assertEqual(state.last_timestamp_ms, 1000)
            xiaomi.play_url.assert_not_called()

            # 第 2 轮轮询：新语音指令到达 (ts=2000)，立即拦截并调用 YouTube Music 播放
            xiaomi.get_latest_ask = AsyncMock(
                return_value=[
                    {"timestamp_ms": 1000, "query": "播放旧历史歌曲", "answer": "好的"},
                    {"timestamp_ms": 2000, "query": "播放周杰伦的晴天", "answer": "需要开通VIP"},
                ]
            )
            await monitor._poll_device(state)
            self.assertEqual(state.last_timestamp_ms, 2000)
            xiaomi.stop.assert_called_with("dev_1")
            xiaomi.play_url.assert_called_once()
            call_kwargs = xiaomi.play_url.call_args.kwargs
            self.assertIn("/stream/dQw4w9WgXcQ.m4a", call_kwargs["audio_url"])

            # 清理后台定时器
            await players.stop_all()

    async def test_web_server_endpoints_and_range_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig.load(tmp)
            cfg.dlna_enabled = False
            cfg.voice_enabled = False

            srv = ApplicationServer(cfg)
            srv.yt_client.search = AsyncMock(
                return_value=[
                    Track(
                        video_id="dQw4w9WgXcQ",
                        title="晴天",
                        artists=["周杰伦"],
                        duration="4:29",
                        duration_seconds=269,
                        display_name="晴天 - 周杰伦",
                        match_score=0.99,
                        index=1,
                    )
                ]
            )

            # 预置缓存音频文件以测试 /download、/stream 与 Range 请求
            audio_bytes = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 100
            fake_file = cfg.cache_dir / "dQw4w9WgXcQ.m4a"
            fake_file.write_bytes(audio_bytes)
            srv.downloader._scan_existing_cache()

            app = srv.create_web_app()
            async with TestClient(TestServer(app)) as client:
                # 1. /healthz
                r_health = await client.get("/healthz")
                self.assertEqual(r_health.status, 200)
                h_json = await r_health.json()
                self.assertEqual(h_json["status"], "ok")

                # 2. /search
                r_search = await client.post("/search", json={"query": "晴天", "limit": 5})
                self.assertEqual(r_search.status, 200)
                s_json = await r_search.json()
                self.assertEqual(s_json["total"], 1)
                sid = s_json["session_id"]

                # 3. /download?mode=json
                r_dl = await client.post("/download?mode=json", json={"session_id": sid, "index": 1})
                self.assertEqual(r_dl.status, 200)
                dl_json = await r_dl.json()
                self.assertEqual(dl_json["video_id"], "dQw4w9WgXcQ")

                # 4. /stream/dQw4w9WgXcQ.m4a 支持 HTTP Range (206 Partial Content)
                r_range = await client.get(
                    "/stream/dQw4w9WgXcQ.m4a",
                    headers={"Range": "bytes=0-15"},
                )
                self.assertEqual(r_range.status, 206)
                chunk = await r_range.read()
                self.assertEqual(chunk, audio_bytes[:16])

                # 5. /api/voice/test
                r_vtest = await client.post(
                    "/api/voice/test",
                    json={"query": "我今天想听周杰伦的晴天", "execute": False},
                )
                self.assertEqual(r_vtest.status, 200)
                vt_json = await r_vtest.json()
                self.assertTrue(vt_json["matched"])
                self.assertEqual(vt_json["argument"], "周杰伦的晴天")

                # 6. /dlna/device.xml
                r_dlna = await client.get("/dlna/device.xml")
                self.assertEqual(r_dlna.status, 200)
                xml_text = await r_dlna.text()
                self.assertIn("MediaRenderer:1", xml_text)


if __name__ == "__main__":
    unittest.main()

"""使用合成 OneBot 响应验证 QQ 导出，不连接真实 QQ。"""

import base64
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from test_sender_mapping import load_export_modules


ROOT = Path(__file__).resolve().parents[1]


def message(identifier, timestamp, text="重复正文", real_seq=None, sender_id="123456"):
    return {
        "message_id": identifier, "real_seq": real_seq, "time": timestamp,
        "sender": {"user_id": sender_id, "nickname": "合成成员", "card": "群名片"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


class Response(io.BytesIO):
    status = 200


class QQExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(sys, "path", [str(ROOT), *sys.path]):
            cls.core, cls.preview = load_export_modules()
        spec = importlib.util.spec_from_file_location("_qq_export_tests", ROOT / "qq_exporter.py")
        cls.qq = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"exporter_core": cls.core}):
            spec.loader.exec_module(cls.qq)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.start = self.core.parse_time_range("2026-10-01", None)["start_ts"]
        self.pages = [[message("103", self.start + 30), message("102", self.start + 20)], []]
        self.calls = []
        self.groups = [{"group_id": 123, "group_name": "合成群"}]
        self.friends = [{"user_id": 456, "nickname": "合成好友", "remark": "好友备注"}]
        self.media = {}

    def api(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        if action == "get_version_info":
            return {"app_name": "NapCat.Onebot", "protocol_version": "v11"}
        if action == "get_login_info":
            return {"user_id": "123456", "nickname": "本人"}
        if action == "get_group_list":
            return self.groups
        if action == "get_friend_list":
            return self.friends
        if action in {"get_group_msg_history", "get_friend_msg_history"}:
            page = self.pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return page if isinstance(page, dict) else {"messages": page}
        result = self.media.get((action, str((params or {}).get("file"))))
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError("合成附件不可用")
        return result

    def export(self, keyword="合成群", **options):
        with patch.object(self.qq.NapCatClient, "call", side_effect=self.api):
            result = self.qq.export_qq_chat(keyword, self.root / "output", endpoint="http://127.0.0.1:3000", **options)
        document = json.loads(Path(result["json"]).read_text(encoding="utf-8-sig"))
        return document, result

    def test_endpoint_only_allows_loopback_http_without_credentials(self):
        for address in ("http://127.0.0.1:3000", "http://localhost:3000/", "http://[::1]:3000"):
            self.qq.NapCatClient(address)
        for address in (
            "http://example.com:3000", "http://127.0.0.2:3000", "http://localhost.example:3000",
            "https://127.0.0.1:3000", "http://user:secret@localhost:3000",
            "http://localhost:0", "http://localhost:bad", "http://localhost:3000?token=secret",
            "http://localhost:3000#secret", "file:///C:/QQ",
        ):
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.qq.NapCatClient(address)

    def test_export_files_use_group_name_or_friend_remark(self):
        for keyword, name in (("合成群", "合成群"), ("好友备注", "好友备注")):
            self.pages = [[message("1", self.start + 10)], []]
            _, result = self.export(keyword)
            for kind in ("txt", "md", "json"):
                self.assertEqual(Path(result[kind]).name, name + "." + kind)
                self.assertTrue(Path(result[kind]).is_file())

    def test_http_post_auth_and_onebot_schema(self):
        client = self.qq.NapCatClient("http://127.0.0.1:3000", "PRIVATE_TOKEN")
        payload = {"status": "ok", "retcode": 0, "data": {"user_id": 1}}
        opener = Mock()
        opener.open.return_value = Response(json.dumps(payload).encode())
        client.opener = opener
        self.assertEqual(client.call("get_login_info"), {"user_id": 1})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer PRIVATE_TOKEN")
        self.assertNotIn("PRIVATE_TOKEN", request.full_url)
        self.assertEqual(json.loads(request.data), {})
        for invalid in ([], {"status": "ok", "retcode": False, "data": {}}, {"status": "ok", "retcode": 0}, {"status": "ok", "retcode": "0", "data": {}}):
            opener.open.return_value = Response(json.dumps(invalid).encode())
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                client.call("get_login_info")
        opener.open.return_value = Response(b"not json")
        with self.assertRaisesRegex(RuntimeError, "JSON"):
            client.call("get_login_info")

    def test_token_rejects_unicode_or_control_characters_without_echo(self):
        for token in ("中文SECRET", "SECRET\n", "SECRET\r", "SECRET\t", "SECRET\x7f"):
            with self.subTest(token=token), self.assertRaises(ValueError) as error:
                self.qq.NapCatClient("http://localhost:3000", token)
            self.assertNotIn("SECRET", str(error.exception))
        self.qq.NapCatClient("http://localhost:3000", "")

    def test_http_and_api_errors_redact_token_and_reject_redirect(self):
        client = self.qq.NapCatClient("http://localhost:3000", "SECRET")
        client.opener = Mock()
        for code in (401, 403, 302, 500):
            client.opener.open.side_effect = HTTPError(client.endpoint, code, "SECRET", {}, None)
            with self.assertRaises(RuntimeError) as error:
                client.call("get_login_info")
            self.assertIn(str(code), str(error.exception))
            self.assertNotIn("SECRET", str(error.exception))
        client.opener.open.side_effect = URLError("SECRET")
        with self.assertRaises(RuntimeError) as error:
            client.call("get_login_info")
        self.assertNotIn("SECRET", str(error.exception))
        client.opener.open.side_effect = None
        client.opener.open.return_value = Response(json.dumps({"status": "failed", "retcode": 1400, "data": None, "message": "SECRET"}).encode())
        with self.assertRaises(RuntimeError) as error:
            client.call("get_login_info")
        self.assertNotIn("SECRET", str(error.exception))
        self.assertIsNone(self.qq._NoRedirect().redirect_request(None, None, 302, "", {}, "http://example.com"))
        with self.assertRaises(ValueError):
            client.call("send_group_msg")

    def test_connection_checks_only_napcat_version_and_login(self):
        with patch.object(self.qq.NapCatClient, "call", side_effect=self.api):
            login = self.qq.check_qq_connection("http://localhost:3000")
        self.assertEqual(login["user_id"], "123456")
        self.assertEqual([action for action, _ in self.calls], ["get_version_info", "get_login_info"])
        for version in ({"app_name": "LLBot", "protocol_version": "v11"}, {"app_name": "NapCat.Onebot", "protocol_version": "v12"}, []):
            with patch.object(self.qq.NapCatClient, "call", return_value=version), self.assertRaisesRegex(RuntimeError, "NapCat"):
                self.qq.check_qq_connection("http://localhost:3000")

    def test_ambiguous_group_friend_and_exact_stable_id(self):
        self.friends[0]["nickname"] = "合成群"
        with self.assertRaises(self.core.AmbiguousContactError) as error:
            self.export()
        self.assertEqual({item["chat_type"] for item in error.exception.candidates}, {"group", "private"})
        self.assertFalse((self.root / "output").exists())
        document, result = self.export(chat_id="qq:group:123")
        self.assertEqual(document["chat_username"], "qq:group:123")
        self.assertEqual(result["platform"], "qq")
        self.assertTrue(result["is_group"])

    def test_time_boundaries_cross_page_overlap_and_same_text(self):
        self.pages = [
            [message("4", self.start + 86400), message("3", self.start + 100)],
            [message("3", self.start + 100), message("2", self.start), message("1", self.start - 1)],
        ]
        document, result = self.export(start_time="2026-10-01", end_time="2026-10-01")
        self.assertEqual([item["local_id"] for item in document["messages"]], ["2", "3"])
        self.assertEqual([item["content"] for item in document["messages"]], ["重复正文", "重复正文"])
        self.assertEqual(result["filter"]["end_ts_exclusive"], self.start + 86400)
        self.assertEqual(document["history_coverage"]["stop_reason"], "start_boundary_reached")
        history_calls = [params for action, params in self.calls if "msg_history" in action]
        self.assertNotIn("message_seq", history_calls[0])
        self.assertEqual(history_calls[1]["message_seq"], "3")
        self.assertEqual(history_calls[0]["count"], 200)
        self.assertFalse(history_calls[0]["parse_mult_msg"])
        self.assertTrue(history_calls[1]["reverse_order"])
        self.assertTrue(history_calls[0]["disable_get_url"])
        self.assertFalse(document["history_coverage"]["history_complete"])

    def test_short_id_collision_preserves_distinct_real_seq(self):
        self.pages = [[message("1", self.start + 20, real_seq="900000000000000001"), message("1", self.start + 10, real_seq="900000000000000000")]]
        document, _ = self.export()
        self.assertEqual(len(document["messages"]), 2)
        self.assertEqual(document["history_coverage"]["stop_reason"], "ambiguous_short_id_cursor")
        self.assertIn("short_id_collision", document["history_coverage"]["warnings"])

    def test_same_second_uses_real_sequence_for_cursor_and_output_order(self):
        self.pages = [
            [message("300", self.start, real_seq="30"), message("100", self.start, real_seq="10"), message("200", self.start, real_seq="20")],
            [message("100", self.start, real_seq="10"), message("50", self.start, real_seq="5")],
            [],
        ]
        document, _ = self.export()
        calls = [params for action, params in self.calls if "msg_history" in action]
        self.assertEqual(calls[1]["message_seq"], "100")
        self.assertEqual(calls[2]["message_seq"], "50")
        self.assertTrue(all(params["reverse_order"] for params in calls))
        self.assertEqual([item["real_seq"] for item in document["messages"]], ["5", "10", "20", "30"])

    def test_no_progress_missing_id_unknown_time_and_page_limit(self):
        cases = (
            ([[message("1", self.start)], [message("1", self.start)]], "no_new_messages"),
            ([[message(None, self.start)]], "missing_cursor_id"),
            ([[message("1", None)]], "unknown_cursor_time"),
            ([[message("1", self.start + 1, real_seq="1")], [message("2", self.start + 2, real_seq="2"), message("1", self.start + 1, real_seq="3")]], "ambiguous_short_id_cursor"),
        )
        for pages, stop in cases:
            self.pages = pages
            with self.subTest(stop=stop):
                document, _ = self.export()
                self.assertEqual(document["history_coverage"]["stop_reason"], stop)
        self.pages = [[message("1", self.start)]]
        with patch.object(self.qq, "MAX_HISTORY_PAGES", 1):
            document, _ = self.export()
        self.assertEqual(document["history_coverage"]["stop_reason"], "page_limit")

    def test_first_history_error_raises_midway_error_preserves_coverage(self):
        self.pages = [RuntimeError("首批失败")]
        with self.assertRaisesRegex(RuntimeError, "首批失败"):
            self.export()
        self.pages = [{"invalid": []}]
        with self.assertRaisesRegex(RuntimeError, "messages"):
            self.export()
        self.pages = [[message("1", self.start)], RuntimeError("中途失败SECRET")]
        log = Mock()
        document, _ = self.export(token="SECRET", progress=log)
        self.assertEqual(document["history_coverage"]["stop_reason"], "api_error")
        self.assertNotIn("SECRET", json.dumps(document))
        self.assertNotIn("SECRET", str(log.call_args_list))

    def test_multiple_attachments_missing_reason_and_formats(self):
        image = self.root / "fixture.png"
        image.write_bytes(b"synthetic-image")
        row = message("1", self.start, "图文正文")
        row["message"] += [
            {"type": "image", "data": {"file": str(image)}},
            {"type": "image", "data": {"file": "second-image"}},
            {"type": "file", "data": {"file": "missing-file", "name": "失效附件.pdf"}},
            {"type": "video", "data": {"file": "video-id"}},
        ]
        self.pages = [[row], []]
        self.media[("get_image", "second-image")] = {"file_name": "第二张图.png", "base64": base64.b64encode(b"second-image").decode()}
        self.media[("get_file", "video-id")] = {"file_name": "合成视频.mp4", "base64": base64.b64encode(b"video").decode()}
        document, result = self.export(export_images=True, export_files=True, export_videos=True)
        attachments = document["messages"][0]["attachments"]
        self.assertEqual(len(attachments), 4)
        self.assertEqual(len(document["messages"][0]["elements"]), 5)
        self.assertFalse(attachments[2]["available"])
        self.assertIn("reason", attachments[2])
        self.assertEqual(result["media_stats"]["images_exported"], 2)
        for item in attachments:
            if item["available"]:
                self.assertFalse(Path(item["path"]).is_absolute())
                self.assertTrue((Path(result["output_dir"]) / item["path"]).is_file())
        for output in (result["txt"], result["md"]):
            content = Path(output).read_text(encoding="utf-8-sig")
            self.assertIn("QQ Chat Export for LLM", content)
            self.assertIn("图文正文", content)
            for item in attachments:
                if item["available"]:
                    expected = self.core._markdown_href(item["path"]) if str(output).endswith(".md") else item["path"]
                    self.assertIn(expected, content)

    def test_unselected_media_never_resolves_or_copies(self):
        row = message("1", self.start)
        row["message"].append({"type": "image", "data": {"file": "image-id"}})
        self.pages = [[row], []]
        document, _ = self.export()
        self.assertEqual(document["messages"][0]["attachments"], [])
        self.assertFalse(any(action in {"get_file", "get_image", "get_record"} for action, _ in self.calls))

    def test_remote_url_not_downloaded_and_cq_string_kept_explicit(self):
        row = message("1", self.start)
        row["message"].append({"type": "image", "data": {"file": "https://example.com/image.png"}})
        cq = message("2", self.start + 1)
        cq["message"] = "原始[CQ:image,file=opaque-id]"
        self.pages = [[row, cq], []]
        document, _ = self.export(export_images=True)
        self.assertIn("远端 URL", document["messages"][0]["attachments"][0]["reason"])
        self.assertEqual(document["messages"][1]["content"], cq["message"])
        self.assertIn("cq_string_unparsed", document["history_coverage"]["warnings"])
        self.assertFalse(any(action == "get_image" for action, _ in self.calls))

    def test_each_voice_retains_audio_and_own_transcript(self):
        row = message("1", self.start)
        row["message"] += [{"type": "record", "data": {"file": key}} for key in ("voice-a", "voice-b")]
        self.pages = [[row], []]
        audio = io.BytesIO()
        with wave.open(audio, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16)
        for key in ("voice-a", "voice-b"):
            self.media[("get_file", key)] = {"file_name": key + ".silk", "base64": base64.b64encode(b"silk").decode()}
            converted = self.root / (key + ".silk.wav")
            converted.write_bytes(audio.getvalue())
            self.media[("get_record", key)] = {"file": str(converted), "file_name": key + ".silk", "base64": base64.b64encode(audio.getvalue()).decode()}
        # 同时覆盖仅有 Base64、仍携带旧 SILK 文件名的转码响应。
        self.media[("get_record", "voice-b")].pop("file")
        with patch.object(self.core, "_create_local_asr_recognizer", return_value=object()), patch.object(self.core, "_transcribe_wav_local", side_effect=[("第一条识别", None), ("第二条识别", None)]):
            document, result = self.export(transcribe_voices=True)
        item = document["messages"][0]
        self.assertIsNone(item["transcript"])
        self.assertEqual([part["transcript"] for part in item["attachments"]], ["第一条识别", "第二条识别"])
        self.assertTrue(all(part["transcript_source"] == "local_asr" for part in item["attachments"]))
        self.assertTrue(all(part["path"].endswith(".wav") and part["decoded"] for part in item["attachments"]))
        self.assertTrue(all((Path(result["output_dir"]) / part["original_path"]).is_file() for part in item["attachments"]))
        for output in (result["txt"], result["md"]):
            text = Path(output).read_text(encoding="utf-8-sig")
            self.assertIn("第一条识别", text)
            self.assertIn("第二条识别", text)

    def test_wav_extension_without_wave_header_does_not_start_asr(self):
        row = message("1", self.start)
        row["message"] = [{"type": "record", "data": {"file": "invalid-voice"}}]
        self.pages = [[row], []]
        self.media[("get_record", "invalid-voice")] = {"file_name": "invalid.wav", "base64": base64.b64encode(b"not-wave-audio").decode()}
        with patch.object(self.core, "_create_local_asr_recognizer", return_value=object()), patch.object(self.core, "_transcribe_wav_local") as asr:
            document, _ = self.export(transcribe_voices=True)
        asr.assert_not_called()
        attachment = document["messages"][0]["attachments"][0]
        self.assertTrue(attachment["available"])
        self.assertFalse(attachment["decoded"])
        self.assertIn("RIFF/WAVE", attachment["decode_reason"])
        self.assertEqual(document["media_stats"]["voice_transcripts_failed"], 1)

    def test_preview_accepts_qq_normalized_message_without_wechat_db(self):
        document, _ = self.export()
        owner = SimpleNamespace(text=Mock(), chat_dir=self.root, _insert_separator=Mock(), _render_image=Mock(), _insert_button=Mock())
        self.preview.ChatPreview._render_message(owner, document["messages"][0])
        self.assertIn("重复正文", str(owner.text.insert.call_args_list))
        self.core.WeChatDB.assert_not_called()


if __name__ == "__main__":
    unittest.main()

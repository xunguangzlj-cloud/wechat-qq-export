"""验证微信与 QQ 合成数据的累计导出，不读取真实聊天。"""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_qq_export as qq_test_helpers
from test_sender_mapping import FakeDB, load_export_modules

message = qq_test_helpers.message


class WeChatIncrementalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core, _ = load_export_modules()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = FakeDB(self.root)
        self.start = self.core.parse_time_range("2026-10-01", None)["start_ts"]
        self.rows = [self.row(1, self.start + 10), self.row(2, self.start + 20)]

    def row(self, identifier, timestamp, server_id=None, source="message/message_0.db"):
        return {
            "local_id": identifier, "server_id": identifier if server_id is None else server_id,
            "create_time": timestamp, "sort_seq": identifier, "local_type": 1,
            "message_content": "相同正文", "_db_rel": source,
            "_sender_username": "wxid_target", "_sender_status": "resolved",
        }

    def export(self, **options):
        with patch.object(self.core, "WeChatDB", return_value=self.db), patch.object(self.core, "load_rows", return_value=[dict(row) for row in self.rows]):
            result = self.core.export_chat("Target remark", self.root / "output", incremental=True, **options)
        document = json.loads(Path(result["json"]).read_text(encoding="utf-8-sig"))
        return document, result

    def test_initial_repeat_zero_and_same_second_new_messages(self):
        first, result = self.export(start_time="2026-10-01")
        self.assertEqual(result["new_message_count"], 2)
        self.assertEqual(result["message_count"], 2)
        self.assertEqual([Path(result[kind]).name for kind in ("txt", "md", "json")],
                         ["Target remark." + kind for kind in ("txt", "md", "json")])
        self.rows.append(self.row(3, self.start + 20))
        second, next_result = self.export(start_time="2026-10-01")
        self.assertEqual(next_result["new_message_count"], 1)
        self.assertEqual(next_result["message_count"], 3)
        self.assertEqual(next_result["output_dir"], result["output_dir"])
        self.assertEqual([item["server_id"] for item in second["messages"]], [1, 2, 3])
        _, repeat = self.export(start_time="2026-10-01")
        self.assertEqual(repeat["new_message_count"], 0)
        self.assertEqual(repeat["message_count"], 3)
        self.assertFalse(repeat["checkpoint_advanced"])
        self.assertEqual(first["incremental_state"]["completed_ranges"][0]["checkpoint_ts"], self.start + 20)

    def test_account_isolation_and_renamed_contact_keep_stable_directory(self):
        _, first = self.export()
        self.db.target["remark"] = "更名的联系人"
        _, renamed = self.export()
        self.assertEqual(renamed["output_dir"], first["output_dir"])
        self.assertEqual(renamed["new_message_count"], 0)
        for kind in ("txt", "md", "json"):
            self.assertEqual(Path(renamed[kind]).name, "更名的联系人." + kind)
            self.assertFalse(Path(first[kind]).exists())
        self.db.self_info["username"] = "wxid_other_account"
        document, other = self.export()
        self.assertNotEqual(other["output_dir"], first["output_dir"])
        self.assertEqual(other["new_message_count"], 2)
        self.assertEqual(document["incremental_state"]["account_id"], "wxid_other_account")

    def test_unknown_account_refuses_incremental(self):
        self.db.self_info["username"] = None
        with self.assertRaisesRegex(ValueError, "登录账号"):
            self.export()
        self.assertFalse((self.root / "output").exists())

    def test_legacy_generic_files_migrate_without_losing_messages(self):
        _, initial = self.export()
        directory = Path(initial["output_dir"])
        (directory / ".export-state.json").unlink()
        for kind, legacy in (("txt", "chat_full_for_llm.txt"), ("md", "chat_full_for_llm.md"),
                             ("json", "chat_full_parsed.json")):
            Path(initial[kind]).rename(directory / legacy)
        self.rows.append(self.row(3, self.start + 30))
        document, migrated = self.export()
        self.assertEqual(migrated["new_message_count"], 1)
        self.assertEqual(document["message_count"], 3)
        self.assertEqual(migrated["output_dir"], initial["output_dir"])
        self.assertFalse((directory / "chat_full_parsed.json").exists())
        self.assertEqual(Path(migrated["json"]).name, "Target remark.json")
        _, repeated = self.export()
        self.assertEqual(repeated["new_message_count"], 0)

    def test_zero_server_id_uses_source_database_and_local_id(self):
        self.rows = [self.row(1, self.start, 0, "message/a.db"), self.row(1, self.start, 0, "message/b.db")]
        document, result = self.export()
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(len({item["incremental_key"] for item in document["messages"]}), 2)
        _, repeat = self.export()
        self.assertEqual(repeat["new_message_count"], 0)

    def test_extending_start_backwards_rereads_and_merges(self):
        self.export(start_time=self.core.fmt_time(self.start + 15))
        self.rows.insert(0, self.row(5, self.start - 20))
        document, result = self.export(start_time=self.core.fmt_time(self.start - 30))
        self.assertEqual(result["new_message_count"], 2)
        self.assertEqual([item["server_id"] for item in document["messages"]], [5, 1, 2])

    def test_late_synchronized_older_message_is_added_without_moving_start(self):
        self.export(start_time="2026-10-01")
        self.rows.insert(0, self.row(8, self.start + 5))
        document, result = self.export(start_time="2026-10-01")
        self.assertEqual(result["new_message_count"], 1)
        self.assertEqual([item["server_id"] for item in document["messages"]], [8, 1, 2])

    def test_empty_initial_range_has_three_files_and_can_later_add(self):
        self.rows = []
        document, result = self.export(start_time="2026-10-01")
        self.assertEqual(document["message_count"], 0)
        for field in ("txt", "md", "json"):
            self.assertTrue(Path(result[field]).is_file())
        self.rows = [self.row(1, self.start)]
        _, updated = self.export(start_time="2026-10-01")
        self.assertEqual(updated["new_message_count"], 1)

    def test_existing_attachment_and_transcript_are_not_processed_again(self):
        self.rows = [self.row(1, self.start)]
        self.rows[0]["local_type"] = 34
        def save_voice(db, row, username, directory):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "voice.silk"
            path.write_bytes(b"SYNTHETIC-VOICE")
            return {"kind": "voice", "path": str(path), "available": True}, "", "保留转写"
        with patch.object(self.core, "MediaDownloader", return_value=Mock()), patch.object(self.core, "_find_rust_silk", return_value=Path("synthetic")), patch.object(self.core, "_write_voice_from_row", side_effect=save_voice) as writer:
            document, first = self.export(export_voices=True)
            attachment_path = document["messages"][0]["media"]["path"]
            second, repeat = self.export(export_voices=True)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(repeat["new_message_count"], 0)
        self.assertEqual(second["messages"][0]["media"]["path"], attachment_path)
        self.assertEqual(second["messages"][0]["transcript"], "保留转写")
        self.assertEqual((Path(first["output_dir"]) / attachment_path).read_bytes(), b"SYNTHETIC-VOICE")

    def test_export_write_failure_does_not_advance_and_retry_succeeds(self):
        _, first = self.export()
        before = Path(first["json"]).read_bytes()
        self.rows.append(self.row(3, self.start + 30))
        with patch.object(self.core, "_write_markdown", side_effect=OSError("合成写失败")), self.assertRaises(OSError):
            self.export()
        self.assertEqual(Path(first["json"]).read_bytes(), before)
        _, retried = self.export()
        self.assertEqual(retried["new_message_count"], 1)
        self.assertEqual(retried["message_count"], 3)


class QQIncrementalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        qq_test_helpers.QQExportTests.setUpClass()
        cls.core, cls.qq = qq_test_helpers.QQExportTests.core, qq_test_helpers.QQExportTests.qq

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.start = self.core.parse_time_range("2026-10-01", None)["start_ts"]
        self.calls = []
        self.groups = [{"group_id": 123, "group_name": "合成群"}]
        self.friends = []
        self.media = {}
        self.account_id = "123456"
        self.pages = []

    def api(self, action, params=None):
        if action == "get_login_info":
            return {"user_id": self.account_id, "nickname": "本人"}
        return qq_test_helpers.QQExportTests.api(self, action, params)

    def export(self, keyword="合成群", **options):
        return qq_test_helpers.QQExportTests.export(self, keyword, incremental=True, start_time="2026-10-01", **options)

    def page(self, messages):
        self.pages = [messages + [message("old", self.start - 1, real_seq="1")]]

    def test_initial_zero_repeat_same_second_new_and_account_isolation(self):
        rows = [message("1", self.start + 20, real_seq="10"), message("2", self.start + 20, real_seq="20")]
        self.page(rows)
        _, first = self.export()
        self.page(rows + [message("3", self.start + 20, real_seq="30")])
        document, second = self.export()
        self.assertEqual(first["new_message_count"], 2)
        self.assertEqual(second["new_message_count"], 1)
        self.assertEqual([item["real_seq"] for item in document["messages"]], ["10", "20", "30"])
        self.page(rows + [message("3", self.start + 20, real_seq="30")])
        _, repeat = self.export()
        self.assertEqual(repeat["new_message_count"], 0)
        self.assertEqual(repeat["message_count"], 3)
        self.account_id = "654321"
        self.page(rows)
        _, other = self.export()
        self.assertNotEqual(first["output_dir"], other["output_dir"])
        self.assertEqual(other["new_message_count"], 2)

    def test_group_rename_changes_files_and_keeps_identity(self):
        rows = [message("1", self.start + 20, real_seq="10")]
        self.page(rows)
        _, initial = self.export()
        self.groups[0]["group_name"] = "新版群名"
        self.page(rows)
        _, renamed = self.export(keyword="新版群名")
        self.assertEqual(renamed["output_dir"], initial["output_dir"])
        self.assertEqual(renamed["new_message_count"], 0)
        for kind in ("txt", "md", "json"):
            self.assertEqual(Path(renamed[kind]).name, "新版群名." + kind)
            self.assertFalse(Path(initial[kind]).exists())

    def test_partial_history_keeps_messages_without_advancing_then_retries(self):
        newest = message("3", self.start + 30, real_seq="30")
        self.pages = [[newest], RuntimeError("合成历史失败")]
        document, partial = self.export()
        self.assertEqual(partial["new_message_count"], 1)
        self.assertTrue(partial["pending_read"])
        self.assertFalse(partial["checkpoint_advanced"])
        self.assertEqual(document["incremental_state"]["completed_ranges"], [])
        self.page([newest, message("2", self.start + 20, real_seq="20")])
        retried, result = self.export()
        self.assertEqual(result["new_message_count"], 1)
        self.assertEqual(result["message_count"], 2)
        self.assertFalse(result["pending_read"])
        self.assertTrue(result["checkpoint_advanced"])
        self.assertEqual(retried["incremental_state"]["completed_ranges"][0]["checkpoint_ts"], self.start + 30)

    def test_media_saved_once_old_paths_retained_and_new_paths_do_not_collide(self):
        first_message = message("1", self.start + 10, real_seq="10")
        first_message["message"] = [{"type": "image", "data": {"file": "img1"}}]
        self.media[("get_image", "img1")] = {"file_name": "same.png", "base64": base64.b64encode(b"FIRST-IMAGE").decode()}
        self.page([first_message])
        document, first = self.export(export_images=True)
        original_path = document["messages"][0]["attachments"][0]["path"]
        second_message = message("2", self.start + 20, real_seq="20")
        second_message["message"] = [{"type": "image", "data": {"file": "img2"}}]
        self.media[("get_image", "img2")] = {"file_name": "same.png", "base64": base64.b64encode(b"SECOND-IMAGE").decode()}
        self.page([first_message, second_message])
        merged, result = self.export(export_images=True)
        self.assertEqual([action for action, _ in self.calls].count("get_image"), 2)
        self.assertEqual(result["new_message_count"], 1)
        paths = [item["attachments"][0]["path"] for item in merged["messages"]]
        self.assertEqual(paths[0], original_path)
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual((Path(first["output_dir"]) / original_path).read_bytes(), b"FIRST-IMAGE")
        self.page([first_message, second_message])
        _, repeat = self.export(export_images=True)
        self.assertEqual(repeat["new_message_count"], 0)
        self.assertEqual([action for action, _ in self.calls].count("get_image"), 2)

    def test_short_id_collision_distinct_sequences_are_retained(self):
        self.page([message("7", self.start + 10, real_seq="70"), message("7", self.start + 20, real_seq="71")])
        document, result = self.export()
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(len({item["incremental_key"] for item in document["messages"]}), 2)

    def test_empty_history_is_valid_zero_export(self):
        self.pages = [[]]
        document, result = self.export()
        self.assertEqual(result["new_message_count"], 0)
        self.assertEqual(document["messages"], [])
        self.assertFalse(result["pending_read"])
        for field in ("txt", "md", "json"):
            self.assertTrue(Path(result[field]).is_file())

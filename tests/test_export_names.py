"""使用合成会话验证文件名、Windows 限制与同名导出隔离。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from export_names import export_paths, export_stem
from incremental_export import resolve_chat_json
from test_sender_mapping import FakeDB, load_export_modules


class ExportNameTests(unittest.TestCase):
    def test_names_preserve_display_names_and_replace_illegal_characters(self):
        self.assertEqual(export_stem(" 学习群/2026：讨论? "), "学习群_2026：讨论_")
        self.assertEqual(export_stem('群<>:"\\|?*\n名.'), "群_________名")
        self.assertEqual(export_stem("..."), "未命名会话")
        self.assertEqual(export_stem(None, "未命名文章"), "未命名文章")

    def test_windows_reserved_names_include_extensions_and_superscript_digits(self):
        for name in ("CON", "con.txt", "COM1", "LPT9.md", "COM¹", "CONOUT$", ".export-state"):
            with self.subTest(name=name):
                self.assertEqual(export_stem(name), "_" + name)

    def test_long_unicode_names_do_not_split_emoji(self):
        value = export_stem("标题" + "😀" * 100)
        self.assertEqual(len(value.encode("utf-16-le")) // 2, 80)
        self.assertEqual(value, "标题" + "😀" * 39)
        for path in export_paths("目录", "群名").values():
            self.assertEqual(path.parent, Path("目录"))
            self.assertEqual(path.stem, "群名")

    def test_preview_resolves_named_legacy_and_current_incremental_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            document = {"chat_name": "好友名", "chat_username": "wxid_fake", "messages": []}
            named = directory / "好友名.json"
            named.write_text(json.dumps(document), encoding="utf-8")
            (directory / "report.json").write_text('{"results": []}', encoding="utf-8")
            self.assertEqual(resolve_chat_json(directory), named)
            legacy = directory / "chat_full_parsed.json"
            named.rename(legacy)
            self.assertEqual(resolve_chat_json(directory), legacy)
            legacy.rename(named)
            (directory / ".export-state.json").write_text(
                json.dumps({"schema_version": 1, "json": named.name}), encoding="utf-8")
            self.assertEqual(resolve_chat_json(directory), named)

    def test_preview_refuses_reports_and_ambiguous_named_chats(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "report.json").write_text('{"messages": [], "title": "文章"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "唯一"):
                resolve_chat_json(directory)
            for filename in ("群一.json", "群二.json"):
                (directory / filename).write_text(json.dumps({
                    "chat_name": "同名", "chat_username": filename, "platform": "qq", "messages": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "唯一"):
                resolve_chat_json(directory)


class WeChatNamedExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core, cls.preview = load_export_modules()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = FakeDB(self.root)
        self.rows = [{"local_id": 1, "server_id": 1, "create_time": 1700000000,
                      "sort_seq": 1, "local_type": 1, "message_content": "合成正文",
                      "_sender_username": "wxid_target", "_sender_status": "resolved"}]

    def export(self):
        with patch.object(self.core, "WeChatDB", return_value=self.db), \
                patch.object(self.core, "load_rows", return_value=[dict(row) for row in self.rows]):
            return self.core.export_chat("合成会话", self.root / "output")

    def test_private_and_group_files_use_display_name(self):
        for name, identifier, expected in (("好友备注", "wxid_target", "好友备注"),
                                           ("研究/讨论群", "group@chatroom", "研究_讨论群")):
            self.db.target.update({"username": identifier, "remark": name})
            result = self.export()
            for kind in ("txt", "md", "json"):
                path = Path(result[kind])
                self.assertEqual(path.name, expected + "." + kind)
                self.assertTrue(path.is_file())
            self.assertIn("合成正文", Path(result["txt"]).read_text(encoding="utf-8-sig"))

    def test_same_name_distinct_chats_keep_separate_directories(self):
        self.db.target["remark"] = "同名群"
        first = self.export()
        self.db.target["username"] = "another@chatroom"
        second = self.export()
        self.assertNotEqual(first["output_dir"], second["output_dir"])
        self.assertEqual(Path(first["json"]).name, Path(second["json"]).name)
        self.assertTrue(Path(first["json"]).is_file())
        self.assertTrue(Path(second["json"]).is_file())

    def test_windows_reserved_display_name_can_create_directory_and_files(self):
        self.db.target["remark"] = "CON.txt"
        result = self.export()
        self.assertTrue(Path(result["output_dir"]).name.startswith("_CON.txt_"))
        self.assertEqual(Path(result["txt"]).name, "_CON.txt.txt")
        self.assertTrue(Path(result["json"]).is_file())


if __name__ == "__main__":
    unittest.main()

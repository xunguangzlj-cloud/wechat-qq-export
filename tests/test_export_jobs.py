"""用合成导出器验证批量队列，不读取真实聊天。"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_sender_mapping import load_export_modules


ROOT = Path(__file__).resolve().parents[1]


class ExportJobsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(sys, "path", [str(ROOT), *sys.path]):
            cls.core, _ = load_export_modules()
        cls.qq = SimpleNamespace(export_qq_chat=Mock())
        spec = importlib.util.spec_from_file_location("_export_jobs_tests", ROOT / "export_jobs.py")
        cls.jobs = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"exporter_core": cls.core, "qq_exporter": cls.qq}):
            spec.loader.exec_module(cls.jobs)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "导出"
        self.wechat = Mock(side_effect=self.synthetic_result)
        self.qq.export_qq_chat = Mock(side_effect=self.synthetic_result)
        patcher = patch.object(self.core, "export_chat", self.wechat)
        patcher.start()
        self.addCleanup(patcher.stop)

    def synthetic_result(self, keyword, *, out_root, **options):
        path = Path(out_root) / keyword
        result = {
            "chat_name": keyword, "chat_username": "id:" + keyword, "message_count": 3,
            "output_dir": str(path), "txt": str(path / "chat.txt"),
            "md": str(path / "chat.md"), "json": str(path / "chat.json"),
            "media_stats": {"images_exported": 2},
        }
        if options.get("incremental"):
            result.update({"incremental": True, "new_message_count": 1,
                           "checkpoint_advanced": False, "pending_read": True})
        return result

    def read_report(self, result):
        return json.loads(Path(result["report"]).read_text(encoding="utf-8-sig"))

    def test_names_use_lines_deduplicate_and_preserve_commas(self):
        result = self.jobs.export_chats("  群一,群二\n\n群三\n群一,群二\n 群三 ", self.root)
        self.assertEqual([call.args[0] for call in self.wechat.call_args_list], ["群一,群二", "群三"])
        self.assertEqual(self.read_report(result)["names"], ["群一,群二", "群三"])
        self.assertEqual([item["keyword"] for item in result["results"]], ["群一,群二", "群三"])
        self.assertTrue(Path(result["output_dir"]).name.startswith("batch_"))

    def test_list_names_are_trimmed_without_splitting(self):
        self.jobs.export_chats([" 群一,群二 ", "", "群一,群二", " 群三 "], self.root)
        self.assertEqual([call.args[0] for call in self.wechat.call_args_list], ["群一,群二", "群三"])

    def test_failure_continues_following_items(self):
        self.wechat.side_effect = [RuntimeError("合成失败"), self.synthetic_result("群二", out_root=self.root)]
        result = self.jobs.export_chats("群一\n群二", self.root)
        self.assertEqual(result["failures"], [{"keyword": "群一", "error": "合成失败"}])
        self.assertEqual([item["keyword"] for item in result["results"]], ["群二"])
        self.assertEqual(self.read_report(result)["failure_count"], 1)

    def test_cancel_ambiguous_chat_continues(self):
        candidates = [{"username": "稳定ID"}]
        self.wechat.side_effect = [self.core.AmbiguousContactError("同名", candidates),
                                   self.synthetic_result("群二", out_root=self.root)]
        selector = Mock(return_value=None)
        result = self.jobs.export_chats("同名\n群二", self.root, select_contact=selector)
        selector.assert_called_once_with(candidates, "同名")
        self.assertEqual(result["cancelled"], ["同名"])
        self.assertEqual(result["failures"], [])
        self.assertEqual(len(result["results"]), 1)

    def test_ambiguous_chat_retries_stable_id(self):
        candidates = [{"username": "稳定ID"}]
        self.wechat.side_effect = [self.core.AmbiguousContactError("同名", candidates),
                                   self.synthetic_result("准确群", out_root=self.root)]
        result = self.jobs.export_chats("同名", self.root, select_contact=Mock(return_value="稳定ID"))
        self.assertEqual(self.wechat.call_count, 2)
        self.assertEqual(self.wechat.call_args.kwargs["chat_id"], "稳定ID")
        self.assertEqual(result["results"][0]["keyword"], "同名")

    def test_ambiguous_chat_without_selector_is_failure(self):
        self.wechat.side_effect = [self.core.AmbiguousContactError("同名", []),
                                   self.synthetic_result("群二", out_root=self.root)]
        result = self.jobs.export_chats("同名\n群二", self.root)
        self.assertEqual(result["failures"][0]["keyword"], "同名")
        self.assertEqual(len(result["results"]), 1)

    def test_second_ambiguity_does_not_loop(self):
        self.wechat.side_effect = self.core.AmbiguousContactError("同名", [])
        result = self.jobs.export_chats("同名", self.root, select_contact=Mock(return_value="错误ID"))
        self.assertEqual(self.wechat.call_count, 2)
        self.assertEqual(len(result["failures"]), 1)

    def test_incremental_uses_stable_root_and_forwards_options(self):
        result = self.jobs.export_chats("群一\n群二", self.root, incremental=True, db_dir="合成数据库目录",
                                       start_time="2026-10-01", end_time="2026-10-07", export_images=True)
        for call in self.wechat.call_args_list:
            self.assertEqual(call.kwargs["out_root"], self.root.resolve())
            self.assertTrue(call.kwargs["incremental"])
            self.assertEqual(call.kwargs["db_dir"], "合成数据库目录")
            self.assertEqual(call.kwargs["start_time"], "2026-10-01")
            self.assertEqual(call.kwargs["end_time"], "2026-10-07")
            self.assertTrue(call.kwargs["export_images"])
        report = self.read_report(result)
        self.assertTrue(report["results"][0]["pending_read"])
        self.assertFalse(report["results"][0]["checkpoint_advanced"])
        self.assertEqual(report["results"][0]["new_message_count"], 1)

    def test_regular_batch_always_uses_report_directory(self):
        result = self.jobs.export_chats("群一\n群二", self.root)
        for call in self.wechat.call_args_list:
            self.assertEqual(call.kwargs["out_root"], Path(result["output_dir"]))
            self.assertFalse(call.kwargs["incremental"])
        self.wechat.reset_mock()
        single = self.jobs.export_chats("群一", self.root)
        self.assertEqual(self.wechat.call_args.kwargs["out_root"], Path(single["output_dir"]))

    def test_qq_token_redacted_from_logs_errors_and_report(self):
        secret = "synthetic-token"
        logs = []

        def export(keyword, *, out_root, progress, **options):
            progress("合成日志 " + secret)
            if keyword == "群一":
                raise RuntimeError("合成错误 " + secret)
            result = self.synthetic_result(keyword, out_root=out_root)
            result["chat_name"] = "合成名称 " + secret
            result["token"] = secret
            result["database_keys"] = "不应序列化"
            return result

        self.qq.export_qq_chat.side_effect = export
        result = self.jobs.export_chats("群一\n群二", self.root, platform="QQ", token=secret,
                                        endpoint="http://127.0.0.1:3000", progress=logs.append)
        self.assertEqual(self.wechat.call_count, 0)
        self.assertNotIn(secret, "\n".join(logs))
        self.assertNotIn(secret, result["failures"][0]["error"])
        report_text = Path(result["report"]).read_text(encoding="utf-8-sig")
        self.assertNotIn(secret, report_text)
        self.assertNotIn("database_keys", report_text)
        self.assertNotIn("endpoint", report_text)
        self.assertNotIn("token", report_text)
        self.assertEqual(self.qq.export_qq_chat.call_args.kwargs["token"], secret)

    def test_report_write_failure_is_raised_after_items(self):
        with patch.object(self.jobs.Path, "write_text", side_effect=OSError("报告无法写入")):
            with self.assertRaisesRegex(OSError, "报告无法写入"):
                self.jobs.export_chats("群一\n群二", self.root)
        self.assertEqual(self.wechat.call_count, 2)

    def test_empty_names_and_unknown_platform_do_not_export(self):
        for names, platform in (("\n ", "微信"), ("群一", "未知平台")):
            with self.assertRaises(ValueError):
                self.jobs.export_chats(names, self.root, platform=platform)
        self.assertFalse(self.root.exists())
        self.wechat.assert_not_called()


if __name__ == "__main__":
    unittest.main()

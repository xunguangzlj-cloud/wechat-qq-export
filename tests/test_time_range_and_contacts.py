import contextlib
import importlib.util
import io
import json
import queue
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from test_sender_mapping import FakeDB, load_export_modules


ROOT = Path(__file__).resolve().parents[1]


class TimeRangeAndContactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core, _ = load_export_modules()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = FakeDB(self.root)
        self.start = self.core.parse_time_range("2026-10-01", None)["start_ts"]

    def export(self, **options):
        with patch.object(self.core, "WeChatDB", return_value=self.db):
            result = self.core.export_chat("Target remark", self.root / "output", **options)
        data = json.loads(Path(result["json"]).read_text(encoding="utf-8-sig"))
        return data, result

    def add_messages(self, messages):
        return self.db.add_shard({2: "wxid_target"}, messages)

    def test_beijing_range_date_end_and_minute_precision(self):
        date_range = self.core.parse_time_range("2026-10-01", "2026-10-01")
        self.assertEqual(date_range["start_ts"], 1790784000)
        self.assertEqual(date_range["end_ts_exclusive"] - date_range["start_ts"], 86400)
        self.assertTrue(date_range["end_date_inclusive"])
        minute = self.core.parse_time_range("2026-10-01 08:30", "2026-10-01 09:30:00")
        self.assertEqual(minute["end_ts_exclusive"] - minute["start_ts"], 3600)
        self.assertFalse(minute["end_date_inclusive"])
        self.assertEqual(self.core.fmt_time(date_range["start_ts"]), "2026-10-01 00:00:00")

    def test_invalid_input_is_rejected_before_database_or_cache(self):
        for start, end in (("2026-02-30", None), ("明天", None), ("2026-10-02", "2026-10-01"), ("2026-10-01 09:00", "2026-10-01 09:00")):
            with self.subTest(start=start, end=end), patch.object(self.core, "WeChatDB") as database, patch.object(self.core, "_create_sensitive_workdir") as cache:
                with self.assertRaises(ValueError):
                    self.core.export_chat("Target", start_time=start, end_time=end)
                database.assert_not_called()
                cache.assert_not_called()

    def test_shard_boundaries_milliseconds_and_repeated_text_are_preserved(self):
        self.add_messages([
            {"create_time": self.start - 1, "message_content": "界外"},
            {"create_time": self.start, "message_content": "同文重复", "sort_seq": 2},
        ])
        self.add_messages([
            {"create_time": (self.start + 1) * 1000, "message_content": "同文重复", "sort_seq": 3},
            {"create_time": self.start + 60, "message_content": "结束界限", "sort_seq": 4},
        ])
        data, result = self.export(start_time="2026-10-01 00:00", end_time="2026-10-01 00:01")
        self.assertEqual([message["content"] for message in data["messages"]], ["同文重复", "同文重复"])
        self.assertEqual(len({message["source_db"] for message in data["messages"]}), 2)
        self.assertEqual(data["chat_username"], "wxid_target")
        self.assertEqual(data["filter"], result["filter"])
        for key in ("txt", "md"):
            content = Path(result[key]).read_text(encoding="utf-8-sig")
            self.assertEqual(content.count("同文重复"), 2)
            self.assertNotIn("界外", content)
            self.assertNotIn("结束界限", content)

    def test_invalid_timestamps_are_counted_only_for_limited_ranges(self):
        self.add_messages([
            {"create_time": None}, {"create_time": "bad"},
            {"create_time": self.start},
        ])
        bounded, _ = self.export(start_time="2026-10-01")
        self.assertEqual(bounded["message_count"], 1)
        self.assertEqual(bounded["filter"]["invalid_time_excluded"], 2)
        all_messages, _ = self.export()
        self.assertEqual(all_messages["message_count"], 3)
        self.assertEqual(all_messages["filter"]["invalid_time_excluded"], 0)

    def test_open_start_and_end_boundaries(self):
        self.add_messages([{"create_time": self.start - 1}, {"create_time": self.start}])
        self.assertEqual(self.export(end_time="2026-10-01 00:00")[0]["message_count"], 1)
        self.assertEqual(self.export(start_time="2026-10-01 00:00")[0]["message_count"], 1)

    def test_empty_filtered_range_creates_no_output_and_cleans_cache(self):
        self.add_messages([{"create_time": self.start - 1}, {"create_time": None}])
        with patch.object(self.core, "WeChatDB", return_value=self.db) as constructor, patch.object(self.core, "MediaDownloader") as media:
            with self.assertRaisesRegex(ValueError, "时间无效已排除 1 条"):
                self.core.export_chat("Target", self.root / "output", start_time="2026-10-01", export_images=True)
        self.assertFalse((self.root / "output").exists())
        self.assertFalse(Path(constructor.call_args.kwargs["workdir"]).exists())
        media.assert_not_called()

    def test_same_name_is_never_silently_selected_and_ids_are_exact(self):
        candidates = [
            {"username": "id1@chatroom", "nick_name": "测试群", "remark": ""},
            {"username": "id2@chatroom", "nick_name": "测试群", "remark": ""},
        ]
        database = SimpleNamespace(search_contact=lambda _: candidates)
        with self.assertRaises(self.core.AmbiguousContactError) as raised:
            self.core.find_contact(database, "测试群")
        self.assertEqual(raised.exception.candidates, candidates)
        self.assertEqual(self.core.find_contact(database, "测试群", "id2@chatroom"), candidates[1])
        self.assertEqual(self.core.find_contact(database, "id1@chatroom"), candidates[0])
        with self.assertRaises(ValueError):
            self.core.find_contact(database, "测试群", "id")

    def test_duplicate_candidate_records_do_not_create_false_ambiguity(self):
        candidate = {"username": "wxid_target", "nick_name": "昵称", "remark": "备注"}
        database = SimpleNamespace(search_contact=lambda _: [candidate, candidate.copy()])
        self.assertEqual(self.core.find_contact(database, "备注"), candidate)

    def test_contact_ambiguity_cleans_cache_and_creates_no_output(self):
        self.db.search_contact = lambda _: [
            {"username": "one", "nick_name": "同名"},
            {"username": "two", "nick_name": "同名"},
        ]
        with patch.object(self.core, "WeChatDB", return_value=self.db) as constructor:
            with self.assertRaises(self.core.AmbiguousContactError):
                self.core.export_chat("同名", self.root / "output")
        self.assertFalse(Path(constructor.call_args.kwargs["workdir"]).exists())
        self.assertFalse((self.root / "output").exists())

    def test_each_run_has_a_separate_directory(self):
        self.add_messages([{"create_time": self.start}])
        first = self.export()[1]
        second = self.export()[1]
        self.assertNotEqual(first["output_dir"], second["output_dir"])
        self.assertTrue(Path(first["json"]).is_file())
        self.assertTrue(Path(second["json"]).is_file())

    def test_media_and_asr_only_receive_filtered_messages(self):
        self.add_messages([
            {"local_type": 3, "create_time": self.start - 1, "sort_seq": 1},
            {"local_type": 34, "create_time": self.start - 1, "sort_seq": 2},
            {"local_type": 3, "create_time": self.start, "sort_seq": 3},
            {"local_type": 34, "create_time": self.start, "sort_seq": 4},
        ])
        voice_media = {"kind": "voice", "available": True, "decoded": True, "path": str(self.root / "fixture.wav"), "voice_sha256": "fixture"}
        with (
            patch.object(self.core, "MediaDownloader", return_value=Mock()),
            patch.object(self.core, "_index_image_files", return_value={}),
            patch.object(self.core, "_ensure_cfg_dword", return_value=(None, None)),
            patch.object(self.core, "_write_image_from_row", return_value=(None, "合成缺失")) as image,
            patch.object(self.core, "_write_voice_from_row", return_value=(voice_media, None, "")) as voice,
            patch.object(self.core, "_find_rust_silk", return_value=self.root / "fake-decoder"),
            patch.object(self.core, "local_asr_status", return_value=(True, "")),
            patch.object(self.core, "_create_local_asr_recognizer", return_value=object()),
            patch.object(self.core, "_load_asr_cache", return_value=(self.root / "cache.json", {})),
            patch.object(self.core, "_save_asr_cache"),
            patch.object(self.core, "_transcribe_wav_local", return_value=("合成转写", None)) as asr,
        ):
            data, _ = self.export(start_time="2026-10-01", export_images=True, transcribe_voices=True)
        self.assertEqual(image.call_args.args[2]["local_id"], 3)
        self.assertEqual(voice.call_args.args[1]["local_id"], 4)
        image.assert_called_once()
        voice.assert_called_once()
        asr.assert_called_once()
        self.assertEqual(data["media_stats"]["images_requested"], 1)
        self.assertEqual(data["media_stats"]["voices_requested"], 1)
        self.assertEqual(data["messages"][1]["transcript"], "合成转写")

    def run_cli(self, arguments, exporter):
        with patch.dict(sys.modules, {"exporter_core": self.core}), patch.object(self.core, "export_chat", exporter), patch.object(sys, "argv", ["cli.py", *arguments]), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            runpy.run_path(str(ROOT / "cli.py"), run_name="__main__")
        return output.getvalue()

    def test_cli_passes_time_id_and_media_options(self):
        exporter = Mock(return_value={"message_count": 1})
        self.run_cli(["群名", "--start", "2026-10-01", "--end", "2026-10-02 12:30", "--chat-id", "id2@chatroom", "--media"], exporter)
        options = exporter.call_args.kwargs
        self.assertEqual(options["start_time"], "2026-10-01")
        self.assertEqual(options["end_time"], "2026-10-02 12:30")
        self.assertEqual(options["chat_id"], "id2@chatroom")
        self.assertTrue(options["export_images"] and options["export_voices"])

    def test_cli_rejects_invalid_time_without_export(self):
        exporter = Mock()
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(["群名", "--start", "错误日期"], exporter)
        self.assertEqual(raised.exception.code, 2)
        exporter.assert_not_called()

    def test_cli_ambiguity_prints_precise_ids(self):
        exporter = Mock(side_effect=self.core.AmbiguousContactError("同名", [{"username": "id@chatroom", "nick_name": "同名"}]))
        with patch.dict(sys.modules, {"exporter_core": self.core}), patch.object(self.core, "export_chat", exporter), patch.object(sys, "argv", ["cli.py", "同名"]), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(ROOT / "cli.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--chat-id id@chatroom", output.getvalue())

    def load_app(self):
        preview = ModuleType("preview")
        preview.open_chat_preview = Mock()
        directories = ModuleType("wechat_data_dirs")
        directories.choose_wechat_data_dir = Mock()
        with patch.dict(sys.modules, {"exporter_core": self.core, "psutil": Mock(), "preview": preview, "wechat_data_dirs": directories}):
            spec = importlib.util.spec_from_file_location("_range_test_app", ROOT / "app.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def test_gui_worker_preserves_request_for_main_thread_selection(self):
        app = self.load_app()
        owner = SimpleNamespace(q=queue.Queue())
        candidates = [{"username": "id@chatroom", "nick_name": "同名"}]
        with patch.object(app, "export_chat", side_effect=self.core.AmbiguousContactError("同名", candidates)):
            app.App.worker(owner, "同名", "output", True, False, False, True, False, None, "2026-10-01", "2026-10-02")
        kind, payload = owner.q.get_nowait()
        self.assertEqual(kind, "choose_contact")
        self.assertEqual(payload["request"]["start_time"], "2026-10-01")
        self.assertTrue(payload["request"]["export_videos"])
        owner.choose_contact = Mock(return_value="id@chatroom")
        owner.log = Mock()
        owner.after = Mock()
        owner.poll_queue = Mock()
        owner.q.put((kind, payload))
        with patch.object(app.threading, "Thread") as thread:
            owner.worker = Mock()
            app.App.poll_queue(owner)
        owner.choose_contact.assert_called_once_with(candidates)
        self.assertEqual(thread.call_args.kwargs["kwargs"]["chat_id"], "id@chatroom")
        self.assertEqual(thread.call_args.kwargs["kwargs"]["end_time"], "2026-10-02")
        thread.return_value.start.assert_called_once()

    def test_gui_cancel_does_not_start_export(self):
        app = self.load_app()
        owner = SimpleNamespace(
            q=queue.Queue(), choose_contact=Mock(return_value=None),
            export_btn=Mock(), clear_cache_btn=Mock(), log=Mock(),
            after=Mock(), poll_queue=Mock(),
        )
        owner.q.put(("choose_contact", {"candidates": [{"username": "id"}], "request": {}}))
        with patch.object(app.threading, "Thread") as thread:
            app.App.poll_queue(owner)
        thread.assert_not_called()
        owner.export_btn.configure.assert_called_once_with(state="normal")
        owner.clear_cache_btn.configure.assert_called_once_with(state="normal")

    def test_gui_worker_passes_exact_id_and_times(self):
        app = self.load_app()
        owner = SimpleNamespace(q=queue.Queue())
        with patch.object(app, "export_chat", return_value={"message_count": 1}) as exporter:
            app.App.worker(owner, "群名", "output", False, False, False, False, False, None, "2026-10-01", None, "id@chatroom")
        self.assertEqual(exporter.call_args.kwargs["chat_id"], "id@chatroom")
        self.assertEqual(exporter.call_args.kwargs["start_time"], "2026-10-01")
        self.assertEqual(owner.q.get_nowait()[0], "done")


if __name__ == "__main__":
    unittest.main()

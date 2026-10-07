"""独立验证累计提交的故障恢复与覆盖窗口，使用合成消息。"""

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TIMEZONE = timezone(timedelta(hours=8))


class IncrementalReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("_incremental_review", ROOT / "incremental_export.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def state(self, start=1000, end=10000):
        return self.module.IncrementalExport(self.root, "wechat", "合成账号", "合成会话", {
            "start_ts": start, "end_ts_exclusive": end, "start": "合成开始", "end": "合成结束",
            "end_date_inclusive": True, "invalid_time_excluded": 0,
        })

    def row(self, identifier, timestamp):
        return {"server_id": identifier, "create_time": timestamp, "sort_seq": identifier}

    def writer(self, messages, path, is_group, chat_name, **options):
        path.write_text("\n".join(message["incremental_key"] for message in messages), encoding="utf-8")

    def commit(self, state, rows, complete=True, chat_name="合成会话"):
        selected = state.select_rows(rows)
        document = {
            "chat_name": chat_name, "chat_username": "合成会话",
            "messages": [{"incremental_key": row["_incremental_key"],
                          "time": datetime.fromtimestamp(row["create_time"], TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                          "sort_seq": row["sort_seq"], "content": "合成正文"} for row in selected],
            "type_counts": {"文本": len(selected)},
            "sender_resolution_counts": {"resolved": len(selected)},
            "media_stats": {},
        }
        return state.commit(document, rows, complete, self.writer, self.writer, False, "合成导出器")

    def document(self, state):
        return json.loads(state._archive_path().read_text(encoding="utf-8-sig"))

    def test_json_replace_failure_rolls_back_text_and_checkpoint(self):
        initial = self.state()
        result = self.commit(initial, [self.row(1, 1100)])
        before = {key: Path(result[key]).read_bytes() for key in ("txt", "md", "json")}
        retry = self.state()
        real_replace = self.module.os.replace

        def replace(source, destination):
            if Path(destination).name == "合成会话.json":
                raise OSError("合成 JSON 提交失败")
            real_replace(source, destination)

        with patch.object(self.module.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "合成 JSON 提交失败"):
                self.commit(retry, [self.row(2, 1200)])
        for key, content in before.items():
            self.assertEqual(Path(result[key]).read_bytes(), content)
        reopened = self.state()
        self.assertEqual(reopened.read_filter["start_ts"], 1100)
        self.assertEqual(len(reopened.select_rows([self.row(2, 1200)])), 1)
        self.assertEqual(list(initial.directory.glob(".pending-*")), [])
        self.assertEqual(list(initial.directory.glob(".previous-*")), [])

    def test_first_json_failure_leaves_no_false_success_files(self):
        initial = self.state()
        real_replace = self.module.os.replace

        def replace(source, destination):
            if Path(destination).suffix == ".json":
                raise OSError("首次 JSON 提交失败")
            real_replace(source, destination)

        with patch.object(self.module.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "首次 JSON 提交失败"):
                self.commit(initial, [self.row(1, 1100)])
        self.assertEqual(list(initial.directory.glob("*.txt")), [])
        self.assertEqual(list(initial.directory.glob("*.md")), [])
        self.assertEqual(list(initial.directory.glob("*.json")), [])
        self.assertIsNone(self.state().previous_digest)

    def test_stale_concurrent_snapshot_cannot_overwrite_successful_commit(self):
        first, stale = self.state(), self.state()
        result = self.commit(first, [self.row(1, 1100)])
        before = Path(result["json"]).read_bytes()
        with self.assertRaisesRegex(RuntimeError, "另一导出任务修改"):
            self.commit(stale, [self.row(2, 1200)])
        self.assertEqual(Path(result["json"]).read_bytes(), before)
        self.assertEqual(len(self.document(first)["messages"]), 1)

    def test_os_lock_rejects_second_commit_without_mutating_archive(self):
        first, concurrent = self.state(), self.state()
        with first._commit_lock():
            with self.assertRaisesRegex(RuntimeError, "正在提交"):
                self.commit(concurrent, [self.row(2, 1200)])
        self.assertFalse((first.directory / "chat_full_parsed.json").exists())

    def test_marker_failure_restores_all_public_files_and_checkpoint(self):
        initial = self.state()
        result = self.commit(initial, [self.row(1, 1100)])
        before = {key: Path(result[key]).read_bytes() for key in ("txt", "md", "json")}
        marker = (initial.directory / ".export-state.json").read_bytes()
        real_replace = self.module.os.replace

        def replace(source, destination):
            if Path(destination).name == ".export-state.json":
                raise OSError("合成状态提交失败")
            real_replace(source, destination)

        with patch.object(self.module.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "合成状态提交失败"):
                self.commit(self.state(), [self.row(2, 1200)])
        for key, content in before.items():
            self.assertEqual(Path(result[key]).read_bytes(), content)
        self.assertEqual((initial.directory / ".export-state.json").read_bytes(), marker)
        self.assertEqual(self.state().read_filter["start_ts"], 1100)

    def test_invalid_manifest_never_reads_outside_export_directory(self):
        state = self.state()
        for value in ([], {"schema_version": 1, "json": "../outside.json"},
                      {"schema_version": 1, "json": "C:\\outside.json"},
                      {"schema_version": 1, "json": ".export-state.json"}):
            (state.directory / ".export-state.json").write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "文件名无效"):
                self.state()

    def test_rename_marker_failure_keeps_old_files_and_retry_merges(self):
        initial = self.state()
        result = self.commit(initial, [self.row(1, 1100)])
        before = {key: Path(result[key]).read_bytes() for key in ("txt", "md", "json")}
        real_replace = self.module.os.replace

        def replace(source, destination):
            if Path(destination).name == ".export-state.json":
                raise OSError("合成改名提交失败")
            real_replace(source, destination)

        with patch.object(self.module.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "合成改名提交失败"):
                self.commit(self.state(), [self.row(2, 1200)], chat_name="新群名")
        for key, content in before.items():
            self.assertEqual(Path(result[key]).read_bytes(), content)
        self.assertEqual(list(initial.directory.glob("新群名.*")), [])
        updated = self.commit(self.state(), [self.row(2, 1200)], chat_name="新群名")
        self.assertEqual(updated["message_count"], 2)
        self.assertEqual(Path(updated["json"]).name, "新群名.json")
        self.assertEqual(list(initial.directory.glob("合成会话.*")), [])

    def test_same_legacy_basename_is_not_mistaken_for_legacy_layout(self):
        initial = self.state()
        result = self.commit(initial, [self.row(1, 1100)], chat_name="chat_full_parsed")
        updated = self.commit(self.state(), [], chat_name="新群名")
        self.assertEqual(updated["message_count"], 1)
        for key in ("txt", "md", "json"):
            self.assertFalse(Path(result[key]).exists())

    def test_old_file_cleanup_failure_does_not_undo_successful_checkpoint(self):
        initial = self.state()
        result = self.commit(initial, [self.row(1, 1100)])
        old_files = {Path(result[key]) for key in ("txt", "md", "json")}
        real_unlink = Path.unlink

        def unlink(path, **options):
            if path in old_files:
                raise OSError("合成旧文件占用")
            return real_unlink(path, **options)

        with patch.object(Path, "unlink", unlink):
            updated = self.commit(self.state(), [self.row(2, 1200)], chat_name="新群名")
        self.assertEqual(set(updated["filename_cleanup_errors"]), {path.name for path in old_files})
        self.assertEqual(self.state().read_filter["start_ts"], 1200)
        self.assertEqual(self.state().old["message_count"], 2)

    def test_partial_keeps_messages_without_advancing_completed_range(self):
        initial = self.state()
        self.commit(initial, [self.row(1, 1100)])
        before_ranges = self.document(initial)["incremental_state"]["completed_ranges"]
        partial = self.state()
        result = self.commit(partial, [self.row(2, 1200)], complete=False)
        document = self.document(partial)
        self.assertEqual(document["incremental_state"]["completed_ranges"], before_ranges)
        self.assertTrue(result["pending_read"])
        self.assertFalse(result["checkpoint_advanced"])
        self.assertEqual(document["message_count"], 2)
        retry = self.state()
        self.assertEqual(retry.read_filter["start_ts"], 1100)
        selected = retry.select_rows([self.row(2, 1200), self.row(3, 1150)])
        self.assertEqual([row["server_id"] for row in selected], [3])

    def test_disjoint_completed_ranges_do_not_skip_gap(self):
        first = self.state(start=1000, end=1500)
        self.commit(first, [self.row(1, 1100)])
        later = self.state(start=2000, end=3000)
        self.commit(later, [self.row(2, 2100)])
        ranges = self.document(later)["incremental_state"]["completed_ranges"]
        self.assertEqual(len(ranges), 2)
        gap = self.state(start=1500, end=3000)
        self.assertEqual(gap.read_filter["start_ts"], 1500)
        self.commit(gap, [self.row(3, 1600), self.row(2, 2100)])
        document = self.document(gap)
        self.assertEqual(document["message_count"], 3)
        self.assertEqual(len(document["incremental_state"]["completed_ranges"]), 2)

    def test_earlier_window_expands_and_merges_existing_ranges(self):
        first = self.state(start=2000, end=3000)
        self.commit(first, [self.row(2, 2100), self.row(3, 2200)])
        earlier = self.state(start=1000, end=3000)
        self.assertEqual(earlier.read_filter["start_ts"], 1000)
        result = self.commit(earlier, [self.row(1, 1100), self.row(2, 2100), self.row(3, 2200)])
        document = self.document(earlier)
        self.assertEqual(result["new_message_count"], 1)
        self.assertEqual(document["message_count"], 3)
        self.assertEqual(document["incremental_state"]["completed_ranges"], [{"start_ts": 1000, "checkpoint_ts": 2200}])

    def test_cumulative_filter_text_and_timestamps_agree(self):
        first = self.state(start=2000, end=3000)
        self.commit(first, [self.row(2, 2100)])
        earlier = self.state(start=1000, end=3000)
        self.commit(earlier, [self.row(1, 1100)])
        time_filter = self.document(earlier)["filter"]
        for text_key, numeric_key in (("start", "start_ts"), ("end", "end_ts_exclusive")):
            parsed = datetime.strptime(time_filter[text_key], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
            self.assertEqual(int(parsed.timestamp()), time_filter[numeric_key])
        self.assertEqual(time_filter["start_ts"], 1100)
        self.assertEqual(time_filter["end_ts_exclusive"], 2101)
        self.assertFalse(time_filter["end_date_inclusive"])
        self.assertFalse(time_filter["coverage_complete"])


if __name__ == "__main__":
    unittest.main()

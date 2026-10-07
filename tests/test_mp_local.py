"""本机会话链接采集的合成数据库测试，不读取真实微信。"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_sensitive_cache import load_core
from test_sender_mapping import FakeDB

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mp_articles as mp
import mp_local as local


SHORT = "https://mp.weixin.qq.com/s/synthetic_Article-1"
SECOND = "https://mp.weixin.qq.com/s/synthetic_Article-2"
HTML = '<h1 id="activity-name">合成文章</h1><script>var ct="1790812800";</script><div id="js_content"><p>合成正文</p></div>'


class LocalArticleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = load_core()
        self.targets = [{"username": "gh_synthetic", "nick_name": "合成公众号", "remark": ""}]
        self.db = SimpleNamespace(search_contact=Mock(side_effect=lambda keyword: list(self.targets)))
        self.rows = [{"message_content": SHORT, "create_time": 1}]
        self.logs = []
        self.constructed = []

    def construct(self, **kwargs):
        directory = Path(kwargs["workdir"])
        self.constructed.append(directory)
        self.assertTrue((directory / self.core.SENSITIVE_OWNER_MARKER).is_file())
        self.assertEqual(Path(os.environ[self.core.UPSTREAM_KEYS_ENV]), directory / "upstream-stable-keys")
        (directory / "keys.json").write_text("合成密钥，不可导出", encoding="utf-8")
        (directory / "decrypted.db").write_bytes("合成明文库".encode())
        return self.db

    def export(self, names="合成公众号", **options):
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"exporter_core": self.core}))
            stack.enter_context(patch.object(self.core.tempfile, "gettempdir", return_value=str(self.root / "cache")))
            constructor = stack.enter_context(patch.object(self.core, "WeChatDB", side_effect=self.construct))
            loader = stack.enter_context(patch.object(self.core, "load_rows", return_value=self.rows))

            def fetch(url, **kwargs):
                self.assertTrue(all(not directory.exists() for directory in self.constructed))
                return HTML, url

            network = stack.enter_context(patch.object(mp, "_fetch", side_effect=fetch))
            result = local.export_local_articles(names, self.root / "output", progress=self.logs.append, **options)
        return result, constructor, loader, network

    def test_reads_raw_multi_item_xml_plain_text_source_and_packed_links(self):
        self.rows = [
            {"message_content": '<msg><mmreader><category><item><url>' + SHORT + '</url></item><item><url>' + SECOND + '</url></item></category></mmreader></msg>'},
            {"message_content": SHORT + "?pass_ticket=PRIVATE"},
            {"source": '<url>' + SECOND + '</url>'},
            {"packed_info_data": SHORT.encode()},
        ]
        result, constructor, loader, network = self.export()
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(network.call_count, 2)
        loader.assert_called_once_with(self.db, "gh_synthetic")
        self.assertEqual(result["local_coverage"]["local_message_count"], 4)
        self.assertEqual(result["local_coverage"]["article_link_count"], 2)
        self.assertFalse(result["local_coverage"]["full_history_verified"])
        self.assertTrue(result["sensitive_cache_cleanup"])
        self.assertIsNone(result["sensitive_cache_cleanup_error"])
        self.assertTrue(all(not directory.exists() for directory in self.constructed))
        for item in result["results"]:
            self.assertNotIn("PRIVATE", Path(item["json"]).read_text(encoding="utf-8"))
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["local_coverage"], result["local_coverage"])

    def test_message_time_is_not_mistaken_for_article_publish_time(self):
        self.rows = [{"message_content": SHORT, "create_time": 1}]
        result, _, _, _ = self.export(start_time="2026-10-01", end_time="2026-10-01")
        self.assertEqual(len(result["results"]), 1)
        self.assertFalse(result["local_coverage"]["message_time_filter_applied"])

    def test_ambiguous_names_require_exact_id_selection(self):
        self.targets.append({"username": "gh_other", "nick_name": "合成公众号", "remark": ""})
        selected = Mock(return_value="gh_other")
        result, _, loader, _ = self.export(select_account=selected)
        selected.assert_called_once()
        self.assertEqual(selected.call_args.args[1], "合成公众号")
        loader.assert_called_once_with(self.db, "gh_other")
        self.assertEqual(result["local_coverage"]["accounts"][0]["username"], "gh_other")

    def test_missing_selection_fails_one_name_and_continues_to_other_session(self):
        public = [{"username": "gh_a", "nick_name": "同名号", "remark": ""},
                  {"username": "gh_b", "nick_name": "同名号", "remark": ""}]
        personal = [{"username": "wxid_friend", "nick_name": "合成朋友", "remark": ""}]
        self.db.search_contact.side_effect = lambda keyword: public if keyword == "同名号" else personal
        result, _, loader, _ = self.export(["同名号", "合成朋友"])
        loader.assert_called_once_with(self.db, "wxid_friend")
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["local_coverage"]["accounts"][1]["session_kind"], "conversation")
        self.assertIn("本机会话", result["local_coverage"]["scope_description"])

    def test_wrong_selected_id_cannot_read_another_session_and_cancel_is_skipped(self):
        self.targets.append({"username": "gh_other", "nick_name": "合成公众号", "remark": ""})
        result, _, loader, network = self.export(select_account=lambda candidates, keyword: "gh_unrelated")
        loader.assert_not_called()
        network.assert_not_called()
        self.assertIn("不属于", result["failures"][0]["error"])
        result, _, loader, network = self.export(select_account=lambda candidates, keyword: None)
        loader.assert_not_called()
        self.assertFalse(result["failures"])
        self.assertEqual(result["local_coverage"]["accounts"][0]["stop_reason"], "selection_cancelled")

    def test_public_account_id_is_exact_and_aliases_are_read_once(self):
        result, _, loader, _ = self.export("gh_missing")
        loader.assert_not_called()
        self.assertEqual(len(result["failures"]), 1)
        result, _, loader, _ = self.export(["合成公众号", "gh_synthetic"])
        loader.assert_called_once_with(self.db, "gh_synthetic")
        self.assertEqual(result["local_coverage"]["accounts"][1]["stop_reason"], "duplicate_session")

    def test_no_local_links_still_produces_coverage_without_fetching_articles(self):
        self.rows = []
        result, _, _, network = self.export()
        network.assert_not_called()
        self.assertEqual(result["local_coverage"]["article_link_count"], 0)
        self.assertEqual(result["local_coverage"]["accounts"][0]["message_count"], 0)
        self.assertTrue(result["skipped"])
        self.assertTrue(Path(result["report"]).is_file())

    def test_bad_inputs_fail_before_database_construction(self):
        with patch.dict(sys.modules, {"exporter_core": self.core}), patch.object(self.core, "WeChatDB") as constructor:
            for names, options in (([], {}), ([1], {}), (["合成号"], {"start_time": "不是时间"}),
                                   (["a", "b"], {"chat_id": "gh_explicit"})):
                with self.subTest(names=names), self.assertRaises(ValueError):
                    local.export_local_articles(names, self.root, **options)
            constructor.assert_not_called()

    def test_database_failure_and_interrupt_remove_sensitive_workdir(self):
        for failure in (RuntimeError("合成数据库失败"), KeyboardInterrupt()):
            self.constructed = []

            def failed_constructor(**kwargs):
                self.construct(**kwargs)
                raise failure

            previous_temp = tempfile.tempdir
            with patch.dict(sys.modules, {"exporter_core": self.core}), patch.object(self.core.tempfile, "gettempdir", return_value=str(self.root / "cache")), patch.object(self.core, "WeChatDB", side_effect=failed_constructor):
                with self.assertRaises(type(failure)):
                    local.export_local_articles(["合成公众号"], self.root)
            self.assertTrue(all(not directory.exists() for directory in self.constructed))
            self.assertEqual(tempfile.tempdir, previous_temp)

    def test_open_shards_query_only_selected_chat_table(self):
        database_root = self.root / "sqlite"
        database_root.mkdir()
        database = FakeDB(database_root, username="gh_synthetic")
        database.target["nick_name"] = "合成公众号"
        database.target["remark"] = ""
        relative = database.add_shard({2: "gh_synthetic"}, [{"message_content": SHORT}])
        private_table = "Msg_" + hashlib.md5(b"wxid_unrelated").hexdigest()
        with closing(sqlite3.connect(database.shards[relative])) as connection:
            connection.execute("CREATE TABLE " + private_table + " (message_content TEXT)")
            connection.execute("INSERT INTO " + private_table + " VALUES (?)", (SECOND,))
            connection.commit()
        self.db = database
        with patch.dict(sys.modules, {"exporter_core": self.core}), patch.object(self.core.tempfile, "gettempdir", return_value=str(self.root / "cache")), patch.object(self.core, "WeChatDB", side_effect=self.construct), patch.object(mp, "_fetch", side_effect=lambda url, **kwargs: (HTML, url)):
            result = local.export_local_articles("gh_synthetic", self.root / "output")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["url"], SHORT)
        self.assertFalse(any(private_table in query for query in database.queries))
        self.assertTrue(all(not directory.exists() for directory in self.constructed))

    def test_compressed_content_is_scanned_without_losing_xml_items(self):
        import zstandard
        xml = ('<msg><mmreader><item><url>' + SHORT + '</url></item><item><url>' + SECOND + '</url></item></mmreader></msg>').encode()
        self.rows = [{"message_content": None, "compress_content": zstandard.ZstdCompressor().compress(xml)}]
        with patch.object(self.core, "decompress_zstd", side_effect=zstandard.ZstdDecompressor().decompress):
            result, _, _, _ = self.export()
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["local_coverage"]["accounts"][0]["undecodable_fields"], 0)

    def test_sqlite_failure_in_one_account_preserves_the_following_account(self):
        self.db.search_contact.side_effect = lambda keyword: [{"username": keyword, "nick_name": keyword, "remark": ""}]
        with patch.dict(sys.modules, {"exporter_core": self.core}), patch.object(self.core.tempfile, "gettempdir", return_value=str(self.root / "cache")), patch.object(self.core, "WeChatDB", side_effect=self.construct), patch.object(self.core, "load_rows", side_effect=[sqlite3.OperationalError("合成分片缺少字段"), self.rows]) as loader, patch.object(mp, "_fetch", side_effect=lambda url, **kwargs: (HTML, url)):
            result = local.export_local_articles(["gh_broken", "gh_synthetic"], self.root / "output")
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["local_coverage"]["accounts"][0]["stop_reason"], "account_error")
        self.assertEqual(result["local_coverage"]["accounts"][1]["stop_reason"], "local_message_scan_complete")

    def test_sensitive_environment_is_restored_before_public_network_stage(self):
        previous_temp = tempfile.tempdir
        with patch.dict(os.environ, {self.core.UPSTREAM_KEYS_ENV: "合成原有缓存设置"}):
            result, _, _, _ = self.export()
            self.assertEqual(os.environ[self.core.UPSTREAM_KEYS_ENV], "合成原有缓存设置")
        self.assertEqual(tempfile.tempdir, previous_temp)
        self.assertTrue(result["sensitive_cache_cleanup"])
        self.assertNotIn("合成密钥", Path(result["report"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

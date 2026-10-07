"""用合成本机 WeRSS 响应核验公众号历史接入，不连接真实服务。"""

import io
import json
import sys
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from test_sender_mapping import load_export_modules


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mp_articles as articles
import mp_history as history


AK, SK = "SYNTHETIC_ACCESS_KEY", "SYNTHETIC_SECRET_KEY"
NAME = "合成公众号"
FEED = "MP_WXS_SYNTHETIC"
URL = "https://mp.weixin.qq.com/s/synthetic_article"


def account(identifier=FEED, name=NAME):
    return {"id": identifier, "mp_name": name, "mp_intro": "合成账号介绍", "status": 1}


def article(identifier, timestamp, feed=FEED, url=None):
    return {"id": identifier, "mp_id": feed, "title": "相同标题", "publish_time": timestamp,
            "url": url or URL + "_" + identifier, "has_content": 1, "status": 1}


class Response(io.BytesIO):
    status = 200


class HistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core, _ = load_export_modules()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.start = self.core.parse_time_range("2026-10-01", None)["start_ts"]
        self.accounts = {NAME: [account()]}
        self.pages = {FEED: {0: {"list": [article("A", self.start + 30)], "total": 1}}}
        self.details = {}
        self.calls, self.logs, self.forwarded = [], [], None

    def api(self, resource, params=None):
        params = dict(params or {})
        self.calls.append((resource, params))
        if resource == "mps":
            candidates = self.accounts.get(params.get("kw", NAME), [])
            if isinstance(candidates, Exception):
                raise candidates
            offset = params["offset"]
            return {"list": candidates[offset:offset + params["limit"]], "total": len(candidates)}
        if resource == "articles":
            result = self.pages.get(params["mp_id"], {}).get(params["offset"], {"list": [], "total": 999})
            if isinstance(result, Exception):
                raise result
            return result
        identifier = resource.removeprefix("articles/")
        result = self.details.get(identifier)
        if isinstance(result, Exception):
            raise result
        if result is not None:
            return result
        feed = next((feed for feed, pages in self.pages.items()
                     for page in pages.values() if isinstance(page, dict)
                     for item in page["list"] if item["id"] == identifier), FEED)
        return {"id": identifier, "mp_id": feed, "content": "<p>合成缓存正文</p>",
                "title": "相同标题", "author": "合成作者", "status": 1}

    def fake_export(self, urls, out_root, **options):
        self.forwarded = {"urls": list(urls), **options}
        output = Path(out_root) / "mp_articles"
        output.mkdir(parents=True)
        report, index = output / "report.json", output / "articles_index.json"
        document = {"results": [{"url": url} for url in urls], "failures": [], "skipped": []}
        report.write_text(json.dumps(document), encoding="utf-8")
        index.write_text("{}", encoding="utf-8")
        return {**document, "output_dir": str(output), "report": str(report), "index": str(index)}

    def export(self, names=NAME, *, actual_core=False, **options):
        with patch.dict(sys.modules, {"exporter_core": self.core}), \
                patch.object(history.WeRSSClient, "get", side_effect=self.api), \
                patch.object(articles, "_fetch", side_effect=AssertionError("不得读取真实文章")):
            if actual_core:
                return history.export_history(names, self.root, endpoint="http://127.0.0.1:8001",
                                              access_key=AK, secret_key=SK, progress=self.logs.append, **options)
            with patch.object(articles, "export_articles", side_effect=self.fake_export):
                return history.export_history(names, self.root, endpoint="http://127.0.0.1:8001",
                                              access_key=AK, secret_key=SK, progress=self.logs.append, **options)

    def test_loopback_endpoint_and_fixed_prefix(self):
        for endpoint in ("http://127.0.0.1:8001", "http://localhost:8001/", "http://[::1]:8001",
                         "http://127.0.0.1:8001/api/v1/wx/"):
            client = history.WeRSSClient(endpoint, AK, SK)
            self.assertTrue(client.base.endswith(history.API_PREFIX))
        for endpoint in ("http://example.com:8001", "http://127.0.0.2:8001", "http://localhost.evil:8001",
                         "https://localhost:8001", "http://user:secret@localhost:8001", "http://localhost:0",
                         "http://localhost:wrong", "http://localhost:8001?key=secret", "http://localhost:8001#secret",
                         "http://localhost:8001/api/v1", "file:///C:/werss"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                history.WeRSSClient(endpoint, AK, SK)

    def test_credentials_are_validated_without_echoing_values(self):
        for ak, sk in (("", SK), (AK, ""), ("中文私密值", SK), (AK, "SECRET\nVALUE"),
                       ("ACCESS:KEY", SK), (AK, "SECRET VALUE"), (None, SK)):
            with self.subTest(ak=ak), self.assertRaises(ValueError) as context:
                history.WeRSSClient("http://localhost:8001", ak, sk)
            self.assertNotIn("中文私密值", str(context.exception))
            self.assertNotIn("SECRET", str(context.exception))

    def test_http_contract_auth_header_and_response_schema(self):
        client = history.WeRSSClient("http://localhost:8001", AK, SK)
        client.opener = Mock()
        client.opener.open.return_value = Response(json.dumps({"code": 0, "data": {"list": [], "total": 0}}).encode())
        self.assertEqual(client.get("mps", {"kw": NAME, "offset": 0, "limit": 100})["total"], 0)
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "AK-SK " + AK + ":" + SK)
        self.assertTrue(request.full_url.startswith("http://localhost:8001/api/v1/wx/mps?"))
        self.assertNotIn(AK, request.full_url)
        self.assertNotIn(SK, request.full_url)
        for payload in ({"code": True, "data": {}}, {"code": "0", "data": {}}, {"code": 0}, [],
                        {"code": 401, "message": AK + ":" + SK}):
            client.opener.open.return_value = Response(json.dumps(payload).encode())
            with self.subTest(payload=payload), self.assertRaises(RuntimeError) as context:
                client.get("mps")
            self.assertNotIn(AK, str(context.exception))
            self.assertNotIn(SK, str(context.exception))
        client.opener.open.return_value = Response(b"not-json")
        with self.assertRaisesRegex(RuntimeError, "JSON"):
            client.get("mps")

    def test_http_errors_and_truncated_responses_are_safe(self):
        client = history.WeRSSClient("http://localhost:8001", AK, SK)
        client.opener = Mock()
        for code in (201, 302, 401, 403, 500):
            client.opener.open.side_effect = HTTPError(client.base, code, AK + SK, {}, None)
            with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, "HTTP " + str(code)) as context:
                client.get("mps")
            self.assertNotIn(AK, str(context.exception))
        for error in (URLError(AK + ":" + SK), IncompleteRead(b"synthetic")):
            client.opener.open.side_effect = error
            with self.assertRaises(RuntimeError) as context:
                client.get("mps")
            self.assertNotIn(SK, str(context.exception))
        client.opener.open.side_effect = None
        client.opener.open.return_value = Response(b"12345")
        with patch.object(history, "MAX_RESPONSE_BYTES", 4), self.assertRaisesRegex(RuntimeError, "响应超过"):
            client.get("mps")

    def test_proxy_redirect_and_mutating_endpoints_are_disabled(self):
        with patch.object(history, "build_opener") as builder:
            history.WeRSSClient("http://localhost:8001", AK, SK)
        proxy, redirect = builder.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://example.com"))
        client = history.WeRSSClient("http://localhost:8001", AK, SK)
        for resource in ("mps/search/name", "mps/update/id", "articles/id/refresh", "articles/refresh/tasks/id"):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                client.get(resource)

    def test_connection_check_only_reads_account_count(self):
        with patch.object(history.WeRSSClient, "get", side_effect=self.api):
            result = history.check_history_connection("http://localhost:8001", AK, SK)
        self.assertEqual(result["account_count"], 1)
        self.assertEqual(self.calls, [("mps", {"limit": 1, "offset": 0})])
        for value in ({"list": [], "total": True}, {"list": "bad", "total": 0}, {"list": [1], "total": 1}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                history._page(value)

    def test_exact_name_beats_similar_names_and_mapping_is_sanitized(self):
        self.accounts[NAME] = [account("OTHER", NAME + "分号"), account()]
        self.details["A"] = {"id": "A", "mp_id": FEED, "title": "合成题目", "author": "作者",
                             "content": '<p id="js_content" onclick="danger()">缓存正文</p><script>恶意脚本</script>'
                                        '<img data-src="https://mmbiz.qpic.cn/test.png" onerror="danger()">'
                                        '<a href="javascript:danger()">参考链接</a>', "status": 1}
        result = self.export()
        supplied = self.forwarded["articles"][URL + "_A"]
        self.assertEqual(supplied["source"], "werss")
        self.assertEqual(supplied["account"], NAME)
        self.assertEqual(supplied["published_at"], self.start + 30)
        self.assertEqual(supplied["html"].count('id="js_content"'), 1)
        self.assertIn("缓存正文", supplied["html"])
        self.assertIn("data-src", supplied["html"])
        for invalid in ("恶意脚本", "onclick", "onerror", "javascript:"):
            self.assertNotIn(invalid, supplied["html"])
        self.assertEqual(result["history_coverage"]["accounts"][0]["feed_id"], FEED)

    def test_same_name_requires_exact_feed_selection(self):
        self.accounts[NAME] = [account("OTHER"), account()]
        selector = Mock(return_value=FEED)
        result = self.export(select_account=selector)
        candidates, keyword = selector.call_args.args
        self.assertEqual(keyword, NAME)
        self.assertEqual({item["id"] for item in candidates}, {"OTHER", FEED})
        self.assertEqual(result["history_coverage"]["accounts"][0]["feed_id"], FEED)
        self.assertFalse(result["failures"])

    def test_subscription_id_uses_unfiltered_account_pages_and_exact_match(self):
        self.accounts[NAME] = [account("MP_WXS_OTHER", "其它公众号"), account()]
        with patch.object(history, "PAGE_SIZE", 1):
            result = self.export(FEED)
        account_calls = [params for resource, params in self.calls if resource == "mps"]
        self.assertEqual([params["offset"] for params in account_calls], [0, 1])
        self.assertTrue(all("kw" not in params for params in account_calls))
        self.assertEqual(result["history_coverage"]["accounts"][0]["feed_id"], FEED)
        self.assertFalse(result["failures"])

    def test_missing_subscription_id_never_falls_back_to_unrelated_account(self):
        result = self.export("MP_WXS_DOES_NOT_EXIST", select_account=lambda candidates, keyword: FEED)
        self.assertFalse(result["results"])
        self.assertIn("没有找到", result["failures"][0]["error"])
        self.assertFalse(any(resource == "articles" for resource, _ in self.calls))

    def test_cancelled_and_invalid_selection_are_reported(self):
        self.accounts[NAME] = [account("OTHER"), account()]
        result = self.export(select_account=lambda candidates, keyword: None)
        self.assertEqual(result["history_coverage"]["accounts"][0]["stop_reason"], "selection_cancelled")
        self.assertTrue(result["skipped"])
        self.assertFalse(any(resource == "articles" for resource, _ in self.calls))
        self.root = self.root / "other-output"
        result = self.export(select_account=lambda candidates, keyword: "not-a-candidate")
        self.assertIn("不属于", result["failures"][0]["error"])

    def test_account_failure_continues_later_names(self):
        self.accounts["失败账号"] = RuntimeError("授权失败 " + SK)
        result = self.export("失败账号\n" + NAME)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(len(result["history_coverage"]["accounts"]), 2)
        self.assertNotIn(SK, Path(result["report"]).read_text(encoding="utf-8"))
        self.assertNotIn(SK, " ".join(self.logs))

    def test_beijing_date_includes_end_day_and_rejects_unknown_timestamp(self):
        rows = [article("END", self.start + 86400), article("LAST", self.start + 86399),
                article("START", self.start), article("UNKNOWN", None), article("OLD", self.start - 1)]
        self.pages[FEED] = {0: {"list": rows, "total": 999}}
        result = self.export(start_time="2026-10-01", end_time="2026-10-01")
        self.assertEqual(self.forwarded["urls"], [URL + "_LAST", URL + "_START"])
        coverage = result["history_coverage"]["accounts"][0]
        self.assertEqual(coverage["stop_reason"], "start_boundary_reached")
        self.assertEqual(coverage["invalid_publish_time_excluded"], 1)
        self.assertEqual(coverage["earliest_publish_time_seen"], self.start - 1)
        self.assertEqual(coverage["latest_publish_time_seen"], self.start + 86400)
        self.assertFalse(coverage["history_complete"])

    def test_precise_end_is_exclusive_and_same_text_does_not_deduplicate(self):
        self.pages[FEED] = {0: {"list": [article("B", self.start + 60), article("A2", self.start),
                                                 article("A1", self.start)], "total": 3}}
        self.export(start_time="2026-10-01 00:00:00", end_time="2026-10-01 00:01:00")
        self.assertEqual(self.forwarded["urls"], [URL + "_A2", URL + "_A1"])

    def test_pagination_overlap_is_deduplicated_by_id_and_repetition_stops(self):
        a, b = article("A", self.start + 20), article("B", self.start + 10)
        self.pages[FEED] = {0: {"list": [a, b], "total": 99}, 2: {"list": [a, b], "total": 99}}
        result = self.export()
        coverage = result["history_coverage"]["accounts"][0]
        self.assertEqual(coverage["stop_reason"], "no_new_articles")
        self.assertEqual(coverage["pages_read"], 2)
        self.assertEqual(coverage["articles_seen"], 2)
        pages = [params for resource, params in self.calls if resource == "articles"]
        self.assertEqual([params["offset"] for params in pages], [0, 2])
        self.assertTrue(all(params["mp_id"] == FEED and params["limit"] == 100 for params in pages))

    def test_unsorted_publish_times_disable_early_boundary_stop(self):
        self.pages[FEED] = {0: {"list": [article("OLD", self.start - 1), article("A", self.start + 30)], "total": 3},
                            2: {"list": [article("B", self.start + 10)], "total": 3}}
        result = self.export(start_time="2026-10-01")
        self.assertEqual(self.forwarded["urls"], [URL + "_A", URL + "_B"])
        coverage = result["history_coverage"]["accounts"][0]
        self.assertEqual(coverage["warnings"], ["unstable_publish_order"])
        self.assertEqual(coverage["pages_read"], 2)

    def test_page_limit_is_recorded_without_complete_history_claim(self):
        self.pages[FEED][0]["total"] = 99
        with patch.object(history, "MAX_HISTORY_PAGES", 1):
            result = self.export()
        coverage = result["history_coverage"]
        self.assertEqual(coverage["accounts"][0]["stop_reason"], "page_limit")
        self.assertFalse(coverage["full_history_verified"])
        self.assertIn("上限", coverage["accounts"][0]["stop_description"])

    def test_middle_page_error_preserves_valid_previous_page(self):
        self.pages[FEED] = {0: {"list": [article("A", self.start)], "total": 99},
                            1: RuntimeError("合成中途失败 " + AK + ":" + SK)}
        result = self.export()
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["history_coverage"]["accounts"][0]["stop_reason"], "api_error")
        self.assertEqual(len(result["failures"]), 1)
        serialized = Path(result["report"]).read_text(encoding="utf-8")
        self.assertNotIn(AK, serialized)
        self.assertNotIn(SK, serialized)

    def test_other_feed_record_is_rejected_before_reading_body(self):
        self.pages[FEED] = {0: {"list": [article("A", self.start, feed="wrong-feed")], "total": 1}}
        result = self.export()
        self.assertFalse(result["results"])
        self.assertIn("其它公众号", result["failures"][0]["error"])
        self.assertFalse(any(resource.startswith("articles/") for resource, _ in self.calls))

    def test_missing_deleted_or_failed_body_falls_back_to_public_url(self):
        rows = [article("EMPTY", self.start + 30), article("DELETED", self.start + 20), article("FAILED", self.start + 10)]
        self.pages[FEED] = {0: {"list": rows, "total": 3}}
        self.details = {"EMPTY": {"id": "EMPTY", "mp_id": FEED, "content": " ", "status": 1},
                        "DELETED": {"id": "DELETED", "mp_id": FEED, "content": "DELETED", "status": 1000},
                        "FAILED": RuntimeError("合成详情失败")}
        result = self.export()
        self.assertEqual(self.forwarded["articles"], {})
        self.assertEqual(len(self.forwarded["urls"]), 3)
        reasons = [item["reason"] for item in result["history_coverage"]["accounts"][0]["body_fallbacks"]]
        self.assertTrue(any("没有已有正文" in reason for reason in reasons))
        self.assertTrue(any("删除" in reason for reason in reasons))
        self.assertTrue(any("详情读取失败" in reason for reason in reasons))
        self.assertTrue(all(params == {"content": "true"} for resource, params in self.calls if resource.startswith("articles/")))

    def test_duplicate_names_and_feeds_are_not_requeried(self):
        self.accounts["别名"] = [account()]
        result = self.export([NAME, NAME, "别名"])
        self.assertEqual(len(result["history_coverage"]["accounts"]), 2)
        self.assertEqual(sum(resource == "articles" for resource, _ in self.calls), 1)
        self.assertEqual(result["history_coverage"]["accounts"][1]["stop_reason"], "duplicate_account")

    def test_unknown_times_remain_unknown_without_time_filter(self):
        self.pages[FEED] = {0: {"list": [article("A", None)], "total": 1}}
        result = self.export()
        self.assertIsNone(self.forwarded["articles"][URL + "_A"]["published_at"])
        self.assertIsNone(result["history_coverage"]["accounts"][0]["earliest_publish_time_seen"])
        for invalid in (True, "not-a-date", "1790812800000", -1, 1.5):
            self.assertIsNone(history._publish_time(invalid))

    def test_report_persists_coverage_even_for_empty_history(self):
        self.accounts[NAME] = []
        result = self.export()
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["history_coverage"], result["history_coverage"])
        self.assertEqual(report["failures"], result["failures"])
        self.assertFalse(report["history_coverage"]["full_history_verified"])
        self.assertEqual(self.forwarded["urls"], [])
        self.assertTrue(Path(result["index"]).is_file())

    def test_cached_body_integrates_real_three_format_export_and_media_notes(self):
        self.details["A"] = {"id": "A", "mp_id": FEED, "title": "合成集成标题", "author": "合成作者",
                             "content": '<h2>正文小标题</h2><p>实际缓存文字</p><script>危险内容</script>'
                                        '<audio src="https://example.com/voice.mp3"></audio>'
                                        '<iframe data-src="https://example.com/video"></iframe>', "status": 1}
        result = self.export(actual_core=True, export_images=False, incremental=False)
        self.assertFalse(result["failures"])
        item = result["results"][0]
        document = json.loads(Path(item["json"]).read_text(encoding="utf-8"))
        self.assertEqual(document["source"], "werss")
        self.assertEqual(document["account"], NAME)
        self.assertEqual(document["published_at"], "2026-10-01T00:00:30+08:00")
        self.assertEqual({media["type"] for media in document["media"]}, {"audio", "iframe"})
        for format_name in ("txt", "md", "json"):
            saved = Path(item[format_name]).read_text(encoding="utf-8")
            self.assertIn("实际缓存文字", saved)
            self.assertNotIn("危险内容", saved)
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["history_coverage"]["accounts"][0]["selected_articles"], 1)


if __name__ == "__main__":
    unittest.main()

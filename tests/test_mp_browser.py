"""独立浏览器采集的合成 CDP、人工确认与缓存集成测试，不启动浏览器。"""

import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_sender_mapping import load_export_modules


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mp_browser as browser
import mp_articles as articles


SHORT = "https://mp.weixin.qq.com/s/synthetic_browser_article"
OTHER = "https://mp.weixin.qq.com/s/other_browser_article"
LONG = "https://mp.weixin.qq.com/s?__biz=MzA%3D%3D&mid=123&idx=1&sn=abc"


def snapshot(url=SHORT, body="<p>合成浏览器正文</p>", ct="1790784030", published_text=None):
    return {"url": url, "stage": "article", "body_html": body, "body_text": "合成浏览器正文",
            "media_count": 0, "title": "合成浏览器标题", "account": "合成公众号",
            "author": "合成作者", "ct": ct, "published_text": published_text}


class FakeSocket:
    def __init__(self):
        self.calls, self.responses = [], []
        self.url, self.closed = "about:blank", False

    def send(self, raw):
        command = json.loads(raw)
        self.calls.append(command)
        method = command["method"]
        if method == "Target.createTarget":
            result = {"targetId": "OWN_TARGET"}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "OWN_SESSION"}
        elif method == "Page.navigate":
            self.url = command["params"]["url"]
            result = {"frameId": "OWN_FRAME"}
        elif method == "Runtime.evaluate":
            value = snapshot(self.url) if self.url != "about:blank" else {"url": "about:blank", "stage": "other_page"}
            result = {"result": {"type": "object", "value": value}}
        else:
            result = {}
        # 事件不得误当成本次命令响应。
        self.responses.extend(({"method": "Page.frameNavigated", "params": {}}, {"id": command["id"], "result": result}))

    def recv(self):
        return json.dumps(self.responses.pop(0))

    def settimeout(self, value):
        self.timeout = value

    def close(self, **options):
        self.closed = True


class FakeSession:
    def __init__(self, waits=None, start_error=None, cleanup=None):
        self.waits = list(waits or [(snapshot(), None)])
        self.start_error = start_error
        self.cleanup = cleanup or {"cleanup_ok": True, "cleanup_path": None}
        self.navigations, self.closed = [], False

    def start(self):
        if self.start_error:
            raise self.start_error

    def navigate(self, url):
        self.navigations.append(url)

    def wait_article(self):
        return self.waits.pop(0)

    def close(self):
        self.closed = True
        return self.cleanup


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core, _ = load_export_modules()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.managed = self.root / "managed"
        self.managed.mkdir()
        self.socket = FakeSocket()
        self.websocket = SimpleNamespace(create_connection=Mock(return_value=self.socket))
        self.process = Mock()
        self.process.wait.return_value = 0
        self.popen_calls = []

    def popen(self, args, **options):
        self.popen_calls.append((args, options))
        profile = Path(next(value.split("=", 1)[1] for value in args if value.startswith("--user-data-dir=")))
        (profile / "DevToolsActivePort").write_text("34567\n/devtools/browser/OWN_BROWSER\n", encoding="utf-8")
        return self.process

    def launch(self):
        session = browser._BrowserSession()
        with patch.dict(sys.modules, {"websocket": self.websocket}), \
                patch.object(browser, "_find_browser", return_value="C:/synthetic/msedge.exe"), \
                patch.object(browser, "_managed_root", return_value=self.managed), \
                patch.object(browser.subprocess, "Popen", side_effect=self.popen):
            session.start()
        return session

    def export(self, session=None, urls=None, **options):
        session = session or FakeSession()
        with patch.dict(sys.modules, {"exporter_core": self.core}), \
                patch.object(browser, "_BrowserSession", return_value=session), \
                patch.object(articles, "_fetch", side_effect=AssertionError("不得连接真实网页")):
            result = browser.export_browser_articles(urls or [SHORT], self.root / "output", export_images=False, **options)
        return result, session

    def test_launch_uses_fresh_profile_dynamic_port_and_guarded_handshake(self):
        session = self.launch()
        args, options = self.popen_calls[0]
        self.assertIn("--remote-debugging-port=0", args)
        self.assertEqual(Path(args[1].split("=", 1)[1]).parent, self.managed)
        self.assertNotIn("--remote-allow-origins=*", args)
        self.assertNotIn("shell", options)
        address = self.websocket.create_connection.call_args.args[0]
        self.assertEqual(address, "ws://127.0.0.1:34567/devtools/browser/OWN_BROWSER")
        connection = self.websocket.create_connection.call_args.kwargs
        self.assertTrue(connection["suppress_origin"])
        self.assertEqual(connection["redirect_limit"], 0)
        self.assertEqual(connection["http_no_proxy"], ["127.0.0.1", "localhost", "::1"])
        self.assertNotIn("cookie", connection)
        profile = session.profile
        cleanup = session.close()
        self.assertTrue(cleanup["cleanup_ok"])
        self.assertFalse(profile.exists())
        self.assertTrue(self.socket.closed)
        self.assertEqual(self.socket.calls[-1]["method"], "Browser.close")
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()

    def test_debugger_url_only_allows_matching_loopback_browser_endpoint(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            browser._validate_debugger_url(f"ws://{host}:34567/devtools/browser/OWN_BROWSER", 34567)
        for url in ("ws://example.com:34567/devtools/browser/OWN_BROWSER", "wss://localhost:34567/devtools/browser/OWN_BROWSER",
                    "ws://localhost:34568/devtools/browser/OWN_BROWSER", "ws://localhost:34567/devtools/page/OTHER_TARGET",
                    "ws://secret@localhost:34567/devtools/browser/OWN_BROWSER", "ws://localhost:34567/devtools/browser/OWN_BROWSER?token=secret"):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                browser._validate_debugger_url(url, 34567)

    def test_websocket_failure_is_generic_and_never_follows_redirects(self):
        self.websocket.create_connection.side_effect = OSError("SECRET_COOKIE_OR_HEADER")
        session = browser._BrowserSession()
        with patch.dict(sys.modules, {"websocket": self.websocket}), \
                patch.object(browser, "_find_browser", return_value="C:/synthetic/chrome.exe"), \
                patch.object(browser, "_managed_root", return_value=self.managed), \
                patch.object(browser.subprocess, "Popen", side_effect=self.popen), self.assertRaises(RuntimeError) as context:
            session.start()
        self.assertNotIn("SECRET", str(context.exception))
        self.assertIn("HTML/MHTML", str(context.exception))
        self.assertEqual(self.websocket.create_connection.call_count, 2)
        self.assertTrue(all(call.kwargs["redirect_limit"] == 0 for call in self.websocket.create_connection.call_args_list))
        self.assertTrue(session.close()["cleanup_ok"])

    def test_every_page_command_uses_owned_target_session(self):
        session = self.launch()
        session.navigate(SHORT)
        self.assertEqual(session.snapshot()["url"], SHORT)
        page_commands = [call for call in self.socket.calls if call["method"] in {"Page.navigate", "Runtime.evaluate"}]
        self.assertTrue(all(call["sessionId"] == "OWN_SESSION" for call in page_commands))
        attached = next(call for call in self.socket.calls if call["method"] == "Target.attachToTarget")
        self.assertEqual(attached["params"], {"targetId": "OWN_TARGET", "flatten": True})
        before = len(self.socket.calls)
        with self.assertRaisesRegex(RuntimeError, "其它浏览器页"):
            session.command("Target.attachToTarget", {"targetId": "OTHER_TARGET"})
        with self.assertRaises(RuntimeError):
            session.command("Network.getAllCookies")
        self.assertEqual(len(self.socket.calls), before)
        session.close()

    def test_second_navigation_confirms_blank_before_loading_next_article(self):
        session = self.launch()
        session.navigate(SHORT)
        session.navigate(OTHER)
        navigated = [call["params"]["url"] for call in self.socket.calls if call["method"] == "Page.navigate"]
        self.assertEqual(navigated, [SHORT, "about:blank", OTHER])
        self.assertEqual(session.snapshot()["url"], OTHER)
        session.close()

    def test_failed_blank_switch_cannot_reuse_previous_body(self):
        session = browser._BrowserSession()
        session.navigated = True
        session.command = Mock(return_value={})
        session.snapshot = Mock(return_value=snapshot(SHORT))
        with patch.object(browser.time, "monotonic", side_effect=itertools.count()), \
                patch.object(browser.time, "sleep"), self.assertRaisesRegex(RuntimeError, "误归"):
            session.navigate(OTHER)
        self.assertEqual(session.command.call_args.args, ("Page.navigate", {"url": "about:blank"}))

    def test_runtime_exception_and_invalid_schema_are_not_article_success(self):
        session = browser._BrowserSession()
        session.command = Mock(return_value={"exceptionDetails": {"text": "synthetic"}})
        with self.assertRaisesRegex(RuntimeError, "无法读取正文"):
            session.snapshot()
        session.command.return_value = {"result": {"value": "not-an-object"}}
        with self.assertRaises(RuntimeError):
            session.snapshot()

    def test_complete_dom_expression_executes_in_real_javascript_engine(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("当前测试环境没有 Node.js，完整脚本执行检查需 JavaScript 引擎。")
        script = "const expression=" + json.dumps(browser.DOM_EXPRESSION) + r""";
const assert = require('node:assert/strict'), vm = require('node:vm');
function capture(url, options = {}) {
  const queries = [];
  const body = {innerHTML: '<p>合成正文</p>', innerText: '合成正文', querySelectorAll: () => [1]};
  const document = {title: '合成标题', body: {innerText: options.screen || ''},
    querySelector: selector => {
      queries.push(selector);
      if (selector === '#js_content') return options.missing ? null : body;
      return {textContent: selector === '#publish_time' ? '2026年10月7日 08:55' : '合成公开元数据'};
    }};
  const getComputedStyle = element => ({display: options.hidden ? 'none' : 'block', visibility: 'visible'});
  const context = vm.createContext({URL, location: {href: url}, document, getComputedStyle, window: {ct: '1790784030'}});
  if (options.lexicalClock) vm.runInContext("let ct = '1790784040'", context);
  const value = vm.runInContext(expression, context);
  return {value, queries};
}
assert.equal(capture('https://mp.weixin.qq.com/s/SYNTHETIC_SHORT_ID').value.stage, 'article');
assert.equal(capture('https://mp.weixin.qq.com/s?__biz=test&mid=123&idx=1').value.body_html, '<p>合成正文</p>');
assert.equal(capture('https://mp.weixin.qq.com/s/article').value.published_text, '2026年10月7日 08:55');
assert.equal(capture('https://mp.weixin.qq.com/s/article', {lexicalClock: true}).value.ct, '1790784040');
assert.equal(capture('https://mp.weixin.qq.com/s/article', {hidden: true}).value.stage, 'loading');
assert.equal(capture('https://mp.weixin.qq.com/s/article', {hidden: true}).value.body_html, undefined);
for (const url of ['https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?token=PRIVATE', 'https://example.com/private', 'about:blank']) {
  const result = capture(url);
  assert.notEqual(result.value.stage, 'article');
  assert.equal(result.queries.length, 0);
  assert.equal(result.value.body_html, undefined);
}
assert.equal(capture('https://mp.weixin.qq.com/s/article', {missing: true, screen: '环境异常，请完成验证'}).value.stage, 'verification');
console.log('合成 DOM 执行通过');
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("合成 DOM 执行通过", result.stdout)

    def test_wait_article_accepts_dom_body_and_normalizes_actual_url(self):
        session = browser._BrowserSession()
        session.snapshot = Mock(return_value=snapshot(LONG + "&wxtoken=PRIVATE"))
        value, reason = session.wait_article()
        self.assertIsNone(reason)
        self.assertEqual(value["url"], articles.normalize_article_url(LONG))
        self.assertNotIn("PRIVATE", value["url"])

    def test_verification_and_empty_body_never_return_a_capture(self):
        session = browser._BrowserSession()
        for value in ({"url": SHORT, "stage": "verification"}, snapshot(body="")):
            session.snapshot = Mock(return_value=value)
            with patch.object(browser.time, "monotonic", side_effect=itertools.count()), patch.object(browser.time, "sleep"):
                captured, reason = session.wait_article()
            self.assertIsNone(captured)
            self.assertTrue(reason)

    def test_busy_browser_preserves_managed_profile_and_reports_cleanup_reason(self):
        session = self.launch()
        profile = session.profile
        self.process.wait.side_effect = subprocess.TimeoutExpired("synthetic-browser", 5)
        cleanup = session.close()
        self.assertFalse(cleanup["cleanup_ok"])
        self.assertEqual(cleanup["cleanup_path"], str(profile))
        self.assertIn("仍在运行", cleanup["cleanup_reason"])
        self.assertTrue(profile.exists())
        self.process.terminate.assert_not_called()

    def test_cleanup_refuses_a_directory_outside_managed_root(self):
        session = browser._BrowserSession()
        session.root = self.managed
        session.profile = self.root / "unmanaged"
        session.profile.mkdir()
        with patch.object(browser.shutil, "rmtree") as remove:
            cleanup = session.close()
        self.assertFalse(cleanup["cleanup_ok"])
        remove.assert_not_called()

    def test_profile_cleanup_failure_preserves_explicit_path(self):
        session = self.launch()
        with patch.object(browser.shutil, "rmtree", side_effect=PermissionError("synthetic lock")), patch.object(browser.time, "sleep"):
            cleanup = session.close()
        self.assertFalse(cleanup["cleanup_ok"])
        self.assertTrue(Path(cleanup["cleanup_path"]).is_dir())
        self.assertTrue(cleanup["cleanup_reason"])

    def test_dom_cache_exports_all_formats_without_refetching_body(self):
        result, session = self.export()
        self.assertFalse(result["failures"])
        item = result["results"][0]
        document = json.loads(Path(item["json"]).read_text(encoding="utf-8"))
        self.assertEqual(document["source"], "browser_page")
        self.assertEqual(document["account"], "合成公众号")
        for key in ("txt", "md", "json"):
            self.assertIn("合成浏览器正文", Path(item[key]).read_text(encoding="utf-8"))
        self.assertTrue(session.closed)
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["browser_coverage"], result["browser_coverage"])

    def test_manual_verification_continue_reads_same_browser_page(self):
        session = FakeSession(waits=[(None, "需要人工验证"), (snapshot(), None)])
        confirm = Mock(return_value=True)
        result, session = self.export(session, wait_for_user=confirm)
        self.assertEqual(confirm.call_args.args, (SHORT, "需要人工验证"))
        self.assertEqual(session.navigations, [SHORT])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["browser_coverage"]["captures"][0]["manual_confirmations"], 1)

    def test_manual_skip_continues_next_url(self):
        session = FakeSession(waits=[(None, "需要人工验证"), (snapshot(OTHER), None)])
        result, _ = self.export(session, urls=[SHORT, OTHER], wait_for_user=lambda url, reason: False)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["url"], OTHER)
        self.assertEqual(result["skipped"][0]["url"], SHORT)
        self.assertFalse(result["failures"])

    def test_repeated_verification_has_finite_manual_attempts_and_no_success_file(self):
        confirm = Mock(return_value=True)
        session = FakeSession(waits=[(None, "仍需人工验证")] * 3)
        result, _ = self.export(session, wait_for_user=confirm)
        self.assertEqual(confirm.call_count, 2)
        self.assertFalse(result["results"])
        self.assertEqual(result["failures"][0]["url"], SHORT)
        self.assertFalse(list(Path(result["output_dir"]).glob("*/article.json")))

    def test_requested_to_actual_url_mapping_avoids_wrong_article_identity(self):
        session = FakeSession(waits=[(snapshot(LONG), None)])
        result, _ = self.export(session)
        actual = articles.normalize_article_url(LONG)
        self.assertEqual(result["results"][0]["url"], actual)
        capture = result["browser_coverage"]["captures"][0]
        self.assertEqual((capture["requested_url"], capture["actual_url"]), (SHORT, actual))
        self.assertEqual(result["results"][0]["article_id"], articles._article_id(actual))

    def test_multiple_requests_for_same_actual_page_are_not_saved_twice(self):
        session = FakeSession(waits=[(snapshot(LONG), None), (snapshot(LONG), None)])
        result, _ = self.export(session, urls=[SHORT, OTHER])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["browser_coverage"]["captures"][1]["status"], "duplicate_actual")
        self.assertEqual(result["skipped"][0]["url"], OTHER)

    def test_startup_error_recommends_html_and_still_creates_failure_report(self):
        session = FakeSession(start_error=RuntimeError("调试接口不可用。" + browser.FALLBACK_NOTE))
        result, _ = self.export(session)
        self.assertFalse(result["results"])
        self.assertIn("HTML/MHTML", result["failures"][0]["error"])
        self.assertEqual(result["browser_coverage"]["captures"][0]["status"], "startup_failed")
        self.assertTrue(session.closed)
        self.assertTrue(Path(result["report"]).is_file())

    def test_unreasonable_ct_stays_unknown_and_time_filter_skips_it(self):
        for value in (None, True, "1790784030000", "private-cookie", -1):
            _, cache = browser._cache_from_snapshot(snapshot(ct=value))
            self.assertIsNone(cache["published_at"])
        result, _ = self.export(FakeSession(waits=[(snapshot(ct=None), None)]), start_time="2026-10-01")
        self.assertFalse(result["results"])
        self.assertIn("发布时间未知", result["skipped"][0]["reason"])

    def test_chinese_dom_time_with_unpadded_date_keeps_real_hour_and_minute(self):
        for text in ("2026年10月7日 08:55", "2026年10月7日08:55"):
            _, cache = browser._cache_from_snapshot(snapshot(ct=None, published_text=text))
            self.assertEqual(cache["published_at"], "2026-10-07T08:55:00+08:00")

    def test_cleanup_state_is_merged_into_output_report(self):
        cleanup = {"cleanup_ok": False, "cleanup_path": "C:/synthetic/managed/browser-own", "cleanup_reason": "合成占用"}
        result, _ = self.export(FakeSession(cleanup=cleanup))
        for key, value in cleanup.items():
            self.assertEqual(result["browser_coverage"][key], value)
        saved = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["browser_coverage"]["cleanup_path"], cleanup["cleanup_path"])


if __name__ == "__main__":
    unittest.main()

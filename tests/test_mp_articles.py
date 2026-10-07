"""公开文章的合成 HTML、图片及网络响应测试，不读取真实聊天。"""

import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mp_articles as mp


LONG = "https://mp.weixin.qq.com/s?__biz=MzA%3D%3D&mid=123&idx=1&sn=abc"
SHORT = "https://mp.weixin.qq.com/s/short_Article-1"
OTHER = "https://mp.weixin.qq.com/s/other_Article-2"
PICTURE = "https://mmbiz.qpic.cn/mmbiz_png/image/0?wx_fmt=png"


def page(body="<p>合成正文</p>", *, timestamp="1790812800", canonical=None):
    extra = f'<meta property="og:url" content="{canonical}">' if canonical else ""
    clock = f'<script>var ct = "{timestamp}"; window.hidden = "不得进入JSON";</script>' if timestamp else ""
    return '<html><head>' + extra + '</head><body><h1 id="activity-name">合成文章</h1><a id="js_name">合成公众号</a><span id="js_author_name">合成作者</span>' + clock + '<div id="js_content">' + body + '</div></body></html>'


def png():
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(output, format="PNG")
    return output.getvalue()


class Response(io.BytesIO):
    def __init__(self, data, url=SHORT, content_type="text/html"):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Type": content_type}

    def geturl(self):
        return self.url


class ArticleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def export(self, html=page(), urls=(SHORT,), **options):
        with patch.object(mp, "_fetch", side_effect=lambda url, **kwargs: (html, url)):
            return mp.export_articles(urls, self.root, **options)

    def document(self, result):
        return json.loads(Path(result["results"][0]["json"]).read_text(encoding="utf-8"))

    def test_link_extraction_from_chat_json_and_tracking_deduplication(self):
        text = {"messages": [{"content": LONG + "&amp;pass_ticket=PRIVATE"},
                             {"raw": LONG + "&scene=21"}, {"content": SHORT + "?wxtoken=PRIVATE"}]}
        expected = [mp.normalize_article_url(LONG), SHORT]
        self.assertEqual(mp.extract_article_links(text), expected)
        self.assertEqual(mp.extract_article_links(json.dumps(text)), expected)
        self.assertEqual(mp.extract_article_links("链接（" + LONG + "）。"), expected[:1])
        self.assertNotIn("PRIVATE", " ".join(expected))

    def test_url_validation_rejects_local_and_unrelated_addresses(self):
        invalid = ["file:///C:/chat.json", "http://127.0.0.1/s/test", "https://mp.weixin.qq.com.evil/s/a",
                   "https://user:SECRET@mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com:444/s/a",
                   "https://mp.weixin.qq.com/mp/login", "https://mp.weixin.qq.com/s?mid=1&idx=1",
                   "https://mp.weixin.qq.com/s?__biz=abc&mid=wrong&idx=1"]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                mp.normalize_article_url(url)
        self.assertEqual(mp.normalize_article_url("http://mp.weixin.qq.com/s/abc?appmsg_token=SECRET#rd"), "https://mp.weixin.qq.com/s/abc")

    def test_article_id_uses_biz_mid_idx_and_short_token(self):
        self.assertEqual(mp._article_id(LONG), mp._article_id(LONG.replace("sn=abc", "sn=different") + "&scene=21"))
        self.assertNotEqual(mp._article_id(LONG), mp._article_id(LONG.replace("idx=1", "idx=2")))
        self.assertNotEqual(mp._article_id(LONG), mp._article_id(SHORT))

    def test_export_saves_three_formats_and_known_script_metadata(self):
        result = self.export(page('<h2>正文小标题</h2><p>段落文字</p><script>恶意脚本正文</script><style>隐藏样式</style>'))
        self.assertFalse(result["failures"])
        document = self.document(result)
        self.assertEqual(document["account"], "合成公众号")
        self.assertEqual(document["author"], "合成作者")
        self.assertEqual(document["published_at"], "2026-10-01T08:00:00+08:00")
        self.assertEqual(document["source"], "public_url")
        self.assertIn("## 正文小标题", Path(result["results"][0]["md"]).read_text(encoding="utf-8"))
        self.assertTrue(Path(result["index"]).is_file())
        self.assertTrue(Path(result["report"]).is_file())
        for key in ("txt", "md", "json"):
            self.assertEqual(Path(result["results"][0][key]).name, "合成文章." + key)
        for path in (result["results"][0][key] for key in ("txt", "md", "json")):
            saved = Path(path).read_text(encoding="utf-8")
            self.assertNotIn("不得进入JSON", saved)
            self.assertNotIn("恶意脚本正文", saved)
            self.assertNotIn("隐藏样式", saved)

    def test_original_badge_is_not_mistaken_for_author(self):
        html = page().replace('<span id="js_author_name">', '<span class="rich_media_meta_text">原创</span><span id="js_author_name">')
        self.assertEqual(self.document(self.export(html))["author"], "合成作者")

    def test_title_filenames_are_safe_and_same_titles_remain_separate(self):
        html = page().replace("合成文章</h1>", 'CON.标题/非法:*?</h1>')
        result = self.export(html, urls=(SHORT, OTHER))
        self.assertFalse(result["failures"])
        self.assertEqual(len(result["results"]), 2)
        for article in result["results"]:
            for key in ("txt", "md", "json"):
                self.assertEqual(Path(article[key]).name, "_CON.标题_非法___." + key)
                self.assertTrue(Path(article[key]).is_file())
        self.assertNotEqual(result["results"][0]["output_dir"], result["results"][1]["output_dir"])

    def test_long_emoji_title_has_usable_filenames_and_preserves_original_title(self):
        title = "长标题😀" * 70
        result = self.export(page().replace("合成文章</h1>", title + "</h1>"))
        self.assertFalse(result["failures"])
        self.assertEqual(self.document(result)["title"], title)
        for key in ("txt", "md", "json"):
            path = Path(result["results"][0][key])
            self.assertLessEqual(len(path.stem.encode("utf-16-le")) // 2, 80)
            self.assertTrue(path.is_file())

    def test_incremental_legacy_names_migrate_without_fetch_and_update_index(self):
        first = self.export()
        article = first["results"][0]
        directory = Path(article["output_dir"])
        old_content = {}
        for key in ("txt", "md", "json"):
            path = Path(article[key])
            old_content[key] = path.read_bytes()
            path.rename(directory / f"article.{key}")
        with patch.object(mp, "_fetch") as fetch:
            result = mp.export_articles([SHORT], self.root)
        fetch.assert_not_called()
        migrated = result["results"][0]
        self.assertTrue(migrated["cached"])
        for key in ("txt", "md", "json"):
            self.assertEqual(Path(migrated[key]).name, "合成文章." + key)
            self.assertEqual(Path(migrated[key]).read_bytes(), old_content[key])
            self.assertFalse((directory / f"article.{key}").exists())
        index = json.loads(Path(result["index"]).read_text(encoding="utf-8"))
        self.assertEqual(index["articles"][migrated["article_id"]]["json"], migrated["json"])
        with patch.object(mp, "_fetch") as fetch:
            repeated = mp.export_articles([SHORT], self.root)
        fetch.assert_not_called()
        self.assertTrue(repeated["results"][0]["cached"])

    def test_legacy_name_migration_failure_restores_files(self):
        first = self.export()
        article = first["results"][0]
        directory = Path(article["output_dir"])
        for key in ("txt", "md", "json"):
            Path(article[key]).rename(directory / f"article.{key}")
        original = Path.rename
        calls = 0

        def failing_rename(path, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("合成改名失败")
            return original(path, target)

        with patch.object(Path, "rename", autospec=True, side_effect=failing_rename), patch.object(mp, "_fetch") as fetch:
            result = mp.export_articles([SHORT], self.root)
        fetch.assert_not_called()
        self.assertFalse(result["results"])
        self.assertEqual(result["failures"][0]["error"], "合成改名失败")
        for key in ("txt", "md", "json"):
            self.assertTrue((directory / f"article.{key}").is_file())
            self.assertFalse((directory / f"合成文章.{key}").exists())

    def test_inline_text_br_paragraphs_and_code_indentation(self):
        html = page('<p>公开<span>722</span>篇<strong>论文</strong><br>第二行</p>'
                    '<p>下一段</p><pre>if ready:\n    print(1)\n\n    print(2)</pre>')
        result = self.export(html)
        document = self.document(result)
        self.assertIn("公开722篇论文\n第二行", document["text"])
        self.assertIn("第二行\n\n下一段", document["text"])
        self.assertIn("if ready:\n    print(1)\n\n    print(2)", document["text"])
        self.assertEqual(document["text_layout_version"], 1)

    def test_image_markers_stay_between_their_paragraphs_without_append_duplicate(self):
        html = page(f'<p>图片之前</p><p><img src="{PICTURE}"></p><p>图片之后</p>')
        with patch.object(mp, "_fetch", side_effect=lambda url, **kw: (png(), url) if kw.get("image") else (html, url)):
            result = mp.export_articles([SHORT], self.root)
        document = self.document(result)
        for text in (document["text"], Path(result["results"][0]["txt"]).read_text(encoding="utf-8")):
            self.assertEqual(text.count("[图片：images/001.png]"), 1)
            self.assertLess(text.index("图片之前"), text.index("[图片：images/001.png]"))
            self.assertLess(text.index("[图片：images/001.png]"), text.index("图片之后"))

    def test_pre_nested_spans_and_br_keep_code_lines_and_nonbreaking_indent(self):
        html = page('<p>代码如下</p><pre><span>if ready:</span><span><br></span>'
                    '<span>&nbsp;&nbsp;&nbsp;&nbsp;print(1)</span><span><br></span>'
                    '<span>  print(2)</span></pre><p>代码之后</p>')
        result = self.export(html)
        document = self.document(result)
        self.assertIn("if ready:\n    print(1)\n  print(2)", document["text"])
        self.assertIn("代码之后", document["text"])

    def test_pre_preserves_consecutive_blank_lines_and_trailing_spaces(self):
        code = "first  \n\n\n\n    second \t\n"
        result = self.export(page("<p>代码之前</p><pre>" + code + "</pre><p>代码之后</p>"))
        self.assertIn(code, self.document(result)["text"])
        self.assertIn(code, Path(result["results"][0]["txt"]).read_text(encoding="utf-8"))

    def test_chinese_publication_date_without_leading_zero_filters_correctly(self):
        html = page(timestamp=None).replace('<div id="js_content">', '<span id="publish_time">2026年10月7日 12:27</span><div id="js_content">')
        result = self.export(html, start_time="2026-10-07", end_time="2026-10-07")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(self.document(result)["published_at"], "2026-10-07T12:27:00+08:00")

    def test_empty_batch_still_has_report_and_index(self):
        with patch.object(mp, "_fetch") as fetch:
            result = mp.export_articles([], self.root)
        fetch.assert_not_called()
        self.assertEqual(result["results"], [])
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["skipped"], [])
        self.assertTrue(Path(result["report"]).is_file())
        self.assertTrue(Path(result["index"]).is_file())

    def test_images_are_saved_locally_and_unavailable_images_have_reasons(self):
        html = page(f'<p>图片正文</p><img data-src="{PICTURE}&amp;pass_ticket=PRIVATE"><img src="http://127.0.0.1/image.png">')
        with patch.object(mp, "_fetch", side_effect=lambda url, **kw: (png(), url) if kw.get("image") else (html, url)) as fetch:
            result = mp.export_articles([SHORT], self.root)
        document = self.document(result)
        self.assertEqual([item["status"] for item in document["images"]], ["saved", "missing"])
        self.assertIn("微信图片域名", document["images"][1]["reason"])
        directory = Path(result["results"][0]["output_dir"])
        self.assertTrue((directory / "images/001.png").is_file())
        self.assertIn("images/001.png", Path(result["results"][0]["md"]).read_text(encoding="utf-8"))
        self.assertIn("images/001.png", Path(result["results"][0]["txt"]).read_text(encoding="utf-8"))
        self.assertNotIn("PRIVATE", Path(result["results"][0]["json"]).read_text(encoding="utf-8"))
        self.assertEqual(fetch.call_count, 2)

    def test_unchecked_images_are_not_downloaded_and_invalid_bytes_are_not_successful(self):
        html = page(f'<p>正文</p><img src="{PICTURE}">')
        with patch.object(mp, "_fetch", return_value=(html, SHORT)) as fetch:
            result = mp.export_articles([SHORT], self.root, export_images=False)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.document(result)["images"][0]["status"], "not_requested")
        with patch.object(mp, "_fetch", side_effect=lambda url, **kw: (b"not image", url) if kw.get("image") else (html, url)):
            result = mp.export_articles([SHORT], self.root, incremental=False)
        self.assertEqual(self.document(result)["images"][0]["status"], "missing")

    def test_incremental_retries_previously_unrequested_or_missing_images(self):
        html = page(f'<p>正文</p><img src="{PICTURE}">')
        self.export(html, export_images=False)
        with patch.object(mp, "_fetch", side_effect=lambda url, **kw: (png(), url) if kw.get("image") else (html, url)) as fetch:
            result = mp.export_articles([SHORT], self.root, export_images=True)
        self.assertFalse(result["results"][0]["cached"])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.document(result)["images"][0]["status"], "saved")
        Path(result["results"][0]["output_dir"], "images/001.png").unlink()
        with patch.object(mp, "_fetch", side_effect=lambda url, **kw: (png(), url) if kw.get("image") else (html, url)) as fetch:
            result = mp.export_articles([SHORT], self.root, export_images=True)
        self.assertFalse(result["results"][0]["cached"])
        self.assertEqual(fetch.call_count, 2)
        with patch.object(mp, "_fetch") as fetch:
            result = mp.export_articles([SHORT], self.root, export_images=True)
        fetch.assert_not_called()
        self.assertTrue(result["results"][0]["cached"])

    def test_incremental_failed_images_retry_but_unchecked_images_allow_cached_text(self):
        html = page(f'<p>正文</p><img src="{PICTURE}">')
        with patch.object(mp, "_fetch", side_effect=lambda url, **kw: (b"bad-image", url) if kw.get("image") else (html, url)):
            result = mp.export_articles([SHORT], self.root)
        self.assertEqual(self.document(result)["images"][0]["status"], "missing")
        with patch.object(mp, "_fetch") as fetch:
            result = mp.export_articles([SHORT], self.root, export_images=False)
        fetch.assert_not_called()
        self.assertTrue(result["results"][0]["cached"])
        with patch.object(mp, "_fetch", side_effect=lambda url, **kw: (png(), url) if kw.get("image") else (html, url)) as fetch:
            result = mp.export_articles([SHORT], self.root)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.document(result)["images"][0]["status"], "saved")

    def test_end_date_is_inclusive_and_unknown_published_date_is_not_today(self):
        result = self.export(start_time="2026-10-01", end_time="2026-10-01")
        self.assertEqual(len(result["results"]), 1)
        result = self.export(urls=(OTHER,), start_time="2026-10-02")
        self.assertEqual(len(result["skipped"]), 1)
        self.assertFalse(result["results"])
        result = self.export(page(timestamp=None), urls=(OTHER,), start_time="2026-10-01")
        self.assertIn("发布时间未知", result["skipped"][0]["reason"])
        result = self.export(page(timestamp=None), urls=(OTHER,))
        self.assertIsNone(self.document(result)["published_at"])

    def test_invalid_and_reversed_time_ranges_do_not_fetch(self):
        with patch.object(mp, "_fetch") as fetch:
            for options in ({"start_time": "明天"}, {"end_time": "2026-13-01"},
                            {"start_time": "2026-10-03", "end_time": "2026-10-01"}, {"start_time": False},
                            {"start_time": "2026-10-01 08:00:00", "end_time": "2026-10-01 08:00:00"}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    mp.export_articles([SHORT], self.root, **options)
            fetch.assert_not_called()

    def test_precise_end_timestamp_is_excluded_but_start_timestamp_is_included(self):
        result = self.export(start_time="2026-10-01 07:00:00", end_time="2026-10-01 08:00:00")
        self.assertFalse(result["results"])
        self.assertIn("不在", result["skipped"][0]["reason"])
        result = self.export(start_time="2026-10-01 08:00:00", end_time="2026-10-01 08:00:01")
        self.assertEqual(len(result["results"]), 1)

    def test_audio_video_and_embedded_sources_remain_as_metadata_without_credentials(self):
        html = page('<video src="https://example.com/video.mp4?appmsg_token=PRIVATE&amp;vid=123"></video><audio src="javascript:evil()"></audio><iframe data-src="https://example.com/embed?v=2"></iframe>')
        result = self.export(html)
        document = self.document(result)
        self.assertEqual([item["type"] for item in document["media"]], ["video", "audio", "iframe"])
        self.assertEqual(document["media"][0]["url"], "https://example.com/video.mp4?vid=123")
        self.assertIsNone(document["media"][1]["url"])
        self.assertEqual(document["media"][2]["status"], "not_downloaded")
        self.assertIn("未下载", document["text"])
        self.assertNotIn("PRIVATE", json.dumps(document))

    def test_incremental_skips_successes_and_retries_failures(self):
        with patch.object(mp, "_fetch", side_effect=[(page(), SHORT), RuntimeError("临时错误")]):
            first = mp.export_articles([SHORT, OTHER], self.root)
        self.assertEqual(len(first["results"]), 1)
        self.assertEqual(len(first["failures"]), 1)
        with patch.object(mp, "_fetch", return_value=(page(), OTHER)) as fetch:
            second = mp.export_articles([SHORT, OTHER], self.root)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([item["cached"] for item in second["results"]], [True, False])
        self.assertFalse(second["failures"])
        self.assertNotEqual(first["report"], second["report"])

    def test_short_and_long_links_join_only_with_page_canonical_identity(self):
        html = page(canonical=LONG)
        with patch.object(mp, "_fetch", return_value=(html, SHORT)) as fetch:
            result = mp.export_articles([SHORT, LONG], self.root)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(result["results"]), 1)
        with patch.object(mp, "_fetch") as fetch:
            repeated = mp.export_articles([SHORT], self.root)
        fetch.assert_not_called()
        self.assertTrue(repeated["results"][0]["cached"])
        altered = mp._parse_article(page(canonical=LONG.replace("mid=123", "mid=124")), LONG)
        self.assertEqual(altered["url"], mp.normalize_article_url(LONG))

    def test_short_link_redirect_to_long_link_uses_confirmed_final_identity(self):
        with patch.object(mp, "_fetch", return_value=(page(), mp.normalize_article_url(LONG))) as fetch:
            result = mp.export_articles([SHORT, LONG], self.root)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(self.document(result)["url"], mp.normalize_article_url(LONG))

    def test_deleted_verification_login_and_empty_pages_are_explicit_failures(self):
        cases = [("<p>文章已被发布者删除</p>", "删除"), ("<p>环境异常，请完成验证</p>", "安全验证"),
                 ("<p>请登录后查看</p>", "#js_content"), (page("<script>not正文</script>"), "正文为空")]
        for html, reason in cases:
            with self.subTest(reason=reason):
                result = self.export(html, incremental=False)
                self.assertFalse(result["results"])
                self.assertIn(reason, result["failures"][0]["error"])
        self.assertFalse(list((self.root / "mp_articles").glob("*/article.json")))

    def test_supplied_werss_body_uses_same_parser_and_date_rules_without_network(self):
        supplied = {SHORT: {"html": '<div id="js_content"><p>缓存正文</p></div>', "title": "缓存文章", "account": "缓存公众号",
                            "author": "缓存作者", "published_at": "2026-10-02T12:30:00+08:00", "source": "werss"}}
        with patch.object(mp, "_fetch") as fetch:
            result = mp.export_articles([SHORT], self.root, articles=supplied, start_time="2026-10-02", end_time="2026-10-02")
        fetch.assert_not_called()
        document = self.document(result)
        self.assertEqual(document["source"], "werss")
        self.assertEqual(document["title"], "缓存文章")
        self.assertEqual(document["account"], "缓存公众号")
        self.assertEqual(document["text"], "缓存正文")

    def test_script_and_credentials_are_removed_from_body_links(self):
        html = page('<p><a href="javascript:alert(1)">脚本链接</a> <a href="https://example.com/view?pass_ticket=PRIVATE&amp;id=2">外链</a> <a href="https://user:PRIVATE@example.com">含凭据链接</a></p>')
        result = self.export(html)
        document = self.document(result)
        self.assertNotIn("PRIVATE", json.dumps(document))
        self.assertNotIn("javascript:", document["markdown"])
        self.assertIn("https://example.com/view?id=2", document["markdown"])

    def test_refresh_write_failure_preserves_previous_article_and_leaves_no_success_marker(self):
        first = self.export()
        directory = Path(first["results"][0]["output_dir"])
        previous = {Path(first["results"][0][key]).name: Path(first["results"][0][key]).read_bytes() for key in ("txt", "md", "json")}
        original = mp._atomic_json

        def failing_write(path, document):
            if path.name == "合成文章.json":
                raise OSError("合成写入失败")
            return original(path, document)

        with patch.object(mp, "_atomic_json", side_effect=failing_write):
            second = self.export(page("<p>新正文</p>"), incremental=False)
        self.assertFalse(second["results"])
        self.assertEqual(second["failures"][0]["error"], "合成写入失败")
        for name, data in previous.items():
            self.assertEqual((directory / name).read_bytes(), data)
        self.assertFalse(list((self.root / "mp_articles").glob(".article-*")))

    def test_simultaneous_exports_are_rejected_and_invalid_index_cannot_traverse_paths(self):
        directory = self.root / "mp_articles"
        directory.mkdir()
        with mp._export_lock(directory), self.assertRaisesRegex(RuntimeError, "正在采集"):
            self.export()
        (directory / "articles_index.json").write_text(json.dumps({"schema_version": 1, "articles": {"../escape": {"aliases": [SHORT]}}}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "索引格式无效"):
            self.export()

    def test_http_request_timeout_and_response_limit(self):
        opener = Mock()
        opener.open.return_value = Response(page().encode())
        with patch.object(mp, "build_opener", return_value=opener):
            html, url = mp._fetch(SHORT)
        self.assertIn("合成正文", html)
        self.assertEqual(url, SHORT)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 25)
        self.assertNotIn("Cookie", opener.open.call_args.args[0].headers)
        opener.open.return_value = Response(b"x" * 50)
        with patch.object(mp, "build_opener", return_value=opener), patch.object(mp, "HTML_LIMIT", 10), self.assertRaisesRegex(RuntimeError, "上限"):
            mp._fetch(SHORT)

    def test_each_redirect_is_checked_and_cross_domain_is_never_requested(self):
        opener = Mock()
        opener.open.side_effect = [HTTPError(SHORT, 302, "redirect", {"Location": LONG + "&wxtoken=PRIVATE"}, None),
                                   Response(page().encode(), mp.normalize_article_url(LONG))]
        with patch.object(mp, "build_opener", return_value=opener):
            mp._fetch(SHORT)
        self.assertEqual(opener.open.call_count, 2)
        self.assertNotIn("PRIVATE", opener.open.call_args.args[0].full_url)
        opener = Mock()
        opener.open.side_effect = HTTPError(SHORT, 302, "redirect", {"Location": "http://127.0.0.1/SECRET"}, None)
        with patch.object(mp, "build_opener", return_value=opener), self.assertRaises(ValueError):
            mp._fetch(SHORT)
        self.assertEqual(opener.open.call_count, 1)

    def test_image_redirect_validation_and_mime_checks(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(PICTURE, 302, "redirect", {"Location": "https://other.qpic.cn/image.png"}, None)
        with patch.object(mp, "build_opener", return_value=opener), self.assertRaises(ValueError):
            mp._fetch(PICTURE, image=True)
        self.assertEqual(opener.open.call_count, 1)
        opener = Mock()
        opener.open.return_value = Response(b"login", PICTURE, "text/html")
        with patch.object(mp, "build_opener", return_value=opener), self.assertRaisesRegex(RuntimeError, "非图片"):
            mp._fetch(PICTURE, image=True)

    def test_official_captcha_redirect_reports_access_limit_without_following(self):
        for prefix in ("https://mp.weixin.qq.com", ""):
            with self.subTest(prefix=prefix):
                opener = Mock()
                opener.open.side_effect = HTTPError(SHORT, 302, "redirect", {"Location": prefix + "/mp/wappoc_appmsgcaptcha?poc_token=PRIVATE&target_url=PRIVATE"}, None)
                with patch.object(mp, "build_opener", return_value=opener):
                    result = mp.export_articles([SHORT], self.root)
                self.assertFalse(result["results"])
                self.assertEqual(opener.open.call_count, 1)
                self.assertEqual(result["failures"][0]["url"], SHORT)
                self.assertIn("验证码", result["failures"][0]["error"])
                self.assertNotIn("不是微信公众号文章", result["failures"][0]["error"])
                self.assertNotIn("PRIVATE", Path(result["report"]).read_text(encoding="utf-8"))
                self.assertFalse(list(self.root.rglob("article.json")))

    def test_official_login_redirect_has_distinct_diagnostic(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(SHORT, 302, "redirect", {"Location": "/mp/login?token=PRIVATE"}, None)
        with patch.object(mp, "build_opener", return_value=opener), self.assertRaisesRegex(RuntimeError, "登录或其他非文章页面"):
            mp._fetch(SHORT)
        self.assertEqual(opener.open.call_count, 1)

    def test_captcha_response_url_is_not_treated_as_article(self):
        opener = Mock()
        opener.open.return_value = Response(page().encode(), "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=PRIVATE")
        with patch.object(mp, "build_opener", return_value=opener), self.assertRaisesRegex(RuntimeError, "验证码"):
            mp._fetch(SHORT)

    def test_invalid_initial_link_still_fails_before_network(self):
        with patch.object(mp, "build_opener") as opener, self.assertRaisesRegex(ValueError, "/s 地址"):
            mp._fetch("https://mp.weixin.qq.com/mp/login")
        opener.assert_not_called()

    def test_network_and_http_errors_do_not_create_article_success(self):
        opener = Mock()
        for error in (HTTPError(SHORT, 403, "forbidden", {}, None), URLError("offline")):
            opener.open.side_effect = error
            with patch.object(mp, "build_opener", return_value=opener):
                result = mp.export_articles([SHORT], self.root)
            self.assertEqual(len(result["failures"]), 1)
            self.assertFalse(result["results"])


if __name__ == "__main__":
    unittest.main()

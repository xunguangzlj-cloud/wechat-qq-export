"""已保存公众号网页导入测试，仅使用合成 HTML/MHTML 和图片。"""

import io
import json
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mp_articles as mp
import mp_import as saved


URL = "https://mp.weixin.qq.com/s/synthetic_import-1"
SECOND = "https://mp.weixin.qq.com/s/synthetic_import-2"
IMAGE_URL = "https://mmbiz.qpic.cn/mmbiz_png/synthetic/0?wx_fmt=png"


def page(body="<p>合成保存正文</p>", url=URL, timestamp=True):
    origin = '<meta property="og:url" content="' + url + '">' if url else ""
    clock = '<script>var ct="1790812800";window.secret="脚本不可导出";</script>' if timestamp else ""
    return '<html><head>' + origin + '</head><body><h1 id="activity-name">合成保存文章</h1><a id="js_name">合成公众号</a>' + clock + '<div id="js_content">' + body + '</div></body></html>'


def png():
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(stream, "PNG")
    return stream.getvalue()


def mhtml(html, location=URL, images=()):
    message = EmailMessage()
    message.make_related()
    document = EmailMessage()
    document.set_content(html, subtype="html", charset="utf-8")
    if location:
        document["Content-Location"] = location
    message.attach(document)
    for address, cid, data in images:
        image = EmailMessage()
        image.set_content(data, maintype="image", subtype="png", cte="base64")
        if address:
            image["Content-Location"] = address
        if cid:
            image["Content-ID"] = "<" + cid + ">"
        message.attach(image)
    return message.as_bytes()


class SavedArticleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output"

    def write(self, name="保存.html", html=None, encoding="utf-8"):
        path = self.root / name
        path.write_bytes((page() if html is None else html).encode(encoding))
        return path

    def document(self, result):
        return json.loads(Path(result["results"][0]["json"]).read_text(encoding="utf-8"))

    def test_utf8_bom_gb18030_and_utf16_saved_html_import_without_http(self):
        for index, encoding in enumerate(("utf-8", "utf-8-sig", "gb18030", "utf-16")):
            path = self.write(f"保存{index}.html", encoding=encoding)
            with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")) as fetch:
                result = saved.export_saved_articles([path], self.output, incremental=False)
            fetch.assert_not_called()
            self.assertFalse(result["failures"])
            document = self.document(result)
            self.assertEqual(document["source"], "local_html")
            self.assertEqual(document["text"], "合成保存正文")
            self.assertNotIn("脚本不可导出", json.dumps(document, ensure_ascii=False))
            self.assertNotIn("local_images", document)

    def test_utf8_bom_overrides_stale_meta_encoding(self):
        html = page().replace("<head>", '<head><meta charset="gb18030">')
        path = self.write(html=html, encoding="utf-8-sig")
        with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")):
            result = saved.export_saved_articles(path, self.output)
        self.assertEqual(self.document(result)["text"], "合成保存正文")

    def test_mhtml_content_location_and_embedded_cid_images_need_no_http(self):
        path = self.root / "保存.mhtml"
        path.write_bytes(mhtml(page('<p>正文</p><img src="cid:synthetic-image">', url=None),
                               images=[(None, "synthetic-image", png())]))
        with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")) as fetch:
            result = saved.export_saved_articles([path], self.output)
        fetch.assert_not_called()
        document = self.document(result)
        self.assertEqual(document["url"], URL)
        self.assertEqual(document["images"][0]["status"], "saved")
        self.assertIsNone(document["images"][0]["source_url"])
        self.assertEqual(document["images"][0]["origin"], "local_saved")
        self.assertEqual(document["images"][0]["relative_path"], "images/001.png")
        self.assertEqual(result["import_coverage"]["files"][0]["embedded_images"], 1)
        self.assertTrue(Path(result["results"][0]["output_dir"], "images/001.png").is_file())

    def test_mhtml_network_image_locations_match_original_data_src_without_http(self):
        path = self.root / "保存.mht"
        html = page(f'<p>正文</p><img data-src="{IMAGE_URL}&amp;pass_ticket=PRIVATE" src="cid:image-one">')
        path.write_bytes(mhtml(html, images=[(IMAGE_URL, "image-one", png())]))
        with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")) as fetch:
            result = saved.export_saved_articles(path, self.output)
        fetch.assert_not_called()
        document = self.document(result)
        self.assertEqual(document["images"][0]["status"], "saved")
        self.assertEqual(document["images"][0]["source_url"], IMAGE_URL)
        self.assertNotIn("PRIVATE", json.dumps(document))
        self.assertIn("images/001.png", document["markdown"])

    def test_saved_html_reads_only_matching_files_directory_and_maps_data_src(self):
        path = self.write(html=page(f'<p>正文</p><img data-src="{IMAGE_URL}" src="保存_files/图像.png">'))
        resources = self.root / "保存_files"
        resources.mkdir()
        (resources / "图像.png").write_bytes(png())
        with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")) as fetch:
            result = saved.export_saved_articles([path], self.output)
        fetch.assert_not_called()
        self.assertEqual(self.document(result)["images"][0]["status"], "saved")

    def test_local_relative_encoded_name_is_supported_without_exposing_local_paths(self):
        path = self.write(html=page('<p>正文</p><img src="保存_files/%E5%9B%BE%E5%83%8F.png">'))
        resources = self.root / "保存_files"
        resources.mkdir()
        (resources / "图像.png").write_bytes(png())
        with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")):
            result = saved.export_saved_articles([path], self.output)
        document = self.document(result)
        self.assertEqual(document["images"][0]["status"], "saved")
        self.assertIsNone(document["images"][0]["source_url"])
        self.assertNotIn(str(self.root), json.dumps(document))

    def test_missing_origin_and_missing_body_are_failures_and_batch_continues(self):
        missing_origin = self.write("无来源.html", page(url=None))
        missing_body = self.write("无正文.html", '<meta property="og:url" content="' + URL + '"><p>请登录</p>')
        accepted = self.write("完整.html", page(url=SECOND))
        with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")):
            result = saved.export_saved_articles([missing_origin, missing_body, accepted], self.output)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(len(result["failures"]), 2)
        self.assertIn("原文地址", result["failures"][0]["error"])
        self.assertIn("#js_content", result["failures"][1]["error"])
        self.assertEqual(result["import_coverage"]["accepted_files"], 1)

    def test_absolute_unc_and_parent_resource_paths_are_not_read(self):
        secret = self.root / "secret.png"
        secret.write_bytes(png())
        sources = [secret.as_uri(), str(secret), "../secret.png", "保存_files/../secret.png", "\\\\server\\share\\secret.png", "//server/share/secret.png"]
        path = self.write(html=page('<p>正文</p>' + ''.join('<img src="' + value + '">' for value in sources)))
        with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")) as fetch:
            result = saved.export_saved_articles([path], self.output)
        fetch.assert_not_called()
        self.assertTrue(all(item["status"] == "missing" for item in self.document(result)["images"]))
        self.assertEqual(result["import_coverage"]["files"][0]["missing_images"], len(sources))

    def test_path_resolution_outside_resource_boundary_is_not_read(self):
        path = self.write(html=page('<p>正文</p><img src="保存_files/image.png">'))
        external = self.root / "outside"
        external.mkdir()
        (external / "image.png").write_bytes(png())
        selected_folder = self.root / "保存_files"
        real_resolve = Path.resolve

        def resolved(value, *args, **kwargs):
            # 模拟符号链接/目录联接的解析结果，测试不依赖Windows创建链接权限。
            return external if value == selected_folder else real_resolve(value, *args, **kwargs)

        with patch.object(Path, "resolve", resolved), patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")) as fetch:
            result = saved.export_saved_articles([path], self.output)
        fetch.assert_not_called()
        self.assertEqual(self.document(result)["images"][0]["status"], "missing")

    def test_invalid_selected_extension_and_unc_selection_are_explicit_failures(self):
        text = self.write("普通.txt")
        result = saved.export_saved_articles([text, "\\\\server\\share\\page.mhtml"], self.output)
        self.assertEqual(len(result["failures"]), 2)
        self.assertIn("HTML", result["failures"][0]["error"])
        self.assertIn("UNC", result["failures"][1]["error"])

    def test_original_url_and_image_tracking_credentials_are_stripped(self):
        path = self.write(html=page(url=URL + "?appmsg_token=PRIVATE&wxtoken=PRIVATE"))
        result = saved.export_saved_articles([path], self.output)
        self.assertEqual(self.document(result)["url"], URL)
        self.assertNotIn("PRIVATE", Path(result["report"]).read_text(encoding="utf-8"))

    def test_missing_public_image_uses_only_checked_cdn_and_reports_network_failure(self):
        path = self.write(html=page(f'<p>正文</p><img data-src="{IMAGE_URL}">'))
        with patch.object(mp, "_fetch", side_effect=RuntimeError("合成图片网络不可用")) as fetch:
            result = saved.export_saved_articles([path], self.output)
        fetch.assert_called_once_with(IMAGE_URL, image=True)
        document = self.document(result)
        self.assertEqual(document["images"][0]["status"], "missing")
        self.assertIn("网络不可用", document["images"][0]["reason"])
        self.assertEqual(document["source"], "local_html")

    def test_unchecked_images_never_fetch_and_unknown_date_is_not_misclassified(self):
        path = self.write(html=page(f'<p>正文</p><img data-src="{IMAGE_URL}">', timestamp=False))
        with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")) as fetch:
            result = saved.export_saved_articles([path], self.output, export_images=False)
            filtered = saved.export_saved_articles([path], self.output, export_images=False, start_time="2026-10-01")
        fetch.assert_not_called()
        self.assertEqual(self.document(result)["images"][0]["status"], "not_requested")
        self.assertEqual(len(filtered["skipped"]), 1)
        self.assertIn("发布时间未知", filtered["skipped"][0]["reason"])

    def test_duplicate_files_and_incremental_exports_do_not_repeat_downloads(self):
        first = self.write()
        duplicate = self.write("重复.html")
        result = saved.export_saved_articles([first, duplicate], self.output)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(len(result["skipped"]), 1)
        with patch.object(mp, "_fetch", side_effect=AssertionError("不得访问网络")) as fetch:
            repeated = saved.export_saved_articles([first], self.output)
        fetch.assert_not_called()
        self.assertTrue(repeated["results"][0]["cached"])

    def test_file_and_embedded_html_limits_fail_before_success(self):
        path = self.write()
        with patch.object(saved, "FILE_LIMIT", 10):
            result = saved.export_saved_articles([path], self.output)
        self.assertFalse(result["results"])
        self.assertIn("大小上限", result["failures"][0]["error"])
        with self.assertRaisesRegex(ValueError, "8 MiB"):
            saved._decode_html(b"x" * (mp.HTML_LIMIT + 1))


if __name__ == "__main__":
    unittest.main()

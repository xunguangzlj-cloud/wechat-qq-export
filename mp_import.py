"""导入用户选择的公众号 HTML/MHTML，图片只从对应保存文件内读取。"""

import json
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from mp_articles import HTML_LIMIT, IMAGE_LIMIT, _atomic_json, _image_url, export_articles, normalize_article_url


FILE_LIMIT = 64 * 1024 * 1024
EXTENSIONS = {".html", ".htm", ".mhtml", ".mht"}


def _read_limited(path, limit):
    if path.stat().st_size > limit:
        raise ValueError("所选网页或图片超过允许的大小上限。")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("所选网页或图片超过允许的大小上限。")
    return data


def _decode_html(data, charset=None):
    if len(data) > HTML_LIMIT:
        raise ValueError("已保存正文超过 8 MiB 上限。")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if charset is None:
        found = re.search(br"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", data[:4096], re.I)
        charset = found.group(1).decode("ascii") if found else None
    encodings = []
    if charset and charset.lower().replace("_", "-") in {"utf-8", "utf8", "gb18030", "gbk", "gb2312"}:
        encodings.append("gb18030" if charset.lower().startswith("gb") else "utf-8-sig")
    encodings.extend(["utf-8-sig", "gb18030"])
    for encoding in dict.fromkeys(encodings):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("无法按 UTF-8 或 GB18030 解码已保存网页。")


def _original_url(soup, locations):
    candidates = []
    for element in soup.find_all("link", rel="canonical"):
        candidates.append(element.get("href"))
    for element in soup.find_all("meta", attrs={"property": "og:url"}):
        candidates.append(element.get("content"))
    candidates.extend(locations)
    for candidate in candidates:
        try:
            return normalize_article_url(candidate)
        except (TypeError, ValueError):
            continue
    raise ValueError("已保存网页没有有效的公众号原文地址（canonical、og:url 或 Content-Location）；请从原文重新保存网页。")


def _local_image(path, address):
    parsed = urlsplit(address)
    if parsed.scheme or parsed.netloc or address.startswith(("\\", "/")):
        return None
    relative = unquote(parsed.path).replace("\\", "/")
    if not relative or relative.startswith("/"):
        return None
    selected_folder = path.parent / (path.stem + "_files")
    folder = selected_folder.resolve()
    if folder != selected_folder:
        return None
    candidate = (path.parent / relative).resolve()
    try:
        candidate.relative_to(folder)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return _read_limited(candidate, IMAGE_LIMIT)


def _prepared_file(path):
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("不支持从网络共享或 UNC 路径导入网页。")
    path = Path(path).resolve()
    if path.suffix.lower() not in EXTENSIONS or not path.is_file():
        raise ValueError("请选择存在的 HTML、HTM、MHTML 或 MHT 文件。")
    data = _read_limited(path, FILE_LIMIT)
    locations, embedded = [], {}
    if path.suffix.lower() in {".mhtml", ".mht"}:
        message = BytesParser(policy=policy.default).parsebytes(data)
        selected = None
        for part in message.walk():
            if part.get_content_type() != "text/html":
                continue
            candidate = _decode_html(part.get_payload(decode=True) or b"", part.get_content_charset())
            if BeautifulSoup(candidate, "html.parser").select_one("#js_content"):
                selected = candidate
                locations.extend([part.get("Content-Location"), message.get("Content-Location")])
                break
        if selected is None:
            raise ValueError("MHTML 中没有公众号正文 #js_content；请保存已能看到全文的原始文章页面。")
        page = selected
        for part in message.walk():
            if not part.get_content_type().startswith("image/"):
                continue
            image = part.get_payload(decode=True) or b""
            if len(image) > IMAGE_LIMIT:
                continue
            location = part.get("Content-Location")
            cid = part.get("Content-ID")
            if location:
                embedded[str(location)] = image
                try:
                    embedded[_image_url(str(location))] = image
                except ValueError:
                    pass
            if cid:
                embedded["cid:" + str(cid).strip().strip("<>")] = image
    else:
        page = _decode_html(data)
    soup = BeautifulSoup(page, "html.parser")
    body = soup.select_one("#js_content")
    if body is None:
        raise ValueError("已保存网页没有公众号正文 #js_content；请保存原始文章页面。")
    url = _original_url(soup, locations)
    local_images, missing = {}, 0
    for element in body.find_all("img"):
        addresses = list(dict.fromkeys(value for value in (element.get("data-src"), element.get("src")) if value))
        image = None
        for address in addresses:
            image = embedded.get(address)
            if image is None and urlsplit(address).scheme.lower() == "cid":
                image = embedded.get("cid:" + unquote(address[4:]).strip("<>"))
            if image is None:
                try:
                    image = embedded.get(_image_url(address))
                except ValueError:
                    pass
            if image is None and path.suffix.lower() in {".html", ".htm"}:
                try:
                    image = _local_image(path, address)
                except (OSError, ValueError):
                    pass
            if image is not None:
                break
        if image is None:
            missing += 1
        else:
            for address in addresses:
                local_images[address] = image
    return url, {"html": page, "source": "local_html", "local_images": local_images}, {
        "filename": path.name, "url": url, "embedded_images": len({id(value) for value in local_images.values()}),
        "missing_images": missing,
    }


def export_saved_articles(paths, out_root="exports", *, start_time=None, end_time=None,
                          export_images=True, incremental=True, progress=None):
    paths = [paths] if isinstance(paths, (str, Path)) else list(paths)
    if not paths:
        raise ValueError("请至少选择一个已保存的公众号网页文件。")
    urls, articles, failures, skipped, files = [], {}, [], [], []
    for selected in paths:
        filename = Path(selected).name
        try:
            url, prepared, coverage = _prepared_file(selected)
            if url in articles:
                skipped.append({"url": url, "filename": filename, "reason": "重复原文地址，保留首个导入文件。"})
                continue
            urls.append(url)
            articles[url] = prepared
            files.append(coverage)
            if progress:
                progress("已读取保存网页：" + filename + "；正文导入无需网络访问。")
        except Exception as error:
            failures.append({"url": "", "filename": filename, "error": str(error)[:400] or "保存网页导入失败。"})
            if progress:
                progress("保存网页导入失败，继续下一文件：" + failures[-1]["error"])
    result = export_articles(urls, out_root, start_time=start_time, end_time=end_time,
                             export_images=export_images, incremental=incremental, progress=progress, articles=articles)
    result["failures"].extend(failures)
    result["skipped"].extend(skipped)
    result["import_coverage"] = {
        "source": "local_html", "requested_files": len(paths), "accepted_files": len(files), "files": files,
        "scope_description": "仅导入明确选择的已保存正文；缺失图片只会尝试微信图片白名单地址。",
    }
    report_path = Path(result["report"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update({key: result[key] for key in ("failures", "skipped", "import_coverage")})
    _atomic_json(report_path, report)
    return result

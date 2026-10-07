"""采集公开微信公众号文章；不登录、不执行网页脚本。"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from bs4 import BeautifulSoup, Comment, NavigableString
from markdownify import markdownify
from PIL import Image

from export_names import export_paths


ARTICLE_HOST = "mp.weixin.qq.com"
IMAGE_HOSTS = {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"}
LOCAL_TIMEZONE = timezone(timedelta(hours=8))
HTML_LIMIT = 8 * 1024 * 1024
IMAGE_LIMIT = 16 * 1024 * 1024
TIMEOUT = 25


def normalize_article_url(url):
    """保留文章定位参数，去掉分享追踪及登录参数。"""
    if not isinstance(url, str):
        raise ValueError("文章链接必须是文本。")
    url = html.unescape(url.strip())
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname != ARTICLE_HOST
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
        raise ValueError("仅支持 mp.weixin.qq.com 的公开文章链接。")
    path = parsed.path.rstrip("/")
    if re.fullmatch(r"/s/[A-Za-z0-9_-]+", path):
        return "https://" + ARTICLE_HOST + path
    if path != "/s":
        raise ValueError("链接不是微信公众号文章的 /s 地址。")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if not all(query.get(key) for key in ("__biz", "mid", "idx")):
        raise ValueError("长文章链接缺少 __biz、mid 或 idx 参数。")
    if not query["mid"].isascii() or not query["mid"].isdigit() or not query["idx"].isascii() or not query["idx"].isdigit():
        raise ValueError("文章 mid 和 idx 必须是数字。")
    kept = {key: query[key] for key in ("__biz", "mid", "idx", "sn", "chksm") if query.get(key)}
    return "https://" + ARTICLE_HOST + "/s?" + urlencode(kept)


def _article_id(url):
    parsed = urlsplit(normalize_article_url(url))
    query = dict(parse_qsl(parsed.query))
    identity = "long:" + query["__biz"] + ":" + str(int(query["mid"])) + ":" + str(int(query["idx"])) if query else "short:" + parsed.path[3:]
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def extract_article_links(text_or_chat_json):
    """从粘贴文本或聊天 JSON 中提取文章链接，按文章身份保持原顺序去重。"""
    values = []

    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            values.append(value)

    if isinstance(text_or_chat_json, str):
        try:
            decoded = json.loads(text_or_chat_json)
        except (ValueError, TypeError):
            decoded = text_or_chat_json
        visit(decoded)
    else:
        visit(text_or_chat_json)
    result, seen = [], set()
    for value in values:
        value = html.unescape(html.unescape(value.replace("\\/", "/")))
        for match in re.finditer(r"https?://mp\.weixin\.qq\.com/s(?:/[A-Za-z0-9_-]+|\?[^\s<>\"']+)", value):
            candidate = match.group(0).rstrip("）)、，。；!！?？.,;]}")
            try:
                url = normalize_article_url(candidate)
                identity = _article_id(url)
            except ValueError:
                continue
            if identity not in seen:
                seen.add(identity)
                result.append(url)
    return result


def _image_url(url):
    parsed = urlsplit(html.unescape(url.strip()))
    if (parsed.scheme not in {"https", "http"} or parsed.hostname not in IMAGE_HOSTS
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
        raise ValueError("图片地址不在已支持的微信图片域名中。")
    query = dict(parse_qsl(parsed.query))
    return urlunsplit(("https", parsed.hostname, parsed.path, urlencode({"wx_fmt": query["wx_fmt"]}) if query.get("wx_fmt") else "", ""))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _article_response_url(url):
    """区分用户输入错误和微信服务器返回的非文章页面。"""
    try:
        return normalize_article_url(url)
    except ValueError:
        parsed = urlsplit(url)
        if (parsed.scheme in {"http", "https"} and parsed.hostname == ARTICLE_HOST
                and not parsed.username and not parsed.password and parsed.port in {None, 80, 443}):
            if "captcha" in parsed.path.lower():
                raise RuntimeError("微信将文章请求转到了验证码页面，当前无法直接采集。请在微信或浏览器打开原文查看，可完成验证后稍后重试；浏览器的验证状态不会自动传给本工具，也可使用 WeRSS 已缓存的正文。") from None
            raise RuntimeError("微信将文章请求转到了登录或其他非文章页面，当前无法直接采集。请在微信或浏览器打开原文确认是否仍可访问。") from None
        raise


def _fetch(url, *, image=False):
    validate = _image_url if image else _article_response_url
    current = _image_url(url) if image else normalize_article_url(url)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    for step in range(6):
        request = Request(current, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*" if image else "text/html"})
        try:
            with opener.open(request, timeout=TIMEOUT) as response:
                validate(response.geturl())
                limit = IMAGE_LIMIT if image else HTML_LIMIT
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise RuntimeError("图片超过 16 MiB 上限。" if image else "文章响应超过 8 MiB 上限。")
                content_type = response.headers.get("Content-Type", "").lower()
                if image:
                    if content_type and not content_type.startswith("image/") and "octet-stream" not in content_type:
                        raise RuntimeError("图片接口返回了非图片内容。")
                    return data, current
                if content_type and "html" not in content_type and "text/plain" not in content_type:
                    raise RuntimeError("文章接口返回了非 HTML 内容。")
                return data.decode("utf-8-sig", errors="replace"), current
        except HTTPError as error:
            if error.code in {301, 302, 303, 307, 308}:
                location = error.headers.get("Location")
                error.close()
                if not location or step == 5:
                    raise RuntimeError("重定向缺少地址或超过次数上限。") from None
                current = validate(urljoin(current, location))
                continue
            error.close()
            raise RuntimeError(f"微信接口返回 HTTP {error.code}。") from None
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError("网络连接失败或超时，请稍后重试。") from error
    raise RuntimeError("文章重定向超过次数上限。")


def _time(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isascii() and value.isdigit()):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            result = datetime.fromtimestamp(timestamp, LOCAL_TIMEZONE)
        else:
            cleaned = str(value).strip().replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
            date_parts = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(.*)", cleaned)
            if date_parts:
                year, month, day, remaining = date_parts.groups()
                cleaned = f"{year}-{int(month):02d}-{int(day):02d}{remaining}"
            result = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
            result = result.replace(tzinfo=LOCAL_TIMEZONE) if result.tzinfo is None else result.astimezone(LOCAL_TIMEZONE)
        return result if 2000 <= result.year <= 2100 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _time_range(start_time, end_time):
    start, end = _time(start_time), _time(end_time)
    if start_time not in (None, "") and start is None:
        raise ValueError("起始时间无效，请使用 年-月-日 或 年-月-日 时:分:秒。")
    if end_time not in (None, "") and end is None:
        raise ValueError("结束时间无效，请使用 年-月-日 或 年-月-日 时:分:秒。")
    if end and isinstance(end_time, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_time.strip()):
        end += timedelta(days=1)
    if start and end and start >= end:
        raise ValueError("起始时间必须早于结束时间。")
    return start, end


def _script_value(soup, name):
    pattern = re.compile(r"(?:\b" + re.escape(name) + r"\b|[\"']" + re.escape(name) + r"[\"'])\s*[:=]\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\d+)")
    for script in soup.find_all("script"):
        match = pattern.search(script.string or script.get_text())
        if match:
            value = match.group(1)
            if value.startswith('"'):
                try:
                    return html.unescape(json.loads(value))
                except ValueError:
                    continue
            if value.startswith("'"):
                return html.unescape(value[1:-1].replace("\\'", "'").replace("\\/", "/"))
            return value
    return None


def _body_link(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        cleaned = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                   if key.lower() not in {"pass_ticket", "wxtoken", "appmsg_token", "access_token", "token", "authorization", "cookie"}]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(cleaned), parsed.fragment))
    except ValueError:
        return None


def _parse_article(page, url, supplied=None):
    supplied = supplied or {}
    if not isinstance(page, str):
        raise ValueError("文章正文缓存必须是 HTML 文本。")
    soup = BeautifulSoup(page, "html.parser")
    body = soup.select_one("#js_content")
    if body is None:
        screen = soup.get_text(" ", strip=True)
        if any(value in screen for value in ("已被发布者删除", "内容已被删除", "此内容因违规无法查看", "该内容无法查看", "内容已被投诉")):
            raise RuntimeError("文章已删除或不可查看。")
        if any(value in screen for value in ("验证码", "安全验证", "环境异常", "访问过于频繁", "请完成验证")):
            raise RuntimeError("微信要求安全验证或限制访问；本工具不会绕过验证。")
        raise RuntimeError("网页没有公众号正文 #js_content，可能需要登录或文章已失效。")

    def content(selector):
        element = soup.select_one(selector)
        return element.get_text(" ", strip=True) if element else None

    def meta(name):
        element = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        return element.get("content") if element else None

    title = content("#activity-name, #js_article_title") or meta("og:title") or supplied.get("title") or _script_value(soup, "msg_title") or "未命名公众号文章"
    author = content("#js_author_name") or meta("author") or supplied.get("author")
    if not author:
        author = next((element.get_text(" ", strip=True) for element in soup.select(".rich_media_meta_text")
                       if element.get("id") != "js_ip_wording"
                       and element.get_text(" ", strip=True) not in {"", "原创", "已修改"}), None)
    account = content("#js_name, .profile_nickname") or supplied.get("account") or _script_value(soup, "nickname")
    published = None
    for value in (meta("article:published_time"), content("#publish_time"), _script_value(soup, "ct"), _script_value(soup, "publish_time"), supplied.get("published_at")):
        published = _time(value)
        if published:
            break
    canonical = url
    for value in (meta("og:url"), (soup.find("link", rel="canonical") or {}).get("href"), _script_value(soup, "msg_link")):
        try:
            candidate = normalize_article_url(value)
            if urlsplit(candidate).query and (not urlsplit(url).query or _article_id(url) == _article_id(candidate)):
                canonical = candidate
                break
        except (ValueError, TypeError):
            continue
    media = []
    for element in body.find_all(["video", "audio", "iframe", "mpvoice"]):
        source = element.get("data-src") or element.get("src") or ""
        label = "音频" if element.name in {"audio", "mpvoice"} else "视频/嵌入内容"
        media.append({"type": element.name, "url": _body_link(source), "status": "not_downloaded",
                      "reason": "公众号" + label + "暂未下载；请打开原文查看。"})
        element.replace_with("[" + label + "未下载]")
    for element in body.find_all(["script", "style", "object", "embed", "form", "svg"]):
        element.decompose()
    for comment in body.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for anchor in body.find_all("a"):
        href = anchor.get("href", "")
        safe_link = _body_link(href)
        if safe_link is None:
            anchor.attrs.pop("href", None)
        else:
            anchor["href"] = safe_link
    if not body.get_text(" ", strip=True) and not body.find("img"):
        raise RuntimeError("文章正文为空，未保存为成功记录。")
    return {"article_id": _article_id(canonical), "url": canonical, "title": html.unescape(str(title).strip()),
            "author": str(author).strip() if author else None, "account": str(account).strip() if account else None,
            "published_at": published.isoformat() if published else None, "_published": published, "_body": body,
            "media": media, "source": supplied.get("source") if supplied.get("source") in {"werss", "local_html", "browser_page"} else "public_url",
            "_local_images": supplied.get("local_images") or {}}


def _atomic_json(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _export_lock(directory):
    with (directory / ".export.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("该输出目录正在采集文章，请等本次任务完成后重试。") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _summary(document, directory, cached):
    paths = export_paths(directory, document.get("title"), "未命名文章")
    return {key: document.get(key) for key in ("title", "url", "published_at", "account", "author")} | {
        "article_id": document["article_id"], "output_dir": str(directory),
        **{key: str(path) for key, path in paths.items()}, "cached": cached}


def _cached_document(directory, identity, export_images=False):
    candidates = sorted(path for path in directory.glob("*.json") if path.name != "article.json")
    candidates.append(directory / "article.json")
    for candidate in candidates:
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
            paths = export_paths(directory, document.get("title"), "未命名文章")
            if candidate.name == "article.json":
                paths = {key: directory / f"article.{key}" for key in ("txt", "md", "json")}
            if (candidate != paths["json"] or document.get("schema_version") != 1
                    or document.get("article_id") != identity or document.get("status") != "success"
                    or not isinstance(document.get("text"), str) or not all(path.is_file() for path in paths.values())):
                continue
            if export_images and any(
                    not isinstance(image, dict) or image.get("status") != "saved"
                    or not re.fullmatch(r"images/\d{3,}\.(jpg|png|gif|webp)", image.get("relative_path", ""))
                    or not (directory / image["relative_path"]).is_file()
                    for image in document.get("images", [])):
                return None
            return document
        except (ValueError, OSError, TypeError, AttributeError):
            continue
    return None


def _rename_legacy_files(document, directory):
    """增量命中旧记录时改为标题命名，失败则恢复原文件。"""
    paths = export_paths(directory, document.get("title"), "未命名文章")
    if all(path.is_file() for path in paths.values()):
        return
    renamed = []
    try:
        for key, destination in paths.items():
            source = directory / f"article.{key}"
            if source == destination:
                continue
            if destination.exists():
                raise FileExistsError("标题文件名已存在，已保留原导出文件。")
            source.rename(destination)
            renamed.append((source, destination))
    except Exception:
        for source, destination in reversed(renamed):
            destination.rename(source)
        raise


def _body_text(body):
    """按段落和换行提取正文，行内格式不拆行，图片保留原位置。"""
    blocks = {"p", "div", "section", "blockquote", "li", "ul", "ol", "table", "tr",
              "h1", "h2", "h3", "h4", "h5", "h6"}
    pre_texts = {}
    pre_prefix = "\x00" + uuid.uuid4().hex + "-"

    def render(node, in_pre=False):
        if isinstance(node, NavigableString):
            return str(node) if in_pre else re.sub(r"\s+", " ", str(node))
        if node.name == "br":
            return "\n"
        if node.name == "pre" and not in_pre:
            text = "".join(render(child, True) for child in node.children)
            placeholder = pre_prefix + str(len(pre_texts)) + "\x00"
            pre_texts[placeholder] = text.replace("\r\n", "\n").replace("\xa0", " ")
            return "\n" + placeholder + "\n"
        if node.name == "img":
            return "[图片：" + str(node.get("src") or "不可用") + "]"
        text = "".join(render(child, in_pre) for child in node.children)
        if in_pre:
            return text
        if node.name in {"td", "th"}:
            return text.strip(" ") + "\t"
        return "\n" + text.strip(" ") + "\n" if node.name in blocks else text

    text = re.sub(r"[ \t]+\n", "\n", render(body))
    text = re.sub(r"\n[ \t]+(?=\n)", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    for placeholder, pre_text in pre_texts.items():
        text = text.replace(placeholder, pre_text)
    return text


def _save_article(article, root, export_images):
    identity = article["article_id"]
    directory = root / identity
    staging = Path(tempfile.mkdtemp(prefix=".article-", dir=root))
    backup = root / (".backup-" + uuid.uuid4().hex)
    try:
        body = article.pop("_body")
        article.pop("_published", None)
        local_images = article.pop("_local_images", {})
        images = []
        for number, image in enumerate(body.find_all("img"), 1):
            source = image.get("data-src") or image.get("src") or ""
            record = {"source_url": None, "status": "missing"}
            try:
                try:
                    record["source_url"] = _image_url(source)
                except ValueError:
                    pass
                if not export_images:
                    record.update(status="not_requested", reason="未勾选保存图片。")
                else:
                    data = local_images.get(source)
                    if data is None:
                        source = _image_url(source)
                        data, _ = _fetch(source, image=True)
                    else:
                        if not isinstance(data, bytes) or len(data) > IMAGE_LIMIT:
                            raise RuntimeError("已保存图片无效或超过 16 MiB 上限。")
                        record["origin"] = "local_saved"
                    with Image.open(io.BytesIO(data)) as loaded:
                        suffix = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}.get(loaded.format)
                        loaded.verify()
                    if not suffix:
                        raise RuntimeError("图片格式不在 JPEG、PNG、GIF、WebP 范围内。")
                    relative = f"images/{number:03d}.{suffix}"
                    destination = staging / relative
                    destination.parent.mkdir(exist_ok=True)
                    destination.write_bytes(data)
                    record.update(status="saved", relative_path=relative)
            except Exception as error:
                record["reason"] = str(error)[:400] or "图片保存失败。"
            images.append(record)
            if record["status"] == "saved":
                image.attrs = {"src": quote(record["relative_path"], safe="/"), "alt": image.get("alt", "图片")}
            else:
                image.replace_with(f"[图片：{record.get('reason', '不可用')}]")
        text = _body_text(body)
        markdown = markdownify(str(body), heading_style="ATX", strip=["button", "input"]).strip()
        document = article | {"schema_version": 1, "status": "success", "platform": "wechat_public_account",
                              "text": text, "text_layout_version": 1, "markdown": markdown, "images": images,
                              "exported_at": datetime.now(LOCAL_TIMEZONE).isoformat()}
        metadata = f"标题：{article['title']}\n公众号：{article.get('account') or '未知'}\n作者：{article.get('author') or '未知'}\n发布时间：{article.get('published_at') or '未知'}\n原文：{article['url']}"
        paths = export_paths(staging, article.get("title"), "未命名文章")
        paths["txt"].write_text(metadata + "\n\n" + text + "\n", encoding="utf-8")
        paths["md"].write_text("# " + article["title"] + "\n\n" + metadata.replace("\n", "  \n") + "\n\n" + markdown + "\n", encoding="utf-8")
        _atomic_json(paths["json"], document)
        if directory.exists():
            os.replace(directory, backup)
        try:
            os.replace(staging, directory)
        except Exception:
            if backup.exists():
                os.replace(backup, directory)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return document, directory
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def export_articles(urls, out_root, *, start_time=None, end_time=None, export_images=True,
                    incremental=True, progress=None, articles=None):
    """批量采集公开文章；articles 可提供已核查来源的 #js_content HTML 缓存。"""
    start, end = _time_range(start_time, end_time)
    root = Path(out_root).resolve() / "mp_articles"
    root.mkdir(parents=True, exist_ok=True)
    links = extract_article_links(urls) if isinstance(urls, (str, dict)) else extract_article_links(list(urls))
    supplied = {}
    for url, article in (articles or {}).items():
        supplied[normalize_article_url(url)] = article if isinstance(article, dict) else {"html": article}
    results, failures, skipped = [], [], []
    index_path = root / "articles_index.json"
    report_path = root / "reports" / (datetime.now(LOCAL_TIMEZONE).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json")
    with _export_lock(root):
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            if (index.get("schema_version") != 1 or not isinstance(index.get("articles"), dict)
                    or any(not re.fullmatch(r"[0-9a-f]{24}", key) or not isinstance(value, dict)
                           for key, value in index["articles"].items())):
                raise ValueError
        except FileNotFoundError:
            index = {"schema_version": 1, "articles": {}}
        except (ValueError, AttributeError):
            raise RuntimeError("文章索引格式无效，请选择新的输出目录，避免覆盖已有记录。") from None
        for position, url in enumerate(links, 1):
            if progress:
                progress(f"公众号文章 {position}/{len(links)}：开始处理。")
            try:
                identity = _article_id(url)
                for key, entry in index["articles"].items():
                    if url in entry.get("aliases", []):
                        identity = key
                        break
                cached = _cached_document(root / identity, identity, export_images) if incremental else None
                if cached:
                    article = cached
                    published = _time(cached.get("published_at"))
                else:
                    provided = supplied.get(url, {})
                    if provided.get("html") is not None:
                        page = provided["html"]
                        resolved_url = url
                    else:
                        page, resolved_url = _fetch(url)
                        provided = provided | {"source": "public_url"}
                    article = _parse_article(page, resolved_url, provided)
                    published = article["_published"]
                    if incremental:
                        cached = _cached_document(root / article["article_id"], article["article_id"], export_images)
                        if cached:
                            article = cached
                            published = _time(cached.get("published_at"))
                if (start or end) and published is None:
                    skipped.append({"url": url, "reason": "发布时间未知，无法核验所选时间范围。"})
                    continue
                if published and ((start and published < start) or (end and published >= end)):
                    skipped.append({"url": url, "reason": "文章发布时间不在所选时间范围内。"})
                    continue
                identity = article["article_id"]
                if cached:
                    document, directory = cached, root / identity
                    _rename_legacy_files(document, directory)
                else:
                    document, directory = _save_article(article, root, export_images)
                summary = _summary(document, directory, bool(cached))
                previous = index["articles"].get(identity, {})
                index["articles"][identity] = summary | {"aliases": list(dict.fromkeys(previous.get("aliases", []) + [url, document["url"]]))}
                _atomic_json(index_path, index)
                if not any(item["article_id"] == identity for item in results):
                    results.append(summary)
                if progress:
                    progress(f"公众号文章：{document['title']}，{'已有记录，已跳过重复采集' if cached else '已保存'}。")
            except Exception as error:
                failures.append({"url": url, "error": str(error)[:400] or "采集失败，请稍后重试。"})
                if progress:
                    progress("公众号文章采集失败：" + failures[-1]["error"])
        report = {"schema_version": 1, "created_at": datetime.now(LOCAL_TIMEZONE).isoformat(),
                  "requested_time": {"start": start_time, "end": end_time}, "incremental": bool(incremental),
                  "requested_articles": len(links), "results": results, "failures": failures, "skipped": skipped}
        report_path.parent.mkdir(exist_ok=True)
        _atomic_json(report_path, report)
        if not index_path.exists():
            _atomic_json(index_path, index)
    return {"results": results, "failures": failures, "skipped": skipped, "output_dir": str(root),
            "report": str(report_path), "index": str(index_path)}

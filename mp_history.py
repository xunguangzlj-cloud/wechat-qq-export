"""读取本机 WeRSS 已同步的公众号历史，不触发关注、授权或刷新任务。"""

import html
import json
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


API_PREFIX = "/api/v1/wx"
CONTRACT_COMMIT = "126993c81a00466e9a6bbab041eef34ab27abe9c"
PAGE_SIZE = 100
MAX_ACCOUNT_PAGES = 100
MAX_HISTORY_PAGES = 500
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
BEIJING = timezone(timedelta(hours=8))
STOP_DESCRIPTIONS = {
    "page_limit": "达到本次历史读取页数上限",
    "empty_page": "服务返回空文章页",
    "service_total_reached": "已读取到服务报告的文章总数",
    "start_boundary_reached": "已读取到起始时间之前的文章",
    "no_new_articles": "页面没有新增文章，已停止重复分页",
    "api_error": "服务接口中途出错，保留已读取部分",
    "account_error": "公众号解析或读取失败",
    "selection_cancelled": "用户取消公众号选择",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WeRSSClient:
    def __init__(self, endpoint, access_key="", secret_key=""):
        try:
            parts = urlsplit(str(endpoint).strip())
            valid = (
                parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
                and not parts.username and not parts.password and not parts.query and not parts.fragment
                and (parts.port is None or 1 <= parts.port <= 65535)
                and parts.path.rstrip("/") in {"", API_PREFIX}
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("WeRSS 地址必须是本机 HTTP 服务地址，例如 http://127.0.0.1:8001，可附 /api/v1/wx；不接受远端地址或地址内凭据。")
        if any(not isinstance(value, str) or not value or any(ord(character) < 33 or ord(character) > 126 for character in value) for value in (access_key, secret_key)) or ":" in access_key:
            raise ValueError("请填写 WeRSS 中创建的 AK 和 SK；凭据只能使用非空、可打印的英文字符，不能含中文、空格或控制字符。")
        self.base = str(endpoint).strip().rstrip("/")
        if not parts.path.rstrip("/"):
            self.base += API_PREFIX
        self.access_key, self.secret_key = access_key, secret_key
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def redact(self, value):
        text = str(value)
        for secret in sorted((self.access_key + ":" + self.secret_key, self.access_key, self.secret_key), key=len, reverse=True):
            text = text.replace(secret, "[已隐藏]")
        return text

    def get(self, resource, params=None):
        # 只允许已核验的列表和文章详情，不调用名称搜索、更新或任务接口。
        if resource not in {"mps", "articles"} and not (resource.startswith("articles/") and "/" not in resource[len("articles/"):]):
            raise ValueError("WeRSS 接入只允许读取已有公众号与文章。")
        url = self.base + "/" + resource
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"Authorization": "AK-SK " + self.access_key + ":" + self.secret_key}, method="GET")
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.status != 200:
                    raise RuntimeError(f"WeRSS 接口返回 HTTP {response.status}，请检查服务及 AK/SK 授权。")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise RuntimeError(f"WeRSS 接口返回 HTTP {exc.code}，请检查服务及 AK/SK 授权。") from None
        except (URLError, OSError, HTTPException) as exc:
            raise RuntimeError("WeRSS 连接失败：" + self.redact(exc)) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("WeRSS 单次响应超过 32 MiB，已停止读取。")
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise RuntimeError("WeRSS 接口未返回有效 JSON。") from None
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise RuntimeError("WeRSS 响应结构无效，缺少整数 code。")
        if result["code"] != 0:
            raise RuntimeError(f"WeRSS 接口失败（code={result['code']}）：{self.redact(result.get('message') or '未提供原因')}")
        if "data" not in result:
            raise RuntimeError("WeRSS 响应结构无效，缺少 data。")
        return result["data"]


def _page(data):
    if not isinstance(data, dict) or not isinstance(data.get("list"), list) or type(data.get("total")) is not int or data["total"] < 0:
        raise RuntimeError("WeRSS 列表响应缺少有效 list/total。")
    if any(not isinstance(item, dict) for item in data["list"]):
        raise RuntimeError("WeRSS 列表含有非对象条目。")
    return data["list"], data["total"]


def check_history_connection(endpoint, access_key="", secret_key=""):
    """只验证公众号列表读取权限，不获取文章正文或触发同步。"""
    client = WeRSSClient(endpoint, access_key, secret_key)
    _, total = _page(client.get("mps", {"limit": 1, "offset": 0}))
    return {"account_count": total, "api_prefix": API_PREFIX, "source": "werss", "contract_commit": CONTRACT_COMMIT}


def _identifier(value):
    return value if isinstance(value, str) and value and len(value) <= 255 and not any(ord(character) < 32 for character in value) else None


def _resolve_account(client, keyword, select_account):
    candidates, seen, offset = [], set(), 0
    by_id = keyword.startswith("MP_WXS_")
    for _ in range(MAX_ACCOUNT_PAGES):
        params = {"limit": PAGE_SIZE, "offset": offset}
        # 服务的 kw 仅匹配名称；订阅 ID 必须读取账号列表后精确查找。
        if not by_id:
            params["kw"] = keyword
        items, total = _page(client.get("mps", params))
        added = 0
        for item in items:
            identifier = _identifier(item.get("id"))
            if not identifier or not isinstance(item.get("mp_name"), str):
                raise RuntimeError("WeRSS 公众号条目缺少有效 id/mp_name。")
            if identifier not in seen:
                candidates.append({key: item.get(key) for key in ("id", "mp_name", "mp_intro", "status")})
                seen.add(identifier)
                added += 1
        offset += len(items)
        if offset >= total:
            break
        if not items or not added:
            raise RuntimeError("WeRSS 公众号列表未完成分页或重复，无法确认名称候选。")
    else:
        raise RuntimeError("WeRSS 公众号候选超过分页上限，无法确认准确账号。")
    exact = [item for item in candidates if keyword in {item["mp_name"], item["id"]}]
    matches = exact if by_id else (exact or candidates)
    if not matches:
        raise ValueError("WeRSS 中没有找到此公众号；请先在服务中添加并同步，再使用准确名称。")
    if len(matches) == 1:
        return matches[0]
    if select_account is None:
        raise ValueError("WeRSS 中存在多个同名或相似公众号，请选择准确 feed ID。")
    selected = select_account(matches, keyword)
    if selected is None:
        return None
    for item in matches:
        if selected == item["id"]:
            return item
    raise ValueError("所选公众号 ID 不属于服务返回的名称候选。")


def _publish_time(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        stamp = int(value)
        if stamp <= 0:
            return None
        datetime.fromtimestamp(stamp, BEIJING)
        return stamp
    except (ValueError, OverflowError, OSError):
        return None


def _read_history(client, account, time_filter, log):
    coverage = {
        "feed_id": account["id"], "name": account["mp_name"], "pages_read": 0,
        "articles_seen": 0, "selected_articles": 0, "invalid_publish_time_excluded": 0,
        "time_excluded": 0, "history_complete": False, "default_excludes_deleted": True,
        "stop_reason": "page_limit", "warnings": [], "body_fallbacks": [],
        "earliest_publish_time_seen": None, "latest_publish_time_seen": None,
    }
    selected, seen, offset, last_time = [], set(), 0, None
    for page_number in range(MAX_HISTORY_PAGES):
        try:
            items, total = _page(client.get("articles", {"mp_id": account["id"], "limit": PAGE_SIZE, "offset": offset}))
            if any(not _identifier(item.get("id")) or item.get("mp_id") != account["id"] for item in items):
                raise RuntimeError("WeRSS 文章列表含有无效 ID 或其它公众号的记录。")
        except RuntimeError as exc:
            coverage["stop_reason"] = "api_error"
            coverage["error"] = client.redact(exc)
            log("公众号历史读取停止：" + coverage["error"])
            break
        coverage["pages_read"] = page_number + 1
        coverage["service_total_reported"] = total
        if not items:
            coverage["stop_reason"] = "empty_page"
            break
        added, older_found = 0, False
        for item in items:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            added += 1
            coverage["articles_seen"] += 1
            stamp = _publish_time(item.get("publish_time"))
            if stamp is not None:
                earliest, latest = coverage["earliest_publish_time_seen"], coverage["latest_publish_time_seen"]
                coverage["earliest_publish_time_seen"] = min(earliest, stamp) if earliest is not None else stamp
                coverage["latest_publish_time_seen"] = max(latest, stamp) if latest is not None else stamp
                if last_time is not None and stamp > last_time and "unstable_publish_order" not in coverage["warnings"]:
                    coverage["warnings"].append("unstable_publish_order")
                last_time = stamp
            bounded = time_filter["start_ts"] is not None or time_filter["end_ts_exclusive"] is not None
            if stamp is None and bounded:
                coverage["invalid_publish_time_excluded"] += 1
                continue
            if stamp is not None and time_filter["start_ts"] is not None and stamp < time_filter["start_ts"]:
                older_found = True
                coverage["time_excluded"] += 1
                continue
            if stamp is not None and time_filter["end_ts_exclusive"] is not None and stamp >= time_filter["end_ts_exclusive"]:
                coverage["time_excluded"] += 1
                continue
            selected.append({**item, "_publish_time": stamp})
        offset += len(items)
        log(f"{account['mp_name']}：已读取 {page_number + 1} 页，服务记录 {coverage['articles_seen']} 篇，时间范围内 {len(selected)} 篇。")
        if not added:
            coverage["stop_reason"] = "no_new_articles"
            break
        if older_found and "unstable_publish_order" not in coverage["warnings"]:
            coverage["stop_reason"] = "start_boundary_reached"
            break
        if offset >= total:
            coverage["stop_reason"] = "service_total_reached"
            break
    coverage["selected_articles"] = len(selected)
    coverage["stop_description"] = STOP_DESCRIPTIONS[coverage["stop_reason"]]
    return selected, coverage


class _BodySanitizer(HTMLParser):
    """仅保留静态正文标记，移除脚本、交互区域和事件属性。"""
    # 音视频标记交给正文核心记录未下载原因；核心在生成输出前移除这些元素。
    allowed = {"div", "section", "article", "p", "span", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "b", "i", "u", "blockquote", "pre", "code", "a", "img", "table", "thead", "tbody", "tr", "td", "th", "audio", "video", "iframe", "mpvoice"}
    blocked = {"script", "style", "object", "embed", "form", "button", "input", "textarea", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.ignore = [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.blocked:
            if tag not in {"input", "embed"}:
                self.ignore.append(tag)
            return
        if self.ignore or tag not in self.allowed:
            return
        safe = []
        for key, value in attrs:
            if key not in {"href", "src", "data-src", "alt", "title"} or value is None:
                continue
            if key in {"href", "src", "data-src"} and urlsplit(value.strip()).scheme.lower() not in {"", "http", "https"}:
                continue
            safe.append(f' {key}="{html.escape(value, quote=True)}"')
        self.parts.append("<" + tag + "".join(safe) + ">")

    def handle_endtag(self, tag):
        if self.ignore:
            if tag in self.ignore:
                self.ignore = self.ignore[:self.ignore.index(tag)]
            return
        if tag in self.allowed and tag not in {"img", "br", "hr"}:
            self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        if not self.ignore:
            self.parts.append(html.escape(data))


def _body_html(fragment):
    parser = _BodySanitizer()
    parser.feed(fragment)
    parser.close()
    return '<div id="js_content">' + "".join(parser.parts) + "</div>"


def export_history(names, out_root="exports", *, endpoint, access_key="", secret_key="",
                   start_time=None, end_time=None, export_images=True, incremental=True,
                   progress=None, select_account=None):
    from exporter_core import parse_time_range
    from mp_articles import export_articles, normalize_article_url

    time_filter = parse_time_range(start_time, end_time)
    names = names.splitlines() if isinstance(names, str) else list(names)
    if any(not isinstance(name, str) for name in names):
        raise ValueError("公众号名称应为文本，每行一个。")
    names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not names:
        raise ValueError("请输入至少一个公众号名称，每行一个。")
    client = WeRSSClient(endpoint, access_key, secret_key)
    log = lambda value: progress(client.redact(value)) if progress else None
    urls, articles, feeds_seen = [], {}, set()
    failures, skipped, account_reports = [], [], []
    for keyword in names:
        coverage = {"keyword": keyword, "history_complete": False}
        try:
            account = _resolve_account(client, keyword, select_account)
            if account is None:
                coverage.update({"stop_reason": "selection_cancelled", "stop_description": STOP_DESCRIPTIONS["selection_cancelled"]})
                skipped.append({"url": "", "account": keyword, "reason": "已取消公众号选择。"})
                continue
            if account["id"] in feeds_seen:
                skipped.append({"url": "", "account": keyword, "reason": "该公众号已在本次任务中读取。"})
                coverage.update({"feed_id": account["id"], "stop_reason": "duplicate_account", "stop_description": "该公众号已在本次任务中读取"})
                continue
            feeds_seen.add(account["id"])
            rows, feed_coverage = _read_history(client, account, time_filter, log)
            coverage.update(feed_coverage)
            if coverage.get("error"):
                failures.append({"url": "", "account": keyword, "error": coverage["error"]})
            for row in rows:
                try:
                    url = normalize_article_url(row.get("url") or "")
                except (ValueError, TypeError) as exc:
                    failures.append({"url": "", "account": account["mp_name"], "article_id": row["id"], "error": "服务文章链接无效：" + client.redact(exc)})
                    continue
                if url in urls:
                    skipped.append({"url": url, "account": account["mp_name"], "reason": "重复文章链接，保留首次来源。"})
                    continue
                urls.append(url)
                fallback = None
                try:
                    detail = client.get("articles/" + quote(row["id"], safe=""), {"content": "true"})
                    if not isinstance(detail, dict) or detail.get("id") != row["id"] or detail.get("mp_id") != account["id"]:
                        raise RuntimeError("WeRSS 正文详情 ID 或公众号归属不一致。")
                    fragment = detail.get("content") or detail.get("content_html")
                    if detail.get("status") == 1000 or (isinstance(fragment, str) and fragment.strip() == "DELETED"):
                        fallback = "WeRSS 已标记文章删除，将尝试公开链接。"
                    elif not isinstance(fragment, str) or not fragment.strip():
                        fallback = "WeRSS 没有已有正文，将尝试公开链接。"
                    else:
                        articles[url] = {
                            "html": _body_html(fragment), "title": detail.get("title") or row.get("title"),
                            "author": detail.get("author"), "account": account["mp_name"],
                            "published_at": row["_publish_time"], "source": "werss",
                        }
                except (RuntimeError, ValueError) as exc:
                    fallback = "WeRSS 正文详情读取失败，将尝试公开链接：" + client.redact(exc)
                if fallback:
                    coverage["body_fallbacks"].append({"url": url, "reason": fallback})
                    log(fallback)
            if not rows and not coverage.get("error"):
                skipped.append({"url": "", "account": account["mp_name"], "reason": "服务实际返回的历史中没有符合时间范围的文章。"})
        except (RuntimeError, ValueError) as exc:
            error = client.redact(exc)
            failures.append({"url": "", "account": keyword, "error": error})
            coverage.update({"stop_reason": "account_error", "stop_description": STOP_DESCRIPTIONS["account_error"], "error": error})
            log("公众号处理失败，继续下一名称：" + error)
        finally:
            account_reports.append(coverage)
    result = export_articles(urls, out_root, start_time=start_time, end_time=end_time,
                             export_images=export_images, incremental=incremental,
                             progress=log, articles=articles)
    result["failures"].extend(failures)
    result["skipped"].extend(skipped)
    result["history_coverage"] = {
        "source": "werss", "contract_commit": CONTRACT_COMMIT, "full_history_verified": False,
        "filter": time_filter, "accounts": account_reports,
        "note": "仅导出本机 WeRSS 服务已有或实际返回的历史；列表默认排除服务删除状态，不代表公众号全部历史。",
    }
    # 在本次文章报告中保存覆盖范围，不把 AK/SK 或鉴权头写入输出。
    report = Path(result["report"])
    document = json.loads(report.read_text(encoding="utf-8-sig"))
    document.update({key: result[key] for key in ("failures", "skipped", "history_coverage")})
    temporary = report.with_name(report.name + ".history.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report)
    log("公众号历史导出已结束；请查看报告中的实际覆盖范围与失败原因。")
    return result

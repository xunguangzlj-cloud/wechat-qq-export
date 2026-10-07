"""在独立 Chrome/Edge 会话中人工验证后读取正文，不接触既有浏览器资料。"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from mp_articles import _atomic_json, _time, export_articles, extract_article_links, normalize_article_url


START_TIMEOUT = 15
PAGE_WAIT_SECONDS = 10
COMMAND_TIMEOUT = 5
HTML_LIMIT = 8 * 1024 * 1024
CDP_LIMIT = 32 * 1024 * 1024
FALLBACK_NOTE = "若浏览器调试接口不可用，可在普通浏览器保存文章 HTML/MHTML，再使用本地网页导入。"

# 只读取本次页的正文和公开元数据；在其它域名或验证路径不读取 DOM 正文。
DOM_EXPRESSION = r"""(() => {
  const u = new URL(location.href);
  if (!['http:', 'https:'].includes(u.protocol) || u.hostname !== 'mp.weixin.qq.com')
    return {url: location.href, stage: 'other_page'};
  if (u.pathname !== '/s' && !/^\/s\/[A-Za-z0-9_-]+\/?$/.test(u.pathname))
    return {url: location.href, stage: /captcha/i.test(u.pathname) ? 'verification' : 'other_page'};
  const body = document.querySelector('#js_content');
  if (!body) {
    const screen = (document.body?.innerText || '').slice(0, 1000);
    return {url: location.href, stage: /验证码|安全验证|环境异常|访问过于频繁|请完成验证/.test(screen) ? 'verification' : 'loading'};
  }
  const style = getComputedStyle(body);
  if (style.display === 'none' || style.visibility === 'hidden')
    return {url: location.href, stage: 'loading'};
  const bodyHtml = body.innerHTML;
  if (bodyHtml.length > 8 * 1024 * 1024) return {url: location.href, stage: 'too_large'};
  const text = s => document.querySelector(s)?.textContent?.trim() || null;
  const clock = typeof ct === 'number' || typeof ct === 'string' ? ct : window.ct;
  return {url: location.href, stage: 'article', body_html: bodyHtml,
    body_text: body.innerText || '', media_count: body.querySelectorAll('img,audio,video,iframe,mpvoice').length,
    title: text('#activity-name, #js_article_title') || document.title,
    account: text('#js_name, .profile_nickname'), author: text('#js_author_name'),
    published_text: text('#publish_time'), ct: typeof clock === 'number' || typeof clock === 'string' ? clock : null};
})()"""


def _find_browser():
    candidates = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.extend((Path(base) / "Microsoft/Edge/Application/msedge.exe",
                               Path(base) / "Google/Chrome/Application/chrome.exe"))
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    raise RuntimeError("没有找到 Chrome 或 Edge；请安装浏览器后重试。" + FALLBACK_NOTE)


def _managed_root():
    # 显式使用 TEMP，避免其它微信任务临时修改 tempfile.tempdir 影响会话位置。
    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    root = Path(base).resolve() / "WechatChatExport" / "browser-runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _validate_debugger_url(url, port):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "ws" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                 and parsed.port == port and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment
                 and re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", parsed.path))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise RuntimeError("浏览器调试地址不是本次会话的本机端点，已拒绝连接。")
    return url


class _BrowserSession:
    def __init__(self):
        self.root = self.profile = self.process = self.socket = None
        self.target_id = self.session_id = None
        self.counter, self.navigated, self.closed = 0, False, False

    def start(self):
        try:
            import websocket
        except ImportError:
            raise RuntimeError("当前程序缺少浏览器连接组件 websocket-client，请安装新版工具。" + FALLBACK_NOTE) from None
        executable = _find_browser()
        self.root = _managed_root()
        self.profile = Path(tempfile.mkdtemp(prefix="browser-", dir=self.root))
        # 参数按列表传递，不使用 shell，也不读取用户默认 profile 或 Cookie。
        self.process = subprocess.Popen(
            [executable, "--user-data-dir=" + str(self.profile), "--remote-debugging-port=0",
             "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + START_TIMEOUT
        active = self.profile / "DevToolsActivePort"
        while time.monotonic() < deadline:
            try:
                lines = active.read_text(encoding="utf-8").splitlines()
                port = int(lines[0])
                browser_path = lines[1]
                if not 1 <= port <= 65535 or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", browser_path):
                    raise RuntimeError("专用浏览器生成了无效的调试端点。")
                break
            except (OSError, IndexError, ValueError):
                time.sleep(0.1)
        else:
            raise RuntimeError("专用浏览器调试接口未启动，可能受浏览器策略限制。" + FALLBACK_NOTE)
        for host in ("127.0.0.1", "[::1]"):
            address = _validate_debugger_url(f"ws://{host}:{port}{browser_path}", port)
            try:
                self.socket = websocket.create_connection(
                    address, timeout=COMMAND_TIMEOUT, suppress_origin=True,
                    http_no_proxy=["127.0.0.1", "localhost", "::1"], redirect_limit=0,
                )
                break
            except Exception:
                self.socket = None
        if self.socket is None:
            raise RuntimeError("无法连接本次专用浏览器的本机调试接口。" + FALLBACK_NOTE)
        target = self.command("Target.createTarget", {"url": "about:blank"})
        self.target_id = target.get("targetId")
        if not isinstance(self.target_id, str) or not self.target_id:
            raise RuntimeError("浏览器没有创建本次采集专用页。")
        attached = self.command("Target.attachToTarget", {"targetId": self.target_id, "flatten": True})
        self.session_id = attached.get("sessionId")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise RuntimeError("浏览器没有返回专用页会话。")

    def command(self, method, params=None):
        params = params or {}
        if method not in {"Target.createTarget", "Target.attachToTarget", "Browser.close", "Page.navigate", "Runtime.evaluate"}:
            raise RuntimeError("浏览器采集只允许本次页导航和只读正文提取。")
        if method == "Target.attachToTarget" and params.get("targetId") != self.target_id:
            raise RuntimeError("已拒绝附加到其它浏览器页。")
        if self.socket is None:
            raise RuntimeError("专用浏览器连接已关闭。")
        self.counter += 1
        command = {"id": self.counter, "method": method, "params": params}
        if method in {"Page.navigate", "Runtime.evaluate"}:
            if not self.session_id:
                raise RuntimeError("本次专用页没有可用会话。")
            command["sessionId"] = self.session_id
        deadline = time.monotonic() + COMMAND_TIMEOUT
        try:
            self.socket.send(json.dumps(command, ensure_ascii=False))
            while time.monotonic() < deadline:
                self.socket.settimeout(max(0.1, deadline - time.monotonic()))
                raw = self.socket.recv()
                if not isinstance(raw, (str, bytes)) or len(raw) > CDP_LIMIT:
                    raise RuntimeError("浏览器响应无效或超过大小上限。")
                response = json.loads(raw)
                if not isinstance(response, dict) or response.get("id") != self.counter:
                    continue
                if response.get("error"):
                    raise RuntimeError("浏览器调试命令失败：" + method)
                result = response.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("浏览器命令响应缺少有效结果。")
                return result
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("专用浏览器连接中断或响应超时，请重新打开浏览器采集。") from None
        raise RuntimeError("专用浏览器命令等待超时。")

    def snapshot(self):
        result = self.command("Runtime.evaluate", {"expression": DOM_EXPRESSION, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise RuntimeError("浏览器当前页无法读取正文，请等待页面完成加载后重试。")
        value = (result.get("result") or {}).get("value")
        if not isinstance(value, dict):
            raise RuntimeError("浏览器未返回有效正文信息。")
        return value

    def navigate(self, url):
        url = normalize_article_url(url)
        if self.navigated:
            # 先确认空白页已提交，防止下一篇加载时把上一页正文误归到新 URL。
            self.command("Page.navigate", {"url": "about:blank"})
            deadline = time.monotonic() + COMMAND_TIMEOUT
            while time.monotonic() < deadline:
                if self.snapshot().get("url") == "about:blank":
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("浏览器未完成页面切换，已停止读取，避免误归上一篇文章。")
        result = self.command("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise RuntimeError("浏览器无法打开所选文章，请在专用窗口中检查网络和页面。")
        self.navigated = True

    def wait_article(self):
        deadline = time.monotonic() + PAGE_WAIT_SECONDS
        reason = "正文尚未加载，请在专用浏览器窗口确认文章已打开。"
        while time.monotonic() < deadline:
            value = self.snapshot()
            stage = value.get("stage")
            if stage == "verification":
                reason = "微信要求人工安全验证，请在专用浏览器窗口完成验证并打开正文。"
            elif stage == "too_large":
                return None, "正文超过 8 MiB 上限，请保存 HTML/MHTML 后导入。"
            elif stage == "other_page":
                reason = "当前页不是可采集的公众号文章，请在专用浏览器窗口打开原文章。"
            elif stage == "article":
                try:
                    actual = normalize_article_url(value.get("url"))
                except (ValueError, TypeError):
                    reason = "当前浏览器地址不是有效的公众号文章链接。"
                else:
                    body = value.get("body_html")
                    if not isinstance(body, str) or len(body.encode("utf-8")) > HTML_LIMIT:
                        return None, "浏览器正文无效或超过 8 MiB 上限。"
                    if body.strip() and ((value.get("body_text") or "").strip() or value.get("media_count", 0)):
                        value["url"] = actual
                        return value, None
                    reason = "浏览器当前页没有可见正文，不能保存为成功文章。"
            time.sleep(0.25)
        return None, reason

    def close(self):
        if self.closed:
            return {"cleanup_ok": self.profile is None, "cleanup_path": str(self.profile) if self.profile else None}
        self.closed = True
        close_error = None
        if self.socket is not None:
            try:
                self.command("Browser.close")
            except RuntimeError:
                # 关闭浏览器会先断开 CDP；以进程退出和目录清理结果判断是否完成。
                close_error = "浏览器关闭命令未收到确认。"
            finally:
                try:
                    self.socket.close(timeout=1)
                except Exception:
                    pass
                self.socket = None
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return {"cleanup_ok": False, "cleanup_path": str(self.profile),
                        "cleanup_reason": "本次专用浏览器仍在运行；请关闭其窗口后清理此临时目录。"}
        if self.profile is None:
            return {"cleanup_ok": True, "cleanup_path": None}
        profile, root = self.profile.resolve(), self.root.resolve()
        if profile.parent != root or not profile.name.startswith("browser-"):
            return {"cleanup_ok": False, "cleanup_path": str(self.profile), "cleanup_reason": "目录不在本次浏览器管理范围，已拒绝自动清理。"}
        for attempt in range(5):
            try:
                shutil.rmtree(profile)
                self.profile = None
                return {"cleanup_ok": True, "cleanup_path": None}
            except FileNotFoundError:
                self.profile = None
                return {"cleanup_ok": True, "cleanup_path": None}
            except OSError:
                time.sleep(0.2)
        return {"cleanup_ok": False, "cleanup_path": str(profile),
                "cleanup_reason": close_error or "专用浏览器资料目录仍被占用，未完成清理。"}


def _cache_from_snapshot(value):
    actual = normalize_article_url(value["url"])
    published = None
    ct = value.get("ct")
    if (type(ct) is int or isinstance(ct, str) and ct.isascii() and ct.isdigit()):
        numeric = int(ct)
        if 946684800 <= numeric <= 4133980799:
            published = _time(numeric)
    if published is None:
        text = value.get("published_text")
        # 页面使用不补零的中文日期；转成 ISO 字段后保留实际显示的时分。
        match = re.fullmatch(r"\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*", text) if isinstance(text, str) else None
        if match:
            year, month, day, hour, minute, second = (int(part or 0) for part in match.groups())
            text = f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"
        published = _time(text)
    metadata = '<meta property="og:url" content="' + html.escape(actual, quote=True) + '">'
    page = "<html><head>" + metadata + '</head><body><div id="js_content">' + value["body_html"] + "</div></body></html>"
    return actual, {"html": page, "title": value.get("title"), "account": value.get("account"),
                    "author": value.get("author"), "published_at": published.isoformat() if published else None,
                    "source": "browser_page"}


def export_browser_articles(urls, out_root="exports", *, start_time=None, end_time=None,
                            export_images=True, incremental=True, progress=None, wait_for_user=None):
    from exporter_core import parse_time_range

    parse_time_range(start_time, end_time)
    urls = extract_article_links(urls) if isinstance(urls, str) else list(dict.fromkeys(normalize_article_url(url) for url in urls))
    if not urls:
        raise ValueError("请输入至少一个有效的微信公众号文章链接。")
    log = lambda text: progress(text) if progress else None
    session, caches, actual_urls = _BrowserSession(), {}, []
    failures, skipped, captures = [], [], []
    cleanup = {"cleanup_ok": True, "cleanup_path": None}
    try:
        session.start()
        log("专用浏览器已打开；需要验证时，请在该窗口中手动完成，再选择继续采集。")
        for requested in urls:
            record = {"requested_url": requested, "actual_url": None, "manual_confirmations": 0, "status": "failed"}
            try:
                session.navigate(requested)
                snapshot, reason = session.wait_article()
                cancelled = False
                for _ in range(2):
                    if snapshot is not None or wait_for_user is None:
                        break
                    record["manual_confirmations"] += 1
                    if not wait_for_user(requested, reason):
                        cancelled = True
                        break
                    snapshot, reason = session.wait_article()
                if cancelled:
                    record.update(status="skipped", reason="用户跳过人工验证或页面等待。")
                    skipped.append({"url": requested, "reason": record["reason"]})
                    continue
                if snapshot is None:
                    raise RuntimeError((reason or "当前页没有可采集正文。") + "本次没有保存正文；可使用 HTML/MHTML 导入。")
                actual, cache = _cache_from_snapshot(snapshot)
                record.update(actual_url=actual, status="captured")
                if actual not in caches:
                    caches[actual] = cache
                    actual_urls.append(actual)
                else:
                    record.update(status="duplicate_actual", reason="当前文章已在本次浏览器任务中读取。")
                    skipped.append({"url": requested, "reason": record["reason"]})
            except Exception as error:
                text = str(error)[:400] or "浏览器读取失败。"
                record["reason"] = text
                failures.append({"url": requested, "error": text})
                log("浏览器文章读取失败：" + text)
            finally:
                captures.append(record)
    except Exception as error:
        text = str(error)[:400] or "专用浏览器启动失败。"
        failures.extend({"url": url, "error": text} for url in urls)
        captures.extend({"requested_url": url, "actual_url": None, "status": "startup_failed", "reason": text} for url in urls)
        log("浏览器采集未启动：" + text)
    finally:
        cleanup = session.close()
        if not cleanup["cleanup_ok"]:
            log("专用浏览器临时目录未清理：" + cleanup.get("cleanup_reason", "") + " 路径：" + str(cleanup["cleanup_path"]))
    result = export_articles(actual_urls, out_root, start_time=start_time, end_time=end_time,
                             export_images=export_images, incremental=incremental, progress=progress, articles=caches)
    result["failures"].extend(failures)
    result["skipped"].extend(skipped)
    result["browser_coverage"] = {
        "source": "browser_page", "captures": captures, **cleanup,
        "note": "只读取本次专用浏览器页已加载的正文；人工验证不保证成功。图片沿用正文导出模块，缺失原因见文章 JSON。",
    }
    report_path = Path(result["report"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update({key: result[key] for key in ("failures", "skipped", "browser_coverage")})
    _atomic_json(report_path, report)
    return result

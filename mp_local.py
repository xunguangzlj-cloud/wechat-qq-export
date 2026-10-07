"""从用户指定的微信本机会话提取文章链接，再采集公开正文。"""

import json
from pathlib import Path

from mp_articles import _atomic_json, export_articles, extract_article_links


def export_local_articles(names, out_root="exports", *, db_dir=None, chat_id=None,
                          start_time=None, end_time=None, export_images=True, incremental=True,
                          progress=None, select_account=None):
    import exporter_core as core

    core.parse_time_range(start_time, end_time)
    names = names.splitlines() if isinstance(names, str) else list(names)
    if any(not isinstance(name, str) for name in names):
        raise ValueError("公众号或会话名称应为文本，每行一个。")
    names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not names:
        raise ValueError("请输入至少一个公众号名称、gh_ ID 或明确会话名称。")
    if chat_id is not None and (not isinstance(chat_id, str) or not chat_id.strip() or len(names) != 1):
        raise ValueError("明确会话 ID 仅适用于单个名称，请按名称逐项选择批量会话。")
    chat_id = chat_id.strip() if chat_id else None
    log = lambda message: progress(message) if progress else None
    core.clear_current_sensitive_cache()
    workdir = core._create_sensitive_workdir()
    urls, failures, skipped, reports, seen_sessions = [], [], [], [], set()
    try:
        log("正在从指定微信会话读取本机已有文章链接；消息阶段不按接收时间筛选，正文按发布时间筛选。")
        log("数据库密钥和解密缓存使用一次性临时目录，提取链接后立即清理。")
        with core._upstream_sensitive_sandbox(workdir):
            directory = core.resolve_db_dir(db_dir) if db_dir else None
            db = core.WeChatDB(db_dir=directory, workdir=str(workdir))
            for keyword in names:
                coverage = {"keyword": keyword, "history_complete": False, "message_count": 0, "article_links_found": 0}
                try:
                    try:
                        target = core.find_contact(db, keyword, chat_id=chat_id or (keyword if keyword.startswith("gh_") else None))
                    except core.AmbiguousContactError as error:
                        if select_account is None:
                            raise ValueError("本机存在多个同名或相似会话，请选择准确微信 ID 后读取。") from None
                        selected = select_account(error.candidates, keyword)
                        if selected is None:
                            skipped.append({"url": "", "account": keyword, "reason": "已取消本机会话选择。"})
                            coverage.update(stop_reason="selection_cancelled", stop_description="用户取消会话选择")
                            continue
                        if selected not in {item["username"] for item in error.candidates}:
                            raise ValueError("所选微信 ID 不属于本机返回的名称候选。")
                        target = core.find_contact(db, keyword, chat_id=selected)
                    username = target["username"]
                    display_name = target.get("remark") or target.get("nick_name") or keyword
                    coverage.update(username=username, display_name=display_name,
                                    session_kind="public_account" if username.startswith("gh_") else "conversation")
                    if username in seen_sessions:
                        skipped.append({"url": "", "account": keyword, "reason": "该本机会话已在本次任务中读取。"})
                        coverage.update(stop_reason="duplicate_session", stop_description="本次已读取此会话")
                        continue
                    seen_sessions.add(username)
                    log("读取本机" + ("公众号" if username.startswith("gh_") else "会话分享链接") + "：" + display_name)
                    rows = core.load_rows(db, username)
                    coverage["message_count"] = len(rows)
                    session_links, undecodable = [], 0
                    for row in rows:
                        decoded = []
                        # 保留原始 XML 的全部 item；卡片摘要可能只显示第一篇。
                        for field in ("message_content", "compress_content", "source", "packed_info_data"):
                            raw = row.get(field)
                            if raw not in (None, b"", ""):
                                value = core.decode_blob(raw)
                                decoded.append(value)
                                if not value:
                                    undecodable += 1
                        session_links.extend(extract_article_links(decoded))
                    session_links = extract_article_links(session_links)
                    urls.extend(session_links)
                    coverage.update(article_links_found=len(session_links), undecodable_fields=undecodable,
                                    stop_reason="local_message_scan_complete", stop_description="已扫描该会话本机可读取的消息")
                    if not session_links:
                        skipped.append({"url": "", "account": display_name, "reason": "该本机会话没有可识别的公众号文章链接；本机记录不代表公众号全部历史。"})
                    log(f"{display_name}：本机 {len(rows)} 条消息，发现 {len(session_links)} 个文章链接。")
                except Exception as error:
                    text = str(error)
                    failures.append({"url": "", "account": keyword, "error": text})
                    coverage.update(stop_reason="account_error", stop_description="本机会话查找或读取失败", error=text)
                    log("本机会话读取失败，继续下一名称：" + text)
                finally:
                    reports.append(coverage)
    except BaseException:
        core._cleanup_sensitive_workdir(workdir, progress=progress)
        raise
    cleanup_ok, cleanup_error = core._cleanup_sensitive_workdir(workdir, progress=progress)
    urls = extract_article_links(urls)
    result = export_articles(urls, out_root, start_time=start_time, end_time=end_time,
                             export_images=export_images, incremental=incremental, progress=progress)
    result["failures"].extend(failures)
    result["skipped"].extend(skipped)
    result["local_coverage"] = {
        "source": "wechat_local_messages", "full_history_verified": False,
        "message_time_filter_applied": False, "article_time_filter": {"start": start_time, "end": end_time},
        "local_message_count": sum(item["message_count"] for item in reports), "article_link_count": len(urls),
        "scope_description": "仅来自指定本机会话中可读取的文章分享链接；不代表公众号全部历史。",
        "accounts": reports,
    }
    result["sensitive_cache_cleanup"] = cleanup_ok
    result["sensitive_cache_cleanup_error"] = cleanup_error
    report_path = Path(result["report"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update({key: result[key] for key in ("failures", "skipped", "local_coverage", "sensitive_cache_cleanup", "sensitive_cache_cleanup_error")})
    _atomic_json(report_path, report)
    return result

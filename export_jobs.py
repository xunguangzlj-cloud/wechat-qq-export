"""依次导出用户选择的会话，并留下不含连接凭据的批量报告。"""

import json
import tempfile
from datetime import datetime
from pathlib import Path

import exporter_core as core
import qq_exporter as qq


def _chat_names(names):
    # 只按换行拆分；逗号可以是联系人名或群名的一部分。
    values = names.splitlines() if isinstance(names, str) else names
    return list(dict.fromkeys(str(name).strip() for name in values if str(name).strip()))


def _safe_value(value, token):
    if isinstance(value, str):
        return value.replace(token, "[已隐藏]") if token else value
    if isinstance(value, dict):
        return {key: _safe_value(item, token) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, token) for item in value]
    return value


def export_chats(names, outdir, *, platform="微信", incremental=False,
                 progress=None, select_contact=None, **options):
    """单项失败或取消时继续后项；报告写入失败会向调用者报错。"""
    keywords = _chat_names(names)
    if not keywords:
        raise ValueError("请输入至少一个联系人名或群名，每行一个。")
    if platform not in {"微信", "QQ"}:
        raise ValueError("聊天平台只能选择微信或 QQ。")
    root = Path(outdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(core.CHAT_TIMEZONE).isoformat(timespec="seconds")
    batch_dir = Path(tempfile.mkdtemp(
        prefix="batch_" + datetime.now(core.CHAT_TIMEZONE).strftime("%Y%m%d_%H%M%S") + "_",
        dir=root,
    ))
    # 增量归档必须使用用户选定的固定根目录，不能随批次目录改变。
    chat_root = root if incremental else batch_dir
    exporter = qq.export_qq_chat if platform == "QQ" else core.export_chat
    token = options.get("token", "") if platform == "QQ" else ""
    token = token if isinstance(token, str) else ""
    results, failures, cancelled = [], [], []

    def log(text):
        if progress:
            progress(_safe_value(str(text), token))

    for index, keyword in enumerate(keywords, 1):
        log(f"正在导出 {index}/{len(keywords)}：{keyword}")
        item_options = dict(options, incremental=incremental, progress=log)
        try:
            try:
                result = exporter(keyword, out_root=chat_root, **item_options)
            except core.AmbiguousContactError as error:
                if select_contact is None:
                    raise
                chat_id = select_contact(error.candidates, keyword)
                if chat_id is None:
                    cancelled.append(keyword)
                    log(f"已取消会话：{keyword}；继续下一项。")
                    continue
                item_options["chat_id"] = chat_id
                result = exporter(keyword, out_root=chat_root, **item_options)
            results.append(dict(result, keyword=keyword))
            log(f"已完成会话：{keyword}")
        except Exception as error:
            message = _safe_value(str(error), token)
            failures.append({"keyword": keyword, "error": message})
            log(f"会话导出失败：{keyword}；{message}；继续下一项。")

    # 仅保存会话和输出摘要；不序列化连接选项、Token 或数据库密钥。
    report_keys = (
        "keyword", "chat_name", "chat_username", "is_group", "message_count",
        "output_dir", "txt", "md", "json", "media_stats", "incremental",
        "new_message_count", "checkpoint_advanced", "pending_read",
        "history_coverage",
    )
    report_path = batch_dir / "batch_report.json"
    report = {
        "exporter_version": core.APP_VERSION,
        "platform": platform,
        "incremental": bool(incremental),
        "started_at": started_at,
        "finished_at": datetime.now(core.CHAT_TIMEZONE).isoformat(timespec="seconds"),
        "names": keywords,
        "requested_count": len(keywords),
        "success_count": len(results),
        "failure_count": len(failures),
        "cancelled_count": len(cancelled),
        "results": [{key: result[key] for key in report_keys if key in result} for result in results],
        "failures": failures,
        "cancelled": cancelled,
    }
    report_path.write_text(json.dumps(_safe_value(report, token), ensure_ascii=False, indent=2), encoding="utf-8-sig")
    log(f"批量导出结束：成功 {len(results)}，失败 {len(failures)}，取消 {len(cancelled)}。报告：{report_path}")
    return {
        "results": results, "failures": failures, "cancelled": cancelled,
        "output_dir": str(batch_dir), "report": str(report_path),
    }

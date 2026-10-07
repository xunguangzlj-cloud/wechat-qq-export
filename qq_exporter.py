"""通过用户已配置的本机 NapCat 读取 QQ 历史，不执行登录或发送操作。"""

import base64
import binascii
import hashlib
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import exporter_core as core
from incremental_export import IncrementalExport, message_key
from export_names import export_paths, export_stem


PAGE_SIZE = 200
MAX_HISTORY_PAGES = 500
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MEDIA_KINDS = {"image": "image", "file": "file", "record": "voice", "video": "video"}
MEDIA_LABELS = {"image": "图片", "file": "文件", "voice": "语音", "video": "视频"}
READ_ACTIONS = {
    "get_version_info", "get_login_info", "get_group_list", "get_friend_list",
    "get_group_msg_history", "get_friend_msg_history", "get_image", "get_file", "get_record",
}
STOP_DESCRIPTIONS = {
    "page_limit": "达到本次历史读取页数上限",
    "api_error": "历史接口中途出错",
    "empty_page": "历史接口返回空页",
    "no_new_messages": "页面没有新增消息",
    "unknown_cursor_time": "页面时间均未知，无法确定历史游标",
    "start_boundary_reached": "已读取到起始时间之前的消息",
    "missing_cursor_id": "最早消息缺少可用游标 ID",
    "ambiguous_short_id_cursor": "游标短 ID 对应不同原始序号，无法安全继续",
    "cursor_not_advancing": "历史游标没有前进",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不把本机接口的鉴权信息转发到重定向目标。
        return None


class NapCatClient:
    def __init__(self, endpoint, token=""):
        try:
            parts = urlsplit(str(endpoint).strip())
            valid = (
                parts.scheme == "http"
                and parts.hostname in {"127.0.0.1", "localhost", "::1"}
                and not parts.username and not parts.password
                and not parts.query and not parts.fragment
                and (parts.port is None or 1 <= parts.port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("QQ 接口必须是本机 HTTP 地址，例如 http://127.0.0.1:3000；不接受账号、查询参数或远端地址。")
        if not isinstance(token, str) or any(ord(character) < 32 or ord(character) > 126 for character in token):
            raise ValueError("QQ 接口 Token 只能包含可打印的英文字母、数字和符号；请检查复制内容，不支持中文、换行或控制字符。")
        self.endpoint = str(endpoint).strip().rstrip("/")
        self.token = token
        # 不使用环境代理，避免本机鉴权请求流向代理服务器。
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def redact(self, text):
        value = str(text)
        return value.replace(self.token, "[已隐藏]") if self.token else value

    def call(self, action, params=None):
        if action not in READ_ACTIONS:
            raise ValueError("QQ 接入只允许读取列表、历史和附件。")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(
            self.endpoint + "/" + action,
            data=json.dumps(params or {}, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.status != 200:
                    raise RuntimeError(f"QQ 接口 {action} 返回 HTTP {response.status}。")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            hint = "；请检查 QQ 接口 Token" if exc.code in {401, 403} else ""
            raise RuntimeError(f"QQ 接口 {action} 返回 HTTP {exc.code}{hint}。") from None
        except (URLError, OSError) as exc:
            raise RuntimeError(f"QQ 接口 {action} 连接失败：{self.redact(exc)}") from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise RuntimeError(f"QQ 接口 {action} 响应超过 128 MiB，未继续读取。")
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeError):
            raise RuntimeError(f"QQ 接口 {action} 未返回有效 JSON。") from None
        if not isinstance(result, dict) or type(result.get("retcode")) is not int or "data" not in result:
            raise RuntimeError(f"QQ 接口 {action} 响应结构无效，缺少 retcode/data。")
        if result.get("status") != "ok" or result["retcode"] != 0:
            detail = self.redact(result.get("message") or result.get("wording") or "未提供原因")
            raise RuntimeError(f"QQ 接口 {action} 失败（retcode={result['retcode']}）：{detail}")
        return result["data"]


def _id(value):
    if type(value) is int:
        return str(value) if value != 0 else None
    if isinstance(value, str) and value.strip() and value.strip() != "0":
        return value.strip()
    return None


def _qq_id(value):
    value = _id(value)
    return value if value and value.isascii() and value.isdigit() else None


def _connection(client):
    version = client.call("get_version_info")
    if not isinstance(version, dict) or version.get("app_name") != "NapCat.Onebot" or version.get("protocol_version") != "v11":
        raise RuntimeError("QQ 历史接入仅支持 NapCat.Onebot 的 OneBot v11 接口；不支持其它实现的分页游标。")
    login = client.call("get_login_info")
    if not isinstance(login, dict) or not _qq_id(login.get("user_id")):
        raise RuntimeError("QQ 接口未返回有效登录账号，请先在 NapCat 中登录。")
    if login.get("nickname") is not None and not isinstance(login["nickname"], str):
        raise RuntimeError("QQ 接口登录信息中的昵称结构无效。")
    return {"user_id": _qq_id(login["user_id"]), "nickname": login.get("nickname") or ""}


def check_qq_connection(endpoint, token=""):
    """只检测 NapCat 版本和登录账号，不读取会话或历史。"""
    return _connection(NapCatClient(endpoint, token))


def _find_contact(client, keyword, chat_id):
    candidates = []
    for kind, action, id_field, name_field in (
        ("group", "get_group_list", "group_id", "group_name"),
        ("private", "get_friend_list", "user_id", "nickname"),
    ):
        items = client.call(action)
        if not isinstance(items, list):
            raise RuntimeError(f"QQ 接口 {action} 未返回会话数组。")
        for item in items:
            if not isinstance(item, dict) or not _qq_id(item.get(id_field)):
                raise RuntimeError(f"QQ 接口 {action} 含有无效会话标识。")
            candidates.append({
                "username": f"qq:{kind}:{_qq_id(item[id_field])}",
                "nick_name": str(item.get(name_field) or ""),
                "remark": str(item.get("remark") or ""), "chat_type": kind,
            })
    candidates = list({item["username"]: item for item in candidates}.values())
    query = str(chat_id or keyword).strip()
    if chat_id or query.startswith("qq:"):
        matched = [item for item in candidates if item["username"] == query]
    else:
        exact = [item for item in candidates if query in {
            item["nick_name"], item["remark"], item["username"].rsplit(":", 1)[-1],
        }]
        matched = exact or [item for item in candidates if query in item["nick_name"] or query in item["remark"]]
    if not matched:
        raise ValueError("没有找到指定 QQ 联系人或群聊；请使用更准确名称或 qq:group:群号 / qq:private:QQ号。")
    if len(matched) > 1:
        raise core.AmbiguousContactError(query, matched)
    return matched[0]


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        if isinstance(value, float) and not value.is_integer():
            return None
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        if timestamp <= 0 or len(core.fmt_time(timestamp)) != 19:
            return None
        return timestamp
    except (ValueError, OverflowError):
        return None


def _real_sequence(raw):
    value = _id(raw.get("real_seq"))
    return int(value) if value and value.isascii() and value.isdigit() and int(value) > 0 else None


def _history(client, target, time_filter, log):
    is_group = target["chat_type"] == "group"
    action = "get_group_msg_history" if is_group else "get_friend_msg_history"
    params = {
        "group_id" if is_group else "user_id": target["username"].rsplit(":", 1)[-1],
        "count": PAGE_SIZE, "reverse_order": True,
        "parse_mult_msg": False, "disable_get_url": True,
    }
    rows, seen, cursors, short_ids = [], set(), set(), {}
    coverage = {"history_complete": False, "pages_read": 0, "stop_reason": "page_limit", "warnings": []}
    for page_number in range(MAX_HISTORY_PAGES):
        try:
            data = client.call(action, params)
            if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
                raise RuntimeError("QQ 历史接口未返回 messages 数组。")
            page = data["messages"]
            if any(not isinstance(item, dict) for item in page):
                raise RuntimeError("QQ 历史数组含有非消息对象。")
        except RuntimeError as exc:
            if not rows:
                raise
            coverage["stop_reason"] = "api_error"
            coverage["error"] = client.redact(exc)
            log("QQ 历史中途停止，将导出已获取部分：" + coverage["error"])
            break
        coverage["pages_read"] = page_number + 1
        if not page:
            coverage["stop_reason"] = "empty_page"
            break
        added = 0
        page_rows = []
        for raw in page:
            message_id = _id(raw.get("message_id"))
            sequence = _real_sequence(raw)
            real_seq = str(sequence) if sequence is not None else None
            if _real_sequence(raw) is None and "missing_or_invalid_real_seq" not in coverage["warnings"]:
                coverage["warnings"].append("missing_or_invalid_real_seq")
            key = (real_seq, message_id) if real_seq else (None, message_key("qq", {"raw": raw}) if message_id else None)
            if message_id and key in seen:
                continue
            if message_id:
                seen.add(key)
                seqs = short_ids.setdefault(message_id, set())
                seqs.add(real_seq)
                if len(seqs) > 1 and "short_id_collision" not in coverage["warnings"]:
                    coverage["warnings"].append("short_id_collision")
            row = {"raw": raw, "create_time": _timestamp(raw.get("time")), "_message_id": message_id}
            rows.append(row)
            page_rows.append(row)
            added += 1
        log(f"QQ 历史已读取 {page_number + 1} 页，共 {len(rows)} 条；尚未确认历史完整性。")
        if not added:
            coverage["stop_reason"] = "no_new_messages"
            break
        dated = [row for row in page_rows if row["create_time"] is not None]
        if not dated:
            coverage["stop_reason"] = "unknown_cursor_time"
            break
        oldest = min(dated, key=lambda row: (
            row["create_time"],
            _real_sequence(row["raw"]) if _real_sequence(row["raw"]) is not None else float("inf"),
        ))
        if time_filter["start_ts"] is not None and oldest["create_time"] < time_filter["start_ts"]:
            coverage["stop_reason"] = "start_boundary_reached"
            break
        cursor = oldest["_message_id"]
        if not cursor:
            coverage["stop_reason"] = "missing_cursor_id"
            break
        if len(short_ids.get(cursor, ())) > 1:
            coverage["stop_reason"] = "ambiguous_short_id_cursor"
            break
        if cursor in cursors:
            coverage["stop_reason"] = "cursor_not_advancing"
            break
        cursors.add(cursor)
        params["message_seq"] = cursor
    times = [row["create_time"] for row in rows if row["create_time"] is not None]
    coverage.update({
        "messages_seen": len(rows), "oldest_time": core.fmt_time(min(times)) if times else None,
        "newest_time": core.fmt_time(max(times)) if times else None,
        "page_limit": MAX_HISTORY_PAGES, "cursor_kind": "napcat_short_message_id",
        "stop_description": STOP_DESCRIPTIONS[coverage["stop_reason"]],
    })
    log(f"QQ 历史读取停止：{coverage['stop_description']}；只导出接口实际返回的数据，不声明完整历史。")
    if "missing_or_invalid_real_seq" in coverage["warnings"]:
        log("部分 QQ 消息缺少有效原始序号；同秒消息顺序降级，无法确认接口是否遗漏。")
    if "short_id_collision" in coverage["warnings"]:
        log("检测到 QQ 接口短 ID 对应不同原始序号，已保留不同消息；接口仍可能存在历史缺口。")
    return rows, coverage


def _local_file(value):
    # file://资源ID、远端地址、UNC共享均不作为本机附件路径。
    if not isinstance(value, str) or not value or "://" in value or value.startswith(("\\\\", "//")):
        return None
    path = Path(value)
    try:
        return path if path.is_absolute() and path.is_file() else None
    except OSError:
        return None


def _save_media(data, folder, stem, kind):
    if not isinstance(data, dict):
        raise RuntimeError("QQ 附件接口响应结构无效。")
    source = _local_file(data.get("file")) or _local_file(data.get("url"))
    fallback = {"image": "image.bin", "file": "file.bin", "voice": "voice.silk", "video": "video.mp4"}[kind]
    name = core.safe_file_name(str(data.get("file_name") or (source.name if source else fallback)))
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (stem + "_" + name)
    if source:
        shutil.copyfile(source, destination)
    elif isinstance(data.get("base64"), str) and data["base64"]:
        try:
            content = base64.b64decode(data["base64"], validate=True)
        except (ValueError, binascii.Error):
            raise RuntimeError("QQ 附件 Base64 无效。") from None
        if not content:
            raise RuntimeError("QQ 附件 Base64 内容为空。")
        destination.write_bytes(content)
    else:
        raise RuntimeError("QQ 附件未提供可读取的本机文件或 Base64；未下载远端 URL。")
    return destination, name


def _is_wav(path):
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
        return header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    except OSError:
        return False


def _media_for_element(client, element, chat_dir, stem):
    kind = MEDIA_KINDS[element["type"]]
    data = element.get("data")
    if not isinstance(data, dict):
        return {"kind": kind, "available": False, "path": None, "reason": "QQ 附件元素结构无效。"}
    folder = chat_dir / "media" / ("voices" if kind == "voice" else kind + "s")
    resource = data.get("file_id") or data.get("file")
    attachment = {"kind": kind, "available": False, "path": None, "name": str(data.get("name") or "")}
    original = None
    try:
        if kind == "voice" and (
            not isinstance(resource, (str, int)) or isinstance(resource, bool) or not str(resource)
        ):
            raise RuntimeError("QQ 语音缺少资源标识。")
        if isinstance(resource, str) and resource.lower().startswith(("http:", "https:", "ftp:")):
            raise RuntimeError("QQ 附件仅提供远端 URL；未自动下载。")
        if kind == "voice":
            try:
                raw_data = {"file": str(_local_file(data.get("file")))} if _local_file(data.get("file")) else client.call("get_file", {"file": resource})
                original, _ = _save_media(raw_data, folder, stem + "_original", kind)
            except (RuntimeError, OSError) as exc:
                attachment["original_reason"] = client.redact(exc)
            try:
                resolved = client.call("get_record", {"file": resource, "out_format": "wav"})
                # NapCat 转码后仍可能返回原 SILK 文件名，不能据此把 WAV 保存成 SILK。
                if isinstance(resolved, dict):
                    converted = _local_file(resolved.get("file")) or _local_file(resolved.get("url"))
                    converted_name = converted.name if converted else core.safe_file_name(str(resolved.get("file_name") or "voice"))
                    resolved = {**resolved, "file_name": str(Path(converted_name).with_suffix(".wav"))}
                path, name = _save_media(resolved, folder, stem, kind)
            except (RuntimeError, OSError) as exc:
                if original is None:
                    raise
                path, name = original, original.name
                attachment["decode_reason"] = client.redact(exc)
            attachment["decoded"] = _is_wav(path)
            if not attachment["decoded"] and "decode_reason" not in attachment:
                attachment["decode_reason"] = "语音接口返回内容不含 RIFF/WAVE 文件头，未按可用 WAV 处理。"
            if original:
                attachment["original_path"] = original.relative_to(chat_dir).as_posix()
        else:
            direct = _local_file(data.get("file")) or _local_file(data.get("path"))
            if direct:
                resolved = {"file": str(direct), "file_name": data.get("name") or direct.name}
            else:
                if not isinstance(resource, (str, int)) or isinstance(resource, bool) or not str(resource):
                    raise RuntimeError("QQ 附件缺少资源标识。")
                if str(resource).lower().startswith(("http:", "https:", "ftp:")):
                    raise RuntimeError("QQ 附件仅提供远端 URL；未自动下载。")
                resolved = client.call("get_image" if kind == "image" else "get_file", {"file": str(resource)})
            path, name = _save_media(resolved, folder, stem, kind)
        attachment.update({"available": True, "path": path.relative_to(chat_dir).as_posix(), "name": name})
        if kind == "image":
            attachment["previewable"] = path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        return attachment
    except (RuntimeError, OSError) as exc:
        attachment["reason"] = client.redact(exc)
        return attachment


def _normalize(row, self_id):
    raw = row["raw"]
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    sender_id = _qq_id(sender.get("user_id") or raw.get("user_id"))
    label = sender.get("card") or sender.get("nickname") or sender_id or "未知发送者"
    elements = raw.get("message")
    parts = []
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, dict):
                parts.append("[无法解析的消息元素]")
                continue
            data = element.get("data") if isinstance(element.get("data"), dict) else {}
            kind = element.get("type")
            if kind == "text":
                parts.append(str(data.get("text") or ""))
            elif kind in MEDIA_KINDS:
                parts.append("[" + MEDIA_LABELS[MEDIA_KINDS[kind]] + ("：" + str(data["name"]) if kind == "file" and data.get("name") else "") + "]")
            elif kind == "at":
                parts.append("@" + str(data.get("qq") or "未知"))
            else:
                parts.append("[" + str(kind or "未知元素") + "]")
        element_status = "array"
    else:
        parts.append(elements if isinstance(elements, str) else str(raw.get("raw_message") or "[无法解析的消息]"))
        element_status = "cq_string_unparsed" if isinstance(elements, str) else "missing_elements"
    return {
        "local_id": row["_message_id"], "napcat_message_id": row["_message_id"],
        "real_seq": _id(raw.get("real_seq")), "platform": "qq",
        "sequence_status": "resolved" if _real_sequence(raw) is not None else "missing_or_invalid_real_seq",
        "type": "QQ消息", "type_code": None, "sender": str(label),
        "sender_username": sender_id, "sender_status": "resolved" if sender_id else "unknown_sender_id",
        "is_self": sender_id == self_id if sender_id else None,
        "time": core.fmt_time(row["create_time"]) if row["create_time"] is not None else "未知时间",
        "content": "".join(parts), "sort_seq": raw.get("real_seq"),
        "transcript": None, "transcript_source": None,
        "media": None, "attachments": [], "elements": elements,
        "element_status": element_status,
    }


def export_qq_chat(keyword, out_root="exports", *, endpoint, token="", progress=None,
                   chat_id=None, start_time=None, end_time=None, export_images=False,
                   export_files=False, export_voices=False, export_videos=False,
                   transcribe_voices=False, incremental=False):
    time_filter = core.parse_time_range(start_time, end_time)
    if not str(keyword or chat_id or "").strip():
        raise ValueError("请输入 QQ 联系人名、群名或精确会话 ID。")
    client = NapCatClient(endpoint, token)
    log = lambda text: progress(client.redact(text)) if progress else None
    login = _connection(client)
    target = _find_contact(client, keyword, chat_id)
    target_name = target["remark"] or target["nick_name"] or target["username"]
    log("已识别 QQ 会话：" + target_name)
    incremental_state = IncrementalExport(out_root, "qq", login["user_id"], target["username"], time_filter) if incremental else None
    read_filter = incremental_state.read_filter if incremental_state else time_filter
    rows, coverage = _history(client, target, read_filter, log)
    rows = core._filter_rows_by_time(rows, read_filter)
    coverage_rows = rows
    if incremental_state:
        rows = incremental_state.select_rows(rows)
        time_filter["invalid_time_excluded"] = read_filter["invalid_time_excluded"]
        log(f"QQ 增量筛选：{len(rows)} 条新增消息；已有 {len(incremental_state.old.get('messages', []))} 条累计消息。")
    if not rows and not incremental:
        raise ValueError("QQ 接口实际返回的记录中没有符合时间范围的消息；未声明所选历史完整。")
    rows.sort(key=lambda row: (
        row["create_time"] if row["create_time"] is not None else float("inf"),
        _real_sequence(row["raw"]) if _real_sequence(row["raw"]) is not None else float("inf"),
    ))
    if time_filter["invalid_time_excluded"]:
        log(f"QQ 时间筛选排除 {time_filter['invalid_time_excluded']} 条时间无效的消息。")
    if transcribe_voices:
        export_voices = True
    enabled = {"image": export_images, "file": export_files, "voice": export_voices, "video": export_videos}
    recognizer = None
    if transcribe_voices and any(isinstance(row["raw"].get("message"), list) and any(
        isinstance(element, dict) and element.get("type") == "record" for element in row["raw"]["message"]
    ) for row in rows):
        recognizer = core._create_local_asr_recognizer()
    root = Path(out_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    chat_hash = hashlib.sha256(target["username"].encode()).hexdigest()[:8]
    chat_dir = incremental_state.directory if incremental_state else Path(tempfile.mkdtemp(prefix="QQ_" + export_stem(target_name) + "_" + chat_hash + "_", dir=root))
    stats = {key: 0 for key in (
        "images_requested", "images_exported", "files_requested", "files_exported",
        "voices_requested", "voices_exported", "voices_decoded", "videos_requested", "videos_exported",
        "voice_transcripts", "voice_transcripts_native", "voice_transcripts_local",
        "voice_transcripts_cached", "voice_transcripts_failed",
    )}
    parsed, sender_counts = [], Counter()
    for index, row in enumerate(rows, 1):
        message = _normalize(row, login["user_id"])
        if incremental_state:
            message["incremental_key"] = row["_incremental_key"]
        sender_counts[message["sender_status"]] += 1
        if message["element_status"] != "array":
            warning = "cq_string_unparsed" if message["element_status"] == "cq_string_unparsed" else "missing_elements"
            if warning not in coverage["warnings"]:
                coverage["warnings"].append(warning)
                log("QQ 消息元素未全部解析；保留原始正文，无法确认这些消息的附件完整性。")
        for element_index, element in enumerate(message["elements"] if isinstance(message["elements"], list) else [], 1):
            if not isinstance(element, dict) or element.get("type") not in MEDIA_KINDS:
                continue
            kind = MEDIA_KINDS[element["type"]]
            if not enabled[kind]:
                continue
            prefix = {"image": "images", "file": "files", "voice": "voices", "video": "videos"}[kind]
            stats[prefix + "_requested"] += 1
            stem = hashlib.sha256(row["_incremental_key"].encode()).hexdigest()[:20] if incremental_state else str(index)
            attachment = _media_for_element(client, element, chat_dir, f"{stem}_{element_index}")
            attachment["element_index"] = element_index
            if attachment["available"]:
                stats[prefix + "_exported"] += 1
                if kind == "voice" and attachment.get("decoded"):
                    stats["voices_decoded"] += 1
            if kind == "voice" and transcribe_voices:
                if attachment["available"] and attachment.get("decoded"):
                    text, reason = core._transcribe_wav_local(recognizer, chat_dir / attachment["path"])
                else:
                    text, reason = "", "语音未生成可读取 WAV，无法进行本地识别。"
                if text:
                    attachment.update({"transcript": text, "transcript_source": "local_asr"})
                    stats["voice_transcripts"] += 1
                    stats["voice_transcripts_local"] += 1
                else:
                    attachment["transcript_reason"] = client.redact(reason or "本地识别未返回文字。")
                    stats["voice_transcripts_failed"] += 1
            message["attachments"].append(attachment)
        parsed.append(message)
        if index % 200 == 0:
            log(f"QQ 消息与附件已处理 {index}/{len(rows)} 条。")
    output_paths = export_paths(chat_dir, target_name)
    txt_path, md_path, json_path = (output_paths[kind] for kind in ("txt", "md", "json"))
    is_group = target["chat_type"] == "group"
    document = {
        "exporter_version": core.APP_VERSION, "platform": "qq", "source": "napcat_onebot_http",
        "chat_type": target["chat_type"], "chat_name": target_name, "chat_username": target["username"],
        "filter": time_filter, "message_count": len(parsed), "type_counts": {"QQ消息": len(parsed)},
        "sender_resolution_counts": dict(sender_counts), "media_stats": stats,
        "history_coverage": coverage, "messages": parsed,
    }
    incremental_result = {}
    if incremental_state:
        if _connection(client)["user_id"] != login["user_id"]:
            raise RuntimeError("QQ 登录账号在导出期间改变，本次未提交增量；请确认账号后重试。")
        complete = coverage["stop_reason"] in {"empty_page", "start_boundary_reached"}
        incremental_result = incremental_state.commit(
            document, coverage_rows, complete, core._write_txt, core._write_markdown, is_group, "QQ Chat Export for LLM",
        )
        log(f"QQ 累计导出：新增 {len(parsed)} 条，共 {document['message_count']} 条消息。")
        if incremental_result["filename_cleanup_errors"]:
            log("新名称文件已保存，以下旧名称文件正被占用，暂未删除：" + "、".join(incremental_result["filename_cleanup_errors"]))
        if not complete:
            log("QQ 本次历史窗口读取未完成，成功检查点保持原值；下次会重读未完成范围。")
    else:
        core._write_txt(parsed, txt_path, is_group, target_name, time_filter=time_filter, exporter_name="QQ Chat Export for LLM")
        core._write_markdown(parsed, md_path, is_group, target_name, time_filter=time_filter, exporter_name="QQ Chat Export for LLM")
        with json_path.open("w", encoding="utf-8-sig") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
    log(f"QQ 导出完成：{len(parsed)} 条消息；仅包含接口实际返回的记录。")
    for kind, prefix in (("image", "images"), ("file", "files"), ("voice", "voices"), ("video", "videos")):
        if enabled[kind]:
            log(f"QQ {MEDIA_LABELS[kind]}：{stats[prefix + '_exported']}/{stats[prefix + '_requested']} 已导出；逐项缺失原因保存在 JSON。")
    return {
        **{key: document[key] for key in ("chat_name", "chat_username", "filter", "message_count", "media_stats", "sender_resolution_counts", "history_coverage")},
        "platform": "qq", "is_group": is_group, "output_dir": str(chat_dir), "txt": str(txt_path),
        "md": str(md_path), "json": str(json_path),
        **incremental_result,
    }

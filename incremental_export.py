"""按已验证账号和会话保存累计导出，状态文件最后提交并指向公开 JSON。"""

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from export_names import export_paths


def _archive_path(directory):
    marker = directory / ".export-state.json"
    if not marker.exists():
        return directory / "chat_full_parsed.json"
    state = json.loads(marker.read_text(encoding="utf-8-sig"))
    name = state.get("json") if isinstance(state, dict) else None
    if (not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(name, str)
            or name in {".export-state.json", "", ".", ".."}
            or any(character in name for character in '<>:"/\\|?*')
            or any(ord(character) < 32 for character in name)
            or Path(name).suffix.lower() != ".json"):
        raise ValueError("增量状态中的 JSON 文件名无效，请恢复原导出目录后重试。")
    path = directory / name
    if not path.is_file():
        raise ValueError("增量状态指向的累计 JSON 不存在，请恢复原文件后重试。")
    return path


def resolve_chat_json(folder):
    """为预览定位当前会话 JSON，兼容旧固定文件名，拒绝把报告误作聊天。"""
    directory = Path(folder)
    preferred = _archive_path(directory)

    def valid(path):
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            return False
        return (isinstance(document, dict) and isinstance(document.get("chat_name"), str)
                and isinstance(document.get("chat_username"), str)
                and isinstance(document.get("messages"), list)
                and all(isinstance(message, dict) for message in document["messages"])
                and document.get("platform") in (None, "wechat", "qq"))

    if preferred.exists():
        if not valid(preferred):
            raise ValueError("该目录中的累计 JSON 不是有效的微信或 QQ 聊天记录。")
        return preferred
    candidates = [path for path in directory.glob("*.json") if path.is_file() and valid(path)]
    if len(candidates) != 1:
        raise ValueError("没有找到唯一的聊天 JSON，请选择一个具体的聊天导出文件夹。")
    return candidates[0]


def _positive_id(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        number = int(value)
        return str(number) if number > 0 else None
    return None


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        if isinstance(value, float) and not value.is_integer():
            return None
        value = int(value)
        return (value // 1000 if value > 10_000_000_000 else value) if value > 0 else None
    except (ValueError, OverflowError):
        return None


def message_key(platform, row):
    if platform == "wechat":
        server_id = _positive_id(row.get("server_id"))
        if server_id:
            return "server:" + server_id
        local_id = _positive_id(row.get("local_id"))
        source = row.get("_db_rel") or row.get("source_db")
        if local_id and isinstance(source, str) and source:
            return "local:" + source.replace("\\", "/") + ":" + local_id
        raise ValueError("微信消息缺少有效 server_id 或来源库＋local_id，无法安全增量去重；请改用普通导出。")
    raw = row.get("raw", row)
    short_id = raw.get("message_id") or raw.get("napcat_message_id") or raw.get("local_id")
    short_id = str(short_id).strip() if type(short_id) in (int, str) and str(short_id).strip() not in {"", "0"} else None
    sequence = _positive_id(raw.get("real_seq"))
    if not short_id:
        raise ValueError("QQ 消息缺少稳定消息 ID，无法安全增量去重；请改用普通导出。")
    if sequence:
        return "seq:" + sequence + ":short:" + short_id
    # 缺少原始序号时额外保留原始消息差异，避免短 ID 碰撞吞掉不同消息。
    elements = raw.get("message")
    if isinstance(elements, list):
        cleaned_elements = []
        for element in elements:
            if not isinstance(element, dict):
                cleaned_elements.append(element)
                continue
            data = element.get("data")
            stable_data = {key: value for key, value in data.items() if key not in {"url", "temp_url", "download_url"}} if isinstance(data, dict) else data
            cleaned_elements.append({"type": element.get("type"), "data": stable_data})
        elements = cleaned_elements
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    fallback = json.dumps({
        "time": raw.get("time"), "user_id": sender.get("user_id") or raw.get("user_id"),
        "message": elements, "raw_message": raw.get("raw_message") if elements is None else None,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "short:" + short_id + ":fallback:" + hashlib.sha256(fallback.encode()).hexdigest()


class IncrementalExport:
    def __init__(self, root, platform, account_id, chat_id, time_filter):
        if not isinstance(account_id, str) or not account_id or any(c.isspace() or c == "\x00" for c in account_id):
            raise ValueError("未能确认本机登录账号，不能安全建立增量目录；请使用普通导出或确认登录状态。")
        digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
        self.directory = Path(root).resolve() / "incremental" / platform / digest(account_id) / digest(chat_id)
        self.platform, self.account_id, self.chat_id = platform, account_id, chat_id
        self.requested_filter = dict(time_filter)
        self.old = {}
        self.old_state = {}
        self.seen = set()
        self.new_keys = set()
        path = self._archive_path()
        self.archive_path = path
        self.legacy_archive = not (self.directory / ".export-state.json").exists()
        self.previous_digest = hashlib.sha256(path.read_bytes()).digest() if path.exists() else None
        if path.exists():
            self.old = json.loads(path.read_text(encoding="utf-8-sig"))
            self.old_state = self.old.get("incremental_state", {}) if isinstance(self.old, dict) else {}
            if (
                not isinstance(self.old, dict) or not isinstance(self.old_state, dict)
                or self.old_state.get("schema_version") != 1
                or self.old_state.get("account_id") != account_id
                or self.old.get("platform") != platform
                or self.old.get("chat_username") != chat_id
                or not isinstance(self.old.get("messages"), list)
            ):
                raise ValueError("累计 JSON 的平台、账号或会话身份不匹配；请另选输出目录，避免覆盖已有记录。")
            for message in self.old["messages"]:
                key = message.get("incremental_key") if isinstance(message, dict) else None
                if not isinstance(key, str) or not key:
                    raise ValueError("累计 JSON 缺少消息稳定键；请另选输出目录重新建立增量导出。")
                self.seen.add(key)
        self.read_filter = dict(time_filter)
        start = time_filter["start_ts"]
        # 只跳过已完整读到的连续窗口；向更早时间扩展或跨越历史空档时重读。
        for interval in self.old_state.get("completed_ranges", []):
            lower, upper = interval["start_ts"], interval["checkpoint_ts"]
            includes = (start is None and lower is None) or (
                start is not None and (lower is None or start >= lower) and start <= upper
            )
            if includes:
                self.read_filter["start_ts"] = max(start if start is not None else upper, upper)
                break
        self.directory.mkdir(parents=True, exist_ok=True)

    def _archive_path(self):
        return _archive_path(self.directory)

    def select_rows(self, rows):
        selected = []
        for row in rows:
            key = message_key(self.platform, row)
            if key in self.seen or key in self.new_keys:
                continue
            self.new_keys.add(key)
            row["_incremental_key"] = key
            selected.append(row)
        return selected

    def media_folder(self, base, row):
        return base / hashlib.sha256(row["_incremental_key"].encode()).hexdigest()[:20]

    @contextmanager
    def _commit_lock(self):
        with (self.directory / ".commit.lock").open("a+b") as handle:
            if handle.seek(0, 2) == 0:
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
                raise RuntimeError("另一导出任务正在提交该增量目录，请稍后重试。") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def commit(self, *args, **kwargs):
        with self._commit_lock():
            path = self._archive_path()
            current_digest = hashlib.sha256(path.read_bytes()).digest() if path.exists() else None
            if path != self.archive_path or current_digest != self.previous_digest:
                raise RuntimeError("该累计 JSON 已被另一导出任务修改，本次未提交；请重新运行增量导出。")
            return self._commit_document(*args, **kwargs)

    def _commit_document(self, document, rows, complete, write_txt, write_markdown, is_group, exporter_name):
        old_messages = self.old.get("messages", [])
        new_messages = document["messages"]
        messages = old_messages + new_messages
        def order(message):
            sequence = _positive_id(message.get("sort_seq"))
            return message["time"], int(sequence) if sequence else 0, message["incremental_key"]
        messages.sort(key=order)
        document["messages"] = messages
        document["message_count"] = len(messages)
        for field in ("type_counts", "sender_resolution_counts", "media_stats"):
            counts = Counter(self.old.get(field, {}))
            counts.update(document.get(field, {}))
            document[field] = dict(counts)
        intervals = [dict(item) for item in self.old_state.get("completed_ranges", [])]
        times = [value for row in rows if (value := _timestamp(row.get("create_time"))) is not None]
        if complete and times:
            intervals.append({"start_ts": self.read_filter["start_ts"], "checkpoint_ts": max(times)})
            intervals.sort(key=lambda item: float("-inf") if item["start_ts"] is None else item["start_ts"])
            merged = []
            for interval in intervals:
                if merged and (interval["start_ts"] is None or interval["start_ts"] <= merged[-1]["checkpoint_ts"] + 1):
                    merged[-1]["checkpoint_ts"] = max(merged[-1]["checkpoint_ts"], interval["checkpoint_ts"])
                else:
                    merged.append(interval)
            intervals = merged
        document["incremental_state"] = {
            "schema_version": 1, "account_id": self.account_id, "completed_ranges": intervals,
            "last_requested_filter": self.requested_filter,
            "last_read_filter": self.read_filter, "new_message_count": len(new_messages),
            "pending_read": not complete,
        }
        document["platform"] = self.platform
        # 累计记录可包含多次选定范围，展示实际已保存消息的时间，不误写为最后一次筛选范围。
        known_times = [message["time"] for message in messages if len(message.get("time", "")) == 19]
        cumulative_filter = dict(self.requested_filter)
        cumulative_filter.update({
            "cumulative": True, "start": min(known_times) if known_times else None,
            "end": (datetime.strptime(max(known_times), "%Y-%m-%d %H:%M:%S") + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S") if known_times else None,
            "end_date_inclusive": False, "coverage_complete": False,
        })
        chat_timezone = timezone(timedelta(hours=8))
        for text_field, timestamp_field in (("start", "start_ts"), ("end", "end_ts_exclusive")):
            value = cumulative_filter[text_field]
            cumulative_filter[timestamp_field] = int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=chat_timezone).timestamp()) if value else None
        document["filter"] = cumulative_filter
        paths = export_paths(self.directory, document["chat_name"])
        commit_paths = {**paths, "state": self.directory / ".export-state.json"}
        marker = {"schema_version": 1, "json": paths["json"].name}
        temporary_paths = []
        backups = {}
        replaced_files = []
        try:
            for key in ("txt", "md", "json", "state"):
                descriptor, name = tempfile.mkstemp(prefix=".pending-", suffix="." + key, dir=self.directory)
                os.close(descriptor)
                temporary = Path(name)
                temporary_paths.append(temporary)
                if key in {"json", "state"}:
                    with temporary.open("w", encoding="utf-8-sig") as handle:
                        json.dump(document if key == "json" else marker, handle, ensure_ascii=False, indent=2)
                else:
                    writer = write_txt if key == "txt" else write_markdown
                    writer(messages, temporary, is_group, document["chat_name"], time_filter=cumulative_filter, exporter_name=exporter_name)
            # 保存原公开文件用于失败回滚；状态文件最后提交，检查点不受写入失败影响。
            for key in ("txt", "md", "json"):
                if paths[key].exists():
                    descriptor, name = tempfile.mkstemp(prefix=".previous-", suffix="." + key, dir=self.directory)
                    os.close(descriptor)
                    backups[key] = Path(name)
                    shutil.copyfile(paths[key], backups[key])
            for key, temporary in zip(("txt", "md", "json", "state"), temporary_paths):
                os.replace(temporary, commit_paths[key])
                if key != "state":
                    replaced_files.append(key)
        except BaseException:
            for key in reversed(replaced_files):
                if key in backups:
                    os.replace(backups[key], paths[key])
                else:
                    paths[key].unlink(missing_ok=True)
            raise
        finally:
            for temporary in temporary_paths + list(backups.values()):
                temporary.unlink(missing_ok=True)
        # 名称变化或首次旧版迁移只清理已知旧公开文件，附件仍在原稳定目录中。
        previous_paths = {self.archive_path}
        if self.legacy_archive:
            previous_paths.update(self.directory / name for name in ("chat_full_for_llm.txt", "chat_full_for_llm.md"))
        else:
            previous_paths.update(self.archive_path.with_suffix(suffix) for suffix in (".txt", ".md"))
        cleanup_errors = []
        for previous in previous_paths - set(paths.values()):
            try:
                previous.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(previous.name)
        return {
            "incremental": True, "new_message_count": len(new_messages),
            "checkpoint_advanced": intervals != self.old_state.get("completed_ranges", []), "pending_read": not complete,
            "message_count": len(messages), "output_dir": str(self.directory),
            "filename_cleanup_errors": cleanup_errors,
            **{key: str(path) for key, path in paths.items()},
        }

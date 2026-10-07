# -*- coding: utf-8 -*-
# 本派生版本已修改此上游文件，改动范围见 NOTICE。
from __future__ import annotations

import base64
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import psutil

from exporter_core import (
    AmbiguousContactError,
    CHAT_TIMEZONE,
    clear_current_sensitive_cache,
    clear_sensitive_cache,
    export_chat,
    install_local_asr_model,
    install_rust_silk,
    local_asr_status,
    parse_time_range,
    rust_silk_status,
)
from preview import open_chat_preview
from qq_exporter import check_qq_connection, export_qq_chat
from export_jobs import export_chats
from incremental_export import resolve_chat_json
from wechat_data_dirs import choose_wechat_data_dir

APP_TITLE = "微信 / QQ 聊天导出给大模型"
WECHAT_PROCESS_NAMES = {"weixin.exe", "wechat.exe"}
TIME_PRESETS = ("今天", "昨天", "最近7天", "最近30天", "本月", "上月", "全部", "自定义")


def time_preset_dates(preset, today=None):
    """快捷范围使用北京时间的自然日，结束日期由现有解析器包含整日。"""
    today = today or datetime.now(CHAT_TIMEZONE).date()
    end = today
    if preset == "今天":
        start = today
    elif preset == "昨天":
        start = end = today - timedelta(days=1)
    elif preset in ("最近7天", "最近30天"):
        start = today - timedelta(days=6 if preset == "最近7天" else 29)
    elif preset == "本月":
        start = today.replace(day=1)
    elif preset == "上月":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif preset == "全部":
        return "", ""
    else:
        raise ValueError("请选择已有快捷范围，或手动输入日期")
    return start.isoformat(), end.isoformat()

# 与 EXE 文件图标使用同一套图形。
WINDOW_ICON_PNG_BASE64 = """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"""


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def is_wechat_running() -> bool:
    """检测 Windows 微信是否正在运行。"""
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in WECHAT_PROCESS_NAMES:
                return True
    except (psutil.Error, OSError):
        # 检测本身异常时不阻断导出，让底层给出真实错误。
        return True
    return False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("960x860")
        self.minsize(860, 760)

        try:
            icon_bytes = base64.b64decode(WINDOW_ICON_PNG_BASE64)
            self._window_icon = tk.PhotoImage(data=icon_bytes)
            self.iconphoto(True, self._window_icon)
        except Exception:
            self._window_icon = None

        self.q = queue.Queue()
        self.last_output = None
        self.last_result = None
        self.last_batch_results = []
        self.batch_names = []

        pad = ttk.Frame(self, padding=18)
        pad.grid(row=0, column=0, sticky="nsew")

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        pad.grid_columnconfigure(0, weight=1)
        pad.grid_rowconfigure(9, weight=1)

        ttk.Label(
            pad,
            text="微信 / QQ 聊天信息 → 大模型可读 TXT / Markdown / JSON",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")

        self.source_hint = tk.StringVar(value="使用前请先登录 Windows 微信，并保持微信在后台运行。")
        tk.Label(
            pad,
            textvariable=self.source_hint,
            fg="#b45309",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(8, 2))

        ttk.Label(
            pad,
            text="支持私聊与群聊。输入准确备注名、昵称或群名；程序会自动识别。",
        ).grid(row=2, column=0, sticky="w", pady=(2, 16))

        row = ttk.Frame(pad)
        row.grid(row=3, column=0, sticky="ew")
        row.grid_columnconfigure(1, weight=1)

        platform_row = ttk.Frame(row)
        platform_row.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Label(platform_row, text="聊天平台：").pack(side="left")
        self.platform_var = tk.StringVar(value="微信")
        self.platform_input = ttk.Combobox(platform_row, textvariable=self.platform_var, values=("微信", "QQ"), state="readonly", width=12)
        self.platform_input.pack(side="left", padx=(8, 0))
        self.platform_input.bind("<<ComboboxSelected>>", self.on_platform_changed)
        self.mp_btn = ttk.Button(platform_row, text="公众号文章采集", command=self.open_mp_window)
        self.mp_btn.pack(side="left", padx=(22, 0))

        ttk.Label(row, text="好友 / 群聊：").grid(row=1, column=0, sticky="w")
        self.name_var = tk.StringVar()
        self.entry = ttk.Entry(row, textvariable=self.name_var)
        self.entry.grid(row=1, column=1, sticky="ew", padx=(8, 8))
        self.entry.bind("<Return>", lambda e: self.start_export())

        self.export_btn = ttk.Button(row, text="开始导出", command=self.start_export)
        self.export_btn.grid(row=1, column=2)

        mode_row = ttk.Frame(row)
        mode_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.batch_var = tk.BooleanVar(value=False)
        self.incremental_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mode_row, text="批量导出", variable=self.batch_var).pack(side="left")
        self.batch_btn = ttk.Button(mode_row, text="编辑名单（0）", command=self.edit_batch_names)
        self.batch_btn.pack(side="left", padx=(6, 18))
        ttk.Checkbutton(mode_row, text="增量追加", variable=self.incremental_var).pack(side="left")
        ttk.Label(mode_row, text="同账号、同会话累计保存，重复导出跳过已保存消息。", foreground="#6b7280").pack(side="left", padx=(8, 0))

        time_row = ttk.Frame(row)
        time_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.time_preset_var = tk.StringVar(value="最近7天")
        ttk.Label(time_row, text="快捷范围：").grid(row=0, column=0, sticky="w")
        self.time_preset = ttk.Combobox(
            time_row, textvariable=self.time_preset_var, values=TIME_PRESETS,
            state="readonly", width=24,
        )
        self.time_preset.grid(row=0, column=1, padx=(8, 18), pady=(0, 6), sticky="w")
        self.time_preset.bind("<<ComboboxSelected>>", self.apply_time_preset)
        start, end = time_preset_dates("最近7天")
        self.start_time_var = tk.StringVar(value=start)
        self.end_time_var = tk.StringVar(value=end)
        self._setting_time_range = False
        ttk.Label(time_row, text="起始时间：").grid(row=1, column=0, sticky="w")
        self.start_time_input = ttk.Combobox(
            time_row, textvariable=self.start_time_var, width=24, height=12,
            postcommand=self.refresh_date_choices,
        )
        self.start_time_input.grid(row=1, column=1, padx=(8, 18))
        ttk.Label(time_row, text="结束时间：").grid(row=1, column=2, sticky="w")
        self.end_time_input = ttk.Combobox(
            time_row, textvariable=self.end_time_var, width=24, height=12,
            postcommand=self.refresh_date_choices,
        )
        self.end_time_input.grid(row=1, column=3, padx=(8, 0))
        self.refresh_date_choices()
        self.start_time_var.trace_add("write", self.on_time_edited)
        self.end_time_var.trace_add("write", self.on_time_edited)
        ttk.Label(
            time_row,
            text="默认最近7天（含今天），下拉可选最近一年的日期，也可手动输入更早日期或精确时间。\n北京时间；起始包含，结束日期包含整日，结束时刻不包含。选择“全部”清空两栏。",
            foreground="#6b7280",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        outrow = ttk.Frame(pad)
        outrow.grid(row=4, column=0, sticky="ew", pady=(12, 8))
        outrow.grid_columnconfigure(1, weight=1)

        ttk.Label(outrow, text="输出目录：").grid(row=0, column=0, sticky="w")
        self.out_var = tk.StringVar(value=str(app_dir() / "exports"))
        self.out_entry = ttk.Entry(outrow, textvariable=self.out_var)
        self.out_entry.grid(
            row=0, column=1, sticky="ew", padx=(8, 8)
        )
        ttk.Button(outrow, text="选择", command=self.choose_out).grid(row=0, column=2)

        dbrow = ttk.Frame(pad)
        self.wechat_source_row = dbrow
        dbrow.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        dbrow.grid_columnconfigure(1, weight=1)

        ttk.Label(dbrow, text="微信数据目录：").grid(row=0, column=0, sticky="w")
        self.db_dir_var = tk.StringVar()
        self.db_dir_entry = ttk.Entry(dbrow, textvariable=self.db_dir_var)
        self.db_dir_entry.grid(
            row=0, column=1, sticky="ew", padx=(8, 8)
        )
        ttk.Button(dbrow, text="选择", command=self.choose_db_dir).grid(
            row=0, column=2
        )
        ttk.Label(
            dbrow,
            text="留空自动探测；点“选择”可列出本机发现的微信数据目录，也可手动浏览。",
            foreground="#6b7280",
        ).grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(2, 0))

        self.qq_source_row = ttk.Frame(pad)
        self.qq_source_row.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        self.qq_source_row.grid_columnconfigure(1, weight=1)
        ttk.Label(self.qq_source_row, text="QQ 本机接口：").grid(row=0, column=0, sticky="w")
        self.qq_endpoint_var = tk.StringVar(value="http://127.0.0.1:3000")
        self.qq_endpoint_entry = ttk.Entry(self.qq_source_row, textvariable=self.qq_endpoint_var)
        self.qq_endpoint_entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        self.qq_test_btn = ttk.Button(self.qq_source_row, text="测试连接", command=self.test_qq_connection)
        self.qq_test_btn.grid(row=0, column=2)
        ttk.Label(self.qq_source_row, text="访问 Token：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.qq_token_var = tk.StringVar()
        self.qq_token_entry = ttk.Entry(self.qq_source_row, textvariable=self.qq_token_var, show="●")
        self.qq_token_entry.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(6, 0))
        ttk.Button(self.qq_source_row, text="首次配置说明", command=lambda: webbrowser.open("https://napneko.github.io/guide/install")).grid(row=1, column=2, pady=(6, 0))
        ttk.Label(self.qq_source_row, text="先登录新版 QQ，并启用 NapCat 的本机 HTTP 服务；这里填写服务地址与同一访问 Token。", foreground="#6b7280").grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.qq_source_row.grid_remove()

        self.input_menu = tk.Menu(self, tearoff=False)
        for field in (self.entry, self.start_time_input, self.end_time_input, self.out_entry, self.db_dir_entry, self.qq_endpoint_entry, self.qq_token_entry):
            field.bind("<Button-3>", self.show_input_menu)

        media_row = ttk.Frame(pad)
        media_row.grid(row=6, column=0, sticky="ew", pady=(2, 2))
        ttk.Label(media_row, text="附带导出：").pack(side="left")

        self.images_var = tk.BooleanVar(value=True)
        self.files_var = tk.BooleanVar(value=True)
        self.voices_var = tk.BooleanVar(value=False)
        self.videos_var = tk.BooleanVar(value=False)
        self.transcribe_var = tk.BooleanVar(value=False)

        ttk.Checkbutton(
            media_row, text="图片", variable=self.images_var
        ).pack(side="left", padx=(6, 8))
        ttk.Checkbutton(
            media_row, text="文件", variable=self.files_var
        ).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            media_row, text="语音", variable=self.voices_var
        ).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            media_row, text="视频", variable=self.videos_var
        ).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            media_row,
            text="语音转文字（本地）",
            variable=self.transcribe_var,
            command=self.on_transcribe_toggle,
        ).pack(side="left", padx=(0, 8))

        self.media_hint = tk.StringVar(value="仅导出本机仍有缓存的附件；“语音转文字（本地）”默认关闭，勾选后会自动同时导出语音。")
        ttk.Label(
            pad,
            textvariable=self.media_hint,
        ).grid(row=7, column=0, sticky="w", pady=(2, 4))

        ttk.Separator(pad).grid(row=8, column=0, sticky="ew", pady=10)

        self.status = tk.Text(
            pad,
            height=10,
            wrap="word",
            state="disabled",
        )
        self.status.grid(row=9, column=0, sticky="nsew")

        bottom = ttk.Frame(pad)
        bottom.grid(row=10, column=0, sticky="ew", pady=(10, 0))
        bottom.grid_columnconfigure(0, weight=1)

        self.clear_cache_btn = ttk.Button(
            bottom,
            text="清除敏感缓存",
            command=self.clear_sensitive_cache_ui,
        )
        self.clear_cache_btn.grid(
            row=0, column=1, sticky="e", padx=(0, 8)
        )

        self.existing_preview_btn = ttk.Button(
            bottom,
            text="预览已有聊天",
            command=self.open_existing_preview,
        )
        self.existing_preview_btn.grid(
            row=0, column=2, sticky="e", padx=(0, 8)
        )
        self.preview_btn = ttk.Button(
            bottom,
            text="打开聊天预览",
            command=self.open_preview,
            state="disabled",
        )
        self.preview_btn.grid(row=0, column=3, sticky="e", padx=(0, 8))

        self.open_btn = ttk.Button(
            bottom,
            text="打开导出文件夹",
            command=self.open_output,
            state="disabled",
        )
        self.open_btn.grid(row=0, column=4, sticky="e")

        # 启动时只自动清理本项目自己的失效残留。
        # 不自动碰旧版共享的 %TEMP%\wechatauto_db。
        startup_cache_report = clear_current_sensitive_cache()
        removed = startup_cache_report.get("removed") or []
        failed = startup_cache_report.get("failed") or []
        if removed:
            self.log(
                f"已自动清理上次异常退出留下的敏感临时缓存：{len(removed)} 个目录。"
            )
        if failed:
            self.log(
                "检测到敏感临时缓存残留，但自动清理失败。"
                "可在确认没有导出任务运行后使用‘清除敏感缓存’重试。"
            )

        self.entry.focus_set()
        self.after(100, self.poll_queue)

    def on_platform_changed(self, event=None):
        if self.platform_var.get() == "QQ":
            self.wechat_source_row.grid_remove()
            self.qq_source_row.grid()
            self.source_hint.set("QQ 需登录新版客户端及本机 NapCat；导出其当前可返回的历史和附件。")
            self.media_hint.set("QQ 附件由客户端取回后保存到本机；失效资源会记录原因。语音转文字使用本地模型。")
        else:
            self.qq_source_row.grid_remove()
            self.wechat_source_row.grid()
            self.source_hint.set("使用前请先登录 Windows 微信，并保持微信在后台运行。")
            self.media_hint.set("仅导出本机仍有缓存的附件；“语音转文字（本地）”默认关闭，勾选后会自动同时导出语音。")

    def open_mp_window(self):
        from mp_ui import MPWindow
        if getattr(self, "mp_window", None) is None or not self.mp_window.winfo_exists():
            self.mp_window = MPWindow(self)
        else:
            self.mp_window.lift()
            self.mp_window.focus_set()

    def test_qq_connection(self):
        endpoint, token = self.qq_endpoint_var.get().strip(), self.qq_token_var.get()
        self.qq_test_btn.configure(state="disabled")
        def worker():
            try:
                self.q.put(("qq_connected", check_qq_connection(endpoint, token)))
            except Exception as exc:
                self.q.put(("qq_connection_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def on_transcribe_toggle(self):
        if self.transcribe_var.get():
            self.voices_var.set(True)

    def clear_sensitive_cache_ui(self):
        confirmed = messagebox.askyesno(
            APP_TITLE,
            "将清除本工具可能留下的敏感临时缓存，包括：\n\n"
            "1. v1.3.4 当前版本的一次性临时工作目录；\n"
            "2. v1.3.3 及更早版本 / wechatauto-replica 默认保存在 "
            "%TEMP%\\wechatauto_db 下的数据库密钥、图片密钥和解密数据库缓存。\n\n"
            "这不会删除微信原始聊天数据库，也不会删除已经导出的聊天结果。\n\n"
            "如果还有其他程序正在使用 wechatauto-replica，请先关闭它们。\n\n"
            "确定继续吗？",
        )
        if not confirmed:
            return

        self.export_btn.configure(state="disabled")
        self.clear_cache_btn.configure(state="disabled")
        self.log("")
        self.log("正在清除敏感临时缓存…")
        threading.Thread(
            target=self.clear_sensitive_cache_worker,
            daemon=True,
        ).start()

    def clear_sensitive_cache_worker(self):
        try:
            report = clear_sensitive_cache()
            self.q.put(("cache_cleared", report))
        except Exception as exc:
            self.q.put((
                "cache_clear_error",
                f"{type(exc).__name__}: {exc}",
            ))

    def show_input_menu(self, event):
        field = event.widget
        field.focus_set()
        menu = self.input_menu
        menu.delete(0, "end")
        selected = bool(field.tag_ranges("sel")) if isinstance(field, tk.Text) else field.selection_present()
        selection_state = "normal" if selected else "disabled"
        for label, shortcut, action in (("剪切", "Ctrl+X", "<<Cut>>"), ("复制", "Ctrl+C", "<<Copy>>")):
            menu.add_command(
                label=label, accelerator=shortcut, state=selection_state,
                command=lambda action=action: field.event_generate(action),
            )
        try:
            paste_state = "normal" if field.clipboard_get() else "disabled"
        except tk.TclError:
            paste_state = "disabled"
        menu.add_command(
            label="粘贴", accelerator="Ctrl+V", state=paste_state,
            command=lambda: self.paste_input(field),
        )
        menu.add_separator()
        menu.add_command(label="全选", accelerator="Ctrl+A", command=lambda: self.select_all_input(field))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def paste_input(self, field):
        try:
            value = field.clipboard_get()
        except tk.TclError:
            return
        if not value:
            return
        if isinstance(field, tk.Text):
            if field.tag_ranges("sel"):
                field.mark_set("insert", "sel.first")
                field.delete("sel.first", "sel.last")
            field.insert("insert", value)
            return
        if field.selection_present():
            index = field.index("sel.first")
            field.delete("sel.first", "sel.last")
            field.icursor(index)
        field.insert("insert", value)

    def select_all_input(self, field):
        if isinstance(field, tk.Text):
            field.tag_add("sel", "1.0", "end-1c")
        else:
            field.selection_range(0, "end")
        return "break"

    def edit_batch_names(self):
        dialog = tk.Toplevel(self)
        dialog.title("批量会话名单")
        dialog.geometry("650x430")
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(dialog, text="每行输入一个准确群名、备注、昵称或稳定会话 ID。重复行会合并；同名时逐项选择。\n当前平台、时间与附件选项应用于整批，会话分别保存。").pack(anchor="w", padx=14, pady=12)
        editor = tk.Text(dialog, wrap="word", undo=True)
        editor.pack(fill="both", expand=True, padx=14)
        editor.insert("1.0", "\n".join(self.batch_names))
        editor.bind("<Button-3>", self.show_input_menu)
        editor.bind("<Control-a>", lambda event: self.select_all_input(editor))

        def save():
            self.batch_names = list(dict.fromkeys(line.strip() for line in editor.get("1.0", "end").splitlines() if line.strip()))
            self.batch_var.set(bool(self.batch_names))
            self.batch_btn.configure(text=f"编辑名单（{len(self.batch_names)}）")
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=14, pady=12)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="保存名单", command=save).pack(side="right", padx=(0, 8))
        editor.focus_set()
        self.wait_window(dialog)

    def choose_db_dir(self):
        self.log("正在查找本机微信数据目录……")
        try:
            path = choose_wechat_data_dir(
                self,
                current=self.db_dir_var.get().strip() or None,
            )
        except Exception as exc:
            self.log(f"查找微信数据目录失败：{type(exc).__name__}: {exc}")
            path = filedialog.askdirectory(
                parent=self,
                initialdir=self.db_dir_var.get() or str(Path.home()),
            )
        if path:
            self.db_dir_var.set(path)
            self.log(f"已选择微信数据目录：{path}")

    def choose_out(self):
        path = filedialog.askdirectory(
            initialdir=self.out_var.get() or str(app_dir())
        )
        if path:
            self.out_var.set(path)

    def log(self, msg):
        self.status.configure(state="normal")
        self.status.insert("end", str(msg) + "\n")
        self.status.see("end")
        self.status.configure(state="disabled")

    def apply_time_preset(self, event=None):
        preset = self.time_preset_var.get()
        if preset == "自定义":
            return
        start, end = time_preset_dates(preset)
        self._setting_time_range = True
        try:
            self.start_time_var.set(start)
            self.end_time_var.set(end)
        finally:
            self._setting_time_range = False
        self.refresh_date_choices()

    def on_time_edited(self, *args):
        if not self._setting_time_range:
            self.time_preset_var.set("自定义")

    def refresh_date_choices(self):
        today = datetime.now(CHAT_TIMEZONE).date()
        dates = [(today - timedelta(days=day)).isoformat() for day in range(365)]
        for field, variable in ((self.start_time_input, self.start_time_var), (self.end_time_input, self.end_time_var)):
            current = variable.get().strip()
            values = ([current] if current and current not in dates else []) + dates
            field.configure(values=values)

    def start_export(self):
        name = self.name_var.get().strip()
        names = list(self.batch_names) if self.batch_var.get() else None
        if names == []:
            messagebox.showwarning(APP_TITLE, "请先点击“编辑名单”，每行输入一个会话。")
            return
        if not name and names is None:
            messagebox.showwarning(APP_TITLE, "请输入好友备注名、昵称或群名。")
            return
        start_time = self.start_time_var.get().strip() or None
        end_time = self.end_time_var.get().strip() or None
        try:
            parse_time_range(start_time, end_time)
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        platform = self.platform_var.get()
        if platform == "微信" and not is_wechat_running():
            self.log("未检测到正在运行的 Windows 微信。")
            messagebox.showwarning(
                APP_TITLE,
                "未检测到正在运行的 Windows 微信。\n\n"
                "请先启动并登录微信，保持微信在后台运行，然后重新开始导出。",
            )
            return

        transcribe_voices = self.transcribe_var.get()
        if transcribe_voices:
            self.voices_var.set(True)

        if platform == "微信" and self.voices_var.get():
            ok, reason = rust_silk_status()
            if not ok:
                install_now = messagebox.askyesno(
                    APP_TITLE,
                    "当前没有找到 rust-silk 语音解码器。\n\n"
                    "微信语音需要它才能从 SILK 转成 WAV；"
                    "本地语音转文字也需要先得到 WAV。\n\n"
                    "程序会从 rust-silk 官方 GitHub Release 下载固定的 v0.1.3，"
                    "并在安装前校验 SHA-256。\n\n"
                    "现在下载并安装吗？",
                )
                if install_now:
                    self.export_btn.configure(state="disabled")
                    self.log("")
                    self.log("开始安装 rust-silk 语音解码器…")
                    threading.Thread(
                        target=self.install_rust_silk_worker,
                        daemon=True,
                    ).start()
                    return
                if transcribe_voices:
                    messagebox.showwarning(
                        APP_TITLE,
                        "未安装 rust-silk，无法把 SILK 转成 WAV，"
                        "因此不能继续本地语音转文字。",
                    )
                    return

        if transcribe_voices:
            ok, reason = local_asr_status()
            if not ok:
                if reason.startswith("本地语音识别运行库不可用"):
                    messagebox.showerror(
                        APP_TITLE,
                        "当前程序缺少本地语音识别运行库。\n\n"
                        "如果你使用的是 GitHub 发布的 Windows 版，请重新下载最新构建；"
                        "如果从源码运行，请先安装 requirements.txt。\n\n"
                        f"详细信息：{reason}",
                    )
                    return

                install_now = messagebox.askyesno(
                    APP_TITLE,
                    "本地语音识别模型还没有安装。\n\n"
                    "需要从 sherpa-onnx 官方 GitHub Release 下载约 230 MB 的 "
                    "SenseVoice Small Int8 模型。\n"
                    "模型只下载一次，之后识别在本机完成，不上传聊天语音。\n\n"
                    "现在下载并安装吗？",
                )
                if install_now:
                    self.export_btn.configure(state="disabled")
                    self.log("")
                    self.log("开始安装本地语音识别模型…")
                    threading.Thread(
                        target=self.install_asr_worker,
                        daemon=True,
                    ).start()
                return

        self.export_btn.configure(state="disabled")
        self.clear_cache_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self.last_output = None
        self.last_result = None
        self.last_batch_results = []
        self.log("")
        self.log(f"开始{'增量' if self.incremental_var.get() else ''}导出：" + (f"批量 {len(names)} 个会话" if names else name))

        thread = threading.Thread(
            target=self.worker,
            args=(
                name,
                self.out_var.get().strip() or str(app_dir() / "exports"),
                self.images_var.get(),
                self.files_var.get(),
                self.voices_var.get(),
                self.videos_var.get(),
                transcribe_voices,
                self.db_dir_var.get().strip() or None,
                start_time,
                end_time,
            ),
            kwargs={"platform": platform, "endpoint": self.qq_endpoint_var.get().strip(), "token": self.qq_token_var.get(), "names": names, "incremental": self.incremental_var.get()},
            daemon=True,
        )
        thread.start()

    def install_asr_worker(self):
        try:
            install_local_asr_model(
                progress=lambda m: self.q.put(("log", m))
            )
            self.q.put(("asr_installed", None))
        except Exception as exc:
            self.q.put(("asr_error", f"{type(exc).__name__}: {exc}"))

    def install_rust_silk_worker(self):
        try:
            install_rust_silk(
                progress=lambda m: self.q.put(("log", m))
            )
            self.q.put(("rust_silk_installed", None))
        except Exception as exc:
            self.q.put(("rust_silk_error", f"{type(exc).__name__}: {exc}"))

    def worker(
        self,
        name,
        outdir,
        export_images,
        export_files,
        export_voices,
        export_videos,
        transcribe_voices,
        db_dir,
        start_time=None,
        end_time=None,
        chat_id=None,
        platform="微信",
        endpoint=None,
        token="",
        names=None,
        incremental=False,
    ):
        try:
            exporter = export_qq_chat if platform == "QQ" else export_chat
            connection = {"endpoint": endpoint, "token": token} if platform == "QQ" else {"db_dir": db_dir}
            if names is not None:
                result = export_chats(
                    names, outdir, platform=platform, incremental=incremental,
                    progress=lambda m: self.q.put(("log", m)),
                    select_contact=self.request_batch_contact,
                    export_images=export_images, export_files=export_files,
                    export_voices=export_voices, export_videos=export_videos,
                    transcribe_voices=transcribe_voices,
                    start_time=start_time, end_time=end_time, **connection,
                )
                self.q.put(("batch_done", result))
                return
            result = exporter(
                name,
                outdir,
                progress=lambda m: self.q.put(("log", m)),
                export_images=export_images,
                export_files=export_files,
                export_voices=export_voices,
                export_videos=export_videos,
                transcribe_voices=transcribe_voices,
                **connection,
                start_time=start_time,
                end_time=end_time,
                chat_id=chat_id,
                incremental=incremental,
            )
            self.q.put(("done", result))
        except AmbiguousContactError as exc:
            self.q.put(("choose_contact", {
                "candidates": exc.candidates,
                "request": {
                    "name": name, "outdir": outdir,
                    "export_images": export_images, "export_files": export_files,
                    "export_voices": export_voices, "export_videos": export_videos,
                    "transcribe_voices": transcribe_voices, "db_dir": db_dir,
                    "start_time": start_time, "end_time": end_time,
                    "platform": platform, "endpoint": endpoint, "token": token,
                    "incremental": incremental,
                },
            }))
        except Exception as e:
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def request_batch_contact(self, candidates, keyword):
        # 工作线程等待选择，所有 Tk 操作由队列交回主线程。
        selected = {"candidates": candidates, "keyword": keyword, "event": threading.Event(), "username": None}
        self.q.put(("batch_choose_contact", selected))
        selected["event"].wait()
        return selected["username"]

    def choose_contact(self, candidates, action="export"):
        """仅在 Tk 主线程选择稳定会话 ID，取消时不启动新的导出。"""
        dialog = tk.Toplevel(self)
        dialog.title("选择聊天预览" if action == "preview" else "请选择准确联系人或群聊")
        dialog.geometry("900x350")
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(dialog, text="选择本批次中要预览的会话。" if action == "preview" else "存在同名或相似会话，请选择目标；使用稳定会话 ID 可避免同名误选。").pack(anchor="w", padx=14, pady=12)
        tree = ttk.Treeview(dialog, columns=("kind", "remark", "nick", "username"), show="headings", selectmode="browse", height=8)
        for column, title, width in (
            ("kind", "类型", 70), ("remark", "备注", 180),
            ("nick", "昵称 / 群名", 200), ("username", "稳定会话 ID", 370),
        ):
            tree.heading(column, text=title)
            tree.column(column, width=width)
        for candidate in candidates:
            username = candidate["username"]
            tree.insert("", "end", iid=username, values=(
                "群聊" if candidate.get("chat_type") == "group" or username.endswith("@chatroom") else "联系人",
                candidate.get("remark") or "", candidate.get("nick_name") or "", username,
            ))
        tree.pack(fill="both", expand=True, padx=14)
        selected = {"username": None}

        def confirm():
            items = tree.selection()
            if not items:
                messagebox.showwarning(APP_TITLE, "请先选择一个会话。", parent=dialog)
                return
            selected["username"] = items[0]
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=14, pady=12)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="打开预览" if action == "preview" else "选择并导出", command=confirm).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)
        return selected["username"]

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "qq_connected":
                    self.qq_test_btn.configure(state="normal")
                    self.log(f"QQ 本机接口已连接：{payload.get('nickname') or ''}（{payload.get('user_id') or ''}）。")
                elif kind == "qq_connection_error":
                    self.qq_test_btn.configure(state="normal")
                    self.log(f"QQ 连接失败：{payload}")
                    messagebox.showerror(APP_TITLE, f"QQ 本机接口连接失败：\n\n{payload}\n\n请先按“首次配置说明”启动 NapCat 的 HTTP 服务。")
                elif kind == "choose_contact":
                    username = self.choose_contact(payload["candidates"])
                    if username:
                        request = payload["request"]
                        request["chat_id"] = username
                        self.log("已选定准确会话，重新开始导出。")
                        threading.Thread(target=self.worker, kwargs=request, daemon=True).start()
                    else:
                        self.export_btn.configure(state="normal")
                        self.clear_cache_btn.configure(state="normal")
                        self.log("已取消会话选择，未导出。")
                elif kind == "batch_choose_contact":
                    try:
                        self.log(f"请选择准确会话：{payload['keyword']}")
                        payload["username"] = self.choose_contact(payload["candidates"])
                    finally:
                        payload["event"].set()
                elif kind == "batch_done":
                    self.last_batch_results = payload["results"]
                    self.last_result = self.last_batch_results[-1] if self.last_batch_results else None
                    self.last_output = payload["output_dir"]
                    self.export_btn.configure(state="normal")
                    self.clear_cache_btn.configure(state="normal")
                    self.open_btn.configure(state="normal")
                    self.preview_btn.configure(state="normal" if self.last_result else "disabled")
                    failures = payload.get("failures") or []
                    cancelled = payload.get("cancelled") or []
                    cleanup_failed = any(not result.get("sensitive_cache_cleanup", True) for result in self.last_batch_results)
                    pending_read = any(result.get("pending_read") for result in self.last_batch_results)
                    for item in failures:
                        self.log(f"失败：{item['keyword']}：{item['error']}")
                    summary = f"成功 {len(self.last_batch_results)}，失败 {len(failures)}，取消 {len(cancelled)}。"
                    if any(result.get("incremental") for result in self.last_batch_results):
                        summary += f"\n本次新增 {sum(result.get('new_message_count', 0) for result in self.last_batch_results)} 条消息。"
                    if cleanup_failed:
                        summary += "\n部分敏感缓存清理失败，请使用“清除敏感缓存”重试。"
                    if pending_read:
                        summary += "\n部分会话历史尚未读完，已保留检查点供下次增量重试。"
                    self.log(summary)
                    self.log(f"批量报告：{payload['report']}")
                    show_result = messagebox.showwarning if failures or cleanup_failed or pending_read else messagebox.showinfo
                    show_result(APP_TITLE, f"批量处理完成。\n\n{summary}\n\n每个会话分别生成 TXT、Markdown、JSON。\n打开聊天预览可选择本批次成功的会话。\n批量报告：{payload['report']}")
                elif kind == "done":
                    self.last_output = payload["output_dir"]
                    self.last_result = payload
                    self.log(f"输出：{payload['output_dir']}")
                    self.export_btn.configure(state="normal")
                    self.clear_cache_btn.configure(state="normal")
                    self.open_btn.configure(state="normal")
                    self.preview_btn.configure(state="normal")

                    stats = payload.get("media_stats") or {}
                    history_note = ""
                    if payload.get("platform") == "qq":
                        coverage = payload.get("history_coverage") or {}
                        history_note = f"\n历史读取停止原因：{coverage.get('stop_description') or coverage.get('stop_reason') or '见 JSON 覆盖记录'}\nQQ 客户端未返回的历史不会自动补齐。"
                    media_line = (
                        f"\n图片：{stats.get('images_exported', 0)}/"
                        f"{stats.get('images_requested', 0)}"
                        f"\n文件：{stats.get('files_exported', 0)}/"
                        f"{stats.get('files_requested', 0)}"
                        f"\n语音：{stats.get('voices_exported', 0)}/"
                        f"{stats.get('voices_requested', 0)}"
                        f"（WAV {stats.get('voices_decoded', 0)}）"
                        f"\n语音转文字：{stats.get('voice_transcripts', 0)}/"
                        f"{stats.get('voices_requested', 0)}"
                        f"（本地 {stats.get('voice_transcripts_local', 0)}，"
                        f"缓存 {stats.get('voice_transcripts_cached', 0)}）"
                        f"\n视频：{stats.get('videos_exported', 0)}/"
                        f"{stats.get('videos_requested', 0)}"
                    )

                    cleanup_ok = payload.get("sensitive_cache_cleanup", True)
                    cleanup_error = payload.get("sensitive_cache_cleanup_error")
                    pending_read = payload.get("pending_read", False)
                    cleanup_note = (
                        ""
                        if cleanup_ok
                        else (
                            "\n\n⚠ 敏感临时缓存自动清理失败。"
                            "\n请点击“清除敏感缓存”重试。"
                            + (f"\n详细信息：{cleanup_error}" if cleanup_error else "")
                        )
                    )
                    show_result = (
                        messagebox.showinfo
                        if cleanup_ok and not pending_read
                        else messagebox.showwarning
                    )
                    count_line = f"累计消息：{payload['message_count']}；本次新增：{payload.get('new_message_count', 0)}" if payload.get("incremental") else f"消息数：{payload['message_count']}"
                    incremental_note = "\n所选历史尚未读完，已保留检查点供下次增量重试。" if pending_read else ""
                    show_result(
                        APP_TITLE,
                        f"导出完成。\n\n"
                        f"类型：{'群聊' if payload['is_group'] else '私聊'}\n"
                        f"{count_line}\n"
                        f"会话：{payload['chat_name']}"
                        f"{media_line}{history_note}{incremental_note}\n\n"
                        "已生成 TXT、Markdown 和 JSON。"
                        f"{cleanup_note}",
                    )
                elif kind == "cache_cleared":
                    self.export_btn.configure(state="normal")
                    self.clear_cache_btn.configure(state="normal")
                    removed = payload.get("removed") or []
                    failed = payload.get("failed") or []
                    if removed:
                        for path in removed:
                            self.log(f"已清除：{path}")
                    else:
                        self.log("没有发现需要清理的敏感缓存。")
                    if failed:
                        details = "\n".join(
                            f"{item.get('path')}：{item.get('error')}"
                            for item in failed
                        )
                        self.log("部分敏感缓存清理失败。")
                        messagebox.showwarning(
                            APP_TITLE,
                            "部分敏感缓存清理失败。\n\n"
                            "请确认没有其他程序正在使用这些文件，然后重试。\n\n"
                            + details,
                        )
                    else:
                        messagebox.showinfo(
                            APP_TITLE,
                            "敏感缓存清理完成。"
                            if removed
                            else "没有发现需要清理的敏感缓存。",
                        )
                elif kind == "cache_clear_error":
                    self.export_btn.configure(state="normal")
                    self.clear_cache_btn.configure(state="normal")
                    self.log("敏感缓存清理失败：" + payload)
                    messagebox.showerror(
                        APP_TITLE,
                        "敏感缓存清理失败：\n\n" + payload,
                    )
                elif kind == "asr_installed":
                    self.export_btn.configure(state="normal")
                    self.log("本地语音识别模型安装完成。")
                    messagebox.showinfo(
                        APP_TITLE,
                        "本地语音识别模型安装完成。\n\n"
                        "现在可以重新点击“开始导出”。",
                    )
                elif kind == "rust_silk_installed":
                    self.export_btn.configure(state="normal")
                    self.log("rust-silk 安装完成，继续导出。")
                    self.after(0, self.start_export)
                elif kind == "rust_silk_error":
                    self.export_btn.configure(state="normal")
                    self.log("rust-silk 安装失败：" + payload)
                    messagebox.showerror(
                        APP_TITLE,
                        "rust-silk 安装失败：\n\n" + payload,
                    )
                elif kind == "asr_error":
                    self.export_btn.configure(state="normal")
                    self.log("本地语音识别模型安装失败：" + payload)
                    messagebox.showerror(
                        APP_TITLE,
                        "本地语音识别模型安装失败：\n\n" + payload,
                    )
                elif kind == "error":
                    self.export_btn.configure(state="normal")
                    self.clear_cache_btn.configure(state="normal")
                    self.log("导出失败：" + payload)
                    messagebox.showerror(
                        APP_TITLE,
                        "导出失败：\n\n" + payload,
                    )
        except queue.Empty:
            pass

        self.after(100, self.poll_queue)

    def open_preview(self):
        if not self.last_result:
            return
        result = self.last_result
        if len(self.last_batch_results) > 1:
            by_chat = {item["chat_username"]: item for item in self.last_batch_results}
            candidates = [{"username": item["chat_username"], "nick_name": item["chat_name"], "chat_type": "group" if item["is_group"] else "private"} for item in by_chat.values()]
            username = self.choose_contact(candidates, action="preview")
            if not username:
                return
            result = by_chat[username]
        json_path = result.get("json")
        if json_path and Path(json_path).is_file():
            open_chat_preview(self, json_path)

    def open_existing_preview(self):
        initial_dir = Path(
            self.out_var.get().strip() or str(app_dir() / "exports")
        )
        if not initial_dir.is_dir():
            initial_dir = app_dir()

        folder = filedialog.askdirectory(
            parent=self,
            title="选择已导出的聊天文件夹",
            initialdir=str(initial_dir),
        )
        if not folder:
            return

        try:
            json_path = resolve_chat_json(folder)
        except (OSError, ValueError) as error:
            messagebox.showwarning(
                APP_TITLE,
                f"{error}\n\n请选择一个具体的聊天导出文件夹。",
            )
            return

        open_chat_preview(self, json_path)

    def open_output(self):
        if not self.last_output:
            return
        path = Path(self.last_output)
        if path.exists():
            os.startfile(str(path))


if __name__ == "__main__":
    App().mainloop()

"""公众号文章采集窗口，采集线程通过队列交回界面。"""

import json
import os
import queue
import re
import threading
import tkinter as tk
import webbrowser
from pathlib import Path, PureWindowsPath
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit

from PIL import Image, ImageTk

from exporter_core import parse_time_range
from mp_articles import export_articles, extract_article_links
from mp_history import check_history_connection, export_history
from mp_local import export_local_articles
from mp_browser import export_browser_articles
from mp_import import export_saved_articles


class MPWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("微信公众号文章采集")
        self.geometry("940x780")
        self.minsize(780, 640)
        self.parent_app = parent
        self.q = queue.Queue()
        self.last_result = None
        self.results = []
        self.pending_selections = []
        self.closed = False
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        self.bind("<Destroy>", self.on_destroy, add="+")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        pad = ttk.Frame(self, padding=16)
        pad.grid(sticky="nsew")
        pad.columnconfigure(1, weight=1)
        pad.rowconfigure(8, weight=1)

        ttk.Label(pad, text="公众号文章 → 本地 TXT / Markdown / JSON", font=("Microsoft YaHei UI", 15, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Label(pad, text="采集来源：").grid(row=1, column=0, sticky="w")
        self.mode_var = tk.StringVar(value="浏览器页面（验证后采集）")
        self.mode = ttk.Combobox(pad, textvariable=self.mode_var, values=("浏览器页面（验证后采集）", "文章链接", "微信本地消息链接", "WeRSS 已采集历史"), state="readonly", width=24)
        self.mode.grid(row=1, column=1, sticky="w", padx=8)
        self.mode.bind("<<ComboboxSelected>>", self.on_mode_changed)
        import_row = ttk.Frame(pad)
        import_row.grid(row=1, column=2)
        self.import_btn = ttk.Button(import_row, text="从聊天 JSON 提取链接", command=self.import_chat_links)
        self.import_btn.pack(side="top", anchor="e")
        self.saved_btn = ttk.Button(import_row, text="导入已保存网页", command=self.import_saved_pages)
        self.saved_btn.pack(side="top", anchor="e", pady=(4, 0))
        self.input_label = ttk.Label(pad, text="文章链接：")
        self.input_label.grid(row=2, column=0, sticky="nw", pady=(12, 0))
        self.input_text = tk.Text(pad, height=4, wrap="word", undo=True)
        self.input_text.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(12, 0))
        self.input_text.bind("<Button-3>", parent.show_input_menu)
        self.input_text.bind("<Control-a>", lambda event: parent.select_all_input(self.input_text))
        self.hint = tk.StringVar()
        ttk.Label(pad, textvariable=self.hint, wraplength=780, foreground="#6b7280").grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 8))

        self.service_row = ttk.Frame(pad)
        self.service_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.service_row.columnconfigure(1, weight=1)
        self.endpoint_var = tk.StringVar(value="http://127.0.0.1:8001")
        self.ak_var, self.sk_var = tk.StringVar(), tk.StringVar()
        for number, (label, variable) in enumerate((("WeRSS 本机地址：", self.endpoint_var), ("Access Key：", self.ak_var), ("Secret Key：", self.sk_var))):
            ttk.Label(self.service_row, text=label).grid(row=number, column=0, sticky="w", pady=2)
            field = ttk.Entry(self.service_row, textvariable=variable, show="●" if number else "")
            field.grid(row=number, column=1, sticky="ew", padx=8, pady=2)
            field.bind("<Button-3>", parent.show_input_menu)
        self.test_btn = ttk.Button(self.service_row, text="测试连接", command=self.test_connection)
        self.test_btn.grid(row=0, column=2)
        ttk.Button(self.service_row, text="打开服务登录", command=self.open_service).grid(row=1, column=2)
        ttk.Button(self.service_row, text="首次配置说明", command=lambda: webbrowser.open("https://github.com/rachelos/we-mp-rss")).grid(row=2, column=2)

        times = ttk.Frame(pad)
        times.grid(row=5, column=0, columnspan=3, sticky="ew")
        self.start_var = tk.StringVar(value=parent.start_time_var.get())
        self.end_var = tk.StringVar(value=parent.end_time_var.get())
        choices = parent.start_time_input.cget("values")
        for column, (label, variable) in enumerate((("起始时间：", self.start_var), ("结束时间：", self.end_var))):
            ttk.Label(times, text=label).grid(row=0, column=column * 2, sticky="w")
            field = ttk.Combobox(times, textvariable=variable, values=choices, width=23)
            field.grid(row=0, column=column * 2 + 1, padx=(8, 16))
            field.bind("<Button-3>", parent.show_input_menu)
        ttk.Button(times, text="全部时间", command=self.clear_dates).grid(row=0, column=4)
        ttk.Label(times, text="按文章发布时间筛选；北京时间。结束日期包含整日，结束时刻不包含；时间未知会记录原因。", foreground="#6b7280").grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 8))

        ttk.Label(pad, text="输出目录：").grid(row=6, column=0, sticky="w")
        self.out_var = tk.StringVar(value=parent.out_var.get())
        out_entry = ttk.Entry(pad, textvariable=self.out_var)
        out_entry.grid(row=6, column=1, sticky="ew", padx=8)
        out_entry.bind("<Button-3>", parent.show_input_menu)
        ttk.Button(pad, text="选择", command=self.choose_output).grid(row=6, column=2)
        options = ttk.Frame(pad)
        options.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        self.images_var = tk.BooleanVar(value=parent.images_var.get())
        self.incremental_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="保存正文图片", variable=self.images_var).pack(side="left")
        ttk.Checkbutton(options, text="增量：跳过已保存文章", variable=self.incremental_var).pack(side="left", padx=14)
        self.start_btn = ttk.Button(options, text="开始采集", command=self.start_collect)
        self.start_btn.pack(side="right")

        area = ttk.Panedwindow(pad, orient="vertical")
        area.grid(row=8, column=0, columnspan=3, sticky="nsew")
        self.tree = ttk.Treeview(area, columns=("title", "account", "published", "status"), show="headings", height=6, selectmode="browse")
        for key, label, width in (("title", "文章", 400), ("account", "公众号", 170), ("published", "发布时间", 160), ("status", "状态", 90)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.bind("<Double-1>", lambda event: self.preview_selected())
        self.log_text = tk.Text(area, height=6, state="disabled", wrap="word")
        area.add(self.tree, weight=2)
        area.add(self.log_text, weight=1)
        bottom = ttk.Frame(pad)
        bottom.grid(row=9, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(bottom, text="预览已有文章", command=self.preview_existing).pack(side="left", padx=6)
        self.preview_btn = ttk.Button(bottom, text="预览所选文章", command=self.preview_selected, state="disabled")
        self.preview_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(bottom, text="打开采集文件夹", command=self.open_output, state="disabled")
        self.open_btn.pack(side="left")
        self.on_mode_changed()
        self.after(100, self.poll_queue)

    def on_mode_changed(self, event=None):
        source = self.mode_var.get()
        links_mode = source in {"文章链接", "浏览器页面（验证后采集）"}
        self.input_label.configure(text="文章链接：" if links_mode else "公众号名单：")
        self.import_btn.configure(state="normal" if links_mode else "disabled")
        if source == "WeRSS 已采集历史":
            self.service_row.grid()
            self.hint.set("每行一个公众号名或 WeRSS 订阅 ID。同名会让你选择；需另外部署并登录本机 WeRSS、添加并同步公众号。这里只读取已采集历史，无法保证回补全部历史。")
        elif source == "微信本地消息链接":
            self.service_row.grid_remove()
            self.hint.set("每行一个公众号名、会话名或精确 gh_ ID。保持电脑微信登录；沿用主窗口的微信数据目录。读取所选本地会话已有消息中的文章链接，再按文章发布时间采集正文；不等于公众号全部历史。")
        elif source == "浏览器页面（验证后采集）":
            self.service_row.grid_remove()
            self.hint.set("每行一个文章链接。打开本次专用 Chrome / Edge，读取其中已显示的正文；若出现验证，手动完成后在工具提示中点“是”。结束后关闭本次浏览器；不读取你的原浏览器配置。也可导入浏览器 Ctrl+S 保存的 HTML / MHTML。")
        else:
            self.service_row.grid_remove()
            self.hint.set("每行粘贴一个 mp.weixin.qq.com 文章链接，也可从已导出的聊天 JSON 提取。无需读微信数据库；验证码、已删除或无法访问的文章会记录失败。")

    def clear_dates(self):
        self.start_var.set("")
        self.end_var.set("")

    def choose_output(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.out_var.get() or None)
        if folder:
            self.out_var.set(folder)

    def import_chat_links(self):
        path = filedialog.askopenfilename(parent=self, title="选择已导出的聊天 JSON", filetypes=(("JSON 文件", "*.json"),))
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            links = extract_article_links(data)
            if not links:
                messagebox.showinfo(self.title(), "所选文件中没有可识别的公众号文章链接。", parent=self)
                return
            self.input_text.delete("1.0", "end")
            self.input_text.insert("1.0", "\n".join(links))
            self.log(f"从所选聊天文件提取 {len(links)} 个文章链接。")
        except (OSError, ValueError) as error:
            messagebox.showerror(self.title(), f"聊天文件读取失败：{error}", parent=self)

    def open_service(self):
        url = self.endpoint_var.get().strip()
        try:
            parsed = urlsplit(url)
            port_valid = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            messagebox.showwarning(self.title(), "请输入有效的本机 WeRSS 地址。", parent=self)
            return
        if not port_valid or parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            messagebox.showwarning(self.title(), "请输入本机 WeRSS 根地址，例如 http://127.0.0.1:8001。", parent=self)
            return
        webbrowser.open(url)

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", str(text) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def test_connection(self):
        endpoint, access_key, secret_key = self.endpoint_var.get().strip(), self.ak_var.get(), self.sk_var.get()
        self.test_btn.configure(state="disabled")
        def worker():
            try:
                self.q.put(("connected", check_history_connection(endpoint, access_key=access_key, secret_key=secret_key)))
            except Exception as error:
                self.q.put(("connection_error", str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def start_collect(self):
        value = self.input_text.get("1.0", "end").strip()
        source = self.mode_var.get()
        links_mode = source in {"文章链接", "浏览器页面（验证后采集）"}
        inputs = extract_article_links(value) if links_mode else list(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))
        if not inputs:
            messagebox.showwarning(self.title(), "请先输入有效的公众号文章链接。" if links_mode else "请每行输入一个公众号或会话名。", parent=self)
            return
        options = {"start_time": self.start_var.get().strip() or None, "end_time": self.end_var.get().strip() or None, "export_images": self.images_var.get(), "incremental": self.incremental_var.get()}
        try:
            parse_time_range(options["start_time"], options["end_time"])
        except ValueError as error:
            messagebox.showwarning(self.title(), str(error), parent=self)
            return
        connection = {"endpoint": self.endpoint_var.get().strip(), "access_key": self.ak_var.get(), "secret_key": self.sk_var.get()} if source == "WeRSS 已采集历史" else {"db_dir": self.parent_app.db_dir_var.get().strip() or None} if source == "微信本地消息链接" else {}
        self.start_btn.configure(state="disabled")
        self.saved_btn.configure(state="disabled")
        self.log(f"开始采集：{len(inputs)} 项，来源：{source}。")
        threading.Thread(target=self.worker, args=(inputs, self.out_var.get().strip() or self.parent_app.out_var.get(), source, options, connection), daemon=True).start()

    def worker(self, inputs, outdir, source, options, connection):
        try:
            if source == "WeRSS 已采集历史":
                result = export_history(inputs, outdir, progress=lambda text: self.q.put(("log", text)), select_account=self.request_account, **options, **connection)
            elif source == "微信本地消息链接":
                result = export_local_articles(inputs, outdir, progress=lambda text: self.q.put(("log", text)), select_account=self.request_local_account, **options, **connection)
            elif source == "浏览器页面（验证后采集）":
                result = export_browser_articles(inputs, outdir, progress=lambda text: self.q.put(("log", text)), wait_for_user=self.request_browser_ready, **options)
            else:
                result = export_articles(inputs, outdir, progress=lambda text: self.q.put(("log", text)), **options)
            self.q.put(("done", result))
        except Exception as error:
            text = str(error)
            for secret in (connection.get("access_key"), connection.get("secret_key")):
                if secret:
                    text = text.replace(secret, "[已隐藏]")
            self.q.put(("error", text))

    def import_saved_pages(self):
        paths = filedialog.askopenfilenames(parent=self, title="选择浏览器保存的公众号网页", filetypes=(("已保存网页", "*.html *.htm *.mhtml *.mht"),))
        if not paths:
            return
        options = {"start_time": self.start_var.get().strip() or None, "end_time": self.end_var.get().strip() or None, "export_images": self.images_var.get(), "incremental": self.incremental_var.get()}
        try:
            parse_time_range(options["start_time"], options["end_time"])
        except ValueError as error:
            messagebox.showwarning(self.title(), str(error), parent=self)
            return
        self.start_btn.configure(state="disabled")
        self.saved_btn.configure(state="disabled")
        self.log(f"开始导入已保存网页：{len(paths)} 个文件。")
        threading.Thread(target=self.worker_saved, args=(list(paths), self.out_var.get().strip() or self.parent_app.out_var.get(), options), daemon=True).start()

    def worker_saved(self, paths, outdir, options):
        try:
            result = export_saved_articles(paths, outdir, progress=lambda text: self.q.put(("log", text)), **options)
            self.q.put(("done", result))
        except Exception as error:
            self.q.put(("error", str(error)))

    def request_browser_ready(self, url, reason):
        request = {"url": url, "reason": reason, "event": threading.Event(), "id": False}
        if self.closed:
            return False
        self.pending_selections.append(request)
        self.q.put(("browser_verify", request))
        while not request["event"].wait(0.2):
            if self.closed:
                break
        self.pending_selections.remove(request)
        return bool(request["id"])

    def request_account(self, candidates, keyword):
        request = {"candidates": candidates, "keyword": keyword, "event": threading.Event(), "id": None}
        if self.closed:
            return None
        self.pending_selections.append(request)
        self.q.put(("select_account", request))
        while not request["event"].wait(0.2):
            if self.closed:
                break
        self.pending_selections.remove(request)
        return request["id"]

    def request_local_account(self, candidates, keyword):
        request = {"candidates": candidates, "keyword": keyword, "event": threading.Event(), "id": None}
        if self.closed:
            return None
        self.pending_selections.append(request)
        self.q.put(("select_local_account", request))
        while not request["event"].wait(0.2):
            if self.closed:
                break
        self.pending_selections.remove(request)
        return request["id"]

    def close_window(self):
        self.closed = True
        for request in list(self.pending_selections):
            request["event"].set()
        self.destroy()

    def on_destroy(self, event):
        if event.widget is self:
            self.closed = True
            for request in list(self.pending_selections):
                request["event"].set()

    def choose_account(self, candidates, keyword):
        dialog = tk.Toplevel(self)
        dialog.title(f"选择准确公众号：{keyword}")
        dialog.geometry("780x340")
        dialog.transient(self)
        dialog.grab_set()
        tree = ttk.Treeview(dialog, columns=("name", "id"), show="headings", selectmode="browse")
        tree.heading("name", text="公众号")
        tree.heading("id", text="WeRSS 订阅 ID")
        for index, item in enumerate(candidates):
            tree.insert("", "end", iid=str(index), values=(item.get("mp_name") or item.get("name") or "", item["id"]))
        tree.pack(fill="both", expand=True, padx=12, pady=12)
        selected = {"id": None}
        def choose():
            if tree.selection():
                selected["id"] = candidates[int(tree.selection()[0])]["id"]
                dialog.destroy()
        ttk.Button(dialog, text="选择并读取", command=choose).pack(side="right", padx=12, pady=(0, 12))
        ttk.Button(dialog, text="跳过", command=dialog.destroy).pack(side="right", pady=(0, 12))
        self.wait_window(dialog)
        return selected["id"]

    def poll_queue(self):
        if self.closed:
            return
        try:
            while True:
                if self.closed:
                    return
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind in {"select_account", "select_local_account"}:
                    try:
                        payload["id"] = self.parent_app.choose_contact(payload["candidates"]) if kind == "select_local_account" else self.choose_account(payload["candidates"], payload["keyword"])
                    except tk.TclError:
                        if not self.closed:
                            raise
                    finally:
                        payload["event"].set()
                elif kind == "browser_verify":
                    try:
                        payload["id"] = messagebox.askyesno("在浏览器中完成验证", f"{payload['reason']}\n\n原文：{payload['url']}\n\n请切换到本次专用浏览器，完成验证并等待正文显示。\n正文显示后，点“是”继续采集；点“否”跳过该篇。", parent=self)
                    except tk.TclError:
                        if not self.closed:
                            raise
                    finally:
                        payload["event"].set()
                elif kind in {"connected", "connection_error"}:
                    self.test_btn.configure(state="normal")
                    if kind == "connected":
                        self.log(f"本机 WeRSS 已连接；已添加公众号：{payload.get('account_count', '见服务列表')}。")
                    else:
                        self.log(f"连接失败：{payload}")
                        messagebox.showerror(self.title(), str(payload), parent=self)
                elif kind == "error":
                    self.start_btn.configure(state="normal")
                    self.saved_btn.configure(state="normal")
                    self.log(f"采集失败：{payload}")
                    messagebox.showerror(self.title(), str(payload), parent=self)
                elif kind == "done":
                    self.start_btn.configure(state="normal")
                    self.saved_btn.configure(state="normal")
                    self.last_result = payload
                    self.results = payload["results"]
                    for item in self.tree.get_children():
                        self.tree.delete(item)
                    for index, article in enumerate(self.results):
                        self.tree.insert("", "end", iid=str(index), values=(article.get("title") or "未命名文章", article.get("account") or "", article.get("published_at") or "未知", "已保存" if article.get("cached") else "新增"))
                    self.preview_btn.configure(state="normal" if self.results else "disabled")
                    self.open_btn.configure(state="normal")
                    if self.results:
                        self.tree.selection_set("0")
                    failures, skipped = payload.get("failures") or [], payload.get("skipped") or []
                    for item in failures:
                        self.log(f"失败：{item.get('url') or item.get('keyword') or item.get('account') or ''}：{item.get('error') or ''}")
                    for item in skipped:
                        self.log(f"跳过：{item.get('url') or item.get('keyword') or item.get('account') or ''}：{item.get('reason') or ''}")
                    coverage_note = "\n公众号历史仅以本机服务实际返回为准，覆盖与停止原因见报告。" if payload.get("history_coverage") else ""
                    if payload.get("local_coverage"):
                        coverage_note = "\n链接来自本机所选会话已有消息，覆盖信息见报告；不代表公众号全部历史。"
                    if payload.get("browser_coverage"):
                        coverage_note = "\n正文来自本次浏览器页面；图片状态、实际地址及浏览器清理情况见报告。"
                    summary = f"成功 {len(self.results)}，失败 {len(failures)}，跳过 {len(skipped)}。\n报告：{payload['report']}{coverage_note}"
                    self.log(summary)
                    show = messagebox.showwarning if failures else messagebox.showinfo
                    show(self.title(), summary, parent=self)
        except queue.Empty:
            pass
        if not self.closed:
            self.after(100, self.poll_queue)

    def preview_selected(self):
        selected = self.tree.selection()
        if selected:
            self.preview_article(self.results[int(selected[0])]["json"])

    def preview_existing(self):
        path = filedialog.askopenfilename(parent=self, title="选择已采集的公众号文章 JSON", initialdir=self.out_var.get(), filetypes=(("JSON 文件", "*.json"),))
        if path:
            self.preview_article(path)

    def preview_article(self, json_path):
        try:
            path = Path(json_path)
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or data.get("platform") != "wechat_public_account" or not data.get("title"):
                raise ValueError("请选择单篇公众号文章的 JSON。")
            dialog = tk.Toplevel(self)
            dialog.title(data["title"])
            dialog.geometry("850x700")
            body = tk.Text(dialog, wrap="word", padx=18, pady=12)
            scroll = ttk.Scrollbar(dialog, command=body.yview)
            body.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            body.pack(fill="both", expand=True)
            body.insert("end", f"{data['title']}\n公众号：{data.get('account') or '未知'}\n发布时间：{data.get('published_at') or '未知'}\n原文：{data.get('url') or ''}\n\n")
            dialog.article_images = []
            base = path.parent.resolve()
            image_items = {item.get("relative_path") or item.get("path"): item
                           for item in data.get("images") or [] if isinstance(item, dict)
                           and isinstance(item.get("relative_path") or item.get("path"), str)}

            def insert_image(relative_path):
                if not isinstance(relative_path, str) or PureWindowsPath(relative_path).drive or Path(relative_path).is_absolute():
                    return False
                image_path = (base / relative_path).resolve()
                if not image_path.is_relative_to(base) or not image_path.is_file():
                    return False
                try:
                    with Image.open(image_path) as image:
                        image.thumbnail((720, 900))
                        photo = ImageTk.PhotoImage(image.copy(), master=dialog)
                    dialog.article_images.append(photo)
                    body.image_create("end", image=photo)
                    return True
                except (OSError, ValueError):
                    return False

            content = data.get("text") or data.get("body_text") or data.get("content_text") or data.get("markdown") or ""
            consumed, offset = set(), 0
            for match in re.finditer(r"\[图片：(images/\d{3,}\.(?:jpg|png|gif|webp))\]", content):
                body.insert("end", content[offset:match.start()])
                relative_path = match.group(1)
                if relative_path in image_items and insert_image(relative_path):
                    consumed.add(relative_path)
                else:
                    body.insert("end", match.group(0) + "（无法预览）")
                    consumed.add(relative_path)
                offset = match.end()
            body.insert("end", content[offset:])
            # 兼容没有原位标记的旧版记录。
            for relative_path in image_items:
                if relative_path in consumed:
                    continue
                body.insert("end", "\n\n")
                if not insert_image(relative_path):
                    body.insert("end", "[图片无法预览]")
            body.configure(state="disabled")
        except (OSError, ValueError, TypeError) as error:
            messagebox.showerror(self.title(), f"文章预览失败：{error}", parent=self)

    def open_output(self):
        if self.last_result and Path(self.last_result["output_dir"]).is_dir():
            os.startfile(self.last_result["output_dir"])

"""Codex 中转站切换窗口；由 Python 3.11+ 在 Windows 上运行。"""
from __future__ import annotations

import sys
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from relay_core import REASONING_EFFORTS, RelayError, RelayManager, resolve_home
from relay_images import DEFAULT_OUTPUT_DIR, ImageRelayManager, _normalize_base_url
from relay_image_mcp import register_mcp
from relay_models import fetch_model_catalog
from relay_catalog import refresh_official_model_catalog


BG = "#f3f5f9"
SURFACE = "#ffffff"
TEXT = "#172033"
MUTED = "#475569"
ACCENT = "#3763eb"
LINE = "#e1e6ef"


class ModelRequest:
    """Keep HTTP requests off the Tk thread and deliver results only while open."""
    def __init__(self, owner, base_url, api_key, on_success, on_error,
                 official_catalog_path=None):
        self.owner = owner
        self.on_success = on_success
        self.on_error = on_error
        self.result = queue.Queue(maxsize=1)
        self.pending = None
        self.closed = False
        self.official_catalog_count = None
        self.official_catalog_error = False
        self.destroy_binding = owner.bind("<Destroy>", self.on_destroy, add="+")
        threading.Thread(target=ModelRequest.work,
                         args=(self.result, base_url, api_key, official_catalog_path, self),
                         daemon=True).start()
        self.pending = owner.after(80, self.poll)

    @staticmethod
    def work(result_queue, base_url, api_key, official_catalog_path=None, request=None):
        if official_catalog_path is not None:
            try:
                request.official_catalog_count = refresh_official_model_catalog(official_catalog_path)
            except RelayError:
                request.official_catalog_error = True
        try:
            result_queue.put((True, fetch_model_catalog(base_url, api_key)))
        except Exception as exc:
            result_queue.put((False, exc.with_traceback(None)))

    def poll(self):
        self.pending = None
        if self.closed:
            return
        try:
            success, result = self.result.get_nowait()
        except queue.Empty:
            self.pending = self.owner.after(80, self.poll)
            return
        callback = self.on_success if success else self.on_error
        self.close()
        callback(result)

    def close(self):
        self.closed = True
        if self.pending is not None:
            try:
                self.owner.after_cancel(self.pending)
            except tk.TclError:
                pass
            self.pending = None
        if self.destroy_binding is not None:
            try:
                self.owner.unbind("<Destroy>", self.destroy_binding)
            except tk.TclError:
                pass
            self.destroy_binding = None
        self.owner = None
        self.on_success = self.on_error = None

    def on_destroy(self, event):
        if event.widget == self.owner:
            self.close()


def show_error(parent, exc):
    detail = str(exc) if isinstance(exc, RelayError) else (
        "操作未完成。请检查配置目录是否存在、文件是否可读写。\n"
        f"错误类型：{type(exc).__name__}"
    )
    messagebox.showerror("操作未完成", detail, parent=parent)


class ProfileDialog(tk.Toplevel):
    def __init__(self, parent, manager, profile=None, image=False):
        super().__init__(parent)
        self.manager = manager
        self.profile = dict(profile or ({"name": "八戒图片", "base_url": "https://bajie-api.com/v1"}
                                        if image else {}))
        self.image = image
        self.models = list(self.profile.get("models", []))
        self.model_catalog_changed = False
        self.fetching = False
        self.model_status = tk.StringVar(value=(
            (f"已保存 {len(self.models)} 个生图模型，可选择或手动填写当前生图模型。" if image else
             f"已保存 {len(self.models)} 个模型，可选择或手动填写默认模型。") if self.models else
            ("获取图片模型后选择当前生图模型，也可手动填写。" if image else
             "获取全部模型后选择默认模型；留空沿用 Codex 当前默认模型。")
        ))
        self.saved_id = None
        subject = "生图中转站" if image else "中转站"
        self.title(f"编辑{subject}" if profile else f"添加{subject}")
        self.configure(bg=SURFACE)
        self.resizable(False, False)
        self.transient(parent)
        shell = ttk.Frame(self, style="Surface.TFrame", padding=24)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text=self.title(), style="Surface.TLabel",
                  font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(shell, text="填写接口信息，获取模型后保存。密钥仅在本机加密保存。",
                  style="SurfaceMuted.TLabel").pack(anchor="w", pady=(6, 18))
        content = ttk.Frame(shell, style="Surface.TFrame")
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        self.vars = {}
        entries = {}
        fields = [
            ("name", "生图站点名称" if image else "站点名称",
             "例如：八戒图片" if image else "例如：主力站 / 备用站"),
            ("base_url", "图片 API 地址" if image else "API 地址",
             "例如 https://bajie-api.com/v1" if image else
             "填写站点提供的完整地址，例如 https://example.com/v1"),
            ("api_key", "图片 API Key" if image else "API Key",
             "独立保存在本机，保存后由 Windows 加密" if image else
             "只保存在本机，保存后的站点密钥由 Windows 加密"),
            ("model", "当前生图模型（可选择或手动填写）" if image else
             "默认模型（可选择或手动填写）", ""),
        ]
        for row, (key, label, hint) in enumerate(fields):
            ttk.Label(content, text=label, style="SurfaceField.TLabel").grid(
                row=row * 3, column=0, columnspan=2, sticky="w", pady=(12 if row else 0, 6)
            )
            variable = tk.StringVar(value=self.profile.get(key, ""))
            self.vars[key] = variable
            if key == "model":
                entry = ttk.Combobox(content, textvariable=variable, width=49, values=self.models)
                self.model_combo = entry
            else:
                entry = ttk.Entry(content, textvariable=variable, width=49,
                                  show="●" if key == "api_key" else "")
            entry.grid(row=row * 3 + 1, column=0, sticky="ew")
            entries[key] = entry
            if key == "api_key":
                visible = tk.BooleanVar(value=False)
                ttk.Checkbutton(content, text="显示", variable=visible, style="Surface.TCheckbutton",
                                command=lambda e=entry, v=visible: e.configure(
                                    show="" if v.get() else "●")
                                ).grid(row=row * 3 + 1, column=1, padx=(10, 0))
            elif key == "model":
                self.fetch_button = ttk.Button(content, text="获取模型", command=self.fetch)
                self.fetch_button.grid(row=row * 3 + 1, column=1, padx=(10, 0))
            label_options = {"textvariable": self.model_status} if key == "model" else {"text": hint}
            ttk.Label(content, **label_options, style="SurfaceMuted.TLabel", wraplength=530).grid(
                row=row * 3 + 2, column=0, columnspan=2, sticky="w", pady=(5, 0)
            )
        controls = ttk.Frame(content, style="Surface.TFrame")
        controls.grid(row=12, column=0, columnspan=2, sticky="e", pady=(24, 0))
        ttk.Button(controls, text="取消", command=self.destroy).pack(side="left", padx=(0, 10))
        self.save_button = ttk.Button(controls, text=f"保存{subject}", style="Accent.TButton", command=self.save)
        self.save_button.pack(side="left")
        self.bind("<Escape>", lambda event: self.destroy())
        self.bind("<Return>", lambda event: self.save() if event.widget != self.model_combo else None)
        self.variable_traces = [(key, self.vars[key].trace_add("write", self.invalidate_models))
                                for key in ("base_url", "api_key")]
        self.bind("<Destroy>", self.release_credentials, add="+")
        self.update_idletasks()
        self.geometry(f"+{parent.winfo_rootx() + 110}+{parent.winfo_rooty() + 55}")
        self.grab_set()
        entries["name"].focus_set()

    def release_credentials(self, event):
        if event.widget != self:
            return
        for key, binding in self.variable_traces:
            self.vars[key].trace_remove("write", binding)
        self.variable_traces = []
        self.vars["api_key"].set("")
        self.profile.clear()

    def invalidate_models(self, *args):
        self.models = []
        self.model_catalog_changed = False
        self.model_combo.configure(values=())
        self.model_status.set("地址或密钥已修改，请重新获取模型；也可手动填写当前生图模型。" if self.image else
                              "地址或密钥已修改，请重新获取模型；也可手动填写默认模型。")

    def fetch(self):
        if self.fetching:
            return
        base_url = self.vars["base_url"].get().strip().rstrip("/")
        api_key = self.vars["api_key"].get().strip()
        if self.image:
            try:
                base_url = _normalize_base_url(base_url)
            except RelayError as exc:
                show_error(self, exc)
                return
            if base_url != self.vars["base_url"].get().strip().rstrip("/"):
                self.vars["base_url"].set(base_url)
        self.fetching = True
        self.fetch_button.configure(state="disabled", text="获取中…")
        self.save_button.configure(state="disabled")
        self.model_status.set("正在获取模型列表和 OpenAI 官方模型能力目录…" if not self.image else
                              "正在向此站点获取模型列表…")

        def finish(catalog=None, error=None):
            self.fetching = False
            self.fetch_button.configure(state="normal", text="获取模型")
            self.save_button.configure(state="normal")
            if (self.vars["base_url"].get().strip().rstrip("/") != base_url or
                    self.vars["api_key"].get().strip() != api_key):
                self.model_status.set("地址或密钥已修改，请重新获取模型。")
                return
            if error is not None:
                self.model_status.set("获取失败，可重试或手动填写当前生图模型。" if self.image else
                                      "获取失败，可重试或手动填写默认模型。")
                show_error(self, error)
                return
            if not self.image:
                request = self.model_request
                if request.official_catalog_count is not None:
                    self.model_status.set(
                        f"已获取 {len(catalog.models)} 个模型，并同步 {request.official_catalog_count} 条官方能力。")
                elif request.official_catalog_error:
                    self.model_status.set("站点模型已获取；官方目录刷新失败，将使用已缓存或内置能力。")
            normalized = catalog.base_url != base_url
            if normalized:
                self.vars["base_url"].set(catalog.base_url)
            self.models = list(catalog.models)
            self.model_catalog_changed = True
            self.model_combo.configure(values=self.models)
            if self.image:
                self.model_status.set(f"已获取 {len(self.models)} 个模型，请选择图片模型。" +
                                      ("已更新为有效 API 地址。" if normalized else "仅保存在图片配置中。"))
            else:
                suffix = (f"已同步 {self.model_request.official_catalog_count} 条官方能力。"
                          if self.model_request.official_catalog_count is not None else
                          "官方目录刷新失败，将使用已缓存或内置能力。"
                          if self.model_request.official_catalog_error else "")
                self.model_status.set(f"已获取 {len(self.models)} 个模型，请选择默认模型。{suffix}" +
                                      ("已更新为有效 API 地址。" if normalized else "切换后写入 Codex。"))

        request_options = {} if self.image else {
            "official_catalog_path": self.manager.home / "relay-switcher" / "official_model_catalog.json"}
        self.model_request = ModelRequest(
            self, base_url, api_key, lambda catalog: finish(catalog=catalog),
            lambda error: finish(error=error), **request_options)

    def save(self):
        if self.fetching:
            return
        try:
            values = dict(
                name=self.vars["name"].get(),
                base_url=self.vars["base_url"].get(),
                api_key=self.vars["api_key"].get(),
                model=self.vars["model"].get(),
                profile_id=self.profile.get("id"),
                models=self.models if self.model_catalog_changed else None,
            )
            if not self.image:
                values.update(
                    reasoning_effort=self.profile.get("reasoning_effort", "high"),
                    subagent_reasoning_effort=self.profile.get("subagent_reasoning_effort", "max"),
                    subagent_model=self.profile.get("subagent_model", ""),
                )
            self.saved_id = self.manager.save_profile(**values)
        except Exception as exc:
            show_error(self, exc)
            return
        for variable in self.vars.values():
            variable.set("")
        self.destroy()


class Switcher(tk.Tk):
    def __init__(self, home=None):
        super().__init__()
        self.withdraw()
        self.title("Codex 中转站切换器")
        self.geometry("960x750")
        self.minsize(790, 660)
        self.configure(bg=BG)
        self.manager = None
        self.image_manager = None
        self.profiles = {}
        self.image_profiles = {}
        self.fetching = False
        self.model_profile_id = None
        self.current_profile_id = None
        self.current_auto_review_model = ""
        self.configure_styles()
        self.build_window()
        try:
            self.manager = RelayManager(Path(home) if home else resolve_home())
            self.refresh()
        except Exception as exc:
            show_error(self, exc)
            self.current_text.set("暂未读取配置，请点击“更换配置目录”")
        try:
            image_home = self.manager.home if self.manager else (Path(home) if home else resolve_home())
            self.image_manager = ImageRelayManager(image_home)
            self.refresh_images()
        except Exception as exc:
            self.image_status.set("图片配置暂不可用，请检查所选目录权限。")
            show_error(self, exc)
        self.update_idletasks()
        x = max(0, (self.winfo_screenwidth() - 960) // 2)
        y = max(0, (self.winfo_screenheight() - 700) // 2)
        self.geometry(f"+{x}+{y}")
        self.deiconify()

    def configure_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TFrame", background=BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Panel.TFrame", background=SURFACE, bordercolor=LINE,
                        lightcolor=LINE, darkcolor=LINE, borderwidth=1, relief="solid")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED, font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Field.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Surface.TLabel", background=SURFACE)
        style.configure("SurfaceMuted.TLabel", background=SURFACE, foreground=MUTED,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("SurfaceField.TLabel", background=SURFACE,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Status.TLabel", background="#eaf0ff", foreground="#3155b5",
                        padding=(12, 8), font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TNotebook", background=BG, borderwidth=0, bordercolor=BG,
                        lightcolor=BG, darkcolor=BG, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(22, 10), background=BG,
                        foreground=MUTED, bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", "#e9edf5")],
                  bordercolor=[("selected", SURFACE), ("active", "#e9edf5")],
                  lightcolor=[("selected", SURFACE), ("active", "#e9edf5")],
                  darkcolor=[("selected", SURFACE), ("active", "#e9edf5")],
                  foreground=[("selected", ACCENT), ("active", TEXT)])
        style.configure("TButton", padding=(14, 7), borderwidth=0,
                        background="#e8ecf3", foreground=TEXT, focusthickness=1,
                        focuscolor=ACCENT)
        style.map("TButton", background=[("disabled", "#eceff4"), ("pressed", "#d8dfeb"),
                                         ("active", "#dfe5ef")],
                  foreground=[("disabled", "#9ba4b3")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white")
        style.map("Accent.TButton", background=[("disabled", "#dce4f5"),
                                                ("pressed", "#2445ac"), ("active", "#2a52ce")],
                  foreground=[("disabled", "#8b9cbf")])
        style.configure("Quiet.TButton", background=BG, foreground=MUTED)
        style.map("Quiet.TButton", background=[("active", "#e6ebf4")],
                  foreground=[("disabled", "#a6adba"), ("active", TEXT)])
        style.configure("Danger.TButton", foreground="#ba4353")
        style.map("Danger.TButton", foreground=[("disabled", "#adb2bd"), ("active", "#9d2637")],
                  background=[("disabled", "#eceff4"), ("active", "#f8e8ec")])
        for name in ("TEntry", "TCombobox"):
            style.configure(name, padding=(9, 6), fieldbackground=SURFACE,
                            foreground=TEXT, background=SURFACE, bordercolor="#d4dce9",
                            lightcolor=SURFACE, darkcolor=SURFACE, arrowsize=14,
                            selectbackground=ACCENT, selectforeground=SURFACE)
            style.map(name, bordercolor=[("focus", ACCENT)],
                      fieldbackground=[("disabled", "#f1f3f7"), ("readonly", SURFACE)],
                      foreground=[("disabled", "#9ba4b3")])
        self.option_add("*TEntry.font", ("Microsoft YaHei UI", 10, "bold"))
        self.option_add("*TCombobox.font", ("Microsoft YaHei UI", 10, "bold"))
        self.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10, "bold"))
        self.option_add("*TCombobox*Listbox.selectBackground", "#eaf0ff")
        self.option_add("*TCombobox*Listbox.selectForeground", "#2445ac")
        style.configure("TCheckbutton", background=BG, foreground=TEXT, padding=(0, 4))
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("Surface.TCheckbutton", background=SURFACE)
        style.map("Surface.TCheckbutton", background=[("active", SURFACE)])
        style.configure("Treeview", rowheight=40, background=SURFACE, fieldbackground=SURFACE,
                        foreground=TEXT, borderwidth=0, bordercolor=SURFACE,
                        lightcolor=SURFACE, darkcolor=SURFACE, relief="flat")
        style.configure("Treeview.Heading", background="#f6f8fc", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9, "bold"), padding=(8, 9),
                        borderwidth=0, bordercolor="#f6f8fc", lightcolor="#f6f8fc",
                        darkcolor="#f6f8fc", relief="flat")
        style.map("Treeview.Heading", background=[("active", "#edf1f8")])
        style.map("Treeview", background=[("selected", "#e7eeff")],
                  foreground=[("selected", "#2445ac")])
        style.configure("Vertical.TScrollbar", background="#cbd3e0", troughcolor=SURFACE,
                        bordercolor=SURFACE, lightcolor="#cbd3e0", darkcolor="#cbd3e0",
                        borderwidth=0, arrowsize=9)
        style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
            "sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {
                "expand": "1", "sticky": "nswe"})]})])
        style.map("Vertical.TScrollbar", background=[("active", "#aab8d0"),
                                                     ("pressed", "#92a4c3")])
        style.configure("TSeparator", background=LINE)

    def wrap_label(self, label, container, reserved=0):
        label.configure(anchor="w", justify="left")
        container.bind("<Configure>", lambda event: label.configure(
            wraplength=max(160, event.width - reserved)), add="+")

    def build_window(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.tabs = ttk.Notebook(self)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=24, pady=(18, 0))
        main = ttk.Frame(self.tabs, padding=(0, 18, 0, 0))
        image_page = ttk.Frame(self.tabs, padding=(0, 18, 0, 0))
        self.tabs.add(main, text="语言模型中转")
        self.tabs.add(image_page, text="生图中转")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="语言模型中转", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.current_text = tk.StringVar()
        current = ttk.Label(top, textvariable=self.current_text, style="Muted.TLabel")
        current.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.wrap_label(current, top, 165)
        ttk.Button(top, text="＋ 添加中转站", command=self.add).grid(
            row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))
        table = ttk.Frame(main, style="Panel.TFrame", padding=1)
        table.grid(row=1, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("name", "url", "model"), show="headings",
                                 selectmode="browse")
        for key, label, width in (("name", "站点名称", 175),
                                  ("url", "API 地址", 390), ("model", "默认模型", 190)):
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=width, minwidth=100, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.update_selection)
        self.tree.bind("<Double-1>", lambda event: self.switch() if self.tree.identify_row(event.y) else None)
        self.tree.bind("<Return>", lambda event: self.switch())
        model_controls = ttk.Frame(main, style="Panel.TFrame", padding=(16, 12))
        model_controls.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        model_controls.columnconfigure(0, weight=1)
        ttk.Label(model_controls, text="所选站点默认模型", style="SurfaceField.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(model_controls, text="默认思考强度", style="SurfaceField.TLabel").grid(
            row=0, column=1, sticky="w", padx=(16, 0), pady=(0, 6))
        self.selected_model = tk.StringVar()
        self.model_combo = ttk.Combobox(model_controls, textvariable=self.selected_model, width=18)
        self.model_combo.grid(row=1, column=0, sticky="ew")
        self.selected_effort = tk.StringVar(value="high")
        self.effort_combo = ttk.Combobox(model_controls, textvariable=self.selected_effort,
                                         width=9, state="readonly", values=REASONING_EFFORTS)
        self.effort_combo.grid(row=1, column=1, sticky="ew", padx=(16, 0))
        self.fetch_button = ttk.Button(model_controls, text="获取模型", command=self.fetch)
        self.fetch_button.grid(row=1, column=2, padx=(12, 0))

        reasoning_controls = ttk.Frame(model_controls, style="Surface.TFrame")
        reasoning_controls.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        reasoning_controls.columnconfigure(1, weight=1)
        ttk.Label(reasoning_controls, text="推理强度来源", style="SurfaceField.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10))
        self.official_reasoning_status = tk.StringVar(value="获取模型时同步 OpenAI 官方模型能力目录")
        ttk.Label(reasoning_controls, textvariable=self.official_reasoning_status,
                  style="SurfaceMuted.TLabel").grid(row=0, column=1, columnspan=2, sticky="w")

        subagent_controls = ttk.Frame(model_controls, style="Surface.TFrame")
        subagent_controls.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        subagent_controls.columnconfigure(0, weight=1)
        ttk.Label(subagent_controls, text="子代理模型", style="SurfaceField.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(subagent_controls, text="子代理推理强度", style="SurfaceField.TLabel").grid(
            row=0, column=1, sticky="w", padx=(16, 0), pady=(0, 6))
        self.selected_subagent_model = tk.StringVar(value="")
        self.subagent_model_combo = ttk.Combobox(
            subagent_controls, textvariable=self.selected_subagent_model, width=18)
        self.subagent_model_combo.grid(row=1, column=0, sticky="ew")
        self.selected_subagent_effort = tk.StringVar(value="max")
        self.subagent_effort_combo = ttk.Combobox(
            subagent_controls, textvariable=self.selected_subagent_effort,
            width=9, state="readonly", values=REASONING_EFFORTS)
        self.subagent_effort_combo.grid(row=1, column=1, sticky="ew", padx=(16, 0))
        ttk.Label(subagent_controls, text="留空让 Codex 自行选择", style="SurfaceMuted.TLabel").grid(
            row=1, column=2, sticky="w", padx=(12, 0))

        review_controls = ttk.Frame(model_controls, style="Surface.TFrame")
        review_controls.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        review_controls.columnconfigure(1, weight=1)
        ttk.Label(review_controls, text="Auto Review 模型", style="SurfaceField.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12))
        self.selected_review_model = tk.StringVar()
        self.review_model_combo = ttk.Combobox(
            review_controls, textvariable=self.selected_review_model, width=14)
        self.review_model_combo.grid(row=0, column=1, sticky="ew")
        ttk.Label(review_controls, text="留空使用默认 reviewer", style="SurfaceMuted.TLabel").grid(
            row=0, column=2, padx=(12, 0))
        controls = ttk.Frame(main)
        controls.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.edit_button = ttk.Button(controls, text="编辑", command=self.edit)
        self.edit_button.pack(side="left")
        self.delete_button = ttk.Button(controls, text="删除", style="Danger.TButton", command=self.delete)
        self.delete_button.pack(side="left", padx=(10, 0))
        self.restore_button = ttk.Button(controls, text="恢复上次配置", style="Quiet.TButton", command=self.restore)
        self.restore_button.pack(side="left", padx=(10, 0))
        self.switch_button = ttk.Button(controls, text="应用为全局中转站", style="Accent.TButton",
                                        command=self.switch)
        self.switch_button.pack(side="right")
        note = ttk.Label(main, text="应用后请完全退出并重开 Codex；全局默认设置与旧中转入口会同步更新。子代理模型可留空，强度默认 max；light 对应 low。\n"
                         "Auto Review 模型用于此站点全部主模型；留空使用默认选择。此设置不会启用 Auto Review，也不影响 /review。",
                         style="Muted.TLabel")
        note.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.wrap_label(note, main)
        self.status = tk.StringVar(value="选择站点和模型，点击“应用为全局中转站”。")
        status = ttk.Label(main, textvariable=self.status, style="Status.TLabel")
        status.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        self.wrap_label(status, main, 24)
        footer = ttk.Frame(self, padding=(24, 12))
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.path_text = tk.StringVar(value="配置目录尚未加载")
        path = ttk.Label(footer, textvariable=self.path_text, style="Muted.TLabel")
        path.grid(row=0, column=0, sticky="ew")
        self.wrap_label(path, footer, 205)
        ttk.Button(footer, text="更换配置目录", style="Quiet.TButton", command=self.choose_home).grid(
            row=0, column=1, padx=(16, 0))
        self.update_selection()
        self.build_image_page(image_page)

    def build_image_page(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        top = ttk.Frame(page)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="生图中转", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        subtitle = ttk.Label(top, text="独立管理图片 API，选择当前生图模型后应用。", style="Muted.TLabel")
        subtitle.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.wrap_label(subtitle, top, 165)
        ttk.Button(top, text="＋ 添加生图站点", command=self.add_image).grid(
            row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))
        table = ttk.Frame(page, style="Panel.TFrame", padding=1)
        table.grid(row=1, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.image_tree = ttk.Treeview(table, columns=("name", "url", "model"), show="headings",
                                       selectmode="browse")
        for key, label, width in (("name", "生图站点", 175),
                                  ("url", "图片 API 地址", 390), ("model", "当前生图模型", 190)):
            self.image_tree.heading(key, text=label, anchor="w")
            self.image_tree.column(key, width=width, minwidth=100, anchor="w")
        self.image_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.image_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.image_tree.configure(yscrollcommand=scroll.set)
        self.image_tree.bind("<<TreeviewSelect>>", self.update_image_selection)
        image_controls = ttk.Frame(page, style="Panel.TFrame", padding=(16, 12))
        image_controls.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        image_controls.columnconfigure(0, weight=1)
        ttk.Label(image_controls, text="当前生图模型", style="SurfaceField.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        self.selected_image_model = tk.StringVar()
        self.image_model_combo = ttk.Combobox(image_controls, textvariable=self.selected_image_model, width=18)
        self.image_model_combo.grid(row=1, column=0, sticky="ew")
        self.image_fetch_button = ttk.Button(image_controls, text="获取模型", command=self.fetch_images)
        self.image_fetch_button.grid(row=1, column=1, padx=(12, 0))
        image_actions = ttk.Frame(page)
        image_actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.image_edit_button = ttk.Button(image_actions, text="编辑", command=self.edit_image)
        self.image_edit_button.pack(side="left")
        self.image_delete_button = ttk.Button(image_actions, text="删除", style="Danger.TButton", command=self.delete_image)
        self.image_delete_button.pack(side="left", padx=(8, 0))
        self.image_apply_button = ttk.Button(image_actions, text="应用", style="Accent.TButton",
                                             command=self.apply_image)
        self.image_apply_button.pack(side="right")
        image_settings = ttk.Frame(page, style="Panel.TFrame", padding=(16, 10))
        image_settings.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        image_settings.columnconfigure(1, weight=1)
        self.image_enabled = tk.BooleanVar(value=False)
        self.image_enable_toggle = ttk.Checkbutton(
            image_settings, text="启用生图工具", variable=self.image_enabled,
            command=self.toggle_images, style="Surface.TCheckbutton"
        )
        self.image_enable_toggle.grid(row=0, column=0, sticky="w")
        self.image_output_text = tk.StringVar(value=f"图片保存位置：{DEFAULT_OUTPUT_DIR}")
        output = ttk.Label(image_settings, textvariable=self.image_output_text, style="SurfaceMuted.TLabel")
        output.grid(row=0, column=1, sticky="e", padx=(20, 0))
        self.wrap_label(output, image_settings, 200)
        self.image_status = tk.StringVar(value="选择或添加一个图片 API 站点。")
        note = ttk.Label(page, text="首次应用会注册 MCP 工具，需重启 Codex；之后切换当前生图模型并点击“应用”，下次生图即可使用，无需重启。\n"
                         "启用开关即时影响新请求。仅支持文字生图，每次一张；已提交的请求无法通过开关撤销。",
                         style="Muted.TLabel")
        note.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.wrap_label(note, page)
        status = ttk.Label(page, textvariable=self.image_status, style="Status.TLabel")
        status.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        self.wrap_label(status, page, 24)
        self.update_image_selection()

    def update_image_selection(self, event=None):
        selected = self.image_tree.selection()[0] if self.image_tree.selection() else None
        state = "normal" if self.image_manager and selected else "disabled"
        if getattr(self, "image_fetching", False):
            state = "disabled"
        for button in (self.image_edit_button, self.image_delete_button,
                       self.image_apply_button, self.image_fetch_button):
            button.configure(state=state)
        self.image_model_combo.configure(state=state)
        if selected != getattr(self, "image_model_profile_id", None):
            self.image_model_profile_id = selected
            profile = self.image_profiles.get(selected, {})
            self.selected_image_model.set(profile.get("model", ""))
            self.image_model_combo.configure(values=profile.get("models", []))

    def refresh_images(self, select=None):
        settings = self.image_manager.settings()
        self.image_profiles = {profile["id"]: profile
                               for profile in self.image_manager.list_profiles()}
        self.image_tree.delete(*self.image_tree.get_children())
        for profile in self.image_profiles.values():
            name = profile["name"] + ("（当前）" if profile["id"] == settings["active_id"] else "")
            self.image_tree.insert("", "end", iid=profile["id"], values=(
                name, profile["base_url"], profile.get("model") or "未选择"
            ))
        self.image_enabled.set(settings["enabled"])
        self.image_output_text.set(f"图片保存位置：{settings['output_dir']}")
        self.image_model_profile_id = None
        chosen = select if select in self.image_profiles else settings["active_id"]
        if chosen not in self.image_profiles:
            chosen = next(iter(self.image_profiles), None)
        if chosen:
            self.image_tree.selection_set(chosen)
        self.update_image_selection()
        if settings["enabled"]:
            self.image_status.set("生图工具已启用。新的生图请求会使用当前站点。")
        elif settings["active_id"]:
            self.image_status.set("当前生图站点已保存，生图工具关闭中。")
        else:
            self.image_status.set("选择或添加一个图片 API 站点，选择模型后应用。")

    def add_image(self):
        if not self.image_manager:
            return
        try:
            dialog = ProfileDialog(self, self.image_manager, image=True)
            self.wait_window(dialog)
            if dialog.saved_id:
                self.refresh_images(dialog.saved_id)
                self.image_status.set("图片 API 已保存；选择模型后应用。")
        except Exception as exc:
            show_error(self, exc)

    def edit_image(self):
        profile_id = self.image_tree.selection()[0] if self.image_tree.selection() else None
        if not profile_id:
            return
        try:
            profile = self.image_manager.get_profile(profile_id)
            dialog = ProfileDialog(self, self.image_manager, profile, image=True)
            self.wait_window(dialog)
            profile.clear()
            if dialog.saved_id:
                self.refresh_images(dialog.saved_id)
                self.image_status.set("图片 API 已保存；修改当前站点的地址、密钥或模型会关闭生图，需重新应用并启用。")
        except Exception as exc:
            show_error(self, exc)

    def delete_image(self):
        profile_id = self.image_tree.selection()[0] if self.image_tree.selection() else None
        if not profile_id:
            return
        name = self.image_profiles[profile_id]["name"]
        if not messagebox.askyesno("删除生图站点", f"删除“{name}”的保存配置？", parent=self):
            return
        try:
            self.image_manager.delete_profile(profile_id)
            self.refresh_images()
            self.image_status.set("图片 API 配置已删除。")
        except Exception as exc:
            show_error(self, exc)

    def fetch_images(self):
        profile_id = self.image_tree.selection()[0] if self.image_tree.selection() else None
        if not profile_id or getattr(self, "image_fetching", False):
            return
        try:
            profile = self.image_manager.get_profile(profile_id)
        except Exception as exc:
            show_error(self, exc)
            return
        manager = self.image_manager
        base_url, api_key, name = profile["base_url"], profile["api_key"], profile["name"]
        profile.clear()
        self.image_fetching = True
        self.update_image_selection()
        self.image_fetch_button.configure(text="获取中…")
        self.image_status.set(f"正在获取“{name}”的模型列表…")

        def finish(catalog=None, error=None):
            self.image_fetching = False
            self.image_fetch_button.configure(text="获取模型")
            self.update_image_selection()
            if self.image_manager is not manager:
                return
            if error is not None:
                self.image_status.set(f"“{name}”获取失败，可手动填写模型 ID。")
                show_error(self, error)
                return
            try:
                manager.save_models(profile_id, catalog.models, base_url=base_url,
                                    api_key=api_key, resolved_base_url=catalog.base_url)
                self.refresh_images(profile_id)
                self.image_status.set(f"“{name}”已获取 {len(catalog.models)} 个模型。")
            except Exception as exc:
                show_error(self, exc)

        self.image_model_request = ModelRequest(
            self, base_url, api_key,
            lambda catalog: finish(catalog=catalog), lambda error: finish(error=error)
        )

    def toggle_images(self):
        if not self.image_manager:
            self.image_enabled.set(False)
            return
        try:
            enabled = self.image_enabled.get()
            registration = None
            if enabled:
                if not self.image_manager.settings()["active_id"]:
                    raise RelayError("请先选择模型并点击“应用”。")
                registration = register_mcp(
                    self.image_manager.home,
                    script_path=Path(__file__).with_name("relay_image_mcp.py"),
                    python_path=sys.executable
                )
            self.image_manager.set_enabled(enabled)
            self.refresh_images()
            if registration and registration["changed"]:
                self.image_status.set("已注册并启用生图工具；请完全退出并重启 Codex。")
        except Exception as exc:
            try:
                self.image_enabled.set(self.image_manager.settings()["enabled"])
            except Exception:
                self.image_enabled.set(False)
                self.image_status.set("无法读取生图状态，请检查配置目录权限或配置文件。")
            show_error(self, exc)

    def apply_image(self):
        profile_id = self.image_tree.selection()[0] if self.image_tree.selection() else None
        if not profile_id or getattr(self, "image_fetching", False):
            return
        try:
            self.image_manager.set_enabled(False)
            self.image_manager.apply(profile_id, self.selected_image_model.get(), DEFAULT_OUTPUT_DIR)
            registration = register_mcp(
                self.image_manager.home,
                script_path=Path(__file__).with_name("relay_image_mcp.py"),
                python_path=sys.executable
            )
            self.image_manager.set_enabled(True)
            self.refresh_images(profile_id)
            restart_note = "首次注册需完全重启 Codex。" if registration["changed"] else "当前生图模型已生效，下次生图使用新模型，无需重启 Codex。"
            self.image_status.set(f"生图中转站已应用并启用；{restart_note}")
            messagebox.showinfo(
                "生图中转站已应用",
                "已将图片 API 独立注册为 Codex MCP 工具。\n\n"
                f"{restart_note}后续可在本页即时启停新生图请求。\n"
                "图片保存到 D:\\codex-\\image，每次调用生成一张。\n"
                "Codex 仍可能受客户端自身的审批策略限制。",
                parent=self
            )
        except Exception as exc:
            try:
                self.refresh_images(profile_id)
            except Exception:
                self.image_enabled.set(False)
            self.image_status.set("生图配置未全部应用，请检查错误提示后重试。")
            show_error(self, exc)

    def selected_id(self):
        selection = self.tree.selection()
        return selection[0] if selection else None

    def update_selection(self, event=None):
        selected = self.selected_id()
        state = "normal" if self.manager and selected else "disabled"
        for button in (self.edit_button, self.delete_button, self.switch_button):
            button.configure(state="disabled" if self.fetching and button != self.delete_button else state)
        self.restore_button.configure(
            state="normal" if self.manager and self.manager.has_backup else "disabled"
        )
        self.fetch_button.configure(state="normal" if state == "normal" and not self.fetching else "disabled")
        self.model_combo.configure(state=state)
        self.effort_combo.configure(state="readonly" if state == "normal" else state)
        self.subagent_model_combo.configure(state=state)
        self.subagent_effort_combo.configure(state="readonly" if state == "normal" else state)
        self.review_model_combo.configure(state=state)
        if selected != self.model_profile_id:
            self.model_profile_id = selected
            profile = self.profiles.get(selected, {})
            self.selected_model.set(profile.get("model", ""))
            self.selected_effort.set(profile.get("reasoning_effort", "high"))
            self.selected_subagent_model.set(profile.get("subagent_model", ""))
            self.selected_subagent_effort.set(profile.get("subagent_reasoning_effort", "max"))
            self.selected_review_model.set(profile.get(
                "auto_review_model",
                self.current_auto_review_model if selected == self.current_profile_id else ""
            ))
            self.model_combo.configure(values=profile.get("models", []))
            self.subagent_model_combo.configure(values=profile.get("models", []))
            self.review_model_combo.configure(values=profile.get("models", []))

    def refresh(self, select=None):
        current = self.manager.current()
        self.current_profile_id = current.get("id")
        self.current_auto_review_model = current.get("auto_review_model", "")
        profiles = self.manager.list_profiles()
        self.profiles = {profile["id"]: profile for profile in profiles}
        self.tree.delete(*self.tree.get_children())
        for profile in profiles:
            self.tree.insert("", "end", iid=profile["id"], values=(
                profile["name"], profile["base_url"], profile.get("model") or "沿用当前默认模型"
            ))
        current_model = current.get("model") or "默认模型"
        self.current_text.set(f"全局配置：{current.get('name') or '未配置中转站'} · {current_model}")
        self.path_text.set(f"全局配置文件：{self.manager.config}")
        self.model_profile_id = None
        if select and select in self.profiles:
            self.tree.selection_set(select)
        elif profiles:
            match = next((p["id"] for p in profiles if p["base_url"] == current.get("base_url")
                          and p["name"] == current.get("name")), profiles[0]["id"])
            self.tree.selection_set(match)
        self.update_selection()

    def fetch(self):
        profile_id = self.selected_id()
        if not profile_id or self.fetching:
            return
        manager = self.manager
        try:
            profile = manager.get_profile(profile_id)
        except Exception as exc:
            show_error(self, exc)
            return
        base_url, api_key = profile["base_url"], profile["api_key"]
        station_name = profile["name"]
        self.fetching = True
        self.fetch_button.configure(state="disabled", text="获取中…")
        self.update_selection()
        self.status.set(f"正在获取“{station_name}”的模型列表和官方模型能力…")

        def finish(catalog=None, error=None):
            self.fetching = False
            self.fetch_button.configure(text="获取模型")
            self.update_selection()
            if self.manager is not manager:
                return
            request = self.model_request
            if request.official_catalog_count is not None:
                self.official_reasoning_status.set(
                    f"已同步 OpenAI 官方模型目录：{request.official_catalog_count} 个模型")
            elif request.official_catalog_error:
                self.official_reasoning_status.set("官方目录刷新失败，应用时使用已缓存或内置能力")
            if error is not None:
                self.status.set(f"“{station_name}”获取失败，可重试或手动填写默认模型。")
                show_error(self, error)
                return
            try:
                manager.save_models(profile_id, catalog.models, base_url=base_url, api_key=api_key,
                                    resolved_base_url=catalog.base_url)
                updated = next(p for p in manager.list_profiles() if p["id"] == profile_id)
                self.profiles[profile_id] = updated
                self.tree.item(profile_id, values=(updated["name"], updated["base_url"],
                                                  updated.get("model") or "沿用当前默认模型"))
                if self.selected_id() == profile_id:
                    self.model_combo.configure(values=catalog.models)
                    self.review_model_combo.configure(values=catalog.models)
                self.status.set(f"“{station_name}”已缓存 {len(catalog.models)} 个模型，请选择默认模型后点击切换。" +
                                ("已更新 API 地址。" if catalog.base_url != base_url else ""))
            except Exception as exc:
                self.status.set("站点信息已变化，模型列表未保存，请重新获取。")
                show_error(self, exc)

        self.model_request = ModelRequest(
            self, base_url, api_key, lambda catalog: finish(catalog=catalog),
            lambda error: finish(error=error),
            official_catalog_path=manager.home / "relay-switcher" / "official_model_catalog.json")

    def add(self):
        if not self.manager:
            self.choose_home()
            return
        try:
            dialog = ProfileDialog(self, self.manager)
            self.wait_window(dialog)
            if dialog.saved_id:
                self.refresh(dialog.saved_id)
                self.status.set("站点已保存。选中后点击切换即可使用。")
        except Exception as exc:
            show_error(self, exc)

    def edit(self):
        profile_id = self.selected_id()
        if not profile_id or self.fetching:
            return
        try:
            profile = self.manager.get_profile(profile_id)
            dialog = ProfileDialog(self, self.manager, profile)
            self.wait_window(dialog)
            profile.clear()
            if dialog.saved_id:
                self.refresh(dialog.saved_id)
                self.status.set("站点信息已保存；要应用修改，请点击切换。")
        except Exception as exc:
            show_error(self, exc)

    def delete(self):
        profile_id = self.selected_id()
        if not profile_id:
            return
        name = self.profiles[profile_id]["name"]
        if not messagebox.askyesno("删除保存的站点", f"删除列表中的“{name}”？\nCodex 当前配置保持不变。", parent=self):
            return
        try:
            self.manager.delete_profile(profile_id)
            self.refresh()
            self.status.set("已从列表删除站点。")
        except Exception as exc:
            show_error(self, exc)

    def switch(self):
        profile_id = self.selected_id()
        if not profile_id or self.fetching:
            return
        try:
            profile = self.manager.get_profile(profile_id)
            self.manager.save_profile(profile["name"], profile["base_url"], profile["api_key"],
                                      model=self.selected_model.get(), profile_id=profile_id,
                                      reasoning_effort=self.selected_effort.get(),
                                      auto_review_model=self.selected_review_model.get(),
                                      subagent_reasoning_effort=self.selected_subagent_effort.get(),
                                      subagent_model=self.selected_subagent_model.get())
            profile.clear()
            provider_ids = self.manager.switch(profile_id)
            name = self.profiles[profile_id]["name"]
            self.refresh(profile_id)
            self.status.set(f"已更新“{name}”的全局配置，同步 {len(provider_ids)} 个中转入口；请重开 Codex。")
            messagebox.showinfo("全局配置已更新", f"已将“{name}”应用到 Codex 全局配置。\n"
                                f"同步了 {len(provider_ids)} 个中转入口，旧入口也已改用所选站点。\n\n"
                                "正在运行的对话可能仍保留旧连接。\n"
                                "请完全退出 Codex，再重新打开已有或新对话。\n"
                                "重开后，Codex 下拉框会包含此站点已获取的全部模型。\n"
                                "所选模型作为默认；其他模型可直接在 Codex 中选择。\n\n"
                                "原配置已备份，可用“恢复上次配置”撤回。", parent=self)
        except Exception as exc:
            show_error(self, exc)

    def restore(self):
        try:
            self.manager.restore()
            self.refresh()
            self.status.set("已恢复上次切换前的配置。重启 Codex 后生效。")
            messagebox.showinfo("配置已恢复", "已恢复上次切换前的配置。\n请完全退出并重新打开 Codex。", parent=self)
        except Exception as exc:
            show_error(self, exc)

    def choose_home(self):
        directory = filedialog.askdirectory(title="选择含 config.toml 的 Codex 配置目录",
                                            initialdir=str(self.manager.home if self.manager else resolve_home()), parent=self)
        if not directory:
            return
        previous = self.manager
        previous_image = self.image_manager
        try:
            manager = RelayManager(Path(directory))
            manager.current()
            manager.list_profiles()
            image_manager = ImageRelayManager(manager.home)
            image_manager.settings()
            image_manager.list_profiles()
            self.manager = manager
            self.image_manager = image_manager
            self.refresh()
            self.refresh_images()
            self.status.set("已读取所选配置目录。")
        except Exception as exc:
            self.manager = previous
            self.image_manager = previous_image
            if previous:
                try:
                    self.refresh()
                except Exception:
                    self.tree.delete(*self.tree.get_children())
                    self.profiles = {}
                    self.update_selection()
            if previous_image:
                try:
                    self.refresh_images()
                except Exception:
                    self.image_tree.delete(*self.image_tree.get_children())
                    self.image_profiles = {}
                    self.update_image_selection()
            show_error(self, exc)


if __name__ == "__main__":
    try:
        Switcher(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
    except Exception as exc:
        error_root = tk.Tk()
        error_root.withdraw()
        show_error(error_root, exc)
        error_root.destroy()

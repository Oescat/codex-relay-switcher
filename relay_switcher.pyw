"""Codex 中转站切换窗口；由 Python 3.11+ 在 Windows 上运行。"""
from __future__ import annotations

import sys
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from relay_core import REASONING_EFFORTS, RelayError, RelayManager, resolve_home
from relay_models import fetch_model_catalog


BG = "#f5f7fb"
TEXT = "#182337"
MUTED = "#63718a"
ACCENT = "#235ed7"


class ModelRequest:
    """Keep HTTP requests off the Tk thread and deliver results only while open."""
    def __init__(self, owner, base_url, api_key, on_success, on_error):
        self.owner = owner
        self.on_success = on_success
        self.on_error = on_error
        self.result = queue.Queue(maxsize=1)
        self.pending = None
        self.closed = False
        self.destroy_binding = owner.bind("<Destroy>", self.on_destroy, add="+")
        threading.Thread(target=ModelRequest.work, args=(self.result, base_url, api_key), daemon=True).start()
        self.pending = owner.after(80, self.poll)

    @staticmethod
    def work(result_queue, base_url, api_key):
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
    def __init__(self, parent, manager, profile=None):
        super().__init__(parent)
        self.manager = manager
        self.profile = dict(profile or {})
        self.models = list(self.profile.get("models", []))
        self.model_catalog_changed = False
        self.fetching = False
        self.model_status = tk.StringVar(value=(
            f"已保存 {len(self.models)} 个模型，可选择或手动填写默认模型。" if self.models else
            "获取全部模型后选择默认模型；留空沿用 Codex 当前默认模型。"
        ))
        self.saved_id = None
        self.title("编辑中转站" if profile else "添加中转站")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        content = ttk.Frame(self, padding=24)
        content.pack(fill="both", expand=True)
        self.vars = {}
        entries = {}
        fields = [
            ("name", "站点名称", "例如：主力站 / 备用站"),
            ("base_url", "API 地址", "填写站点提供的完整地址，例如 https://example.com/v1"),
            ("api_key", "API Key", "只保存在本机，保存后的站点密钥由 Windows 加密"),
            ("model", "默认模型（可选择或手动填写）", ""),
        ]
        for row, (key, label, hint) in enumerate(fields):
            ttk.Label(content, text=label, style="Field.TLabel").grid(
                row=row * 3, column=0, columnspan=2, sticky="w", pady=(12 if row else 0, 6)
            )
            variable = tk.StringVar(value=self.profile.get(key, ""))
            self.vars[key] = variable
            if key == "model":
                entry = ttk.Combobox(content, textvariable=variable, width=58, values=self.models)
                self.model_combo = entry
            else:
                entry = ttk.Entry(content, textvariable=variable, width=60,
                                  show="●" if key == "api_key" else "")
            entry.grid(row=row * 3 + 1, column=0, sticky="ew", ipady=5)
            entries[key] = entry
            if key == "api_key":
                visible = tk.BooleanVar(value=False)
                ttk.Checkbutton(content, text="显示", variable=visible,
                                command=lambda e=entry, v=visible: e.configure(
                                    show="" if v.get() else "●")
                                ).grid(row=row * 3 + 1, column=1, padx=(10, 0))
            elif key == "model":
                self.fetch_button = ttk.Button(content, text="获取模型", command=self.fetch)
                self.fetch_button.grid(row=row * 3 + 1, column=1, padx=(10, 0))
            label_options = {"textvariable": self.model_status} if key == "model" else {"text": hint}
            ttk.Label(content, **label_options, style="Muted.TLabel").grid(
                row=row * 3 + 2, column=0, columnspan=2, sticky="w", pady=(5, 0)
            )
        controls = ttk.Frame(content)
        controls.grid(row=12, column=0, columnspan=2, sticky="e", pady=(24, 0))
        ttk.Button(controls, text="取消", command=self.destroy).pack(side="left", padx=(0, 10))
        self.save_button = ttk.Button(controls, text="保存站点", style="Accent.TButton", command=self.save)
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
        self.model_status.set("地址或密钥已修改，请重新获取模型；也可手动填写默认模型。")

    def fetch(self):
        if self.fetching:
            return
        base_url = self.vars["base_url"].get().strip().rstrip("/")
        api_key = self.vars["api_key"].get().strip()
        self.fetching = True
        self.fetch_button.configure(state="disabled", text="获取中…")
        self.save_button.configure(state="disabled")
        self.model_status.set("正在向此站点获取模型列表…")

        def finish(catalog=None, error=None):
            self.fetching = False
            self.fetch_button.configure(state="normal", text="获取模型")
            self.save_button.configure(state="normal")
            if (self.vars["base_url"].get().strip().rstrip("/") != base_url or
                    self.vars["api_key"].get().strip() != api_key):
                self.model_status.set("地址或密钥已修改，请重新获取模型。")
                return
            if error is not None:
                self.model_status.set("获取失败，可重试或手动填写默认模型。")
                show_error(self, error)
                return
            normalized = catalog.base_url != base_url
            if normalized:
                self.vars["base_url"].set(catalog.base_url)
            self.models = list(catalog.models)
            self.model_catalog_changed = True
            self.model_combo.configure(values=self.models)
            self.model_status.set(f"已获取 {len(self.models)} 个模型，请选择默认模型。" +
                                  ("已更新为有效 API 地址。" if normalized else "切换后全部写入 Codex。"))

        self.model_request = ModelRequest(self, base_url, api_key,
                                         lambda catalog: finish(catalog=catalog), lambda error: finish(error=error))

    def save(self):
        if self.fetching:
            return
        try:
            self.saved_id = self.manager.save_profile(
                name=self.vars["name"].get(),
                base_url=self.vars["base_url"].get(),
                api_key=self.vars["api_key"].get(),
                model=self.vars["model"].get(),
                profile_id=self.profile.get("id"),
                models=self.models if self.model_catalog_changed else None,
                reasoning_effort=self.profile.get("reasoning_effort", "high"),
            )
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
        self.profiles = {}
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
        self.update_idletasks()
        x = max(0, (self.winfo_screenwidth() - 960) // 2)
        y = max(0, (self.winfo_screenheight() - 700) // 2)
        self.geometry(f"+{x}+{y}")
        self.deiconify()

    def configure_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Field.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", padding=(15, 9), borderwidth=0)
        style.configure("Accent.TButton", background=ACCENT, foreground="white")
        style.map("Accent.TButton", background=[("active", "#174cad"), ("disabled", "#d8e1f1")],
                  foreground=[("disabled", "#8290a7")])
        style.configure("TCheckbutton", background=BG)
        style.configure("Treeview", rowheight=40, background="white", fieldbackground="white",
                        foreground=TEXT, borderwidth=0)
        style.configure("Treeview.Heading", background="#e9eef8", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9, "bold"), padding=(8, 9))
        style.map("Treeview", background=[("selected", "#dce8ff")],
                  foreground=[("selected", "#123a80")])

    def build_window(self):
        main = ttk.Frame(self, padding=28)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)
        ttk.Label(main, text="Codex 中转站", font=("Microsoft YaHei UI", 22, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(main, text="保存一次，之后选择站点即可更新 Codex 全局中转配置。", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(7, 20)
        )
        top = ttk.Frame(main)
        top.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.current_text = tk.StringVar()
        ttk.Label(top, textvariable=self.current_text, style="Field.TLabel").pack(side="left")
        ttk.Button(top, text="＋ 添加中转站", command=self.add).pack(side="right")
        table = ttk.Frame(main)
        table.grid(row=3, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("name", "url", "model"), show="headings",
                                 selectmode="browse")
        for key, label, width in (("name", "站点名称", 170),
                                  ("url", "API 地址", 420), ("model", "默认模型", 180)):
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=width, minwidth=100, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.update_selection)
        self.tree.bind("<Double-1>", lambda event: self.switch() if self.tree.identify_row(event.y) else None)
        self.tree.bind("<Return>", lambda event: self.switch())
        model_controls = ttk.Frame(main)
        model_controls.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        ttk.Label(model_controls, text="所选站点默认模型", style="Field.TLabel").pack(side="left", padx=(0, 12))
        self.selected_model = tk.StringVar()
        self.model_combo = ttk.Combobox(model_controls, textvariable=self.selected_model, width=38)
        self.model_combo.pack(side="left", fill="x", expand=True)
        ttk.Label(model_controls, text="默认思考强度", style="Field.TLabel").pack(side="left", padx=(14, 8))
        self.selected_effort = tk.StringVar(value="high")
        self.effort_combo = ttk.Combobox(model_controls, textvariable=self.selected_effort,
                                         width=9, state="readonly", values=REASONING_EFFORTS)
        self.effort_combo.pack(side="left")
        self.fetch_button = ttk.Button(model_controls, text="获取模型", command=self.fetch)
        self.fetch_button.pack(side="left", padx=(12, 0))
        review_controls = ttk.Frame(main)
        review_controls.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(review_controls, text="Auto Review 模型", style="Field.TLabel").pack(side="left", padx=(0, 12))
        self.selected_review_model = tk.StringVar()
        self.review_model_combo = ttk.Combobox(
            review_controls, textvariable=self.selected_review_model, width=38
        )
        self.review_model_combo.pack(side="left", fill="x", expand=True)
        ttk.Label(review_controls, text="留空使用 Codex 默认 reviewer", style="Muted.TLabel").pack(
            side="left", padx=(10, 0)
        )
        controls = ttk.Frame(main)
        controls.grid(row=6, column=0, sticky="ew", pady=(16, 0))
        self.edit_button = ttk.Button(controls, text="编辑", command=self.edit)
        self.edit_button.pack(side="left")
        self.delete_button = ttk.Button(controls, text="删除", command=self.delete)
        self.delete_button.pack(side="left", padx=(10, 0))
        self.restore_button = ttk.Button(controls, text="恢复上次配置", command=self.restore)
        self.restore_button.pack(side="left", padx=(10, 0))
        self.switch_button = ttk.Button(controls, text="应用为全局中转站", style="Accent.TButton",
                                        command=self.switch)
        self.switch_button.pack(side="right")
        ttk.Label(main, text="应用时更新全局默认设置，并同步旧中转入口；子代理默认思考强度为 max。\n"
                            "Auto Review 模型应用到此站点全部主模型；留空由 Codex 默认选择。此项不会启用 Auto Review，也不影响 /review。\n"
                            "light 对应 Codex 的 low。请完全退出并重开 Codex，再打开已有或新对话。",
                  style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=(16, 0))
        self.status = tk.StringVar(value="选择一个站点，然后点击切换。")
        ttk.Label(main, textvariable=self.status, foreground=ACCENT).grid(
            row=8, column=0, sticky="w", pady=(12, 12)
        )
        ttk.Separator(main).grid(row=9, column=0, sticky="ew")
        footer = ttk.Frame(main)
        footer.grid(row=10, column=0, sticky="ew", pady=(12, 0))
        self.path_text = tk.StringVar(value="配置目录尚未加载")
        ttk.Label(footer, textvariable=self.path_text, style="Muted.TLabel").pack(side="left")
        ttk.Button(footer, text="更换配置目录", command=self.choose_home).pack(side="right")
        self.update_selection()

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
        self.effort_combo.configure(state=state)
        self.review_model_combo.configure(state=state)
        if selected != self.model_profile_id:
            self.model_profile_id = selected
            profile = self.profiles.get(selected, {})
            self.selected_model.set(profile.get("model", ""))
            self.selected_effort.set(profile.get("reasoning_effort", "high"))
            self.selected_review_model.set(profile.get(
                "auto_review_model",
                self.current_auto_review_model if selected == self.current_profile_id else ""
            ))
            self.model_combo.configure(values=profile.get("models", []))
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
        self.status.set(f"正在获取“{station_name}”的模型列表…")

        def finish(catalog=None, error=None):
            self.fetching = False
            self.fetch_button.configure(text="获取模型")
            self.update_selection()
            if self.manager is not manager:
                return
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

        self.model_request = ModelRequest(self, base_url, api_key,
                                         lambda catalog: finish(catalog=catalog), lambda error: finish(error=error))

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
                                      auto_review_model=self.selected_review_model.get())
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
        try:
            manager = RelayManager(Path(directory))
            manager.current()
            manager.list_profiles()
            self.manager = manager
            self.refresh()
            self.status.set("已读取所选配置目录。")
        except Exception as exc:
            self.manager = previous
            if previous:
                try:
                    self.refresh()
                except Exception:
                    self.tree.delete(*self.tree.get_children())
                    self.profiles = {}
                    self.update_selection()
            show_error(self, exc)


if __name__ == "__main__":
    try:
        Switcher(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
    except Exception as exc:
        error_root = tk.Tk()
        error_root.withdraw()
        show_error(error_root, exc)
        error_root.destroy()

"""Окно «Сохранить запись» в стиле Takeone: своё название, папка и формат.
Папка и формат запоминаются между запусками (~/.takeone/save_prefs.json)."""
import json
import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox

import ui_kit as ui
from ui_kit import C

PREFS = os.path.join(os.path.expanduser("~"), ".takeone", "save_prefs.json")
FORMATS = {"android": ("mp4", "mkv"), "airplay": ("mp4", "mkv"), "ios": ("mp4", "mov"), "edit": ("mp4", "mkv", "mov")}
BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def load_prefs():
    try:
        with open(PREFS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prefs(p):
    try:
        os.makedirs(os.path.dirname(PREFS), exist_ok=True)
        with open(PREFS, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def last_dir(default):
    d = load_prefs().get("dir")
    return d if d and os.path.isdir(d) else default


def clean_name(name):
    name = re.sub(r"\s+", " ", BAD.sub(" ", name)).strip(" ._")  # «Баг: магазин/покупка?» → «Баг магазин покупка»
    root, ext = os.path.splitext(name)
    if ext.lower() in (".mp4", ".mkv", ".mov", ".m4v"):
        name = root
    return name[:180]


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


class SaveDialog(tk.Toplevel):
    W = 460

    def __init__(self, parent, app_name, name, kind="android", title="Сохранить запись", info="",
                 can_delete=True, folder=None):
        super().__init__(parent, bg=C["bg"])
        self.app_name, self.kind, self.can_delete = app_name, kind, can_delete
        self.result = ("cancel", None)
        prefs = load_prefs()
        self.folder = folder or last_dir(os.path.expanduser("~"))
        fmts = FORMATS.get(kind, ("mp4",))
        ext = prefs.get(f"ext_{kind}", fmts[0])
        self.ext = tk.StringVar(value=ext if ext in fmts else fmts[0])
        self.name = tk.StringVar(value=name)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel() if parent else None)
        self.protocol("WM_DELETE_WINDOW", self._close)

        W = self.W
        box = tk.Frame(self, bg=C["bg"], padx=22, pady=20)
        box.pack(fill="both")
        tk.Frame(box, bg=C["bg"], width=W - 44, height=0).pack()
        ui.label(box, title, 17, weight="semibold").pack(anchor="w")
        if info:
            ui.label(box, info, 12, C["muted"]).pack(anchor="w", pady=(2, 0))

        ui.label(box, "Название", 12, C["text2"]).pack(anchor="w", pady=(18, 6))
        row = tk.Frame(box, bg=C["bg"])
        row.pack(fill="x")
        self.ext_lbl = ui.label(row, "", 13, C["text2"], mono=True)
        self.ext_lbl.pack(side="right", padx=(8, 0))
        self.f_name = ui.Field(row, self.name, width=300)
        self.f_name.pack(side="left", fill="x", expand=True)
        self.err = ui.label(box, "", 12, C["amber_text"])
        self.err.pack(anchor="w")

        ui.label(box, "Папка", 12, C["text2"]).pack(anchor="w", pady=(8, 6))
        row = tk.Frame(box, bg=C["bg"])
        row.pack(fill="x")
        b = ui.Button(row, "Изменить…", self._pick_folder, kind="secondary", icon="folder", height=36, radius=8,
                      size=13)
        b.pack(side="right", padx=(8, 0))
        self.folder_w = W - 44 - int(b.cget("width")) - 8 - 16 - 8  # ширина под путь — постоянная
        self.folder_card = ui.Card(row, pad=8, radius=6, fill=C["field"], outline=C["field_line"])
        self.folder_card.pack(side="left", fill="x", expand=True)
        self.folder_lbl = ui.label(self.folder_card.body, "", 13, C["text"], anchor="w")
        self.folder_lbl.pack(fill="x", padx=2)
        self._show_folder()

        if len(fmts) > 1:
            ui.label(box, "Формат", 12, C["text2"]).pack(anchor="w", pady=(16, 6))
            labels = {"mp4": "MP4 — для всех", "mkv": "MKV", "mov": "MOV"}
            ui.Segmented(box, [(f, labels.get(f, f.upper())) for f in fmts], self.ext, self._ext_changed,
                         height=36, radius=9).pack(fill="x")

        ui.label(box, "Папка и формат запомнятся для следующих записей.", 12, C["muted"]).pack(anchor="w",
                                                                                              pady=(14, 0))
        btns = tk.Frame(box, bg=C["bg"])
        btns.pack(fill="x", pady=(18, 0))
        ui.Button(btns, "Сохранить", self._save, kind="primary", height=40, radius=10, size=14,
                  width=140).pack(side="right")
        if can_delete:
            ui.Button(btns, "Удалить запись", self._delete, kind="quiet", height=40, radius=10, size=14).pack(
                side="left")
        else:
            ui.Button(btns, "Отмена", self._cancel, kind="secondary", height=40, radius=10, size=14).pack(
                side="right", padx=(0, 8))

        self._ext_changed()
        self.bind("<Return>", lambda e: self._save())
        self.bind("<KP_Enter>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self._close())
        self.update_idletasks()
        self._center(parent)
        self.f_name.entry.focus_set()
        self.f_name.entry.select_range(0, "end")
        self.f_name.entry.icursor("end")
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _center(self, parent):
        try:
            top = parent.winfo_toplevel()
            x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
            y = top.winfo_rooty() + max(20, (top.winfo_height() - self.winfo_height()) // 3)
        except (tk.TclError, AttributeError):
            x = (self.winfo_screenwidth() - self.winfo_width()) // 2
            y = self.winfo_screenheight() // 4
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _show_folder(self):
        self.folder_lbl.configure(text=ui.ellipsize(ui.font(13), short_path(self.folder), self.folder_w))

    def _ext_changed(self):
        self.ext_lbl.configure(text="." + self.ext.get())

    def _pick_folder(self):
        d = filedialog.askdirectory(parent=self, title="Куда сохранять", initialdir=self.folder, mustexist=True)
        if d:
            self.folder = d
            self._show_folder()

    def _save(self):
        name = clean_name(self.name.get())
        if not name:
            self.err.configure(text="Введите название")
            return
        if not os.path.isdir(self.folder):
            self.err.configure(text="Папки нет — выберите другую")
            return
        path = os.path.join(self.folder, f"{name}.{self.ext.get()}")
        if os.path.exists(path) and not messagebox.askyesno(
                self.app_name, f"Файл «{os.path.basename(path)}» уже есть. Заменить его?", parent=self):
            return
        p = load_prefs()
        p["dir"] = self.folder
        p[f"ext_{self.kind}"] = self.ext.get()
        save_prefs(p)
        self.result = ("save", path)
        self.destroy()

    def _delete(self):
        if messagebox.askyesno(self.app_name, "Удалить эту запись без сохранения?", parent=self):
            self.result = ("delete", None)
            self.destroy()

    def _cancel(self):
        self.result = ("cancel", None)
        self.destroy()

    def _close(self):
        if self.can_delete:
            self._delete()
        else:
            self._cancel()


def ask(parent, app_name, name, **kw):
    """Показать окно и дождаться ответа: ("save", путь) | ("delete", None) | ("cancel", None)."""
    d = SaveDialog(parent, app_name, name, **kw)
    d.wait_window()
    return d.result


def unique_name(folder, name, ext):
    """Имя, которого ещё нет в папке: «имя», «имя (2)», «имя (3)»…"""
    cand, n = name, 2
    while os.path.exists(os.path.join(folder, f"{cand}.{ext}")):
        cand, n = f"{name} ({n})", n + 1
    return cand

"""Takeone UI kit — стиль «Студийный графит».

Общие цвета, шрифты, иконки и нарисованные виджеты для главного окна и окна телефона.
Всё рисуется на tk.Canvas: на Mac — векторно (Tk там сам сглаживает и рисует чётко на Retina),
на Windows/Linux — сглаженными картинками Pillow. Поэтому скругления и иконки выглядят
одинаково аккуратно на всех системах и не зависят от темы ttk."""
import os
import sys
import tkinter as tk
import tkinter.font as tkfont

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# --- токены ---------------------------------------------------------------------------------
C = {
    # поверхности
    "bg": "#0E0F12",          # фон главного окна
    "panel": "#1A1B20",       # карточки, панели окна телефона
    "elev": "#24262D",        # выбранная строка, кнопка-плашка
    "hover": "#2E3039",       # наведение, выбранный сегмент
    "press": "#373A45",
    "line": "#2A2C34",        # границы карточек
    "row_hover": "#202128",
    "field": "#121317",       # фон полей и переключателей-сегментов
    "field_line": "#34363F",
    "field_focus": "#7A7D8C",
    "video": "#0B0B0E",
    "toast": "#24252D",
    "card": "#1F2027",        # карточка сохранения
    "card_line": "#2E3039",
    "mono_bg": "#15161B",
    # текст
    "text": "#ECEDF2", "text2": "#A3A6B4", "muted": "#8A8D9B", "dim": "#5C5F6C", "off": "#3A3C45",
    "on_light": "#141519", "light": "#ECEDF2", "light_hover": "#FFFFFF", "light_press": "#D5D6DD",
    # статусы
    "red": "#E5484D", "rec": "#C8363B", "rec_dim": "#E59A9D",
    "amber": "#F5A524", "amber_text": "#F5B547", "amber_bg": "#3D3220", "amber_hover": "#4A3D27",
    "amber_line": "#5A4A2A", "paused_frame": "#8A6A2A",
    "ok": "#3FB97C", "ok_text": "#6FD3A0",
    "hint_title": "#FFE8B0", "hint_text": "#F3DFB0", "hint_note": "#D9C190",
}
# прежние имена (используются в окне телефона)
C.update(bar=C["panel"], icon=C["text"], pill=C["elev"], pill_hover=C["hover"])


# --- шрифты -----------------------------------------------------------------------------------
_FAM = {}
_FONTS = {}


def _families():
    if not _FAM:
        try:
            fams = set(tkfont.families())
        except Exception:  # noqa — ещё нет корневого окна
            fams = set()
        if IS_WIN:
            _FAM.update(ui="Segoe UI", mono="Consolas",
                        semi="Segoe UI Semibold" if "Segoe UI Semibold" in fams else None)
        else:
            ui = tkfont.nametofont("TkDefaultFont").actual("family")
            mono = "Menlo" if IS_MAC else tkfont.nametofont("TkFixedFont").actual("family")
            _FAM.update(ui=ui, mono=mono, semi=None)
    return _FAM


def font(size, weight="normal", mono=False, underline=False):
    """Шрифт размером в пикселях. weight: normal | semibold | bold."""
    f = _families()
    extra = ("underline",) if underline else ()
    if weight == "semibold" and not mono and f["semi"]:
        return (f["semi"], -size, "normal") + extra
    fam = f["mono"] if mono else f["ui"]
    return (fam, -size, "bold" if weight in ("semibold", "bold") else "normal") + extra


def tkf(spec):
    f = _FONTS.get(spec)
    if f is None:
        f = _FONTS[spec] = tkfont.Font(font=spec)
    return f


def measure(spec, text):
    return tkf(spec).measure(text)


def ellipsize(spec, text, maxw):
    if measure(spec, text) <= maxw:
        return text
    while text and measure(spec, text + "…") > maxw:
        text = text[:-1]
    return text.rstrip() + "…"


def fmt_hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def plural(n, one, few, many):
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# --- иконки ----------------------------------------------------------------------------------
# Сетка 24×24 (в духе Lucide): ("line", точки, толщина) | ("poly", точки) |
# ("circle", cx, cy, r, толщина|None) | ("rrect", x0, y0, x1, y1, r, толщина|None) |
# ("arc", cx, cy, r, нач, длина, толщина) — углы как в Tk: от «3 часов» против часовой стрелки.
_SPEAKER = ("poly", [(3.5, 9.5), (7.5, 9.5), (12, 5.6), (12, 18.4), (7.5, 14.5), (3.5, 14.5)])
ICONS = {
    "pause": [("rrect", 6.5, 5, 10.5, 19, 1.6, None), ("rrect", 13.5, 5, 17.5, 19, 1.6, None)],
    "play": [("poly", [(8, 5.4), (18.8, 12), (8, 18.6)])],
    "stop": [("rrect", 6, 6, 18, 18, 3, None)],
    "camera": [("rrect", 3, 7.5, 21, 19.5, 3.2, 2), ("line", [(8.6, 7.5), (10, 5), (14, 5), (15.4, 7.5)], 2),
               ("circle", 12, 13.4, 3.4, 2)],
    "sound": [_SPEAKER, ("arc", 12.5, 12, 4.2, -55, 110, 2), ("arc", 12.5, 12, 8, -55, 110, 2)],
    "muted": [_SPEAKER, ("line", [(15.5, 9.5), (20.5, 14.5)], 2), ("line", [(20.5, 9.5), (15.5, 14.5)], 2)],
    "back": [("line", [(14.5, 5.5), (8, 12), (14.5, 18.5)], 2.4)],
    "home": [("circle", 12, 12, 7, 2.2)],
    "recents": [("rrect", 6, 6, 18, 18, 3.2, 2.2)],
    "refresh": [("arc", 12, 12, 8.5, 50, 310, 2), ("line", [(20.5, 3.5), (20.5, 9), (15, 9)], 2)],
    "phone": [("rrect", 6.5, 2.5, 17.5, 21.5, 2.6, 1.6), ("line", [(11, 18), (13, 18)], 1.6)],
    "phone_off": [("rrect", 6.5, 2.5, 17.5, 21.5, 2.6, 1.8), ("line", [(11, 18), (13, 18)], 1.8),
                  ("line", [(3, 3.5), (21, 20.5)], 1.8)],
    "alert": [("line", [(12, 3.5), (21.8, 20), (2.2, 20), (12, 3.5)], 1.9), ("line", [(12, 9.5), (12, 13.6)], 2),
              ("circle", 12, 16.8, 1.15, None)],
    "close": [("line", [(6.5, 6.5), (17.5, 17.5)], 2), ("line", [(17.5, 6.5), (6.5, 17.5)], 2)],
    "check": [("line", [(5, 12.5), (9.5, 17), (19, 7)], 2.6)],
    "scissors": [("circle", 6.5, 6.5, 3, 2), ("circle", 6.5, 17.5, 3, 2), ("line", [(9, 8.3), (20, 17)], 2),
                 ("line", [(9, 15.7), (20, 7)], 2)],
    "folder": [("line", [(3, 7), (3, 18.5), (21, 18.5), (21, 8.5), (12, 8.5), (10, 5.5), (3, 5.5), (3, 7)], 2)],
    "film": [("rrect", 3, 4, 21, 20, 2.5, 1.8), ("line", [(7.5, 4), (7.5, 20)], 1.6),
             ("line", [(16.5, 4), (16.5, 20)], 1.6), ("line", [(3, 12), (7.5, 12)], 1.6),
             ("line", [(16.5, 12), (21, 12)], 1.6)],
    "mark_in": [("line", [(10, 4), (6, 4), (6, 20), (10, 20)], 2.2), ("poly", [(11, 8), (18, 12), (11, 16)])],
    "mark_out": [("line", [(14, 4), (18, 4), (18, 20), (14, 20)], 2.2), ("poly", [(13, 8), (6, 12), (13, 16)])],
    "undo": [("arc", 13, 13, 7, 270, 230, 2), ("line", [(4.5, 6), (5.5, 11), (10.5, 9.8)], 2)],
}


def icon_shapes(name, x, y, size, color):
    """Иконка из сетки 24×24 в пиксельные фигуры с левым верхним углом (x, y)."""
    k = size / 24

    def P(px, py):
        return (x + px * k, y + py * k)

    out = []
    for op in ICONS[name]:
        t = op[0]
        if t == "line":
            out.append(("line", [P(*p) for p in op[1]], color, op[2] * k))
        elif t == "poly":
            out.append(("poly", [P(*p) for p in op[1]], color))
        elif t == "circle":
            cx, cy = P(op[1], op[2])
            out.append(("circle", cx, cy, op[3] * k, color, op[4] and op[4] * k))
        elif t == "rrect":
            x0, y0 = P(op[1], op[2])
            x1, y1 = P(op[3], op[4])
            out.append(("rrect", x0, y0, x1, y1, op[5] * k, color, op[6] and op[6] * k))
        elif t == "arc":
            cx, cy = P(op[1], op[2])
            out.append(("arc", cx, cy, op[3] * k, op[4], op[5], color, op[6] * k))
    return out


def box(x0, y0, x1, y1, r, fill=None, outline=None, ow=1):
    """Скруглённый прямоугольник с заливкой и/или рамкой (рамка — внутри границ)."""
    if outline and fill:
        return [("rrect", x0, y0, x1, y1, r, outline, None),
                ("rrect", x0 + ow, y0 + ow, x1 - ow, y1 - ow, max(0, r - ow), fill, None)]
    if outline:
        return [("rrect", x0 + ow / 2, y0 + ow / 2, x1 - ow / 2, y1 - ow / 2, r, outline, ow)]
    if fill:
        return [("rrect", x0, y0, x1, y1, r, fill, None)]
    return []


# --- рисование -------------------------------------------------------------------------------
def _rounded(c, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


def _tk_shape(c, sh):
    t = sh[0]
    if t == "line":
        c.create_line(*[v for p in sh[1] for v in p], fill=sh[2], width=sh[3], capstyle="round",
                      joinstyle="round")
    elif t == "poly":
        c.create_polygon(*[v for p in sh[1] for v in p], fill=sh[2], outline=sh[2], width=1.4,
                         joinstyle="round")
    elif t == "circle":
        _, cx, cy, r, col, w = sh
        if w:
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=col, width=w)
        else:
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col, outline="")
    elif t == "rrect":
        _, x0, y0, x1, y1, r, col, w = sh
        if w:
            _rounded(c, x0, y0, x1, y1, r, fill="", outline=col, width=w)
        else:
            _rounded(c, x0, y0, x1, y1, r, fill=col, outline="")
    elif t == "arc":
        _, cx, cy, r, start, ext, col, w = sh
        c.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=ext, style="arc", outline=col,
                     width=w)


def _rgba(hexcol):
    hexcol = hexcol.lstrip("#")
    return tuple(int(hexcol[i:i + 2], 16) for i in (0, 2, 4)) + (255,)


def _pil_render(w, h, shapes):
    from PIL import Image, ImageDraw
    ss = 4 if w * h <= 60000 else 2
    img = Image.new("RGBA", (max(1, w) * ss, max(1, h) * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def S(v):
        return v * ss

    for sh in shapes:
        t = sh[0]
        if t == "line":
            pts = [(S(x), S(y)) for x, y in sh[1]]
            col, wd = _rgba(sh[2]), max(1, round(S(sh[3])))
            d.line(pts, fill=col, width=wd, joint="curve")
            for x, y in pts:
                d.ellipse((x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2), fill=col)
        elif t == "poly":
            pts = [(S(x), S(y)) for x, y in sh[1]]
            col = _rgba(sh[2])
            d.polygon(pts, fill=col)
            d.line(pts + [pts[0]], fill=col, width=round(S(1.4)), joint="curve")
        elif t == "circle":
            _, cx, cy, r, col, wd = sh
            bx = (S(cx - r), S(cy - r), S(cx + r), S(cy + r))
            if wd:
                d.ellipse(bx, outline=_rgba(col), width=max(1, round(S(wd))))
            else:
                d.ellipse(bx, fill=_rgba(col))
        elif t == "rrect":
            _, x0, y0, x1, y1, r, col, wd = sh
            if x1 - x0 < 0.5 or y1 - y0 < 0.5:
                continue
            bx = (S(x0), S(y0), S(x1), S(y1))
            r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
            if wd:
                d.rounded_rectangle(bx, radius=S(r), outline=_rgba(col), width=max(1, round(S(wd))))
            else:
                d.rounded_rectangle(bx, radius=S(r), fill=_rgba(col))
        elif t == "arc":
            # Tk считает углы против часовой стрелки, Pillow — по часовой
            _, cx, cy, r, start, ext, col, wd = sh
            d.arc((S(cx - r), S(cy - r), S(cx + r), S(cy + r)), start=-(start + ext), end=-start,
                  fill=_rgba(col), width=max(1, round(S(wd))))
    return img.resize((max(1, w), max(1, h)), Image.LANCZOS)


_IMG_CACHE = {}


def paint(c, w, h, shapes, tag="paint", x=0, y=0, clear=True):
    """Нарисовать фигуры в прямоугольнике w×h с левым верхним углом (x, y)."""
    tags = (tag,) if isinstance(tag, str) else tuple(tag)
    if clear:
        c.delete(tags[0])
    if not shapes:
        return
    if IS_MAC:
        before = set(c.find_all())
        for sh in shapes:
            _tk_shape(c, sh)
        for item in set(c.find_all()) - before:
            for t in tags:
                c.addtag_withtag(t, item)
            if x or y:
                c.move(item, x, y)
        return
    from PIL import ImageTk
    w, h = int(round(w)), int(round(h))
    key = (w, h, repr(shapes))
    img = _IMG_CACHE.get(key)
    if img is None:
        img = _IMG_CACHE[key] = ImageTk.PhotoImage(_pil_render(w, h, shapes), master=c)
    c.create_image(x, y, anchor="nw", image=img, tags=tags)


# --- базовые виджеты ------------------------------------------------------------------------
def bg_of(parent):
    try:
        return parent.cget("bg")
    except tk.TclError:
        return C["bg"]


def label(parent, text="", size=13, color=None, weight="normal", mono=False, bg=None, **kw):
    return tk.Label(parent, text=text, fg=color or C["text"], bg=bg or bg_of(parent),
                    font=font(size, weight, mono), bd=0, padx=0, pady=0, **kw)


class Tooltip:
    def __init__(self, widget, text_fn):
        self.w, self.text_fn = widget, text_fn
        self.win = self.job = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _):
        self.job = self.w.after(650, self._show)

    def _leave(self, _):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None
        if self.win:
            self.win.destroy()
            self.win = None

    def _show(self):
        self.job = None
        text = self.text_fn()
        if not text:
            return
        self.win = tw = tk.Toplevel(self.w)
        tw.wm_overrideredirect(True)
        tk.Label(tw, text=text, bg=C["elev"], fg=C["text"], padx=10, pady=6, font=font(12),
                 wraplength=320, justify="left").pack()
        tw.update_idletasks()
        x = self.w.winfo_rootx() + self.w.winfo_width() // 2 - tw.winfo_width() // 2
        tw.wm_geometry(f"+{max(0, x)}+{self.w.winfo_rooty() + self.w.winfo_height() + 6}")


class _Canvas(tk.Canvas):
    def __init__(self, parent, w, h, bg=None):
        super().__init__(parent, width=w, height=h, bg=bg or bg_of(parent), highlightthickness=0, bd=0)

    def size(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w = int(self.cget("width"))
        if h <= 1:
            h = int(self.cget("height"))
        return w, h


class IconButton(_Canvas):
    """Квадратная кнопка с иконкой. tone: "" — обычная, "raised" — с подложкой, "warn" — янтарная."""
    SIZE = 36
    GLYPH = 20

    def __init__(self, parent, icon, command, tip="", bg=None, size=None, glyph=None):
        self.s, self.g = size or self.SIZE, glyph or self.GLYPH
        super().__init__(parent, self.s, self.s, bg)
        self.icon, self.command, self.tip, self.tone = icon, command, tip, ""
        self.enabled = True
        self.hover = self.pressed = False
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        Tooltip(self, lambda: self.tip)
        self._draw()

    def set(self, icon=None, enabled=None, tip=None, tone=None):
        changed = False
        for k, v in (("icon", icon), ("enabled", enabled), ("tip", tip), ("tone", tone)):
            if v is not None and getattr(self, k) != v:
                setattr(self, k, v)
                changed = True
        if changed:
            self._draw()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self._draw()

    def _draw(self):
        s, g = self.s, self.g
        fill, color = None, C["text"]
        if self.enabled:
            raised = self.tone in ("raised", "warn")
            if self.pressed:
                fill = C["press"]
            elif self.hover:
                fill = C["press"] if raised else C["hover"]
            elif raised:
                fill = C["hover"]
            if self.tone == "warn":
                color = C["amber_text"]
        else:
            color = C["dim"]
        shapes = box(0, 0, s, s, 9, fill) + icon_shapes(self.icon, (s - g) / 2, (s - g) / 2, g, color)
        self.configure(cursor="")
        paint(self, s, s, shapes)

    def _release(self, e):
        self._set(pressed=False)
        if self.enabled and 0 <= e.x < self.s and 0 <= e.y < self.s:
            self.command()


class RecPill(_Canvas):
    """Плашка записи: «● Запись» → красная с таймером → янтарная «Пауза» → «Сохранение»."""
    H = 32

    def __init__(self, parent, command, bg=None):
        super().__init__(parent, 84, self.H, bg)
        self.command = command
        self.state, self.text, self.enabled, self.hover, self.blink, self.spin = "idle", "Запись", True, False, True, 0
        self.bind("<Enter>", lambda e: self._h(True))
        self.bind("<Leave>", lambda e: self._h(False))
        self.bind("<ButtonRelease-1>", lambda e: self.clickable() and self.command())
        Tooltip(self, lambda: {"idle": "Начать запись  (Ctrl+R)", "paused": "Продолжить запись  (Ctrl+P)"}
                .get(self.state, "") if self.clickable() else "")
        self._draw()

    def clickable(self):
        return self.enabled and self.state in ("idle", "paused")

    def _h(self, v):
        self.hover = v
        self._draw()

    def set(self, state, text, enabled=True, blink=True, spin=0):
        new = (state, text, enabled, blink, spin)
        if new != (self.state, self.text, self.enabled, self.blink, self.spin):
            self.state, self.text, self.enabled, self.blink, self.spin = new
            self._draw()

    def _font(self):
        if self.state in ("recording", "paused"):
            return font(14, "semibold", mono=True)
        return font(13, "semibold")

    def _draw(self):
        H, st = self.H, self.state
        tx = 34 if st == "saving" else 28
        W = max(84, int(tx + measure(self._font(), self.text) + 14))
        if int(self.cget("width")) != W:
            self.configure(width=W)
        cy = H / 2
        icon = []
        if st == "recording":
            bg, fg = C["rec"], "#FFFFFF"
            icon = [("circle", 15, cy, 5, "#FFFFFF" if self.blink else C["rec_dim"], None)]
        elif st == "paused":
            bg = C["amber_hover"] if (self.hover and self.enabled) else C["amber_bg"]
            fg = C["amber_text"]
            icon = [("rrect", 11.5, cy - 5, 14.5, cy + 5, 1, fg, None), ("rrect", 17.5, cy - 5, 20.5, cy + 5, 1, fg, None)]
        elif st == "saving":
            bg, fg = C["elev"], C["text2"]
            icon = [("circle", 19, cy, 6, "#4A4D58", 2), ("arc", 19, cy, 6, self.spin, 100, C["text"], 2)]
        elif self.enabled:
            bg, fg = (C["hover"] if self.hover else C["elev"]), C["text"]
            icon = [("circle", 15, cy, 5, C["red"], None)]
        else:
            bg, fg = "#1F2026", C["dim"]
            icon = [("circle", 15, cy, 5, C["off"], None)]
        paint(self, W, H, box(0, 0, W, H, H / 2, bg) + icon)
        self.delete("txt")
        self.create_text(tx, cy, text=self.text, anchor="w", fill=fg, tags=("txt",), font=self._font())
        self.configure(cursor="")


class Button(_Canvas):
    """Кнопка: primary — светлая, secondary — с рамкой, quiet — тёмная плашка."""
    STYLES = {
        "primary": dict(bg=C["light"], hover=C["light_hover"], press=C["light_press"], fg=C["on_light"], line=None),
        "secondary": dict(bg=None, hover=C["elev"], press=C["hover"], fg=C["text"], line=C["field_line"]),
        "quiet": dict(bg=C["elev"], hover=C["hover"], press=C["press"], fg=C["text"], line=None),
    }

    def __init__(self, parent, text, command, kind="primary", icon=None, height=40, radius=10, size=14,
                 width=None, bg=None, tip=None):
        self.text, self.command, self.kind, self.icon = text, command, kind, icon
        self.r, self.fs, self.fixed_w = radius, size, width
        self.enabled, self.hover, self.pressed = True, False, False
        self.tip = tip
        super().__init__(parent, width or self._natural(), height, bg)
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda e: self._draw())
        if tip:
            Tooltip(self, lambda: self.tip)
        self._draw()

    def _font(self):
        return font(self.fs, "semibold")

    def _natural(self):
        w = measure(self._font(), self.text) + (2 * 16 if self.fs >= 14 else 2 * 12)
        return int(w + (self.fs + 4 + 6 if self.icon else 0))

    def set(self, text=None, enabled=None, tip=None):
        changed = False
        for k, v in (("text", text), ("enabled", enabled), ("tip", tip)):
            if v is not None and getattr(self, k) != v:
                setattr(self, k, v)
                changed = True
        if changed:
            if not self.fixed_w:
                self.configure(width=self._natural())
            self._draw()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self._draw()

    def _draw(self):
        w, h = self.size()
        st = self.STYLES[self.kind]
        if self.enabled:
            fill = st["press"] if self.pressed else (st["hover"] if self.hover else st["bg"])
            line, fg = st["line"], st["fg"]
        else:
            fill = C["elev"] if self.kind != "secondary" else None
            line = C["line"] if self.kind == "secondary" else None
            fg = C["dim"]
        shapes = box(0, 0, w, h, self.r, fill, line)
        tw = measure(self._font(), self.text)
        isz = self.fs + 4
        total = tw + (isz + 6 if self.icon else 0)
        x = (w - total) / 2
        if self.icon:
            shapes += icon_shapes(self.icon, x, (h - isz) / 2, isz, fg)
            x += isz + 6
        paint(self, w, h, shapes)
        self.delete("txt")
        self.create_text(x, h / 2, text=self.text, anchor="w", fill=fg, font=self._font(), tags=("txt",))
        self.configure(cursor="")

    def _release(self, e):
        self._set(pressed=False)
        w, h = self.size()
        if self.enabled and 0 <= e.x < w and 0 <= e.y < h:
            self.command()


class Segmented(_Canvas):
    """Переключатель-сегменты (вкладки режимов, размер окна)."""

    def __init__(self, parent, options, variable, command=None, height=40, radius=10, size=13, bg=None):
        super().__init__(parent, 240, height, bg)
        self.options, self.var, self.command = options, variable, command
        self.r, self.fs = radius, size
        self.disabled, self.tips = set(), {}
        self.enabled, self.hover = True, None
        variable.trace_add("write", lambda *a: self._draw())
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._hover(None))
        self.bind("<ButtonRelease-1>", self._click)
        Tooltip(self, lambda: self.tips.get(self.hover, ""))
        self._draw()

    def disable(self, value, tip=""):
        self.disabled.add(value)
        self.tips[value] = tip
        self._draw()

    def set_enabled(self, on):
        if on != self.enabled:
            self.enabled = on
            self._draw()

    def set_disabled(self, tips):
        """Недоступные сейчас вкладки: {значение: подсказка}."""
        if tips != self.tips or set(tips) != self.disabled:
            self.tips, self.disabled = dict(tips), set(tips)
            self._draw()

    def _segments(self):
        w, h = self.size()
        n, pad, gap = len(self.options), 3, 2
        sw = (w - 2 * pad - gap * (n - 1)) / n
        return [(pad + i * (sw + gap), pad + i * (sw + gap) + sw) for i in range(n)], pad

    def _at(self, x):
        segs, _ = self._segments()
        for (x0, x1), (val, _) in zip(segs, self.options):
            if x0 <= x <= x1:
                return val
        return None

    def _hover(self, v):
        if v != self.hover:
            self.hover = v
            self._draw()

    def _motion(self, e):
        self._hover(self._at(e.x))

    def _click(self, e):
        v = self._at(e.x)
        if v is None or not self.enabled or v in self.disabled or v == self.var.get():
            return
        self.var.set(v)
        if self.command:
            self.command()

    def _draw(self):
        w, h = self.size()
        segs, pad = self._segments()
        cur = self.var.get()
        shapes = box(0, 0, w, h, self.r, C["field"], C["line"])
        for (x0, x1), (val, _) in zip(segs, self.options):
            if val == cur:
                shapes += box(x0, pad, x1, h - pad, self.r - 2, C["hover"] if self.enabled else C["elev"])
        paint(self, w, h, shapes)
        self.delete("txt")
        for (x0, x1), (val, text) in zip(segs, self.options):
            sel = val == cur
            if val in self.disabled:
                fg = C["dim"]
            elif not self.enabled:
                fg = C["text2"] if sel else C["dim"]
            else:
                fg = C["text"] if (sel or self.hover == val) else C["text2"]
            f = font(self.fs, "semibold" if sel else "normal")
            self.create_text((x0 + x1) / 2, h / 2, text=ellipsize(f, text, x1 - x0 - 8), fill=fg, font=f,
                             tags=("txt",))
        self.configure(cursor="")


class Toggle(_Canvas):
    W, H = 40, 24

    def __init__(self, parent, variable, command=None, bg=None):
        super().__init__(parent, self.W, self.H, bg)
        self.var, self.command, self.enabled = variable, command, True
        variable.trace_add("write", lambda *a: self._draw())
        self.bind("<ButtonRelease-1>", lambda e: self.toggle())
        self._draw()

    def toggle(self):
        if not self.enabled:
            return
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def set_enabled(self, on):
        if on != self.enabled:
            self.enabled = on
            self._draw()

    def _draw(self):
        W, H = self.W, self.H
        on = bool(self.var.get())
        if not self.enabled:
            track, knob = C["elev"], C["dim"]
        elif on:
            track, knob = C["light"], C["on_light"]
        else:
            track, knob = C["field_line"], C["muted"]
        kx = W - 3 - 9 if on else 3 + 9
        paint(self, W, H, box(0, 0, W, H, H / 2, track) + [("circle", kx, H / 2, 9, knob, None)])
        self.configure(cursor="")


class Field(_Canvas):
    """Поле ввода в рамке со скруглением."""

    def __init__(self, parent, variable, width=120, height=36, justify="left", size=14, bg=None):
        super().__init__(parent, width, height, bg)
        self.focus = False
        self.enabled = True
        self.entry = tk.Entry(self, textvariable=variable, bg=C["field"], fg=C["text"], insertbackground=C["text"],
                              disabledbackground=C["field"], disabledforeground=C["dim"], relief="flat", bd=0,
                              highlightthickness=0, font=font(size), justify=justify,
                              selectbackground="#3A3D48", selectforeground=C["text"], insertwidth=1)
        self.win = self.create_window(10, height / 2, anchor="w", window=self.entry, width=width - 20)
        self.entry.bind("<FocusIn>", lambda e: self._f(True))
        self.entry.bind("<FocusOut>", lambda e: self._f(False))
        self.bind("<Button-1>", lambda e: self.enabled and self.entry.focus_set())
        self.bind("<Configure>", self._conf)
        self._draw()

    def _f(self, v):
        self.focus = v
        self._draw()

    def _conf(self, e):
        self.itemconfigure(self.win, width=max(10, e.width - 20))
        self.coords(self.win, 10, e.height / 2)
        self._draw()

    def set_enabled(self, on):
        if on != self.enabled:
            self.enabled = on
            self.entry.configure(state="normal" if on else "disabled")
            self._draw()

    def _draw(self):
        w, h = self.size()
        line = C["field_focus"] if (self.focus and self.enabled) else (C["field_line"] if self.enabled else C["line"])
        paint(self, w, h, box(0, 0, w, h, 6, C["field"], line))
        self.tag_lower("paint")


class Card(tk.Frame):
    """Скруглённая карточка. Содержимое кладётся в .body (обычный Frame с фоном карточки);
    скругление рисуется на холсте позади него, размер карточки — по содержимому."""

    def __init__(self, parent, fill=None, outline=None, radius=12, pad=16):
        super().__init__(parent, bg=bg_of(parent))
        self.fill, self.outline = fill or C["panel"], outline if outline is not None else C["line"]
        self.r = radius
        self.bgc = tk.Canvas(self, bg=bg_of(parent), highlightthickness=0, bd=0, width=1, height=1)
        self.bgc.place(x=0, y=0, relwidth=1, relheight=1)
        self.bgc.tk.call("lower", self.bgc._w)
        self.body = tk.Frame(self, bg=self.fill)
        self.body.pack(fill="both", expand=True, padx=pad, pady=pad)
        self.bind("<Configure>", lambda e: self._draw(e.width, e.height))

    def _draw(self, w, h):
        if w > 1 and h > 1:
            paint(self.bgc, w, h, box(0, 0, w, h, self.r, self.fill, self.outline or None))


class DeviceRow(_Canvas):
    """Строка устройства с галочкой (или «радио»), названием, подписью и статусом."""
    H = 56

    def __init__(self, parent, title, subtitle, status, command, checked=False, enabled=True, radio=False,
                 bg=None):
        super().__init__(parent, 300, self.H, bg)
        self.title, self.subtitle, self.status = title, subtitle, status  # status: ("ok"|"wait", текст)
        self.command, self.checked, self.enabled, self.radio = command, checked, enabled, radio
        self.hover = False
        self.bind("<Enter>", lambda e: self._h(True))
        self.bind("<Leave>", lambda e: self._h(False))
        self.bind("<ButtonRelease-1>", lambda e: self.enabled and self.command(self))
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _h(self, v):
        self.hover = v
        self._draw()

    def set(self, checked=None, enabled=None):
        changed = False
        for k, v in (("checked", checked), ("enabled", enabled)):
            if v is not None and getattr(self, k) != v:
                setattr(self, k, v)
                changed = True
        if changed:
            self._draw()

    def _draw(self):
        w, h = self.size()
        cy = h / 2
        fill = C["elev"] if self.checked else (C["row_hover"] if self.hover and self.enabled else None)
        shapes = box(0, 0, w, h, 8, fill)
        x, s = 12, 18
        y = cy - s / 2
        if self.radio:
            if self.checked:
                shapes += [("circle", x + 9, cy, 9, C["light"], None), ("circle", x + 9, cy, 3.6, C["on_light"], None)]
            else:
                shapes += [("circle", x + 9, cy, 8.25, C["dim"] if self.enabled else C["off"], 1.5)]
        elif self.checked:
            shapes += box(x, y, x + s, y + s, 4, C["light"] if self.enabled else C["dim"])
            shapes += icon_shapes("check", x + 1.5, y + 1.5, s - 3, C["on_light"])
        else:
            shapes += box(x, y, x + s, y + s, 4, None, C["dim"] if self.enabled else C["off"], 1.5)
        chip_w = 0
        if self.status:
            kind, text = self.status
            cf = font(12)
            chip_w = measure(cf, text) + 14
            dot = {"ok": C["ok"], "wait": C["amber"], "live": C["text"]}.get(kind, C["dim"])
            shapes.append(("circle", w - 12 - chip_w + 4, cy, 4, dot, None))
        paint(self, w, h, shapes)
        self.delete("txt")
        tx = x + s + 12
        maxw = w - tx - 12 - (chip_w + 12 if chip_w else 0)
        tf, sf = font(14, "semibold"), font(12)
        self.create_text(tx, cy - 9, text=ellipsize(tf, self.title, maxw), anchor="w", font=tf, tags=("txt",),
                         fill=C["text"] if self.enabled else C["text2"])
        self.create_text(tx, cy + 10, text=ellipsize(sf, self.subtitle, maxw), anchor="w", font=sf,
                         fill=C["muted"], tags=("txt",))
        if self.status:
            kind, text = self.status
            col = {"ok": C["ok_text"], "wait": C["amber_text"], "live": C["text2"]}.get(kind, C["muted"])
            self.create_text(w - 12, cy, text=text, anchor="e", font=font(12), fill=col, tags=("txt",))
        self.configure(cursor="")


class LogoTile(_Canvas):
    def __init__(self, parent, size=32, bg=None):
        super().__init__(parent, size, size, bg)
        paint(self, size, size, box(0, 0, size, size, 9, C["panel"], C["line"])
              + [("circle", size / 2, size / 2, 5.5, C["red"], None)])


def divider(parent, color=None):
    return tk.Frame(parent, bg=color or C["line"], height=1)

"""Takeone — вкладка «Монтаж»: обрезать начало и конец видео, вырезать лишние куски, сохранить.

Просмотр кадров — через PyAV в отдельном потоке (окно не подвисает), сохранение — через ffmpeg
(android_view.export_cut): куски режутся точно по кадрам и склеиваются, лимит размера учитывается."""
import os
import shutil
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import ui_kit as ui
from ui_kit import C

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".m4v")
MIN_PART = 0.2  # самый короткий кусок, который имеет смысл оставлять или вырезать, с


def fmt_t(t):
    t = max(0.0, t)
    m, s = divmod(t, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{int(h)}:{int(m):02d}:{s:04.1f}"
    return f"{int(m):02d}:{s:04.1f}"


def merge(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1e-3:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def keep_segments(tin, tout, cuts):
    """Что останется: [tin, tout] минус вырезанные куски."""
    segs, t = [], tin
    for a, b in merge(cuts):
        a, b = max(a, tin), min(b, tout)
        if b <= a:
            continue
        if a - t > 0.02:
            segs.append((t, a))
        t = max(t, b)
    if tout - t > 0.02:
        segs.append((t, tout))
    return segs


# --- чтение видео ------------------------------------------------------------------------
class Reader:
    def __init__(self, path):
        import av
        self.c = av.open(path)
        if not self.c.streams.video:
            self.c.close()
            raise RuntimeError("В файле нет видео.")
        self.vs = self.c.streams.video[0]
        self.vs.thread_type = "AUTO"
        self.tb = float(self.vs.time_base) if self.vs.time_base else 1 / 90000
        self.t0 = float(self.vs.start_time * self.tb) if self.vs.start_time is not None else 0.0
        if self.c.duration:
            self.duration = self.c.duration / 1_000_000 - max(0.0, self.t0 - (self.c.start_time or 0) / 1e6)
        elif self.vs.duration:
            self.duration = float(self.vs.duration * self.tb)
        else:
            self.duration = self._scan_duration()
        self.fps = float(self.vs.average_rate) if self.vs.average_rate else 30.0
        self.it = None

    def _scan_duration(self):
        last = 0.0
        for p in self.c.demux(self.vs):
            if p.pts is not None:
                last = max(last, p.pts * self.tb - self.t0)
        self.c.seek(0)
        return last

    def close(self):
        try:
            self.c.close()
        except Exception:  # noqa
            pass

    def _seek(self, t):
        self.c.seek(max(0, int((t + self.t0) / self.tb)), stream=self.vs, backward=True)
        self.it = self.c.decode(self.vs)

    def frame_at(self, t, exact=True):
        """Кадр в момент t. exact=False — ближайший опорный кадр (быстро, для перетаскивания)."""
        self._seek(t)
        prev = None
        for f in self.it:
            if f.time is None:
                continue
            ft = f.time - self.t0
            if not exact or ft >= t - 0.5 / self.fps:
                return ft, f
            prev = (ft, f)
        return prev

    def next(self):
        if self.it is None:
            return None
        for f in self.it:
            if f.time is not None:
                return f.time - self.t0, f
        return None


class Preview:
    """Поток, который достаёт кадры: по запросу (перемотка) или подряд (просмотр без звука)."""

    def __init__(self, path, size):
        self.reader = Reader(path)
        self.duration = self.reader.duration
        self.size = size
        self.cv = threading.Condition()
        self.req = self.play = None
        self.out = None
        self.seq = 0
        self.pos = 0.0
        self.alive = True
        self.busy = False
        threading.Thread(target=self._loop, daemon=True).start()

    def request(self, t, exact=True):
        with self.cv:
            self.req, self.play = (t, exact), None
            self.cv.notify()

    def start_play(self, t, cuts, end):
        with self.cv:
            self.req = None
            self.play = {"t": t, "cuts": list(cuts), "end": end, "new": True}
            self.cv.notify()

    def stop_play(self):
        with self.cv:
            self.play = None

    @property
    def playing(self):
        return self.play is not None

    def close(self):
        with self.cv:
            self.alive = False
            self.cv.notify()

    def latest(self):
        with self.cv:
            return self.out

    def _emit(self, t, f):
        img = f.to_image()
        img.thumbnail(self.size)
        with self.cv:
            self.seq += 1
            self.out = (self.seq, t, img)
            self.pos = t

    def _loop(self):
        r = self.reader
        while True:
            with self.cv:
                while self.alive and self.req is None and self.play is None:
                    self.cv.wait()
                if not self.alive:
                    break
                req, play = self.req, self.play
                self.req = None
            try:
                if req:
                    self.busy = True
                    res = r.frame_at(*req)
                    self.busy = False
                    if res:
                        self._emit(*res)
                    continue
                if play["new"]:
                    play["new"] = False
                    res = r.frame_at(play["t"], True)
                    if not res:
                        self._end_play(play)
                        continue
                    play["wall0"], play["t0"] = time.monotonic(), res[0]
                    self._emit(*res)
                    continue
                res = r.next()
                if res is None or res[0] >= play["end"]:
                    self._end_play(play)
                    continue
                ft, f = res
                cut = next((c for c in play["cuts"] if c[0] <= ft < c[1]), None)
                if cut:  # при просмотре вырезанные куски пропускаются — видно, как будет в итоге
                    if cut[1] >= play["end"]:
                        self._end_play(play)
                        continue
                    res = r.frame_at(cut[1], True)
                    if not res:
                        self._end_play(play)
                        continue
                    play["wall0"], play["t0"] = time.monotonic(), res[0]
                    self._emit(*res)
                    continue
                due = play["wall0"] + (ft - play["t0"])
                now = time.monotonic()
                if due > now:
                    with self.cv:
                        self.cv.wait(due - now)
                        if self.play is not play:
                            continue
                elif now - due > 0.12:  # не успеваем — кадр пропускаем
                    with self.cv:
                        self.pos = ft
                    continue
                self._emit(ft, f)
            except Exception:  # noqa — битый кусок файла: просто остановиться
                self.busy = False
                self._end_play(play)
        r.close()

    def _end_play(self, play):
        with self.cv:
            if play is not None and self.play is play:
                self.play = None


def make_strip(path, strip_w, tile_h, done):
    """Миниатюры для шкалы (в фоне), столько, чтобы заполнить её целиком: done(список картинок PIL)."""
    def work():
        imgs = []
        try:
            r = Reader(path)
            cc = r.vs.codec_context
            tile_w = tile_h * (cc.width / cc.height) if cc.width and cc.height else tile_h * 0.5
            n = max(4, min(30, int(strip_w / max(8.0, tile_w)) + 1))
            for i in range(n):
                res = r.frame_at((i + 0.5) * r.duration / n, exact=False)
                if not res:
                    break
                img = res[1].to_image()
                k = tile_h / img.height
                imgs.append(img.resize((max(1, int(img.width * k)), tile_h)))
            r.close()
        except Exception:  # noqa
            pass
        done(imgs)
    threading.Thread(target=work, daemon=True).start()


# --- шкала времени ---------------------------------------------------------------------------
class Timeline(tk.Canvas):
    H, PAD, TY0, TY1 = 66, 10, 9, 57

    def __init__(self, parent, ed):
        super().__init__(parent, height=self.H, width=400, bg=ui.bg_of(parent), highlightthickness=0, bd=0)
        self.ed = ed
        self.tiles = []
        self._imgs = []
        self.drag = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)

    def x_of(self, t):
        w = self.winfo_width()
        d = self.ed.dur or 1
        return self.PAD + (w - 2 * self.PAD) * min(max(t, 0), d) / d

    def t_of(self, x):
        w = self.winfo_width()
        return min(max((x - self.PAD) / max(1, w - 2 * self.PAD), 0.0), 1.0) * (self.ed.dur or 0)

    def _veil(self, x0, x1, rgba):
        from PIL import Image, ImageTk
        x0, x1 = int(round(x0)), int(round(x1))
        if x1 - x0 < 1:
            return
        img = ImageTk.PhotoImage(Image.new("RGBA", (x1 - x0, self.TY1 - self.TY0), rgba), master=self)
        self._imgs.append(img)
        self.create_image(x0, self.TY0, anchor="nw", image=img)

    def redraw(self):
        from PIL import Image, ImageDraw, ImageTk
        self.delete("all")
        self._imgs = []
        ed = self.ed
        w = self.winfo_width()
        if w <= 1:
            return
        x0, x1 = self.PAD, w - self.PAD
        th = self.TY1 - self.TY0
        if self.tiles and ed.dur:
            strip = Image.new("RGBA", (int(x1 - x0), th), (36, 38, 45, 255))
            x = 0
            n = len(self.tiles)
            step = (x1 - x0) / n
            for i, t in enumerate(self.tiles):  # каждая миниатюра — по центру своего отрезка
                cx = int(i * step + step / 2 - t.width / 2)
                strip.paste(t, (cx, 0))
                x += step
            mask = Image.new("L", strip.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, strip.width - 1, th - 1), radius=8, fill=255)
            strip.putalpha(mask)
            img = ImageTk.PhotoImage(strip, master=self)
            self._imgs.append(img)
            self.create_image(x0, self.TY0, anchor="nw", image=img)
        else:
            ui.paint(self, x1 - x0, th, ui.box(0, 0, x1 - x0, th, 8, C["elev"]), tag="strip", x=x0, y=self.TY0,
                     clear=False)
        if not ed.dur:
            return
        # что не попадёт в видео
        self._veil(x0, self.x_of(ed.tin), (10, 11, 14, 200))
        self._veil(self.x_of(ed.tout), x1, (10, 11, 14, 200))
        for i, (a, b) in enumerate(ed.cuts):
            xa, xb = self.x_of(a), self.x_of(b)
            self._veil(xa, xb, (200, 54, 59, 150))
            self.create_line(xa, self.TY0, xa, self.TY1, fill=C["red"], width=2)
            self.create_line(xb, self.TY0, xb, self.TY1, fill=C["red"], width=2)
            cx, cy = (xa + xb) / 2, (self.TY0 + self.TY1) / 2
            tag = ("cutx", f"cut{i}")
            ui.paint(self, 20, 20, [("circle", 10, 10, 10, C["rec"], None)] + ui.icon_shapes("close", 4, 4, 12, "#FFFFFF"),
                     tag=tag, x=cx - 10, y=cy - 10, clear=False)
        if ed.sel_a is not None:
            a = ed.sel_a
            b = ed.sel_b if ed.sel_b is not None else a
            xa, xb = sorted((self.x_of(a), self.x_of(b)))
            self._veil(xa, xb, (245, 181, 71, 70))
            for xx in {xa, xb}:
                self.create_line(xx, self.TY0 - 3, xx, self.TY1 + 3, fill=C["amber_text"], width=2)
        # ручки начала и конца
        for t in (ed.tin, ed.tout):
            x = self.x_of(t)
            ui.paint(self, 10, th + 8, ui.box(0, 0, 10, th + 8, 3, C["light"])
                     + [("line", [(3.5, th / 2 - 2), (3.5, th / 2 + 10)], C["on_light"], 1.2),
                        ("line", [(6.5, th / 2 - 2), (6.5, th / 2 + 10)], C["on_light"], 1.2)],
                     tag="handle", x=x - 5, y=self.TY0 - 4, clear=False)
        # бегунок
        x = self.x_of(ed.pos)
        self.create_line(x, 2, x, self.H - 2, fill="#FFFFFF", width=2)
        ui.paint(self, 12, 12, [("circle", 6, 6, 6, "#FFFFFF", None)], tag="knob", x=x - 6, y=0, clear=False)

    def _grab(self, x):
        ed = self.ed
        cands = [("in", ed.tin), ("out", ed.tout)]
        if ed.sel_a is not None:
            cands.append(("sa", ed.sel_a))
        if ed.sel_b is not None:
            cands.append(("sb", ed.sel_b))
        best = min(cands, key=lambda c: abs(self.x_of(c[1]) - x))
        return best[0] if abs(self.x_of(best[1]) - x) <= 8 else None

    def _hover(self, e):
        if not self.ed.dur:
            return
        tags = self.gettags("current")
        self.configure(cursor="" if "cutx" in tags else
                       ("sb_h_double_arrow" if self._grab(e.x) else ""))

    def _press(self, e):
        ed = self.ed
        if not ed.dur or ed.saving:
            return
        for t in self.gettags("current"):
            if t.startswith("cut") and t != "cutx":
                ed.restore_cut(int(t[3:]))
                return
        ed.stop_play()
        self.drag = self._grab(e.x) or "scrub"
        self._motion(e)

    def _motion(self, e):
        ed = self.ed
        if not self.drag or not ed.dur:
            return
        t = self.t_of(e.x)
        d = self.drag
        if d == "in":
            ed.tin = min(t, ed.tout - MIN_PART)
            t = ed.tin
        elif d == "out":
            ed.tout = max(t, ed.tin + MIN_PART)
            t = ed.tout
        elif d == "sa":
            ed.sel_a = t
        elif d == "sb":
            ed.sel_b = t
        ed.seek(t, exact=False)
        ed.changed()

    def _release(self, e):
        if self.drag:
            self.drag = None
            self.ed.seek(self.ed.pos, exact=True)


# --- вкладка ---------------------------------------------------------------------------------
class EditorPanel(tk.Frame):
    PREV_H = 320

    def __init__(self, parent, app, width):
        super().__init__(parent, bg=C["bg"])
        self.app, self.W = app, width
        self.path = None
        self.prev = None
        self.dur = 0.0
        self.tin = self.tout = self.pos = 0.0
        self.cuts = []
        self.sel_a = self.sel_b = None
        self.saving = False
        self.shown_seq = 0
        self._photo = None
        self._last_dest = None
        self._strip_imgs = None
        self._build()
        self._tick()

    # --- интерфейс
    def _build(self):
        bg = C["bg"]
        head = tk.Frame(self, bg=bg)
        head.pack(fill="x")
        self.title = ui.label(head, "Монтаж", 15, weight="semibold")
        self.title.pack(side="left")
        self.b_open = ui.Button(head, "Открыть…", self.open_dialog, kind="secondary", icon="folder", height=32,
                                radius=8, size=13)
        self.b_open.pack(side="right")
        self.b_last = ui.Button(head, "Последняя запись", self.open_last, kind="secondary", icon="film", height=32,
                                radius=8, size=13)
        self.b_last.pack(side="right", padx=(0, 8))

        card = ui.Card(self, pad=6, fill=C["panel"])
        card.pack(fill="x", pady=(10, 0))
        self.pc = tk.Canvas(card.body, bg=C["video"], height=self.PREV_H, width=self.W - 12, highlightthickness=0, bd=0)
        self.pc.pack(fill="x")
        self.pc.bind("<Configure>", lambda e: self._draw_preview())
        self.pc.bind("<ButtonRelease-1>", lambda e: self.toggle_play() if self.dur else self.open_dialog())

        row = tk.Frame(self, bg=bg)
        row.pack(fill="x", pady=(10, 0))
        self.b_play = ui.IconButton(row, "play", self.toggle_play, "Смотреть без звука  (Пробел)", bg=bg)
        self.b_play.pack(side="left")
        self.time_lbl = ui.label(row, "00:00.0 / 00:00.0", 13, C["text2"], mono=True)
        self.time_lbl.pack(side="left", padx=(8, 0))
        self.b_out = ui.Button(row, "Закончить здесь", lambda: self.set_trim("out"), kind="quiet", icon="mark_out",
                               height=32, radius=8, size=13,
                               tip="Всё, что после бегунка, не попадёт в видео")
        self.b_out.pack(side="right")
        self.b_in = ui.Button(row, "Начать отсюда", lambda: self.set_trim("in"), kind="quiet", icon="mark_in",
                              height=32, radius=8, size=13, tip="Всё, что до бегунка, не попадёт в видео")
        self.b_in.pack(side="right", padx=(0, 8))

        self.tl = Timeline(self, self)
        self.tl.pack(fill="x", pady=(8, 0))

        row = tk.Frame(self, bg=bg)
        row.pack(fill="x", pady=(8, 0))
        self.b_sa = ui.Button(row, "Начало куска", lambda: self.mark("a"), kind="secondary", height=32, radius=8,
                              size=13, tip="Поставьте бегунок в начало лишнего куска")
        self.b_sa.pack(side="left")
        self.b_sb = ui.Button(row, "Конец куска", lambda: self.mark("b"), kind="secondary", height=32, radius=8,
                              size=13, tip="…и в его конец")
        self.b_sb.pack(side="left", padx=(8, 0))
        self.b_cut = ui.Button(row, "Вырезать", self.cut, kind="quiet", icon="scissors", height=32, radius=8,
                               size=13, tip="Убрать выделенный кусок  (Delete)")
        self.b_cut.pack(side="left", padx=(8, 0))
        self.b_reset = ui.IconButton(row, "undo", self.reset, "Сбросить обрезку и вырезанные куски", bg=bg)
        self.b_reset.pack(side="right")

        self.help = ui.label(self, "", 12, C["muted"], wraplength=self.W, justify="left")
        self.help.pack(anchor="w", pady=(8, 0))

        card = ui.Card(self, pad=14)
        card.pack(fill="x", pady=(14, 0))
        b = card.body
        top = tk.Frame(b, bg=C["panel"])
        top.pack(fill="x")
        ui.label(top, "Итог", 14, weight="semibold").pack(side="left")
        self.total_lbl = ui.label(top, "—", 14, weight="semibold", mono=True)
        self.total_lbl.pack(side="right")
        self.sum_lbl = ui.label(b, "", 12, C["muted"], justify="left", wraplength=self.W - 40)
        self.sum_lbl.pack(anchor="w", pady=(2, 0))
        ui.divider(b).pack(fill="x", pady=(12, 0))
        lr = tk.Frame(b, bg=C["panel"])
        lr.pack(fill="x", pady=(12, 0))
        right = tk.Frame(lr, bg=C["panel"])
        right.pack(side="right")
        self.f_limit = ui.Field(right, self.app.limit_mb, width=64, justify="right")
        self.f_limit.pack(side="left")
        ui.label(right, "МБ", 13, C["text2"]).pack(side="left", padx=(8, 0))
        lab = tk.Frame(lr, bg=C["panel"])
        lab.pack(side="left")
        ui.label(lab, "Видео не больше", 14, weight="semibold").pack(anchor="w")
        ui.label(lab, "Больше — сожмётся при сохранении · 0 без лимита", 12, C["muted"]).pack(anchor="w", pady=(2, 0))

        self.b_save = ui.Button(self, "Сохранить видео…", self.save, kind="primary", height=44, radius=10, size=15)
        self.b_save.pack(fill="x", pady=(16, 0))
        self.status = ui.label(self, "", 12, C["muted"], wraplength=self.W, justify="center")
        self.status.pack(fill="x", pady=(10, 0))
        self.status.bind("<ButtonRelease-1>", lambda e: self._open_folder())
        self.changed()

    # --- файлы
    def _video_dir(self):
        return self.app.video_dir()

    def open_dialog(self):
        if self.saving:
            return
        path = filedialog.askopenfilename(parent=self, title="Открыть видео", initialdir=self._video_dir(),
                                          filetypes=[("Видео", " ".join("*" + e for e in VIDEO_EXT)),
                                                     ("Все файлы", "*")])
        if path:
            self.load(path)

    def last_recording(self):
        import android_view
        p = android_view.last_video()
        if p:
            return p
        d = self._video_dir()
        try:
            files = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(VIDEO_EXT)]
        except OSError:
            return None
        return max(files, key=os.path.getmtime) if files else None

    def open_last(self):
        if self.saving:
            return
        p = self.last_recording()
        if not p:
            messagebox.showinfo(self.app.app_name, f"В папке {self._video_dir()} пока нет видео.", parent=self)
            return
        self.load(p)

    def load(self, path):
        self.stop_play()
        try:
            prev = Preview(path, (self.W - 12, self.PREV_H))
        except Exception as e:  # noqa
            messagebox.showerror(self.app.app_name, f"Не получилось открыть видео:\n{e}", parent=self)
            return
        if prev.duration <= 0:
            prev.close()
            messagebox.showerror(self.app.app_name, "Не удалось узнать длительность видео.", parent=self)
            return
        if self.prev:
            self.prev.close()
        self.prev, self.path = prev, path
        self.dur = prev.duration
        self.tin, self.tout, self.pos = 0.0, self.dur, 0.0
        self.cuts, self.sel_a, self.sel_b = [], None, None
        self._photo = None
        self.shown_seq = 0
        self.tl.tiles = []
        self.status.configure(text="", cursor="")
        self._last_dest = None
        prev.request(0.0)
        make_strip(path, self.W - 2 * Timeline.PAD, Timeline.TY1 - Timeline.TY0, lambda imgs: setattr(self, "_strip_imgs", (path, imgs)))
        self.changed()

    # --- действия
    def seek(self, t, exact=True):
        if not self.prev:
            return
        self.pos = min(max(t, 0.0), self.dur)
        self.prev.request(self.pos, exact)

    def toggle_play(self):
        if not self.prev or self.saving:
            return
        if self.prev.playing:
            self.stop_play()
            return
        start = self.pos
        if start < self.tin or start >= self.tout - 0.05:
            start = self.tin
        self.prev.start_play(start, merge(self.cuts), self.tout)
        self.changed()

    def stop_play(self):
        if self.prev and self.prev.playing:
            self.prev.stop_play()
            self.seek(self.prev.pos, exact=True)
        self.changed()

    def step(self, dt):
        if self.prev:
            self.stop_play()
            self.seek(self.pos + dt)
            self.changed()

    def set_trim(self, which):
        if not self.prev:
            return
        self.stop_play()
        if which == "in":
            self.tin = min(self.pos, self.tout - MIN_PART)
        else:
            self.tout = max(self.pos, self.tin + MIN_PART)
        self.changed()

    def mark(self, which):
        if not self.prev:
            return
        self.stop_play()
        if which == "a":
            self.sel_a = self.pos
            if self.sel_b is not None and self.sel_b <= self.sel_a:
                self.sel_b = None
        else:
            if self.sel_a is None:
                self.sel_a = max(self.tin, self.pos - 1.0)
            self.sel_b = self.pos
        self.changed()

    def _sel(self):
        if self.sel_a is None or self.sel_b is None:
            return None
        a, b = sorted((self.sel_a, self.sel_b))
        a, b = max(a, self.tin), min(b, self.tout)
        return (a, b) if b - a >= 0.05 else None

    def cut(self):
        s = self._sel()
        if not s:
            return
        self.stop_play()
        self.cuts = merge(self.cuts + [s])
        self.sel_a = self.sel_b = None
        self.seek(s[1] if s[1] < self.tout else s[0])
        self.changed()

    def restore_cut(self, i):
        if 0 <= i < len(self.cuts):
            del self.cuts[i]
            self.changed()

    def reset(self):
        if not self.prev:
            return
        self.stop_play()
        self.tin, self.tout, self.cuts, self.sel_a, self.sel_b = 0.0, self.dur, [], None, None
        self.changed()

    def segments(self):
        return keep_segments(self.tin, self.tout, self.cuts)

    # --- состояние кнопок и подписей
    def changed(self):
        loaded = self.prev is not None
        idle = not self.saving
        playing = loaded and self.prev.playing
        self.b_play.set("pause" if playing else "play", loaded and idle,
                        "Пауза  (Пробел)" if playing else "Смотреть без звука  (Пробел)")
        for b in (self.b_in, self.b_out, self.b_sa, self.b_sb):
            b.set(enabled=loaded and idle)
        self.b_cut.set(enabled=loaded and idle and self._sel() is not None)
        self.b_reset.set(enabled=loaded and idle and (bool(self.cuts) or self.tin > 0 or self.tout < self.dur
                                                     or self.sel_a is not None))
        self.b_open.set(enabled=idle)
        self.b_last.set(enabled=idle)
        self.f_limit.set_enabled(idle)
        segs = self.segments() if loaded else []
        total = sum(b - a for a, b in segs)
        if not self.saving:
            self.b_save.set(text="Сохранить видео…", enabled=loaded and total > 0.05)
        self.time_lbl.configure(text=f"{fmt_t(self.pos)} / {fmt_t(self.dur)}")
        if loaded:
            self.title.configure(text=ui.ellipsize(ui.font(15, "semibold"), os.path.basename(self.path), self.W - 300))
            self.total_lbl.configure(text=fmt_t(total))
            parts = [f"из {fmt_t(self.dur)}"]
            if self.tin > 0.05:
                parts.append(f"начало обрезано на {fmt_t(self.tin)}")
            if self.tout < self.dur - 0.05:
                parts.append(f"конец — на {fmt_t(self.dur - self.tout)}")
            n = len([c for c in merge(self.cuts) if min(c[1], self.tout) > max(c[0], self.tin)])
            if n:
                parts.append(f"вырезано {n} {ui.plural(n, 'кусок', 'куска', 'кусков')}")
            self.sum_lbl.configure(text=" · ".join(parts))
        else:
            self.title.configure(text="Монтаж")
            self.total_lbl.configure(text="—")
            self.sum_lbl.configure(text="Откройте видео, чтобы обрезать его или вырезать лишнее.")
        if not loaded:
            hint = ""
        elif self.sel_a is not None and self.sel_b is None:
            hint = "Начало куска отмечено. Поставьте бегунок в его конец и нажмите «Конец куска»."
        elif self._sel():
            hint = "Кусок выделен (жёлтый) — нажмите «Вырезать». Края можно подвинуть мышью."
        elif self.cuts:
            hint = "Красное не попадёт в видео — нажмите ✕ на куске, чтобы вернуть его."
        else:
            hint = ("Тяните белые ручки на шкале, чтобы обрезать начало и конец. Лишний кусок: бегунок в его "
                    "начало → «Начало куска», в конец → «Конец куска» → «Вырезать».")
        self.help.configure(text=hint)
        self.tl.redraw()

    # --- просмотр
    def _draw_preview(self):
        c = self.pc
        c.delete("all")
        w, h = max(c.winfo_width(), 10), max(c.winfo_height(), 10)
        if self._photo is not None:
            c.create_image(w / 2, h / 2, image=self._photo)
        elif not self.prev:
            ui.paint(c, 56, 56, ui.box(0, 0, 56, 56, 16, C["panel"], C["line"])
                     + ui.icon_shapes("film", 14, 14, 28, C["text2"]), tag="empty", x=w / 2 - 28, y=h / 2 - 70,
                     clear=False)
            c.create_text(w / 2, h / 2 + 6, text="Откройте видео", fill=C["text"], font=ui.font(16, "semibold"))
            c.create_text(w / 2, h / 2 + 34, text="Запись Takeone или любой MP4 / MOV / MKV.\n"
                                                   "Нажмите сюда или «Последняя запись».",
                          fill=C["text2"], font=ui.font(12), justify="center")
        if self.prev and self.prev.busy:
            c.create_text(w - 12, 12, text="…", fill=C["text2"], anchor="ne", font=ui.font(14, "semibold"))

    def _tick(self):
        p = self.prev
        if p:
            out = p.latest()
            if out and out[0] != self.shown_seq:
                from PIL import ImageTk
                self.shown_seq = out[0]
                self._photo = ImageTk.PhotoImage(out[2], master=self.pc)
                if p.playing:
                    self.pos = out[1]
                self._draw_preview()
                self.time_lbl.configure(text=f"{fmt_t(self.pos)} / {fmt_t(self.dur)}")
                self.tl.redraw()
            was = getattr(self, "_was_playing", False)
            if was and not p.playing:  # просмотр дошёл до конца
                self.pos = p.pos
                self.changed()
            self._was_playing = p.playing
        si = self._strip_imgs
        if si is not None:
            self._strip_imgs = None
            if self.path == si[0]:
                self.tl.tiles = si[1]
                self.tl.redraw()
        self.after(30 if (p and p.playing) else 60, self._tick)

    # --- сохранение
    def save(self):
        if not self.prev or self.saving:
            return
        self.stop_play()
        segs = self.segments()
        if not segs:
            return
        import save_dialog
        base, _ = os.path.splitext(os.path.basename(self.path))
        folder = save_dialog.last_dir(os.path.dirname(self.path))
        ext = save_dialog.load_prefs().get("ext_edit", "mp4")
        n = len(segs)
        res, dest = save_dialog.ask(
            self, self.app.app_name, save_dialog.unique_name(folder, f"{base}_монтаж", ext), kind="edit",
            title="Сохранить видео", can_delete=False, folder=folder,
            info=f"Длительность {fmt_t(sum(b - a for a, b in segs))} · "
                 f"{n} {ui.plural(n, 'кусок', 'куска', 'кусков')} из {os.path.basename(self.path)}")
        if res != "save":
            return
        if os.path.abspath(dest) == os.path.abspath(self.path):
            messagebox.showwarning(self.app.app_name, "Сохраните под другим именем — исходное видео сейчас открыто.",
                                   parent=self)
            return
        import android_view
        ffmpeg = self.app.ffmpeg()
        if not ffmpeg:
            messagebox.showerror(self.app.app_name, "Не найден ffmpeg. Переустановите Takeone.", parent=self)
            return
        limit = self.app.limit()
        src = self.path
        self.saving = True
        self.status.configure(text="", cursor="")
        self.changed()
        state = {"f": 0.0}
        self.b_save.set(text="Сохраняю… 0%", enabled=False)

        def progress(f, *_):
            state["f"] = f

        def work():
            tmp = tempfile.mkdtemp(prefix="takeone_edit_")
            err = None
            try:
                android_view.export_cut(ffmpeg, src, dest, segs, limit, progress, tmp)
            except Exception as e:  # noqa
                err = str(e).strip().splitlines()[-1][:300] if str(e).strip() else repr(e)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            state["done"] = (err,)

        threading.Thread(target=work, daemon=True).start()

        def poll():
            if "done" in state:
                self.saving = False
                err = state["done"][0]
                self.changed()
                if err:
                    self.status.configure(text=f"Не сохранилось: {err}", cursor="", fg=C["amber_text"])
                else:
                    mb = os.path.getsize(dest) / 1_000_000
                    self._last_dest = dest
                    self.status.configure(text=f"Сохранено: {os.path.basename(dest)} · {mb:.1f} МБ — показать в папке",
                                          cursor="", fg=C["text2"])
                return
            self.b_save.set(text=f"Сохраняю… {int(state['f'] * 100)}%", enabled=False)
            self.after(200, poll)
        poll()

    def _open_folder(self):
        if not self._last_dest:
            return
        import android_view
        android_view.open_folder(os.path.dirname(self._last_dest))

    # --- вкладка показана / скрыта
    def on_hide(self):
        self.stop_play()

    def key(self, e):
        """Клавиши, когда открыта вкладка «Монтаж» (фокус не в поле ввода)."""
        if isinstance(e.widget, tk.Entry) or not self.prev:
            return None
        k = e.keysym
        if k == "space":
            self.toggle_play()
        elif k in ("Left", "Right"):
            self.step((-1 if k == "Left" else 1) * (0.1 if e.state & 0x1 else 1.0))
        elif k in ("Delete", "BackSpace"):
            self.cut()
        else:
            return None
        return "break"

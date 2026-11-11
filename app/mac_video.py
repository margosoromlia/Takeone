"""Показ трансляции на Mac через Core Animation.

Tk на Mac рисует картинки по одному пикселю на «точку», поэтому на Retina видео растягивается
вдвое и мылится, а перерисовка через Tk ещё и медленная (рывки). Здесь кадр уходит прямо в
слой Core Animation в полном разрешении экрана — чётко и плавно, масштабирует видеочип.

Слой лежит поверх холста Tk и пропускает мышь насквозь (касания, свайпы работают как раньше).
Подсказки поверх видео (плашки, карточка сохранения, предупреждение) дублируются здесь же
нативными слоями — иначе их закрыло бы видео."""
import tkinter.font as tkfont

import AppKit
import Foundation
import Quartz

_VIEW_CLS = None


def _view_class():
    global _VIEW_CLS
    if _VIEW_CLS is None:
        class TakeoneVideoView(AppKit.NSView):
            def hitTest_(self, point):  # мышь и колесо — сквозь видео в окно Tk
                return None

            def isFlipped(self):
                return True

        _VIEW_CLS = TakeoneVideoView
    return _VIEW_CLS


def _rgb(hexcol):
    h = hexcol.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def nscolor(hexcol, alpha=1.0):
    r, g, b = _rgb(hexcol)
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


def cgcolor(hexcol, alpha=1.0):
    return nscolor(hexcol, alpha).CGColor()


def find_nswindow(tkwin):
    """NSWindow для окна Tk: ищем по временному уникальному заголовку."""
    import uuid
    old = tkwin.title()
    mark = "takeone-" + uuid.uuid4().hex[:10]
    tkwin.title(mark)
    tkwin.update_idletasks()
    found = None
    try:
        wins = list(AppKit.NSApplication.sharedApplication().windows())
        found = next((w for w in wins if str(w.title()) == mark), None)
        if found is None:  # запасной вариант — по прежнему заголовку (у каждого телефона он свой)
            same = [w for w in wins if str(w.title()) == old]
            found = same[0] if len(same) == 1 else None
    finally:
        tkwin.title(old)
    return found


class MacVideo:
    def __init__(self, tkwin, canvas):
        self.tkwin, self.canvas = tkwin, canvas
        self.nswin = find_nswindow(tkwin)
        if self.nswin is None:
            raise RuntimeError("не нашлось окно NSWindow")
        self.scale = float(self.nswin.backingScaleFactor() or 1.0)
        self.view = _view_class().alloc().initWithFrame_(((0, 0), (10, 10)))
        self.view.setWantsLayer_(True)
        lay = self.layer = self.view.layer()
        lay.setContentsGravity_(Quartz.kCAGravityResizeAspect)
        lay.setMinificationFilter_(Quartz.kCAFilterTrilinear)
        lay.setMagnificationFilter_(Quartz.kCAFilterLinear)
        lay.setBackgroundColor_(cgcolor("#0B0B0E"))
        try:
            self.cs = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
        except Exception:  # noqa
            self.cs = Quartz.CGColorSpaceCreateDeviceRGB()
        self.nswin.contentView().addSubview_(self.view)
        self.visible = True
        self.overlays = {}
        self.place()

    # --- геометрия
    def place(self):
        c, w = self.canvas, self.tkwin
        x = c.winfo_rootx() - w.winfo_rootx()
        y = c.winfo_rooty() - w.winfo_rooty()
        cw, ch = c.winfo_width(), c.winfo_height()
        content = self.nswin.contentView()
        if not content.isFlipped():
            y = content.frame().size.height - y - ch
        self._tx(lambda: self.view.setFrame_(((x, y), (cw, ch))))
        self.h = ch
        self.scale = float(self.nswin.backingScaleFactor() or self.scale)

    def _tx(self, fn):
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        try:
            fn()
        finally:
            Quartz.CATransaction.commit()

    # --- кадр
    def show(self, arr):
        """arr — кадр h×w×4 в порядке BGRA (numpy, uint8)."""
        h, w = arr.shape[0], arr.shape[1]
        data = Foundation.NSData.dataWithBytes_length_(arr.tobytes(), w * h * 4)
        provider = Quartz.CGDataProviderCreateWithCFData(data)
        img = Quartz.CGImageCreate(w, h, 8, 32, w * 4, self.cs,
                                   Quartz.kCGBitmapByteOrder32Little | Quartz.kCGImageAlphaNoneSkipFirst,
                                   provider, None, False, Quartz.kCGRenderingIntentDefault)
        self._tx(lambda: self.layer.setContents_(img))
        if not self.visible:
            self.set_visible(True)

    def set_visible(self, on):
        self.visible = on
        self.view.setHidden_(not on)

    def set_border(self, hexcol):
        def fn():
            if hexcol:
                self.layer.setBorderColor_(cgcolor(hexcol))
                self.layer.setBorderWidth_(2.0)
            else:
                self.layer.setBorderWidth_(0.0)
        self._tx(fn)

    # --- подсказки поверх видео
    def set_overlay(self, key, prims):
        """prims: [{"rect": (x, y, w, h), "r": радиус, "fill": "#hex", "line": "#hex"|None} |
                   {"text": str, "rect": (x, y, w, h), "size": px, "bold": bool, "mono": bool,
                    "color": "#hex", "align": "left|center|right", "underline": bool}] в координатах холста."""
        def fn():
            old = self.overlays.pop(key, None)
            if old is not None:
                old.removeFromSuperlayer()
            if not prims:
                return
            box = Quartz.CALayer.layer()
            box.setFrame_(self.layer.bounds())
            flipped = self.layer.isGeometryFlipped()
            H = self.layer.bounds().size.height
            for p in prims:
                x, y, w, h = p["rect"]
                fy = y if flipped else H - y - h
                if "text" in p:
                    tl = Quartz.CATextLayer.layer()
                    tl.setContentsScale_(self.scale)
                    tl.setWrapped_(True)
                    tl.setString_(self._attr(p))
                    tl.setFrame_(((x, fy), (w, h)))
                    box.addSublayer_(tl)
                else:
                    rl = Quartz.CALayer.layer()
                    rl.setFrame_(((x, fy), (w, h)))
                    rl.setCornerRadius_(p.get("r", 0))
                    rl.setBackgroundColor_(cgcolor(p["fill"]))
                    if p.get("line"):
                        rl.setBorderColor_(cgcolor(p["line"]))
                        rl.setBorderWidth_(1.0)
                    box.addSublayer_(rl)
            self.layer.addSublayer_(box)
            self.overlays[key] = box
        self._tx(fn)

    def _attr(self, p):
        size, bold = p.get("size", 13), p.get("bold", False)
        weight = AppKit.NSFontWeightSemibold if bold else AppKit.NSFontWeightRegular
        font = None
        if p.get("mono"):
            try:
                font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, weight)
            except Exception:  # noqa — macOS старше 10.15
                font = AppKit.NSFont.fontWithName_size_("Menlo-Bold" if bold else "Menlo-Regular", size)
        if font is None:
            font = AppKit.NSFont.systemFontOfSize_weight_(size, weight)
        ps = AppKit.NSMutableParagraphStyle.alloc().init()
        ps.setAlignment_({"center": AppKit.NSTextAlignmentCenter, "right": AppKit.NSTextAlignmentRight}
                         .get(p.get("align"), AppKit.NSTextAlignmentLeft))
        attrs = {AppKit.NSFontAttributeName: font,
                 AppKit.NSForegroundColorAttributeName: nscolor(p.get("color", "#FFFFFF")),
                 AppKit.NSParagraphStyleAttributeName: ps}
        if p.get("underline"):
            attrs[AppKit.NSUnderlineStyleAttributeName] = AppKit.NSUnderlineStyleSingle
        return Foundation.NSAttributedString.alloc().initWithString_attributes_(p["text"], attrs)

    def close(self):
        try:
            self.view.removeFromSuperview()
        except Exception:  # noqa
            pass


def text_prims(canvas, tag, mono_family):
    """Текстовые элементы холста Tk с тегом → описания для set_overlay (та же позиция и шрифт)."""
    out = []
    for i in canvas.find_withtag(tag):
        if canvas.type(i) != "text" or canvas.itemcget(i, "state") == "hidden":
            continue
        text = canvas.itemcget(i, "text")
        bb = canvas.bbox(i)
        if not text or not bb:
            continue
        a = tkfont.Font(font=canvas.itemcget(i, "font")).actual()
        align = canvas.itemcget(i, "justify") or "left"
        x0 = bb[0] - {"center": 3, "right": 6}.get(align, 0)  # запас по ширине, чтобы строки не переносились иначе
        out.append({"text": text, "rect": (x0, bb[1], bb[2] - bb[0] + 6, bb[3] - bb[1] + 2),
                    "size": abs(int(a.get("size", 13))) or 13, "bold": a.get("weight") == "bold",
                    "mono": a.get("family") == mono_family, "color": canvas.itemcget(i, "fill") or "#FFFFFF",
                    "align": align, "underline": bool(a.get("underline"))})
    return out

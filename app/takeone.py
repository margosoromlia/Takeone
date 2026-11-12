#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Takeone — трансляция и запись экрана Android / iOS по USB с паузой.

Android: через scrcpy (Windows / macOS / Linux).
iOS:     только macOS, через нативный AVFoundation (Swift-хелпер ios_capture.swift,
         собирается автоматически при первом запуске).
"""
import datetime
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox

import ui_kit as ui
from ui_kit import C

APP = "Takeone"
VERSION = "1.5.0"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = getattr(sys, "frozen", False)
BUNDLE = getattr(sys, "_MEIPASS", HERE)       # в собранном приложении — папка с ресурсами
BIN = os.path.join(BUNDLE, "bin")              # встроенные scrcpy / adb / ffmpeg / uxplay / ios_capture
DATA_DIR = os.path.join(os.path.expanduser("~"), ".takeone")
LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def _setup_path():
    """Сначала ищем встроенные программы, потом системные (Homebrew и т.п.).
    Приложение, запущенное из Finder, не видит PATH терминала — добавляем папки Homebrew сами."""
    dirs = [os.path.join(BIN, d) for d in ("scrcpy", "ffmpeg", "uxplay")] + [BIN]
    if not IS_WIN:
        dirs += ["/opt/homebrew/bin", "/usr/local/bin"]
    dirs = [d for d in dirs if os.path.isdir(d)]
    os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
    # Встроенному scrcpy явно показываем его сервер и adb: внутри .app файлы могут лежать в разных папках
    sdir = os.path.join(BIN, "scrcpy")
    server = os.path.join(sdir, "scrcpy-server")
    adb = os.path.join(sdir, "adb.exe" if IS_WIN else "adb")
    if os.path.exists(server):
        os.environ.setdefault("SCRCPY_SERVER_PATH", server)
    if os.path.exists(adb):
        os.environ.setdefault("ADB", adb)


_setup_path()

NOWIN = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: без всплывающих консолей на Windows
DEVNULL = subprocess.DEVNULL
PIPE = subprocess.PIPE


# ----------------------------------------------------------------- утилиты
def tool(name):
    """Ищет исполняемый файл в PATH (adb ещё и рядом с scrcpy)."""
    p = shutil.which(name)
    if p:
        return p
    if name == "adb":
        s = shutil.which("scrcpy")
        if s:
            cand = os.path.join(os.path.dirname(os.path.realpath(s)), "adb.exe" if IS_WIN else "adb")
            if os.path.exists(cand):
                return cand
    return None


def run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NOWIN)


def popen(cmd, **kw):
    return subprocess.Popen(cmd, creationflags=NOWIN, **kw)


def stop_proc(p, timeout=6.0):
    """Мягко останавливает процесс (SIGINT — scrcpy корректно закрывает файл), иначе убивает."""
    if p is None or p.poll() is not None:
        return
    try:
        if IS_WIN:
            p.terminate()  # MKV переживает резкое завершение
        else:
            p.send_signal(signal.SIGINT)
        p.wait(timeout)
    except Exception:
        try:
            p.kill()
            p.wait(3)
        except Exception:
            pass


def tail(path, n=1500):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data[-n:].decode("utf-8", "replace")
    except OSError:
        return ""


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def default_video_dir():
    for d in ("Movies", "Videos", "Desktop"):
        p = os.path.join(os.path.expanduser("~"), d)
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


# ----------------------------------------------------------------- Android
SCRCPY_SERVER_VERSION = "5.0.1"  # должна совпадать с версией scrcpy в скриптах сборки


def find_scrcpy_server():
    """scrcpy-server и его версия: встроенный в сборку или от установленного scrcpy (запуск из исходников)."""
    bundled = os.path.join(BIN, "scrcpy", "scrcpy-server")
    if os.path.exists(bundled):
        return bundled, SCRCPY_SERVER_VERSION
    s = shutil.which("scrcpy")
    if not s:
        return None, None
    m = re.search(r"scrcpy\s+v?(\d+\.\d+(?:\.\d+)?)", run([s, "--version"]).stdout)
    ver = m.group(1) if m else None
    real = os.path.dirname(os.path.realpath(s))
    for c in (os.environ.get("SCRCPY_SERVER_PATH"), os.path.join(real, "scrcpy-server"),
              os.path.join(real, "..", "share", "scrcpy", "scrcpy-server"),
              "/usr/local/share/scrcpy/scrcpy-server", "/usr/share/scrcpy/scrcpy-server"):
        if c and os.path.exists(c):
            return os.path.abspath(c), ver
    return None, ver


WIN_SIZES = {"Маленький": 0.55, "Средний": 0.7, "Большой": 0.86}
SETTINGS = os.path.join(DATA_DIR, "settings.json")


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(cfg):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def default_shots_dir():
    return os.path.join(os.path.expanduser("~"), "Pictures", APP)


def list_android():
    """[(serial, название, подпись, готов ли)]"""
    adb = tool("adb")
    if not adb:
        raise RuntimeError("adb не найден. Установите scrcpy (в нём есть adb) или Android platform-tools.")
    out = run([adb, "devices", "-l"]).stdout
    devs = []
    lines = out.splitlines()
    start = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("List of devices")), 0)
    for line in lines[start:]:
        parts = line.split()
        if len(parts) < 2 or line.startswith("*"):
            continue
        serial, state = parts[0], parts[1]
        model = next((x.split(":", 1)[1] for x in parts if x.startswith("model:")), "")
        title = model.replace("_", " ") or serial
        wifi = ":" in serial or serial.startswith("adb-")
        if state == "device":
            rel = market = ""
            try:
                props = run([adb, "-s", serial, "shell",
                             "getprop ro.build.version.release; getprop ro.product.marketname; "
                             "getprop ro.config.marketing_name"], timeout=6).stdout.splitlines()
                props = [p.strip() for p in props] + ["", "", ""]
                rel, market = props[0], props[1] or props[2]
            except Exception:  # noqa — подпись не главное
                pass
            if market and len(market) <= 40:
                title = market
            sub = (f"Android {rel}" if rel else "Android") + (" · Wi-Fi" if wifi else " · USB")
            devs.append((serial, title, sub, True))
        else:
            if "no permissions" in line or state == "no":
                sub = "Нет доступа к USB-устройству (проверьте права udev)"
            else:
                sub = {"unauthorized": "Разрешите отладку по USB на телефоне",
                       "offline": "Телефон не отвечает — переподключите кабель",
                       "authorizing": "Подтвердите доступ на телефоне"}.get(state, f"Недоступен: {state}")
            devs.append((serial, title, sub, False))
    return devs


# ----------------------------------------------------------------- iOS (macOS)
SWIFT_SRC = os.path.join(HERE, "ios_capture.swift")
BUNDLED_HELPER = os.path.join(BIN, "ios_capture")
HELPER = BUNDLED_HELPER if os.path.exists(BUNDLED_HELPER) else os.path.join(DATA_DIR, "ios_capture")


def ensure_ios_helper():
    if not IS_MAC:
        raise RuntimeError("Запись экрана iPhone/iPad по USB возможна только на macOS.")
    if HELPER == BUNDLED_HELPER:
        return HELPER  # собранное приложение: хелпер уже скомпилирован
    if not os.path.exists(SWIFT_SRC):
        raise RuntimeError(f"Не найден файл {SWIFT_SRC} — положите его рядом с takeone.py.")
    if os.path.exists(HELPER) and os.path.getmtime(HELPER) >= os.path.getmtime(SWIFT_SRC):
        return HELPER
    swiftc = tool("swiftc")
    cmd = [swiftc] if swiftc else ["xcrun", "swiftc"]
    r = run([*cmd, "-O", SWIFT_SRC, "-o", HELPER], timeout=600)
    if r.returncode:
        raise RuntimeError("Не удалось собрать iOS-хелпер (нужны Xcode Command Line Tools: "
                           "xcode-select --install).\n\n" + r.stderr[-1500:])
    return HELPER


def list_ios():
    helper = ensure_ios_helper()
    r = run([helper, "--list"], timeout=30)
    devs = []
    for line in r.stdout.splitlines():
        if "\t" in line:
            uid, name = line.split("\t", 1)
            devs.append((uid, name))
    return devs


class IOSEngine:
    """Хелпер показывает окно трансляции и пишет файл .mov через AVCaptureMovieFileOutput,
    у которого есть нативные pause/resume — получается один цельный файл."""

    def __init__(self, uid):
        self.uid = uid
        self.p = None
        self.q = queue.Queue()
        self.tmpdir = self.file = None
        self.log = os.path.join(LOG_DIR, "ios_helper.log")

    def connect(self):
        helper = ensure_ios_helper()
        self._logf = open(self.log, "wb")
        self.p = subprocess.Popen([helper, "--device", self.uid], stdin=PIPE, stdout=PIPE,
                                  stderr=self._logf, text=True, bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()
        self._expect("ready", 40)

    def _reader(self):
        for line in self.p.stdout:
            self.q.put(line.strip())
        self.q.put("__eof__")

    def _expect(self, word, timeout):
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                raise RuntimeError(f"iOS-хелпер не ответил ({word}).\n\n" + tail(self.log))
            try:
                line = self.q.get(timeout=left)
            except queue.Empty:
                continue
            if line == "__eof__":
                raise RuntimeError("iOS-хелпер завершился.\n\n" + tail(self.log))
            if line.startswith("error"):
                raise RuntimeError(line[5:].strip() or "Неизвестная ошибка iOS-хелпера")
            if line.startswith(word):
                return line

    def _send(self, cmd, expect, timeout=20):
        self.p.stdin.write(cmd + "\n")
        self.p.stdin.flush()
        return self._expect(expect, timeout)

    def start(self):
        self.tmpdir = tempfile.mkdtemp(prefix="takeone_")
        self.file = os.path.join(self.tmpdir, "record.mov")
        self._send(f"start {self.file}", "started")

    def pause(self):
        self._send("pause", "paused")

    def resume(self):
        self._send("resume", "resumed")

    def stop(self):
        self._send("stop", "stopped", 120)

    def save(self, dest):
        if not self.file or not os.path.exists(self.file):
            raise RuntimeError("Файл записи не найден.")
        ext = os.path.splitext(dest)[1].lower()
        ff = tool("ffmpeg")
        if ext == ".mov" or not ff:
            dest = os.path.splitext(dest)[0] + ".mov"
            shutil.move(self.file, dest)
        else:
            r = run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", self.file,
                     "-map", "0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                     "-movflags", "+faststart", dest], timeout=3600)
            if r.returncode:
                raise RuntimeError("ffmpeg: " + r.stderr[-1500:] + f"\n\nИсходник: {self.file}")
        self.cleanup()
        return dest

    def cleanup(self):
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None

    def disconnect(self):
        if self.p and self.p.poll() is None:
            try:
                self.p.stdin.write("quit\n")
                self.p.stdin.flush()
                self.p.wait(3)
            except Exception:
                self.p.kill()
        self.p = None

    def alive(self):
        return self.p is not None and self.p.poll() is None

    rec_alive = alive


# ----------------------------------------------------------------- iOS по AirPlay (Windows / macOS / Linux)
MSYS_BINS = [r"C:\msys64\ucrt64\bin", r"C:\msys64\mingw64\bin"]


UXPLAY_DIR = os.path.join(BIN, "uxplay")      # встроенный UxPlay (Windows) со своим GStreamer


def find_uxplay():
    exe = "uxplay.exe" if IS_WIN else "uxplay"
    bundled = os.path.join(UXPLAY_DIR, exe)
    if os.path.exists(bundled):
        return bundled
    p = tool("uxplay")
    if p:
        return p
    cands = [os.path.join(d, exe) for d in MSYS_BINS] if IS_WIN else []
    return next((c for c in cands if os.path.exists(c)), None)


def uxplay_env(uxplay):
    """UxPlay нужны его DLL и плагины GStreamer. Для встроенной копии явно указываем,
    где лежат плагины, чтобы не подхватить чужой GStreamer из системы."""
    env = os.environ.copy()
    root = os.path.dirname(uxplay)
    if IS_WIN:
        extra = [root] + [d for d in MSYS_BINS if os.path.isdir(d)]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    if os.path.normcase(root) == os.path.normcase(UXPLAY_DIR):
        plugins = os.path.join(root, "lib", "gstreamer-1.0")
        scanner = os.path.join(root, "libexec", "gstreamer-1.0",
                               "gst-plugin-scanner.exe" if IS_WIN else "gst-plugin-scanner")
        env["GST_PLUGIN_SYSTEM_PATH"] = plugins
        env["GST_PLUGIN_PATH"] = plugins
        if os.path.exists(scanner):
            env["GST_PLUGIN_SCANNER"] = scanner
        env["GST_REGISTRY"] = os.path.join(DATA_DIR, f"gst-registry-{VERSION}.bin")
    return env


def check_airplay_tools():
    if not find_uxplay():
        raise RuntimeError("Не найден UxPlay — AirPlay-приёмник. На macOS установите его командой "
                           "«brew install uxplay» (или пользуйтесь режимом «iPhone (USB)»), "
                           "затем нажмите «Обновить».")
    if not tool("ffmpeg"):
        raise RuntimeError("Для режима AirPlay нужен ffmpeg. Установите его (см. README).")


def list_airplay(name):
    check_airplay_tools()
    return [("airplay", f"AirPlay-приёмник «{name}» — на iPhone: Пункт управления → Повтор экрана")]


def probe_size(path):
    """Размер кадра из вывода «ffmpeg -i» (ffprobe не нужен)."""
    r = run([tool("ffmpeg"), "-hide_banner", "-i", path])
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", r.stderr)
    if not m:
        return 1080, 1920
    w, h = int(m.group(1)), int(m.group(2))
    return w - w % 2, h - h % 2


class AirPlayEngine:
    """UxPlay принимает «Повтор экрана» с iPhone, показывает его в окне и пишет весь сеанс
    во фрагментированный MP4 (читается прямо во время записи и не ломается при обрыве).
    Кнопки «Запись/Пауза/Стоп» отмечают интервалы времени; при сохранении ffmpeg
    вырезает ровно эти куски и склеивает их в один файл."""

    FPS = 30

    def __init__(self, name):
        self.name = (name or "Takeone").strip()
        self.p = None
        self.tmpdir = None
        self.files = {}          # путь -> момент (time.time()), соответствующий t=0 файла
        self.intervals = []      # [(начало, конец)] в time.time()
        self.rec_from = None
        self.log = os.path.join(LOG_DIR, "airplay_uxplay.log")
        self._mon_stop = threading.Event()

    def connect(self):
        check_airplay_tools()
        ux = find_uxplay()
        self.tmpdir = tempfile.mkdtemp(prefix="takeone_airplay_")
        self._logf = open(self.log, "wb")
        cmd = [ux, "-n", self.name, "-nh", "-mp4", os.path.join(self.tmpdir, "session")]
        self.p = popen(cmd, stdout=self._logf, stderr=subprocess.STDOUT, env=uxplay_env(ux))
        time.sleep(2.5)
        if self.p.poll() is not None:
            raise RuntimeError("AirPlay-приёмник не запустился.\n\n" + tail(self.log) +
                               "\n\nНа macOS отключите «Приёмник AirPlay» в Настройки → Основные → "
                               "AirDrop и Handoff, если он занимает порты.")
        threading.Thread(target=self._monitor, daemon=True).start()

    def _monitor(self):
        """Засекаем момент, когда в новом файле сеанса появились первые данные = t=0 файла."""
        while not self._mon_stop.is_set():
            try:
                for fn in os.listdir(self.tmpdir):
                    path = os.path.join(self.tmpdir, fn)
                    if fn.endswith(".mp4") and path not in self.files and os.path.getsize(path) > 0:
                        self.files[path] = time.time() - 0.05
            except OSError:
                pass
            time.sleep(0.05)

    def client_connected(self):
        """iPhone сейчас транслирует: есть файл с видео, который пишется."""
        now = time.time()
        for f in self.files:
            if ".H26" in os.path.basename(f):
                try:
                    if now - os.path.getmtime(f) < 5:
                        return True
                except OSError:
                    pass
        return False

    def start(self):
        self.intervals = []
        self.rec_from = time.time()

    def pause(self):
        if self.rec_from is not None:
            self.intervals.append((self.rec_from, time.time()))
            self.rec_from = None

    def resume(self):
        self.rec_from = time.time()

    def stop(self):
        self.pause()
        time.sleep(2.5)  # UxPlay сбрасывает фрагменты раз в 2 с — ждём последний

    def _pieces(self):
        pieces = []
        for a, b in self.intervals:
            for f, t0 in sorted(self.files.items(), key=lambda x: x[1]):
                if ".H26" not in os.path.basename(f):
                    continue  # аудио-only сеанс (музыка без повтора экрана)
                try:
                    t_end = os.path.getmtime(f)
                except OSError:
                    continue
                s, e = max(a, t0), min(b, t_end)
                if e - s >= 0.3:
                    pieces.append((f, s - t0, e - t0, ".AAC." in os.path.basename(f)))
        return pieces

    def save(self, dest):
        pieces = self._pieces()
        if not pieces:
            raise RuntimeError("В записанных интервалах нет видео с iPhone. Проверьте, что на iPhone включён "
                               f"«Повтор экрана» на «{self.name}» во время записи.")
        W, H = probe_size(pieces[0][0])
        # Каждый файл декодируется один раз целиком (перемотка во фрагментированном MP4 неточная),
        # затем split раздаёт кадры на его куски. Куски идут по времени, поэтому буферизации нет.
        cmd = [tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error"]
        order = []
        for p in pieces:
            if p[0] not in order:
                order.append(p[0])
        for f in order:
            cmd += ["-reinit_filter", "0", "-i", f]
        filters, labels = [], []
        for fi, f in enumerate(order):
            mine = [(i, p) for i, p in enumerate(pieces) if p[0] == f]
            n, has_audio = len(mine), mine[0][1][3]
            filters.append(f"[{fi}:v]fps={self.FPS},scale={W}:{H}:force_original_aspect_ratio=decrease,"
                           f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                           f"split={n}" + "".join(f"[sv{i}]" for i, _ in mine))
            if has_audio:
                filters.append(f"[{fi}:a]aresample=async=1,aformat=sample_rates=44100:channel_layouts=stereo,"
                               f"asplit={n}" + "".join(f"[sa{i}]" for i, _ in mine))
            for i, (_, a, b, _) in mine:
                d = b - a
                filters.append(f"[sv{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,"
                               f"tpad=stop_mode=clone:stop_duration={d:.3f},trim=end={d:.3f},"
                               f"setpts=PTS-STARTPTS[v{i}]")
                src = (f"[sa{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS," if has_audio
                       else "anullsrc=r=44100:cl=stereo,")
                filters.append(src + f"apad,atrim=end={d:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels = "".join(f"[v{i}][a{i}]" for i in range(len(pieces)))
        filters.append(labels + f"concat=n={len(pieces)}:v=1:a=1[v][a]")
        cmd += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k"]
        if os.path.splitext(dest)[1].lower() in (".mp4", ".mov"):
            cmd += ["-movflags", "+faststart"]
        cmd.append(dest)
        r = run(cmd, timeout=6 * 3600)
        if r.returncode:
            raise RuntimeError("ffmpeg не смог собрать файл:\n" + r.stderr[-1500:])
        self.intervals = []
        return dest

    def cleanup(self):
        self.intervals = []
        self.rec_from = None

    def disconnect(self):
        self._mon_stop.set()
        stop_proc(self.p)
        self.p = None
        try:
            self._logf.close()
        except Exception:
            pass
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None

    def alive(self):
        return self.p is not None and self.p.poll() is None

    rec_alive = alive


# ----------------------------------------------------------------- GUI
ANDROID_NOTE = "Запись, пауза, звук и скриншот — на панели в окне телефона"
IOS_NOTE = "Ctrl+R — запись, Ctrl+P — пауза, Ctrl+S — стоп и сохранить"


class App:
    W = 512  # ширина содержимого главного окна

    def __init__(self, root):
        self.root = root
        root.title(f"{APP} {VERSION}")
        root.resizable(True, True)  # можно растянуть или развернуть на весь экран
        root.minsize(560, 600)
        root.configure(bg=C["bg"])
        self.engine = None
        self.state = "idle"  # idle | connected | recording | paused
        self.busy = False
        self.searching = False
        self.saving = False
        self.devices = []      # [(id, название, подпись, готов)]
        self.selected = []     # id отмеченных устройств
        self.rows = []
        self.elapsed = 0.0
        self.seg_started = None
        self.close_after_save = False

        cfg = load_settings()
        self.platform = tk.StringVar(value=cfg.get("platform", "Android"))
        if self.platform.get() == "iOS" and not IS_MAC:
            self.platform.set("Android")
        self.max_size = tk.StringVar(value=cfg.get("max_size", "1920"))
        self.bitrate = tk.StringVar(value=cfg.get("bitrate", "8"))
        self.audio = tk.BooleanVar(value=cfg.get("audio", True))
        self.together = tk.BooleanVar(value=cfg.get("together", True))
        self.sharp = tk.BooleanVar(value=cfg.get("sharp", True))
        self.ap_name = tk.StringVar(value=cfg.get("ap_name", "Takeone"))
        self.limit_mb = tk.StringVar(value=cfg.get("limit_mb", "50"))
        win_size = cfg.get("win_size", "Большой")
        self.win_size = tk.StringVar(value=win_size if win_size in WIN_SIZES else "Большой")
        self.android_wins = []
        self.android_layout = None
        self._group_state = None
        self._quitting = False
        self.ap_hint = None
        self.status = tk.StringVar(value="")

        self._build_ui()
        root.bind("<Control-r>", lambda e: self.start_rec())
        root.bind("<Control-p>", lambda e: self.toggle_pause())
        root.bind("<Control-s>", lambda e: self.stop_rec())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<KeyPress>", lambda e: self.editor.key(e) if self.platform.get() == "Edit" else None)

        self._layout()
        self._restore_geometry(cfg.get("geom"))
        self.update_buttons()
        self.refresh()
        self.tick()

    # --- интерфейс
    def _build_ui(self):
        bg, W = C["bg"], self.W
        outer = self.outer = tk.Frame(self.root, bg=bg, padx=24)
        outer.pack(fill="both", expand=True)

        # шапка
        hd = tk.Frame(outer, bg=bg)
        hd.pack(fill="x", pady=(20, 16))
        ui.LogoTile(hd).pack(side="left")
        tt = tk.Frame(hd, bg=bg)
        tt.pack(side="left", padx=(12, 0))
        ui.label(tt, APP, 17, weight="bold").pack(anchor="w")
        ui.label(tt, "Запись экрана телефона", 12, C["muted"]).pack(anchor="w")
        ui.label(hd, VERSION, 12, C["muted"], mono=True).pack(side="right")

        # режим
        self.tabs = ui.Segmented(outer, [("Android", "Android"), ("iOS", "iPhone · USB"),
                                         ("AirPlay", "iPhone · Wi-Fi"), ("Edit", "Монтаж")],
                                 self.platform, self._platform_changed)
        self.tabs.pack(fill="x")

        # середина окна: прокручивается, если не влезает; низ (кнопки) закреплён
        self.mid = tk.Frame(outer, bg=bg)
        self.scroll = ui.ScrollArea(self.mid, bg=bg)
        sc = self.scroll.body

        # монтаж: обрезка и вырезание кусков
        import editor
        self.editor = editor.EditorPanel(self.mid, self, W, foot_parent=outer)

        # список устройств
        self.dev_sec = sec = tk.Frame(sc, bg=bg)
        head = tk.Frame(sec, bg=bg)
        head.pack(fill="x")
        self.dev_title = ui.label(head, "Телефоны", 15, weight="semibold")
        self.dev_title.pack(side="left")
        self.btn_refresh = ui.Button(head, "Обновить", self.refresh, kind="secondary", icon="refresh",
                                     height=32, radius=8, size=13)
        self.btn_refresh.pack(side="right")
        self.dev_card = ui.Card(sec, pad=6)
        self.dev_card.pack(fill="x", pady=(10, 0))
        self.dev_hint = ui.autowrap(ui.label(sec, "", 12, C["text2"], wraplength=W, justify="left"), pad=12)
        self.dev_hint.pack(anchor="w", pady=(10, 0))

        # AirPlay: имя приёмника и шаги
        self.ap_sec = sec = tk.Frame(sc, bg=bg)
        ui.label(sec, "Приёмник AirPlay", 15, weight="semibold").pack(anchor="w")
        card = ui.Card(sec, pad=16)
        card.pack(fill="x", pady=(10, 0))
        b = card.body
        ui.label(b, "Имя приёмника на iPhone", 12, C["text2"]).pack(anchor="w")
        self.f_ap = ui.Field(b, self.ap_name, width=240)
        self.f_ap.pack(anchor="w", pady=(6, 0))
        self.ap_steps = ui.autowrap(ui.label(b, "", 13, C["text2"], justify="left", wraplength=W - 40))
        self.ap_steps.pack(anchor="w", pady=(14, 0))
        self.ap_name.trace_add("write", lambda *a: self._ap_steps())
        self._ap_steps()

        # настройки
        self.set_card = ui.Card(sc, pad=16)
        sb = self.set_card.body
        ui.label(sb, "Настройки", 15, weight="semibold").pack(anchor="w")
        self.android_opts = ao = tk.Frame(sb, bg=C["panel"])
        grid = tk.Frame(ao, bg=C["panel"])
        grid.pack(fill="x", pady=(16, 0))
        grid.columnconfigure(0, weight=1, uniform="c")
        grid.columnconfigure(1, weight=1, uniform="c")
        self.f_size = self._num_field(grid, 0, "Макс. сторона видео", self.max_size, "px", (0, 6))
        self.f_rate = self._num_field(grid, 1, "Битрейт", self.bitrate, "Мбит/с", (6, 0))
        ui.label(ao, "Размер окна телефона", 12, C["text2"]).pack(anchor="w", pady=(16, 6))
        self.seg_size = ui.Segmented(ao, [(k, k) for k in WIN_SIZES], self.win_size, height=36, radius=9)
        self.seg_size.pack(fill="x")
        ui.divider(ao).pack(fill="x", pady=(16, 0))
        row = tk.Frame(ao, bg=C["panel"])
        row.pack(fill="x", pady=(16, 0))
        self.tg_audio = ui.Toggle(row, self.audio)
        self.tg_audio.pack(side="right")
        lab = tk.Frame(row, bg=C["panel"])
        lab.pack(side="left", fill="x", expand=True)
        t1 = ui.label(lab, "Звук телефона", 14, weight="semibold")
        t1.pack(anchor="w")
        t2 = ui.label(lab, "Android 11+. Пока включён, динамик телефона молчит", 12, C["muted"],
                      wraplength=W - 110, justify="left")
        ui.autowrap(t2, ref=row, pad=64)
        t2.pack(anchor="w", pady=(2, 0))
        for w in (t1, t2, lab):
            w.bind("<ButtonRelease-1>", lambda e: self.tg_audio.toggle())
        row = tk.Frame(ao, bg=C["panel"])
        row.pack(fill="x", pady=(16, 0))
        self.tg_together = ui.Toggle(row, self.together, self._together_changed)
        self.tg_together.pack(side="right")
        lab = tk.Frame(row, bg=C["panel"])
        lab.pack(side="left", fill="x", expand=True)
        t1 = ui.label(lab, "Общая запись телефонов", 14, weight="semibold")
        t1.pack(anchor="w")
        self.tg_together_sub = t2 = ui.label(lab, "", 12, C["muted"], wraplength=W - 110, justify="left")
        ui.autowrap(t2, ref=row, pad=64)
        t2.pack(anchor="w", pady=(2, 0))
        for w in (t1, t2, lab):
            w.bind("<ButtonRelease-1>", lambda e: self.tg_together.toggle())
        self._together_text()
        self.tg_sharp = None
        if IS_MAC:
            row = tk.Frame(ao, bg=C["panel"])
            row.pack(fill="x", pady=(16, 0))
            self.tg_sharp = ui.Toggle(row, self.sharp, self.save_settings)
            self.tg_sharp.pack(side="right")
            lab = tk.Frame(row, bg=C["panel"])
            lab.pack(side="left", fill="x", expand=True)
            t1 = ui.label(lab, "Чёткая картинка (Retina)", 14, weight="semibold")
            t1.pack(anchor="w")
            t2 = ui.label(lab, "Видео через видеочип Mac — резко и плавно. Для новых окон телефона", 12,
                          C["muted"], wraplength=W - 110, justify="left")
            ui.autowrap(t2, ref=row, pad=64)
            t2.pack(anchor="w", pady=(2, 0))
            for w in (t1, t2, lab):
                w.bind("<ButtonRelease-1>", lambda e: self.tg_sharp.toggle())

        self.limit_row = lr = tk.Frame(sb, bg=C["panel"])
        lr.pack(fill="x", pady=(16, 0))
        right = tk.Frame(lr, bg=C["panel"])
        right.pack(side="right")
        self.f_limit = ui.Field(right, self.limit_mb, width=64, justify="right")
        self.f_limit.pack(side="left")
        ui.label(right, "МБ", 13, C["text2"]).pack(side="left", padx=(8, 0))
        lab = tk.Frame(lr, bg=C["panel"])
        lab.pack(side="left", fill="x")
        ui.label(lab, "Видео не больше", 14, weight="semibold").pack(anchor="w")
        ui.label(lab, "Больше — сожмётся при сохранении · 0 без лимита", 12, C["muted"]).pack(anchor="w", pady=(2, 0))

        # низ: главная кнопка / панель записи iPhone / статус
        self.footer = ft = tk.Frame(outer, bg=bg)
        self.btn_main = ui.Button(ft, "Открыть трансляцию", self.toggle_connect, kind="primary", height=44,
                                  radius=10, size=15)
        self.recbar = ui.Card(ft, pad=8)
        rb = self.recbar.body
        self.pill = ui.RecPill(rb, self._pill_click, bg=C["panel"])
        self.pill.pack(side="left", padx=(2, 0))
        self.b_pause = ui.IconButton(rb, "pause", self.toggle_pause, "Пауза  (Ctrl+P)", bg=C["panel"])
        self.b_pause.pack(side="left", padx=(4, 0))
        self.b_stop = ui.IconButton(rb, "stop", self.stop_rec, "Остановить и сохранить  (Ctrl+S)", bg=C["panel"])
        self.b_stop.pack(side="left", padx=(4, 0))
        self.btn_disc = ui.Button(rb, "Закрыть окна", self._recbar_right, kind="secondary", height=32, radius=8,
                                  size=13)
        self.btn_disc.pack(side="right", padx=(0, 2))
        self.status_lbl = ui.autowrap(ui.label(ft, "", 12, C["muted"], wraplength=W, justify="center"))
        self.status.trace_add("write", lambda *a: self.status_lbl.configure(text=self._status_text()))

    def _status_text(self):
        if self.status.get():
            return self.status.get()
        if self.platform.get() != "Android":
            return IOS_NOTE
        if self.android_wins:
            return "● Запись — здесь (для всех окон) или на панели окна телефона. Звук и скриншот — в окне телефона."
        return "Сначала откройте трансляцию, потом нажмите «● Запись» — здесь или в окне телефона."

    def _num_field(self, grid, col, title, var, unit, padx):
        box = tk.Frame(grid, bg=C["panel"])
        box.grid(row=0, column=col, sticky="we", padx=padx)
        ui.label(box, title, 12, C["text2"]).pack(anchor="w")
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x", pady=(6, 0))
        ui.label(row, unit, 13, C["text2"]).pack(side="right", padx=(8, 0))
        f = ui.Field(row, var, width=80)
        f.pack(side="left", fill="x", expand=True)
        return f

    def _ap_steps(self):
        name = self.ap_name.get().strip() or APP
        self.ap_steps.configure(text="1.  Компьютер и iPhone — в одной сети Wi-Fi\n"
                                     "2.  Нажмите «Подключить» внизу\n"
                                     f"3.  На iPhone: Пункт управления → Повтор экрана → «{name}»")

    def _layout(self):
        """Порядок блоков зависит от режима: Android, iPhone по кабелю или по Wi-Fi."""
        plat = self.platform.get()
        for w in (self.dev_sec, self.ap_sec, self.set_card, self.footer, self.editor, self.editor.foot,
                  self.scroll, self.mid):
            w.pack_forget()
        if plat == "Edit":
            self.editor.foot.pack(side="bottom", fill="x", pady=(12, 18))
            self.mid.pack(fill="both", expand=True, pady=(16, 0))
            self.editor.pack(fill="both", expand=True)
            return
        self.footer.pack(side="bottom", fill="x", pady=(12, 18))
        self.mid.pack(fill="both", expand=True, pady=(16, 0))
        self.scroll.pack(fill="both", expand=True)
        (self.ap_sec if plat == "AirPlay" else self.dev_sec).pack(fill="x", pady=(4, 0))
        self.dev_title.configure(text="Телефоны" if plat == "Android" else "iPhone и iPad")
        self.set_card.pack(fill="x", pady=(20, 4))
        if plat == "Android":
            self.android_opts.pack(fill="x", before=self.limit_row)
        else:
            self.android_opts.pack_forget()
        self.scroll.to_top()

    def _platform_changed(self):
        if self.platform.get() != "Edit":
            self.editor.on_hide()
        self.devices, self.selected = [], []
        self.status.set("")
        self._render_rows()
        self._layout()
        self.update_buttons()
        self.refresh()

    def _render_rows(self):
        body = self.dev_card.body
        for w in body.winfo_children():
            w.destroy()
        self.rows = []
        plat = self.platform.get()
        if not self.devices:
            box = tk.Frame(body, bg=C["panel"])
            box.pack(fill="x", pady=14, padx=12)
            if self.searching:
                ui.label(box, "Ищу устройства…", 13, C["text2"]).pack()
            else:
                ui.label(box, "Телефоны не найдены" if plat == "Android" else "iPhone не найден", 14,
                         weight="semibold").pack()
                ui.label(box, "Подключите телефон кабелем и включите «Отладку по USB»,\nзатем нажмите «Обновить»."
                         if plat == "Android" else
                         "Подключите iPhone кабелем, разблокируйте и нажмите «Доверять»,\nзатем «Обновить».",
                         12, C["muted"], justify="center").pack(pady=(4, 0))
            return
        live = self._open_ids()
        for i, (uid, title, sub, ready) in enumerate(self.devices):
            st = ("live", "В окне") if uid in live else (("ok", "Готов") if ready else ("wait", "Ждёт"))
            r = ui.DeviceRow(body, title, sub, st, self._row_click, checked=uid in self.selected or uid in live,
                             enabled=ready and uid not in live, radio=plat != "Android")
            r.uid = uid
            r.ready = ready and uid not in live
            r.pack(fill="x", pady=(0 if i == 0 else 4, 0))
            self.rows.append(r)

    app_name = APP

    def video_dir(self):
        return default_video_dir()

    def ffmpeg(self):
        return tool("ffmpeg")

    def _groups(self):
        """Группы записи открытых окон телефонов (одна общая или по одной на телефон)."""
        out = []
        for w in self.android_wins:
            if w.group not in out:
                out.append(w.group)
        return out

    def _rec_active(self):
        return any(g.state != "idle" for g in self._groups())

    def _new_group(self):
        import android_view
        return android_view.RecordGroup(tool("ffmpeg"), default_video_dir(), None, self.limit(), APP)

    def _together_changed(self):
        """Переключили «Общую запись» при открытых окнах — перераспределить окна по группам."""
        self._together_text()
        if self.android_wins and not self._rec_active():
            if self.together.get():
                g = self._new_group()
                for w in self.android_wins:
                    w.set_group(g)
            else:
                for w in self.android_wins:
                    w.set_group(self._new_group())
        self.save_settings()
        self.update_buttons()

    def _together_text(self):
        self.tg_together_sub.configure(
            text="Несколько телефонов — одно видео: экраны рядом, без звука" if self.together.get() else
            "Каждый телефон — своё видео со звуком и свои кнопки записи")

    def _open_ids(self):
        return {w.link.serial for w in self.android_wins}

    def _row_click(self, row):
        if self.busy or self.state != "idle" or row.uid in self._open_ids():
            return
        if self.platform.get() == "Android":
            if row.uid in self.selected:
                self.selected.remove(row.uid)
            else:
                self.selected.append(row.uid)
        else:
            self.selected = [row.uid]
        for r in self.rows:
            r.set(checked=r.uid in self.selected)
        self.update_buttons()

    def _pill_click(self):
        if self.platform.get() == "Android":
            if any(g.state == "paused" for g in self._groups()):
                for g in self._groups():
                    if g.state == "paused":
                        g.toggle_pause()
            else:
                self.start_rec()
            return
        if self.state == "paused":
            self.toggle_pause()
        else:
            self.start_rec()

    # --- фоновые операции, чтобы окно не зависало
    def bg(self, fn, done=None, fail=None, text=None):
        if text:
            self.status.set(text)
        self.busy = True
        self.update_buttons()

        def work():
            try:
                res, err = fn(), None
            except Exception as e:  # noqa
                res, err = None, e
            self.root.after(0, lambda: self._bg_done(res, err, done, fail))

        threading.Thread(target=work, daemon=True).start()

    def _bg_done(self, res, err, done, fail):
        self.busy = False
        if err:
            self.status.set("Ошибка: " + str(err).splitlines()[0])
            if fail:
                fail()
            messagebox.showerror(APP, str(err))
        elif done:
            done(res)
        self.update_buttons()

    def update_buttons(self):
        s, b = self.state, self.busy
        plat = self.platform.get()
        android = plat == "Android"
        idle = s == "idle" and not b
        live = self._open_ids()
        rec = self._rec_active()
        self.tabs.set_enabled(not b or self.searching)
        dis = {}
        if not IS_MAC:
            dis["iOS"] = "Запись iPhone по кабелю работает только на Mac.\nНа Windows используйте «iPhone · Wi-Fi»."
        if live:
            for k in ("iOS", "AirPlay"):
                dis.setdefault(k, "Сначала закройте окна Android-телефонов")
        if s != "idle":
            for k in ("Android", "iOS", "AirPlay"):
                if k != plat:
                    dis.setdefault(k, "Сначала отключите iPhone")
        self.tabs.set_disabled(dis)
        if plat == "Edit":
            self.editor.changed()
            return
        self.btn_refresh.set(enabled=idle and not self.searching)
        for r in self.rows:
            r.set(enabled=idle and r.ready)
        for f in (self.f_size, self.f_rate, self.f_ap):
            f.set_enabled(idle)
        self.f_limit.set_enabled(idle and not rec)
        self.seg_size.set_enabled(idle)
        self.tg_audio.set_enabled(idle)
        if self.tg_sharp:
            self.tg_sharp.set_enabled(not b)
        self.tg_together.set_enabled(not b and not rec)
        sel = [d for d in self.devices if d[0] in self.selected and d[3] and d[0] not in live]

        # подсказка под списком
        if android and live:
            n, k = len(sel), len(live)
            hint = ("Идёт запись — добавить телефон можно после её остановки." if rec else
                    f"Открыто окон: {k}. Отметьте ещё телефон, чтобы добавить его к трансляции." if n == 0 else
                    f"Добавится {n} — окон станет {k + n}, "
                    + ("запись пойдёт в одно видео без звука." if self.together.get() else
                       "у каждого телефона своя запись."))
        elif android:
            n = len(sel)
            hint = ("" if not self.devices else
                    "Отметьте телефон, чтобы открыть трансляцию." if n == 0 else
                    "Окно телефона откроется с панелью записи сверху." if n == 1 else
                    f"Отмечено {n} — окна откроются рядом, "
                    + ("запись пойдёт в одно видео без звука." if self.together.get() else
                       "у каждого телефона своя запись со звуком."))
        else:
            hint = ("Запись сохраняется одним файлом, паузы вырезаются." if self.devices else "")
        self.dev_hint.configure(text=hint)
        if hint:
            self.dev_hint.pack(anchor="w", pady=(10, 0))
        else:
            self.dev_hint.pack_forget()

        # низ окна
        for w in (self.btn_main, self.recbar, self.status_lbl):
            w.pack_forget()
        if android and live:  # окна телефонов открыты — запись можно вести отсюда
            self.recbar.pack(fill="x")
            self._update_recbar()
            n = len(sel)
            if n:
                self.btn_main.set(text=f"Добавить {n} {ui.plural(n, 'трансляцию', 'трансляции', 'трансляций')}",
                                  enabled=not rec and not b)
                self.btn_main.pack(fill="x", pady=(8, 0))
        elif android or s == "idle":
            n = len(sel)
            if android:
                verb = "Добавить" if live else "Открыть"
                text = (f"{verb} трансляцию" if n <= 1 else
                        f"{verb} {n} {ui.plural(n, 'трансляцию', 'трансляции', 'трансляций')}")
                ok = n > 0 and not rec
            else:
                text, ok = "Подключить", plat == "AirPlay" or bool(sel)
            self.btn_main.set(text=text, enabled=ok and not b and s == "idle")
            self.btn_main.pack(fill="x")
        else:
            self.recbar.pack(fill="x")
            self._update_recbar()
        self.status_lbl.configure(text=self._status_text())
        self.status_lbl.pack(fill="x", pady=(10, 0))

    def _update_recbar(self):
        if self.platform.get() == "Android" and self.android_wins:
            self._update_android_bar()
            return
        self.btn_disc.set(text="Отключить")
        s, b = self.state, self.busy
        total = self.elapsed + (time.monotonic() - self.seg_started if self.seg_started else 0)
        if self.saving:
            self.pill.set("saving", "Сохранение", enabled=False, spin=int(time.monotonic() * 12) % 12 * 30)
        elif s == "recording":
            self.pill.set("recording", ui.fmt_hms(total), blink=int(time.monotonic() * 2) % 2 == 0)
        elif s == "paused":
            self.pill.set("paused", ui.fmt_hms(total), enabled=not b)
        else:
            self.pill.set("idle", "Запись", enabled=not b and s == "connected")
        active = s in ("recording", "paused")
        self.b_pause.set("play" if s == "paused" else "pause", active and not b,
                         "Продолжить  (Ctrl+P)" if s == "paused" else "Пауза  (Ctrl+P)")
        self.b_stop.set(enabled=active and not b, tone="raised" if active else "")
        self.btn_disc.set(enabled=s == "connected" and not b)

    def _update_android_bar(self):
        """Панель записи в главном окне, когда открыты окна Android-телефонов (управляет всеми)."""
        groups = self._groups()
        st = [g.state for g in groups]
        t = max((g.time() for g in groups), default=0.0)
        n = len(self.android_wins)
        if "saving" in st:
            self.pill.set("saving", "Сохранение", enabled=False, spin=int(time.monotonic() * 12) % 12 * 30)
        elif "recording" in st:
            self.pill.set("recording", ui.fmt_hms(t), blink=int(time.monotonic() * 2) % 2 == 0)
        elif "paused" in st:
            self.pill.set("paused", ui.fmt_hms(t))
        else:
            ready = any(g.ready() for g in groups)
            self.pill.set("idle", f"Запись ×{n}" if n > 1 and self.together.get() else "Запись", enabled=ready)
        active = any(x in ("recording", "paused") for x in st)
        self.b_pause.set("play" if ("paused" in st and "recording" not in st) else "pause", active,
                         "Продолжить  (Ctrl+P)" if "paused" in st else "Пауза  (Ctrl+P)")
        self.b_stop.set(enabled=active, tone="raised" if active else "")
        self.btn_disc.set(text="Закрыть окна", enabled=not active and "saving" not in st)

    def _recbar_right(self):
        if self.platform.get() == "Android" and self.android_wins:
            if not self._rec_active():
                for w in list(self.android_wins):
                    w.close()
            return
        self.toggle_connect()

    # --- устройства
    def refresh(self):
        if self.state != "idle" or self.busy or self.platform.get() == "Edit":
            return
        plat = self.platform.get()
        ios = plat == "iOS"
        if plat == "AirPlay":
            name = self.ap_name.get()
            fn = lambda: list_airplay(name)  # noqa
        else:
            fn = list_ios if ios else list_android
        text = ("Ищу iPhone/iPad (первый раз собирается хелпер, ~1 мин)…" if ios and not os.path.exists(HELPER)
                else "")
        self.searching = True
        if plat != "AirPlay":
            self.devices = []
            self._render_rows()

        def norm(d):
            if len(d) >= 4:
                return tuple(d[:4])
            ready = "—" not in d[1]
            return (d[0], d[1].split("  (")[0], "iPhone · USB" if ios else d[1], ready)

        def stale():
            """Пока искали, переключили режим — результат уже не нужен, ищем заново."""
            if self.platform.get() == plat:
                return False
            self.root.after(20, self.refresh)
            return True

        def done(devs):
            self.searching = False
            if stale():
                return
            if plat == "AirPlay":
                self.devices = []
                self.status.set("")
                return
            prev = list(self.selected)
            self.devices = [norm(d) for d in devs]
            ready = [d[0] for d in self.devices if d[3]]
            keep = [u for u in prev if u in ready]
            if plat == "Android":
                self.selected = keep or ([] if self.android_wins else ready[:1])
            else:
                self.selected = keep[:1] or ready[:1]
            self._render_rows()
            self.status.set("")

        def fail():
            self.searching = False
            if stale():
                return
            self.devices, self.selected = [], []
            if plat != "AirPlay":
                self._render_rows()

        self.bg(fn, done, fail, text)

    def make_engine(self):
        if self.platform.get() == "AirPlay":
            self.ap_hint = None
            return AirPlayEngine(self.ap_name.get())
        sel = [d for d in self.devices if d[0] in self.selected and d[3]]
        if not sel:
            raise RuntimeError("Выберите устройство.")
        if self.platform.get() == "iOS":
            return IOSEngine(sel[0][0])
        raise RuntimeError("Android открывается в своём окне трансляции.")

    # --- Android: окна трансляции с панелью записи (одно или несколько связанных)
    def limit(self):
        try:
            return max(0.0, float(self.limit_mb.get().replace(",", ".") or 0))
        except ValueError:
            return 0.0

    def _restore_geometry(self, geom):
        """Размер окна — как в прошлый раз (но не больше экрана); иначе — по высоте экрана."""
        r = self.root
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w, h, x, y = 560, min(860, sh - 90), None, None
        m = re.match(r"(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)", geom or "")
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            x, y = int(m.group(3)), int(m.group(4))
        w, h = max(560, min(w, sw)), max(600, min(h, sh - 60))
        if x is None or not (0 <= x <= sw - 120 and 0 <= y <= sh - 120):
            x, y = max(0, (sw - w) // 2), max(0, min(40, sh - h - 60))
        r.geometry(f"{w}x{h}+{x}+{y}")

    def _geometry(self):
        try:
            if self.root.state() == "zoomed" or self.root.attributes("-fullscreen"):
                return None
        except tk.TclError:
            pass
        return self.root.geometry()

    def save_settings(self):
        save_settings({"geom": self._geometry() or load_settings().get("geom"), "platform": self.platform.get(), "max_size": self.max_size.get(),
                       "bitrate": self.bitrate.get(), "audio": self.audio.get(),
                       "ap_name": self.ap_name.get(), "limit_mb": self.limit_mb.get(),
                       "win_size": self.win_size.get(), "together": self.together.get(),
                       "sharp": self.sharp.get()})

    def open_android(self):
        """Открывает окна отмеченных телефонов. Если трансляция уже идёт — добавляет новые
        телефоны к ней (в общую запись), не закрывая открытые окна."""
        if self.busy:
            return
        if self._rec_active():
            messagebox.showinfo(APP, "Сейчас идёт запись. Остановите её в окне телефона — "
                                     "после этого можно добавить ещё телефон.")
            return
        live = self._open_ids()
        serials = [d[0] for d in self.devices if d[0] in self.selected and d[3] and d[0] not in live]
        if not serials:
            messagebox.showwarning(APP, "Отметьте телефон в списке.")
            return
        try:
            ms = int(self.max_size.get() or 0)
            br = max(1.0, float(self.bitrate.get() or 8))
        except ValueError:
            messagebox.showwarning(APP, "Проверьте числа в настройках Android.")
            return
        server, ver = find_scrcpy_server()
        if not server:
            messagebox.showerror(APP, "Не найден scrcpy-server. Переустановите Takeone.")
            return
        if not ver or int(ver.split(".")[0]) < 5:
            messagebox.showerror(APP, f"Нужен scrcpy 5.x (найден {ver or 'неизвестной версии'}).")
            return
        try:
            import android_view
        except ImportError as e:
            messagebox.showerror(APP, f"Не хватает компонента для трансляции: {e}\n"
                                      "Установите: pip install av pillow numpy sounddevice")
            return
        self.save_settings()
        links = [android_view.AndroidLink(tool("adb"), s, server, ver, ms, br, self.audio.get(), LOG_DIR)
                 for s in serials]

        def start_all():
            for ln in links:
                ln.start()

        def stop_all():
            for ln in links:
                ln.stop()

        def done(_):
            if self.android_layout is None or not self.android_wins:
                self.android_layout = android_view.WindowLayout()
            shared = None
            if self.together.get():
                shared = self.android_wins[0].group if self.android_wins else self._new_group()
            frac = WIN_SIZES.get(self.win_size.get(), 0.86)
            for ln in links:
                group = shared or self._new_group()
                self.android_wins.append(android_view.DeviceWindow(
                    self.root, ln, group, default_shots_dir(), self._android_closed, frac, APP,
                    layout=self.android_layout, sharp=self.sharp.get()))
            self.android_layout.arranged = False
            for w in self.android_wins:  # окна делят ширину экрана — пересчитать размеры
                if w.dev_size:
                    w._fit_window()
            for g in self._groups():
                g.limit_mb = self.limit()
                g.refresh()
            self.selected = [u for u in self.selected if u not in self._open_ids()]
            self.status.set("")
            self._render_rows()

        self.bg(start_all, done, stop_all,
                "Подключаюсь к телефону…" if len(links) == 1 else f"Подключаюсь к телефонам ({len(links)})…")

    def _android_closed(self, win):
        if win in self.android_wins:
            self.android_wins.remove(win)
        if not self.android_wins:
            self.android_layout = None
            if self._quitting:
                self.shutdown()
                return
        self._render_rows()
        self.update_buttons()

    # --- подключение
    def toggle_connect(self):
        if self.platform.get() == "Android":
            self.open_android()
            return
        if self.state == "idle":
            try:
                eng = self.make_engine()
            except RuntimeError as e:
                messagebox.showwarning(APP, str(e))
                return

            def done(_):
                self.engine, self.state = eng, "connected"
                self.status.set("Трансляция запущена. Можно начинать запись.")

            self.bg(eng.connect, done, lambda: eng.disconnect(), "Подключаюсь к устройству…")
        elif self.state == "connected":
            eng = self.engine

            def done(_):
                self.engine, self.state = None, "idle"
                self.status.set("Отключено.")

            self.bg(eng.disconnect, done, text="Отключаюсь…")

    # --- запись
    def start_rec(self):
        if self.platform.get() == "Edit":
            return
        if self.platform.get() == "Android":
            for g in self._groups():
                g.start()
            return
        if self.busy or self.state not in ("idle", "connected"):
            return
        if self.state == "idle":
            try:
                eng = self.make_engine()
            except RuntimeError as e:
                messagebox.showwarning(APP, str(e))
                return
            fn = lambda: (eng.connect(), eng.start())  # noqa
            fail = lambda: eng.disconnect()  # noqa
        else:
            eng = self.engine
            fn, fail = eng.start, None

        def done(_):
            self.engine, self.state = eng, "recording"
            self.elapsed, self.seg_started = 0.0, time.monotonic()
            self.status.set("Идёт запись…")

        self.bg(fn, done, fail, "Запускаю запись…")

    def toggle_pause(self):
        if self.platform.get() == "Edit":
            return
        if self.platform.get() == "Android":
            for g in self._groups():
                g.toggle_pause()
            return
        if self.busy or self.state not in ("recording", "paused"):
            return
        if self.state == "recording":
            self._accumulate()

            def done(_):
                self.state = "paused"
                self.status.set("Пауза. Нажмите «Продолжить», чтобы дописать в тот же файл.")

            self.bg(self.engine.pause, done, text="Ставлю на паузу…")
        else:
            def done(_):
                self.state, self.seg_started = "recording", time.monotonic()
                self.status.set("Идёт запись…")

            self.bg(self.engine.resume, done, text="Продолжаю запись…")

    def _accumulate(self):
        if self.seg_started is not None:
            self.elapsed += time.monotonic() - self.seg_started
            self.seg_started = None

    def stop_rec(self):
        if self.platform.get() == "Edit":
            return
        if self.platform.get() == "Android":
            done = []
            for w in list(self.android_wins):
                if w.group not in done:
                    done.append(w.group)
                    w.stop_rec()
            return
        if self.busy or self.state not in ("recording", "paused"):
            return
        self._accumulate()
        self.saving = True
        self.bg(self.engine.stop, lambda _: self.ask_save(), lambda: setattr(self, "saving", False),
                text="Завершаю запись…")

    def ask_save(self):
        import save_dialog
        ios = isinstance(self.engine, IOSEngine)
        kind = "ios" if ios else "airplay"
        name = f"{APP}_iPhone_" + datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        folder = save_dialog.last_dir(default_video_dir())
        ext = save_dialog.load_prefs().get(f"ext_{kind}", "mp4")
        res, dest = save_dialog.ask(self.root, APP, save_dialog.unique_name(folder, name, ext), kind=kind,
                                    folder=folder, info=f"Длительность {ui.fmt_hms(self.elapsed)}")
        if res != "save":
            self.engine.cleanup()
            self.after_record("Запись удалена.")
            return

        eng, limit, ff = self.engine, self.limit(), tool("ffmpeg")

        def save():
            path = eng.save(dest)
            if limit and ff and os.path.getsize(path) > limit * 1_000_000:
                import android_view
                tmpdir = tempfile.mkdtemp(prefix="takeone_fit_")
                src = os.path.join(tmpdir, "orig" + os.path.splitext(path)[1])
                shutil.move(path, src)
                try:
                    android_view.fit_file(ff, src, path, limit, None, tmpdir)
                finally:
                    if os.path.exists(path):
                        shutil.rmtree(tmpdir, ignore_errors=True)
            try:
                import android_view
                android_view.remember_video(path)
            except Exception:  # noqa
                pass
            return path

        self.save_settings()
        self.bg(save, lambda path: self.after_record(
            f"Сохранено: {path} · {os.path.getsize(path) / 1_000_000:.1f} МБ"),
                lambda: self.after_record(None), "Сохраняю файл…")

    def after_record(self, msg):
        self.saving = False
        self.state = "connected" if self.engine and self.engine.alive() else "idle"
        if msg:
            self.status.set(msg)
        self.update_buttons()
        if self.close_after_save:
            self.shutdown()

    # --- таймер и контроль процессов
    def tick(self):
        if self.state in ("connected", "recording", "paused") or (
                self.android_wins and self.platform.get() == "Android"):
            self._update_recbar()
        groups = self._groups()
        gs = tuple(g.state for g in groups)
        if gs != self._group_state:
            self._group_state = gs
            self.update_buttons()
        for g in groups:
            if g.state == "idle":
                g.limit_mb = self.limit()
        if not self.busy and isinstance(self.engine, AirPlayEngine) and self.engine.alive():
            hint = ("iPhone транслирует экран." if self.engine.client_connected() else
                    f"Ждём iPhone: Пункт управления → Повтор экрана → «{self.engine.name}».")
            if hint != self.ap_hint:
                self.ap_hint = hint
                prefix = {"recording": "Идёт запись. ", "paused": "Пауза. "}.get(self.state, "")
                self.status.set(prefix + hint)
        if not self.busy and self.engine:
            if self.state == "connected" and not self.engine.alive():
                self.engine.disconnect()
                self.engine, self.state = None, "idle"
                self.status.set("Трансляция закрыта или устройство отключено.")
                self.update_buttons()
            elif self.state == "recording" and not self.engine.rec_alive():
                self._accumulate()
                self.state = "paused"
                self.status.set("Запись прервалась (устройство отключено?). Можно нажать «Продолжить» "
                                "или «Стоп и сохранить» — уже записанное сохранится.")
                self.update_buttons()
        self.root.after(300, self.tick)

    def on_close(self):
        if self.busy:
            return
        if self.android_wins:
            if self._rec_active():
                messagebox.showinfo(APP, "Идёт запись. Остановите и сохраните её в окне телефона, "
                                         "потом закройте Takeone.")
                return
            self._quitting = True
            for w in list(self.android_wins):
                w.close()
            return
        if self.state in ("recording", "paused"):
            ans = messagebox.askyesnocancel(APP, "Идёт запись. Сохранить её перед выходом?")
            if ans is None:
                return
            if ans:
                self.close_after_save = True
                self.stop_rec()
                return
            try:
                self.engine.stop()
                self.engine.cleanup()
            except Exception:
                pass
        self.shutdown()

    def shutdown(self):
        try:
            self.save_settings()
        except Exception:  # noqa
            pass
        if self.engine:
            try:
                self.engine.disconnect()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

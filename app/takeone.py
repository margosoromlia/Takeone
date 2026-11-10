#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Takeone — трансляция и запись экрана Android / iOS по USB с паузой.

Android: через scrcpy (Windows / macOS / Linux).
iOS:     только macOS, через нативный AVFoundation (Swift-хелпер ios_capture.swift,
         собирается автоматически при первом запуске).
"""
import datetime
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
from tkinter import filedialog, messagebox, ttk

APP = "Takeone"
VERSION = "1.1.0"
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


def default_shots_dir():
    return os.path.join(os.path.expanduser("~"), "Pictures", APP)


def list_android():
    adb = tool("adb")
    if not adb:
        raise RuntimeError("adb не найден. Установите scrcpy (в нём есть adb) или Android platform-tools.")
    out = run([adb, "devices", "-l"]).stdout
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = next((x.split(":", 1)[1] for x in parts if x.startswith("model:")), "")
        label = f"{model.replace('_', ' ') or serial}  ({serial})"
        if state != "device":
            label += f"  — {state}"
        devs.append((serial, label))
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
class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP} {VERSION}")
        root.resizable(False, False)
        self.engine = None
        self.android_win = None
        self.state = "idle"  # idle | connected | recording | paused
        self.busy = False
        self.devices = []
        self.elapsed = 0.0
        self.seg_started = None
        self.close_after_save = False

        self.platform = tk.StringVar(value="Android")
        self.device = tk.StringVar()
        self.max_size = tk.StringVar(value="1920")
        self.bitrate = tk.StringVar(value="8")
        self.audio = tk.BooleanVar(value=True)
        self.ap_name = tk.StringVar(value="Takeone")
        self.ap_hint = None
        self.status = tk.StringVar(value="Подключите устройство по USB и нажмите «Обновить».")
        self.timer = tk.StringVar(value="00:00:00")

        f = ttk.Frame(root, padding=12)
        f.grid(sticky="nsew")

        ttk.Label(f, text="Платформа:").grid(row=0, column=0, sticky="w")
        pf = ttk.Frame(f)
        pf.grid(row=0, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(pf, text="Android (USB)", value="Android", variable=self.platform,
                        command=self.refresh).pack(side="left")
        ios_rb = ttk.Radiobutton(pf, text="iPhone (USB, macOS)", value="iOS", variable=self.platform,
                                 command=self.refresh)
        ios_rb.pack(side="left", padx=(12, 0))
        if not IS_MAC:
            ios_rb.state(["disabled"])
        ttk.Radiobutton(pf, text="iPhone (AirPlay, Wi-Fi)", value="AirPlay", variable=self.platform,
                        command=self.refresh).pack(side="left", padx=(12, 0))

        ttk.Label(f, text="Устройство:").grid(row=1, column=0, sticky="w", pady=6)
        self.dev_cb = ttk.Combobox(f, textvariable=self.device, state="readonly", width=42)
        self.dev_cb.grid(row=1, column=1, sticky="we", pady=6)
        self.btn_refresh = ttk.Button(f, text="Обновить", command=self.refresh)
        self.btn_refresh.grid(row=1, column=2, padx=(6, 0))

        self.opts = ttk.LabelFrame(f, text="Настройки Android", padding=8)
        self.opts.grid(row=2, column=0, columnspan=3, sticky="we", pady=(4, 8))
        ttk.Label(self.opts, text="Макс. сторона, px (0 = оригинал):").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.opts, textvariable=self.max_size, width=8).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(self.opts, text="Битрейт, Мбит/с:").grid(row=1, column=0, sticky="w")
        ttk.Entry(self.opts, textvariable=self.bitrate, width=8).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Checkbutton(self.opts, text="Записывать звук устройства (Android 11+)",
                        variable=self.audio).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.ap_opts = ttk.LabelFrame(f, text="Настройки AirPlay", padding=8)
        self.ap_opts.grid(row=2, column=0, columnspan=3, sticky="we", pady=(4, 8))
        ttk.Label(self.ap_opts, text="Имя приёмника:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.ap_opts, textvariable=self.ap_name, width=20).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(self.ap_opts, foreground="#666", wraplength=440,
                  text="Компьютер и iPhone должны быть в одной сети Wi-Fi. После «Подключить» на iPhone: "
                       "Пункт управления → Повтор экрана → выберите это имя.").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, columnspan=3, sticky="we")
        self.btn_conn = ttk.Button(bf, text="Подключить", command=self.toggle_connect, width=13)
        self.btn_rec = ttk.Button(bf, text="● Запись", command=self.start_rec, width=11)
        self.btn_pause = ttk.Button(bf, text="⏸ Пауза", command=self.toggle_pause, width=14)
        self.btn_stop = ttk.Button(bf, text="■ Стоп и сохранить", command=self.stop_rec, width=18)
        for i, b in enumerate((self.btn_conn, self.btn_rec, self.btn_pause, self.btn_stop)):
            b.grid(row=0, column=i, padx=(0 if i == 0 else 6, 0))

        self.timer_row = tf = ttk.Frame(f)
        tf.grid(row=4, column=0, columnspan=3, sticky="we", pady=(10, 0))
        self.dot = tk.Label(tf, text="●", fg="gray", font=("TkDefaultFont", 16))
        self.dot.pack(side="left")
        ttk.Label(tf, textvariable=self.timer, font=("TkFixedFont", 20)).pack(side="left", padx=6)
        ttk.Label(f, textvariable=self.status, wraplength=470, foreground="#555").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.hint = ttk.Label(f, text="", foreground="#888", wraplength=470)
        self.hint.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))

        root.bind("<Control-r>", lambda e: self.start_rec())
        root.bind("<Control-p>", lambda e: self.toggle_pause())
        root.bind("<Control-s>", lambda e: self.stop_rec())
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.update_buttons()
        self.refresh()
        self.tick()

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
        en = lambda w, on: w.state(["!disabled"] if on else ["disabled"])  # noqa
        en(self.btn_refresh, not b and s == "idle")
        en(self.dev_cb, not b and s == "idle")
        self.dev_cb.configure(state="readonly" if (not b and s == "idle") else "disabled")
        plat = self.platform.get()
        android = plat == "Android"
        en(self.btn_conn, not b and s in ("idle", "connected"))
        self.btn_conn.configure(text="Открыть трансляцию" if android else
                                ("Отключить" if s != "idle" else "Подключить"),
                                width=20 if android else 13)
        for w in (self.btn_rec, self.btn_pause, self.btn_stop):
            (w.grid_remove if android else w.grid)()
        (self.timer_row.grid_remove if android else self.timer_row.grid)()
        self.hint.configure(text="Запись, пауза, звук и скриншоты — на панели вверху окна телефона."
                            if android else
                            "Горячие клавиши (в этом окне): Ctrl+R — запись, Ctrl+P — пауза, Ctrl+S — стоп")
        en(self.btn_rec, not b and s in ("idle", "connected") and bool(self.devices))
        en(self.btn_pause, not b and s in ("recording", "paused"))
        self.btn_pause.configure(text="▶ Продолжить" if s == "paused" else "⏸ Пауза")
        en(self.btn_stop, not b and s in ("recording", "paused"))
        self.dot.configure(fg={"recording": "red", "paused": "orange", "connected": "green"}.get(s, "gray"))
        (self.opts.grid if plat == "Android" else self.opts.grid_remove)()
        (self.ap_opts.grid if plat == "AirPlay" else self.ap_opts.grid_remove)()

    # --- устройства
    def refresh(self):
        if self.state != "idle":
            return
        self.update_buttons()
        plat = self.platform.get()
        ios = plat == "iOS"
        if plat == "AirPlay":
            name = self.ap_name.get()
            fn = lambda: list_airplay(name)  # noqa
        else:
            fn = list_ios if ios else list_android
        text = ("Ищу iPhone/iPad (первый раз собирается хелпер, ~1 мин)…" if ios and not os.path.exists(HELPER)
                else "Ищу устройства…")

        def done(devs):
            self.devices = devs
            self.dev_cb["values"] = [d[1] for d in devs]
            if devs and plat == "AirPlay":
                self.dev_cb.current(0)
                self.status.set("Нажмите «Подключить», затем на iPhone включите «Повтор экрана».")
            elif devs:
                self.dev_cb.current(0)
                self.status.set(f"Найдено устройств: {len(devs)}. Нажмите «" + ("Открыть трансляцию" if plat == "Android" else "Подключить") + "».")
            else:
                self.device.set("")
                self.status.set("Устройства не найдены. " + (
                    "Подключите iPhone кабелем, разблокируйте и нажмите «Доверять»." if ios else
                    "Включите «Отладку по USB» и подтвердите доступ на телефоне."))

        def fail():
            self.devices = []
            self.dev_cb["values"] = []
            self.device.set("")

        self.bg(fn, done, fail, text)

    def make_engine(self):
        idx = self.dev_cb.current()
        if idx < 0:
            raise RuntimeError("Выберите устройство.")
        uid = self.devices[idx][0]
        if self.platform.get() == "iOS":
            return IOSEngine(uid)
        if self.platform.get() == "AirPlay":
            self.ap_hint = None
            return AirPlayEngine(self.ap_name.get())
        raise RuntimeError("Android открывается в своём окне трансляции.")

    # --- Android: отдельное окно трансляции с панелью записи
    def open_android(self):
        if self.busy or self.android_win is not None:
            return
        idx = self.dev_cb.current()
        if idx < 0:
            messagebox.showwarning(APP, "Выберите устройство.")
            return
        serial = self.devices[idx][0]
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
        link = android_view.AndroidLink(tool("adb"), serial, server, ver, ms, br, self.audio.get(), LOG_DIR)

        def done(_):
            self.android_win = android_view.DeviceWindow(
                self.root, link, tool("ffmpeg"), default_shots_dir(), default_video_dir(), None,
                self._android_closed, APP)
            self.state = "connected"
            self.status.set("Трансляция открыта в отдельном окне.")
            self.root.withdraw()

        self.bg(link.start, done, link.stop, "Подключаюсь к телефону…")

    def _android_closed(self):
        self.android_win = None
        self.state = "idle"
        self.status.set("Трансляция закрыта.")
        self.root.deiconify()
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
        if self.busy or self.state not in ("idle", "connected") or self.platform.get() == "Android":
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
        if self.busy or self.state not in ("recording", "paused"):
            return
        self._accumulate()
        self.bg(self.engine.stop, lambda _: self.ask_save(), text="Завершаю запись…")

    def ask_save(self):
        ios = isinstance(self.engine, IOSEngine)
        name = "screen_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        while True:
            dest = filedialog.asksaveasfilename(
                title="Сохранить запись", initialdir=default_video_dir(), initialfile=name,
                defaultextension=".mp4",
                filetypes=[("MP4", "*.mp4"), ("QuickTime MOV", "*.mov")] if ios
                else [("MP4", "*.mp4"), ("Matroska MKV", "*.mkv")])
            if dest:
                break
            if messagebox.askyesno(APP, "Удалить эту запись без сохранения?"):
                self.engine.cleanup()
                self.after_record("Запись удалена.")
                return

        self.bg(lambda: self.engine.save(dest), lambda path: self.after_record(f"Сохранено: {path}"),
                lambda: self.after_record(None), "Сохраняю файл…")

    def after_record(self, msg):
        self.state = "connected" if self.engine and self.engine.alive() else "idle"
        if msg:
            self.status.set(msg)
        self.update_buttons()
        if self.close_after_save:
            self.shutdown()

    # --- таймер и контроль процессов
    def tick(self):
        total = self.elapsed + (time.monotonic() - self.seg_started if self.seg_started else 0)
        self.timer.set(fmt_time(total))
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
        if self.android_win is not None:
            self.android_win.close()
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

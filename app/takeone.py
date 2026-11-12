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
VERSION = "1.6.0"
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
    out = run([adb, "devices", "-l"], timeout=20).stdout
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
        safe = re.sub(r"[^A-Za-z0-9]", "", uid)[-12:] or "dev"
        self.log = os.path.join(LOG_DIR, f"ios_helper_{safe}.log")  # у каждого iPhone/iPad свой лог

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

    def __init__(self, name, port=None, slot=1):
        self.name = (name or "Takeone").strip()
        self.port = port      # для 2-го и 3-го приёмника — свои порты, чтобы не мешать первому
        self.slot = slot
        self.p = None
        self.tmpdir = None
        self.files = {}          # путь -> момент (time.time()), соответствующий t=0 файла
        self.intervals = []      # [(начало, конец)] в time.time()
        self.rec_from = None
        self.log = os.path.join(LOG_DIR, "airplay_uxplay.log" if slot == 1 else f"airplay_uxplay_{slot}.log")
        self._mon_stop = threading.Event()

    def connect(self):
        check_airplay_tools()
        ux = find_uxplay()
        self.tmpdir = tempfile.mkdtemp(prefix="takeone_airplay_")
        self._logf = open(self.log, "wb")
        cmd = [ux, "-n", self.name, "-nh", "-mp4", os.path.join(self.tmpdir, "session")]
        if self.port:
            cmd += ["-p", str(self.port)]
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

    def client_name(self):
        """Имя подключившегося iPhone из лога приёмника (если UxPlay его пишет)."""
        try:
            text = tail(self.log, 20000)
        except Exception:  # noqa
            return None
        found = None
        for rx in (r"connection request from (.+?) \(", r"[Cc]lient (?:device )?name\s*[=:]\s*\"?([^\"\n]+)",
                   r"DeviceName\W+([^\"\n<]+)"):
            for m in re.finditer(rx, text):
                found = m.group(1).strip()
        return found[:40] if found else None

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


# ----------------------------------------------------------------- iPhone / iPad в общем списке
AP_MAX = 3            # сколько приёмников «iPhone по Wi-Fi» можно открыть сразу
AP_PORTS = {2: 7200, 3: 7300}


def ap_receiver_name(base, slot):
    base = (base or APP).strip() or APP
    return base if slot == 1 else f"{base} {slot}"


def ext_save(eng, dest, limit):
    """Сохранить запись iPhone/iPad в dest; если больше лимита — сжать."""
    path = eng.save(dest)
    ff = tool("ffmpeg")
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
    return path


class ExtDevice:
    """iPhone или iPad: по кабелю (Mac, окно показывает хелпер) или по Wi-Fi (окно AirPlay-приёмника).
    Запись управляется из главного окна: сама по себе (свой файл со звуком) или в общей записи
    (через RecordGroup.extras — тогда видео встаёт рядом с остальными экранами)."""

    def __init__(self, app, dev):
        self.app, self.id, self.kind = app, dev["id"], dev["kind"]
        self.base_title = dev["title"]
        if self.kind == "ios":
            self.engine = IOSEngine(dev["uid"])
        else:
            slot = dev["slot"]
            self.engine = AirPlayEngine(ap_receiver_name(app.ap_name.get(), slot), AP_PORTS.get(slot), slot)
        self.state = "idle"      # idle | recording | paused | saving (когда пишет сам по себе)
        self.elapsed, self.since = 0.0, None
        self.ext_error = None
        self.jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    # команды хелперу/приёмнику идут по очереди в отдельном потоке — окно не подвисает
    def _worker(self):
        while True:
            fn = self.jobs.get()
            try:
                if fn is None:
                    return
                fn()
            except Exception as e:  # noqa
                self.ext_error = str(e)
            finally:
                self.jobs.task_done()

    def _do(self, fn):
        self.jobs.put(fn)

    def _wait(self):
        self.jobs.join()

    # --- состояние
    def connect(self):
        self.engine.connect()

    def alive(self):
        return self.engine.alive()

    def ready(self):
        return self.alive() and (self.kind == "ios" or self.engine.client_connected())

    @property
    def label(self):
        if self.kind == "airplay":
            return self.engine.client_name() or "iPhone"
        return self.base_title

    def time(self):
        return self.elapsed + (time.monotonic() - self.since if self.since else 0.0)

    def row(self):
        """(название, подпись, статус) для строки списка."""
        if self.kind == "ios":
            return self.base_title, "по кабелю · окно iPhone/iPad открыто", ("live", "В окне")
        name = self.engine.name
        if self.engine.client_connected():
            return (self.engine.client_name() or "iPhone по Wi-Fi"), f"по Wi-Fi · через «{name}»", ("live", "В окне")
        return f"iPhone по Wi-Fi · «{name}»", f"Включите на iPhone: Повтор экрана → «{name}»", ("wait", "Ждёт iPhone")

    # --- для общей записи (RecordGroup)
    def ext_start(self):
        self.ext_error = None
        self._do(self.engine.start)

    def ext_pause(self):
        self._do(self.engine.pause)

    def ext_resume(self):
        self._do(self.engine.resume)

    def ext_finish(self, tmp):
        self._wait()
        self.engine.stop()
        if self.kind == "ios":
            path = self.engine.file
        else:
            path = os.path.join(tmp, f"airplay_{self.engine.slot}.mp4")
            self.engine.save(path)
        if not path or not os.path.exists(path):
            return None
        return path, probe_size(path)

    def ext_cleanup(self):
        try:
            self.engine.cleanup()
        except Exception:  # noqa
            pass

    # --- своя запись (раздельный режим)
    def start(self):
        if self.state != "idle" or not self.ready():
            return
        self.ext_start()
        self.state, self.elapsed, self.since = "recording", 0.0, time.monotonic()

    def toggle_pause(self):
        if self.state == "recording":
            self.ext_pause()
            self.elapsed += time.monotonic() - self.since
            self.state, self.since = "paused", None
        elif self.state == "paused":
            self.ext_resume()
            self.state, self.since = "recording", time.monotonic()

    def stop(self, parent=None):
        if self.state not in ("recording", "paused"):
            return
        if self.since:
            self.elapsed += time.monotonic() - self.since
            self.since = None
        self.state = "saving"
        dur = self.elapsed
        app = self.app

        def stop_then_ask():
            err = None
            try:
                self._wait()
                self.engine.stop()
            except Exception as e:  # noqa
                err = str(e)
            app.root.after(0, lambda: ask(err))

        def ask(err):
            import save_dialog
            if err:
                self._finish(f"{self.label}: запись не остановилась — {err}")
                return
            if save_dialog.OPEN[0]:  # уже открыто другое окно сохранения — подождём своей очереди
                app.root.after(400, lambda: ask(None))
                return
            kind = "ios" if self.kind == "ios" else "airplay"
            slug = re.sub(r"[^\w-]+", "_", self.label).strip("_")[:30] or "iPhone"
            name = f"{APP}_{slug}_" + datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            folder = save_dialog.last_dir(default_video_dir())
            ext = save_dialog.load_prefs().get(f"ext_{kind}", "mp4")
            res, dest = save_dialog.ask(app.root, APP, save_dialog.unique_name(folder, name, ext), kind=kind,
                                        folder=folder, info=f"Длительность {ui.fmt_hms(dur)} · {self.label}")
            if res != "save":
                self.ext_cleanup()
                self._finish(f"{self.label}: запись удалена")
                return
            limit = app.limit()
            app.status.set(f"{self.label}: сохраняю…")

            def work():
                try:
                    path = ext_save(self.engine, dest, limit)
                    import android_view
                    android_view.remember_video(path)
                    msg = f"Сохранено: {os.path.basename(path)} · {os.path.getsize(path) / 1_000_000:.1f} МБ"
                except Exception as e:  # noqa
                    msg = f"{self.label}: не сохранилось — {str(e).splitlines()[0][:200]}"
                app.root.after(0, lambda: self._finish(msg))
            threading.Thread(target=work, daemon=True).start()

        threading.Thread(target=stop_then_ask, daemon=True).start()

    def _finish(self, msg):
        self.state, self.elapsed, self.since = "idle", 0.0, None
        self.app.status.set(msg)
        self.app.update_buttons()

    def close(self):
        def work():
            try:
                self.engine.disconnect()
            except Exception:  # noqa
                pass
            self.jobs.put(None)
        threading.Thread(target=work, daemon=True).start()


# ----------------------------------------------------------------- GUI
class App:
    W = 512  # ширина содержимого главного окна (по умолчанию)
    app_name = APP

    def __init__(self, root):
        self.root = root
        root.title(f"{APP} {VERSION}")
        root.resizable(True, True)  # можно растянуть или развернуть на весь экран
        root.minsize(560, 600)
        root.configure(bg=C["bg"])
        self.busy = False
        self.searching = False
        self.dev_android = []   # [{id, kind, title, sub, ready}]
        self.dev_ios = []
        self.ios_error = None
        self.selected = []      # id отмеченных устройств
        self.rows = []
        self.android_wins = []
        self.android_layout = None
        self.ext = {}           # id -> ExtDevice (открытые iPhone/iPad)
        self.shared = None      # общая запись (RecordGroup) — когда включена «Общая запись»
        self._sig = None
        self._quitting = False
        self.status = tk.StringVar(value="")

        cfg = load_settings()
        self.platform = tk.StringVar(value="Edit" if cfg.get("platform") == "Edit" else "Phones")
        self.max_size = tk.StringVar(value=cfg.get("max_size", "1920"))
        self.bitrate = tk.StringVar(value=cfg.get("bitrate", "8"))
        self.audio = tk.BooleanVar(value=cfg.get("audio", True))
        self.together = tk.BooleanVar(value=cfg.get("together", True))
        self.sharp = tk.BooleanVar(value=cfg.get("sharp", True))
        self.ap_name = tk.StringVar(value=cfg.get("ap_name", "Takeone"))
        self.ap_slots = max(1, min(AP_MAX, int(cfg.get("ap_slots", 1) or 1)))
        self.limit_mb = tk.StringVar(value=cfg.get("limit_mb", "50"))
        win_size = cfg.get("win_size", "Большой")
        self.win_size = tk.StringVar(value=win_size if win_size in WIN_SIZES else "Большой")
        self.more_open = bool(cfg.get("more_open", False))

        self._build_ui()
        for mod in ("Control", "Command") if IS_MAC else ("Control",):
            try:
                root.bind(f"<{mod}-r>", lambda e: self.start_rec())
                root.bind(f"<{mod}-p>", lambda e: self.toggle_pause())
                root.bind(f"<{mod}-s>", lambda e: self.stop_rec())
            except tk.TclError:
                pass
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

        hd = tk.Frame(outer, bg=bg)
        hd.pack(fill="x", pady=(20, 16))
        ui.LogoTile(hd).pack(side="left")
        tt = tk.Frame(hd, bg=bg)
        tt.pack(side="left", padx=(12, 0))
        ui.label(tt, APP, 17, weight="bold").pack(anchor="w")
        ui.label(tt, "Запись экрана телефона", 12, C["muted"]).pack(anchor="w")
        ui.label(hd, VERSION, 12, C["muted"], mono=True).pack(side="right")

        self.tabs = ui.Segmented(outer, [("Phones", "Телефоны"), ("Edit", "Монтаж")], self.platform,
                                 self._platform_changed)
        self.tabs.pack(fill="x")

        self.mid = tk.Frame(outer, bg=bg)
        self.scroll = ui.ScrollArea(self.mid, bg=bg)
        sc = self.scroll.body

        import editor
        self.editor = editor.EditorPanel(self.mid, self, W, foot_parent=outer)

        # устройства
        self.dev_sec = sec = tk.Frame(sc, bg=bg)
        sec.pack(fill="x", pady=(4, 0))
        head = tk.Frame(sec, bg=bg)
        head.pack(fill="x")
        ui.label(head, "Устройства", 15, weight="semibold").pack(side="left")
        self.btn_refresh = ui.Button(head, "Обновить", self.refresh, kind="secondary", icon="refresh",
                                     height=32, radius=8, size=13)
        self.btn_refresh.pack(side="right")
        self.dev_card = ui.Card(sec, pad=6)
        self.dev_card.pack(fill="x", pady=(10, 0))
        row = tk.Frame(sec, bg=bg)
        row.pack(fill="x", pady=(10, 0))
        self.btn_more_ap = ui.Button(row, "Ещё iPhone по Wi-Fi", self._add_slot, kind="secondary", icon="phone",
                                     height=32, radius=8, size=13,
                                     tip="Ещё один приёмник: «Takeone 2», «Takeone 3» — для второго и третьего iPhone")
        self.btn_more_ap.pack(side="left")
        self.btn_less_ap = ui.IconButton(row, "close", self._remove_slot, "Убрать последний приёмник Wi-Fi", bg=bg,
                                         size=32, glyph=16)
        self.ap_note = ui.label(row, f"до {AP_MAX} приёмников", 12, C["muted"])
        self.ap_note.pack(side="left", padx=(10, 0))
        self.dev_hint = ui.autowrap(ui.label(sec, "", 12, C["text2"], wraplength=W, justify="left"), pad=12)
        self.dev_hint.pack(anchor="w", pady=(10, 0))

        # настройки: главное сверху, остальное — под «Ещё»
        self.set_card = ui.Card(sc, pad=16)
        self.set_card.pack(fill="x", pady=(20, 4))
        sb = self.set_card.body
        ui.label(sb, "Настройки", 15, weight="semibold").pack(anchor="w")
        self.tg_together, self.tg_together_sub = self._toggle_row(sb, "Общая запись телефонов", "", self.together,
                                                                  self._together_changed)
        self._together_text()
        lr = tk.Frame(sb, bg=C["panel"])
        lr.pack(fill="x", pady=(16, 0))
        right = tk.Frame(lr, bg=C["panel"])
        right.pack(side="right")
        self.f_limit = ui.Field(right, self.limit_mb, width=64, justify="right")
        self.f_limit.pack(side="left")
        ui.label(right, "МБ", 13, C["text2"]).pack(side="left", padx=(8, 0))
        lab = tk.Frame(lr, bg=C["panel"])
        lab.pack(side="left", fill="x", expand=True)
        ui.label(lab, "Видео не больше", 14, weight="semibold").pack(anchor="w")
        ui.label(lab, "Больше — сожмётся при сохранении · 0 без лимита", 12, C["muted"]).pack(anchor="w", pady=(2, 0))

        self.btn_more = ui.Button(sb, "", self._toggle_more, kind="quiet", height=30, radius=8, size=12)
        self.btn_more.pack(anchor="w", pady=(16, 0))
        self.more = mo = tk.Frame(sb, bg=C["panel"])
        grid = tk.Frame(mo, bg=C["panel"])
        grid.pack(fill="x", pady=(12, 0))
        grid.columnconfigure(0, weight=1, uniform="c")
        grid.columnconfigure(1, weight=1, uniform="c")
        self.f_size = self._num_field(grid, 0, "Макс. сторона видео (Android)", self.max_size, "px", (0, 6))
        self.f_rate = self._num_field(grid, 1, "Битрейт (Android)", self.bitrate, "Мбит/с", (6, 0))
        ui.label(mo, "Размер окна телефона", 12, C["text2"]).pack(anchor="w", pady=(16, 6))
        self.seg_size = ui.Segmented(mo, [(k, k) for k in WIN_SIZES], self.win_size, height=36, radius=9)
        self.seg_size.pack(fill="x")
        self.tg_audio, _ = self._toggle_row(mo, "Звук телефона", "Android 11+. Пока включён, динамик телефона молчит",
                                            self.audio, None)
        self.tg_sharp = None
        if IS_MAC:
            self.tg_sharp, _ = self._toggle_row(mo, "Чёткая картинка (Retina)",
                                                "Видео Android через видеочип Mac — резко и плавно", self.sharp,
                                                self.save_settings)
        ui.label(mo, "Имя приёмника для iPhone по Wi-Fi", 12, C["text2"]).pack(anchor="w", pady=(16, 6))
        self.f_ap = ui.Field(mo, self.ap_name, width=240)
        self.f_ap.pack(anchor="w")
        self.ap_name.trace_add("write", lambda *a: self._render_rows())
        self._show_more()

        # низ: главная кнопка / панель записи / подсказка
        self.footer = ft = tk.Frame(outer, bg=bg)
        self.btn_main = ui.Button(ft, "Открыть трансляцию", self.open_devices, kind="primary", height=44,
                                  radius=10, size=15)
        self.recbar = ui.Card(ft, pad=8)
        rb = self.recbar.body
        self.pill = ui.RecPill(rb, self._pill_click, bg=C["panel"])
        self.pill.pack(side="left", padx=(2, 0))
        self.b_pause = ui.IconButton(rb, "pause", self.toggle_pause, "Пауза  (Ctrl+P)", bg=C["panel"])
        self.b_pause.pack(side="left", padx=(4, 0))
        self.b_stop = ui.IconButton(rb, "stop", self.stop_rec, "Остановить и сохранить  (Ctrl+S)", bg=C["panel"])
        self.b_stop.pack(side="left", padx=(4, 0))
        self.btn_disc = ui.Button(rb, "Закрыть окна", self.close_all, kind="secondary", height=32, radius=8, size=13)
        self.btn_disc.pack(side="right", padx=(0, 2))
        self.status_lbl = ui.autowrap(ui.label(ft, "", 12, C["muted"], wraplength=W, justify="center"))
        self.status.trace_add("write", lambda *a: self.status_lbl.configure(text=self._status_text()))

    def _toggle_row(self, parent, title, sub, var, command):
        row = tk.Frame(parent, bg=C["panel"])
        row.pack(fill="x", pady=(16, 0))
        tg = ui.Toggle(row, var, command)
        tg.pack(side="right")
        lab = tk.Frame(row, bg=C["panel"])
        lab.pack(side="left", fill="x", expand=True)
        t1 = ui.label(lab, title, 14, weight="semibold")
        t1.pack(anchor="w")
        t2 = ui.label(lab, sub, 12, C["muted"], wraplength=self.W - 110, justify="left")
        ui.autowrap(t2, ref=row, pad=64)
        t2.pack(anchor="w", pady=(2, 0))
        for w in (t1, t2, lab):
            w.bind("<ButtonRelease-1>", lambda e: tg.toggle())
        return tg, t2

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

    def _toggle_more(self):
        self.more_open = not self.more_open
        self._show_more()
        self.save_settings()

    def _show_more(self):
        if self.more_open:
            self.more.pack(fill="x")
            self.btn_more.set(text="Скрыть дополнительные настройки ▴")
        else:
            self.more.pack_forget()
            self.btn_more.set(text="Ещё: размер видео, битрейт, окна, звук, приёмник Wi-Fi ▾")

    def _together_text(self):
        self.tg_together_sub.configure(
            text="Все экраны — одно видео рядом, без звука" if self.together.get() else
            "Каждое устройство — свой файл со звуком")

    def _layout(self):
        plat = self.platform.get()
        for w in (self.footer, self.editor, self.editor.foot, self.scroll, self.mid):
            w.pack_forget()
        if plat == "Edit":
            self.editor.foot.pack(side="bottom", fill="x", pady=(12, 18))
            self.mid.pack(fill="both", expand=True, pady=(16, 0))
            self.editor.pack(fill="both", expand=True)
            return
        self.footer.pack(side="bottom", fill="x", pady=(12, 18))
        self.mid.pack(fill="both", expand=True, pady=(16, 0))
        self.scroll.pack(fill="both", expand=True)

    def _platform_changed(self):
        if self.platform.get() != "Edit":
            self.editor.on_hide()
        self._layout()
        self.update_buttons()
        self.save_settings()

    # --- список устройств
    def _airplay_ok(self):
        return bool(find_uxplay()) and bool(tool("ffmpeg"))

    def devices(self):
        """Все строки списка: Android, iPhone/iPad по кабелю, приёмники Wi-Fi."""
        out = list(self.dev_android) + list(self.dev_ios)
        ok = self._airplay_ok()
        for k in range(1, self.ap_slots + 1):
            name = ap_receiver_name(self.ap_name.get(), k)
            out.append({"id": f"airplay:{k}", "kind": "airplay", "slot": k, "ready": ok,
                        "title": f"iPhone по Wi-Fi · «{name}»",
                        "sub": (f"На iPhone: Пункт управления → Повтор экрана → «{name}»" if ok else
                                ("Нужен приёмник: в Терминале «brew install uxplay», затем «Обновить»" if IS_MAC
                                 else "Не найден AirPlay-приёмник — переустановите Takeone"))})
        return out

    def _open_ids(self):
        return {w.link.serial for w in self.android_wins} | set(self.ext)

    def _render_rows(self):
        body = self.dev_card.body
        for w in body.winfo_children():
            w.destroy()
        self.rows = []
        live = self._open_ids()
        if not self.dev_android:
            box = tk.Frame(body, bg=C["panel"])
            box.pack(fill="x", pady=(8, 10), padx=12)
            if self.searching:
                ui.label(box, "Ищу телефоны…", 13, C["text2"]).pack(anchor="w")
            else:
                ui.label(box, "Android-телефоны не найдены", 13, weight="semibold").pack(anchor="w")
                ui.label(box, "Подключите кабелем, включите «Отладку по USB» и нажмите «Обновить».", 12,
                         C["muted"]).pack(anchor="w", pady=(2, 0))
        for i, d in enumerate(self.devices()):
            uid = d["id"]
            title, sub = d["title"], d["sub"]
            if uid in self.ext:
                title, sub, st = self.ext[uid].row()
            elif uid in live:
                st = ("live", "В окне")
            else:
                st = ("ok", "Готов") if d["ready"] else ("wait", "Ждёт")
            r = ui.DeviceRow(body, title, sub, st, self._row_click, checked=uid in self.selected or uid in live,
                             enabled=d["ready"] and uid not in live)
            r.uid, r.ready = uid, d["ready"] and uid not in live
            r.pack(fill="x", pady=(0 if (i == 0 and self.dev_android) else 4, 0))
            self.rows.append(r)
        if IS_MAC and self.ios_error and not self.dev_ios:
            ui.label(body, f"iPhone по кабелю: {self.ios_error}", 12, C["muted"], wraplength=self.W - 40,
                     justify="left").pack(anchor="w", padx=12, pady=(8, 4))
        self._sig = self._row_sig()

    def _row_sig(self):
        return tuple((uid, x.row()) for uid, x in self.ext.items())

    def _row_click(self, row):
        if self.busy or self._rec_active() or row.uid in self._open_ids():
            return
        if row.uid in self.selected:
            self.selected.remove(row.uid)
        else:
            self.selected.append(row.uid)
        for r in self.rows:
            r.set(checked=r.uid in self.selected or r.uid in self._open_ids())
        self.update_buttons()

    def _add_slot(self):
        if self.ap_slots < AP_MAX:
            self.ap_slots += 1
            self.selected.append(f"airplay:{self.ap_slots}")
            self._render_rows()
            self.update_buttons()
            self.save_settings()

    def _remove_slot(self):
        uid = f"airplay:{self.ap_slots}"
        if self.ap_slots > 1 and uid not in self.ext:
            self.ap_slots -= 1
            if uid in self.selected:
                self.selected.remove(uid)
            self._render_rows()
            self.update_buttons()
            self.save_settings()

    def refresh(self):
        if self.busy or self.searching:
            return
        self.searching = True
        self._render_rows()
        self.update_buttons()

        def work():
            res = {}
            try:
                res["android"] = list_android()
            except Exception as e:  # noqa
                res["android_err"] = str(e)
            self.root.after(0, lambda: self._android_found(res))
            if IS_MAC:
                try:
                    res["ios"] = list_ios()
                except Exception as e:  # noqa
                    res["ios_err"] = str(e).splitlines()[0][:160]
                self.root.after(0, lambda: self._ios_found(res))
        threading.Thread(target=work, daemon=True).start()

    def _android_found(self, res):
        if not IS_MAC:
            self.searching = False
        prev = list(self.selected)
        devs = []
        for d in res.get("android", []):
            if len(d) >= 4:
                uid, title, sub, ready = d[:4]
            else:
                uid, title = d[0], d[1].split("  (")[0]
                sub, ready = d[1], "—" not in d[1]
            devs.append({"id": uid, "kind": "android", "title": title, "sub": sub, "ready": ready})
        self.dev_android = devs
        ready = [d["id"] for d in devs if d["ready"]]
        if not any(u in ready for u in prev) and not self._open_ids() and ready and \
                not any(u.startswith("airplay:") or u.startswith("ios:") for u in prev):
            self.selected = prev + ready[:1]  # по умолчанию — первый готовый телефон
        if res.get("android_err"):
            self.status.set("Android: " + res["android_err"].splitlines()[0])
        self._render_rows()
        self.update_buttons()

    def _ios_found(self, res):
        self.searching = False
        self.ios_error = res.get("ios_err")
        self.dev_ios = [{"id": f"ios:{uid}", "uid": uid, "kind": "ios", "title": name,
                         "sub": "iPhone/iPad · по кабелю", "ready": True} for uid, name in res.get("ios", [])]
        self._render_rows()
        self.update_buttons()

    # --- группы записи
    def _new_group(self):
        import android_view
        g = android_view.RecordGroup(tool("ffmpeg"), default_video_dir(), None, self.limit(), APP)
        g.dialog_parent = self.root
        g.on_done = lambda msg, folder: (self.status.set(msg), self.update_buttons())
        return g

    def _groups(self):
        out = []
        for w in self.android_wins:
            if w.group not in out:
                out.append(w.group)
        if self.shared is not None and self.shared not in out and self.shared.extras:
            out.append(self.shared)
        return out

    def _controllers(self):
        """Что управляет записью: общая группа или (раздельно) группы Android + каждое iPhone/iPad."""
        if self.together.get():
            return self._groups()
        return self._groups() + list(self.ext.values())

    def _rec_active(self):
        return any(c.state != "idle" for c in self._controllers())

    def _sync_extras(self):
        if self.together.get():
            if self.shared is None:
                self.shared = self._new_group()
            self.shared.extras = list(self.ext.values())
        elif self.shared is not None:
            self.shared.extras = []

    def _together_changed(self):
        self._together_text()
        if not self._rec_active():
            if self.together.get():
                self.shared = self._new_group()
                for w in self.android_wins:
                    w.set_group(self.shared)
            else:
                for w in self.android_wins:
                    w.set_group(self._new_group())
                self.shared = None
            self._sync_extras()
        self.save_settings()
        self.update_buttons()

    # --- открыть трансляции
    def open_devices(self):
        """Открывает окна отмеченных устройств. Если трансляция уже идёт — добавляет новые к ней."""
        if self.busy:
            return
        if self._rec_active():
            messagebox.showinfo(APP, "Сейчас идёт запись. Остановите её — после этого можно добавить устройство.")
            return
        live = self._open_ids()
        devs = {d["id"]: d for d in self.devices()}
        chosen = [devs[u] for u in self.selected if u in devs and devs[u]["ready"] and u not in live]
        if not chosen:
            messagebox.showwarning(APP, "Отметьте устройство в списке.")
            return
        android = [d for d in chosen if d["kind"] == "android"]
        ext = [ExtDevice(self, d) for d in chosen if d["kind"] != "android"]
        links = []
        if android:
            try:
                ms = int(self.max_size.get() or 0)
                br = max(1.0, float(self.bitrate.get() or 8))
            except ValueError:
                messagebox.showwarning(APP, "Проверьте числа в настройках (размер видео и битрейт).")
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
                messagebox.showerror(APP, f"Не хватает компонента для трансляции: {e}")
                return
            links = [android_view.AndroidLink(tool("adb"), d["id"], server, ver, ms, br, self.audio.get(), LOG_DIR)
                     for d in android]
        self.save_settings()

        def start_all():
            errors = []
            ok_links = []
            for ln in links:
                try:
                    ln.start()
                    ok_links.append(ln)
                except Exception as e:  # noqa
                    errors.append(f"{ln.serial}: {str(e).splitlines()[0]}")
            ok_ext = []
            for x in ext:
                try:
                    x.connect()
                    ok_ext.append(x)
                except Exception as e:  # noqa
                    errors.append(f"{x.base_title}: {str(e).splitlines()[0][:300]}")
                    x.close()
            return ok_links, ok_ext, errors

        def done(res):
            ok_links, ok_ext, errors = res
            import android_view
            if ok_links:
                if self.android_layout is None or not self.android_wins:
                    self.android_layout = android_view.WindowLayout()
                if self.together.get() and self.shared is None:
                    self.shared = self._new_group()
                frac = WIN_SIZES.get(self.win_size.get(), 0.86)
                for ln in ok_links:
                    group = self.shared if self.together.get() else self._new_group()
                    self.android_wins.append(android_view.DeviceWindow(
                        self.root, ln, group, default_shots_dir(), self._android_closed, frac, APP,
                        layout=self.android_layout, sharp=self.sharp.get()))
                self.android_layout.arranged = False
                for w in self.android_wins:
                    if w.dev_size:
                        w._fit_window()
            for x in ok_ext:
                self.ext[x.id] = x
            self._sync_extras()
            for g in self._groups():
                g.limit_mb = self.limit()
                g.refresh()
            self.selected = [u for u in self.selected if u not in self._open_ids()]
            self.status.set("")
            self._render_rows()
            if errors:
                messagebox.showerror(APP, "Не все устройства подключились:\n\n" + "\n\n".join(errors))

        n = len(chosen)
        self.bg(start_all, done, None, f"Подключаю {n} {ui.plural(n, 'устройство', 'устройства', 'устройств')}…")

    def _android_closed(self, win):
        if win in self.android_wins:
            self.android_wins.remove(win)
        self._after_close()

    def _after_close(self):
        if not self.android_wins:
            self.android_layout = None
        if not self.android_wins and not self.ext:
            self.shared = None
            if self._quitting:
                self.shutdown()
                return
        self._render_rows()
        self.update_buttons()

    def close_all(self):
        if self._rec_active():
            return
        for x in list(self.ext.values()):
            x.close()
        self.ext.clear()
        self._sync_extras()
        for w in list(self.android_wins):
            w.close()
        self._after_close()

    # --- запись (кнопки главного окна управляют всеми устройствами)
    def start_rec(self):
        if self.platform.get() == "Edit":
            return
        for c in self._controllers():
            c.start()
        self.update_buttons()

    def toggle_pause(self):
        if self.platform.get() == "Edit":
            return
        cs = self._controllers()
        want = "recording" if any(c.state == "recording" for c in cs) else "paused"
        for c in cs:
            if c.state == want:
                c.toggle_pause()
        self.update_buttons()

    def stop_rec(self):
        if self.platform.get() == "Edit":
            return
        for c in list(self._controllers()):
            if c.state in ("recording", "paused"):
                if hasattr(c, "members"):
                    c.stop(parent=c.members[0] if c.members else self.root)
                else:
                    c.stop(parent=self.root)
        self.update_buttons()

    def _pill_click(self):
        cs = self._controllers()
        if any(c.state == "paused" for c in cs) and not any(c.state == "recording" for c in cs):
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

    def _status_text(self):
        if self.status.get():
            return self.status.get()
        if self._open_ids():
            return "● Запись, пауза и стоп — здесь, сразу для всех устройств. Звук и скриншот — в окне Android."
        return "Отметьте устройства и нажмите «Открыть трансляцию», потом «● Запись»."

    def update_buttons(self):
        b = self.busy
        plat = self.platform.get()
        live = self._open_ids()
        rec = self._rec_active()
        self.tabs.set_enabled(True)
        self.tabs.set_disabled({})
        if plat == "Edit":
            self.editor.changed()
            return
        self.btn_refresh.set(enabled=not b and not self.searching)
        for r in self.rows:
            r.set(enabled=not b and not rec and r.ready)
        for f in (self.f_size, self.f_rate, self.f_ap):
            f.set_enabled(not b)
        self.f_limit.set_enabled(not rec)
        self.seg_size.set_enabled(not b)
        self.tg_audio.set_enabled(not b)
        self.tg_together.set_enabled(not b and not rec)
        if self.tg_sharp:
            self.tg_sharp.set_enabled(not b)
        self.btn_more_ap.set(enabled=self.ap_slots < AP_MAX and not rec)
        if self.ap_slots > 1:
            if not self.btn_less_ap.winfo_ismapped():
                self.btn_less_ap.pack(side="left", padx=(6, 0), before=self.ap_note)
            self.btn_less_ap.set(enabled=f"airplay:{self.ap_slots}" not in self.ext and not rec)
        elif self.btn_less_ap.winfo_ismapped():
            self.btn_less_ap.pack_forget()

        devs = {d["id"]: d for d in self.devices()}
        sel = [u for u in self.selected if u in devs and devs[u]["ready"] and u not in live]
        n, k = len(sel), len(live)
        many = "запись пойдёт в одно видео без звука." if self.together.get() else "у каждого — свой файл со звуком."
        if live:
            hint = ("Идёт запись — добавить устройство можно после её остановки." if rec else
                    f"Открыто: {k}. Отметьте ещё устройство, чтобы добавить его." if n == 0 else
                    f"Добавится {n} — всего станет {k + n}, {many}")
        else:
            hint = ("Отметьте устройства, которые нужно показать и записать." if n == 0 else
                    "Окно откроется с панелью записи." if n == 1 else
                    f"Отмечено {n} — окна откроются рядом, {many}")
        if any(u.startswith("airplay:") for u in sel):
            hint += " iPhone по Wi-Fi появится, когда вы включите на нём «Повтор экрана»."
        self.dev_hint.configure(text=hint)

        for w in (self.btn_main, self.recbar, self.status_lbl):
            w.pack_forget()
        if live:
            self.recbar.pack(fill="x")
            self._update_bar()
            if n:
                self.btn_main.set(text=f"Добавить {n} {ui.plural(n, 'трансляцию', 'трансляции', 'трансляций')}",
                                  enabled=not rec and not b)
                self.btn_main.pack(fill="x", pady=(8, 0))
        else:
            text = ("Открыть трансляцию" if n <= 1 else
                    f"Открыть {n} {ui.plural(n, 'трансляцию', 'трансляции', 'трансляций')}")
            self.btn_main.set(text=text, enabled=n > 0 and not b)
            self.btn_main.pack(fill="x")
        self.status_lbl.configure(text=self._status_text())
        self.status_lbl.pack(fill="x", pady=(10, 0))

    def _update_bar(self):
        cs = self._controllers()
        st = [c.state for c in cs]
        t = max((c.time() for c in cs), default=0.0)
        n = len(self._open_ids())
        if "saving" in st and not any(x in ("recording", "paused") for x in st):
            self.pill.set("saving", "Сохранение", enabled=False, spin=int(time.monotonic() * 12) % 12 * 30)
        elif "recording" in st:
            self.pill.set("recording", ui.fmt_hms(t) + (f" ×{n}" if n > 1 and self.together.get() else ""),
                          blink=int(time.monotonic() * 2) % 2 == 0)
        elif "paused" in st:
            self.pill.set("paused", ui.fmt_hms(t))
        else:
            ready = any(c.ready() for c in cs)
            self.pill.set("idle", f"Запись ×{n}" if n > 1 and self.together.get() else "Запись", enabled=ready)
        active = any(x in ("recording", "paused") for x in st)
        self.b_pause.set("play" if ("paused" in st and "recording" not in st) else "pause", active,
                         "Продолжить  (Ctrl+P)" if "paused" in st else "Пауза  (Ctrl+P)")
        self.b_stop.set(enabled=active, tone="raised" if active else "")
        self.btn_disc.set(enabled=not active and "saving" not in st and not self.busy)

    def limit(self):
        try:
            return max(0.0, float(self.limit_mb.get().replace(",", ".") or 0))
        except ValueError:
            return 0.0

    def video_dir(self):
        return default_video_dir()

    def ffmpeg(self):
        return tool("ffmpeg")

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
        save_settings({"geom": self._geometry() or load_settings().get("geom"), "platform": self.platform.get(),
                       "max_size": self.max_size.get(), "bitrate": self.bitrate.get(), "audio": self.audio.get(),
                       "ap_name": self.ap_name.get(), "ap_slots": self.ap_slots, "limit_mb": self.limit_mb.get(),
                       "win_size": self.win_size.get(), "together": self.together.get(),
                       "sharp": self.sharp.get(), "more_open": self.more_open})

    # --- таймер
    def tick(self):
        # закрытые пользователем окна iPhone/iPad (хелпер или приёмник завершился)
        gone = [uid for uid, x in self.ext.items() if not x.alive() and x.state == "idle"]
        for uid in gone:
            self.ext.pop(uid).close()
        if gone:
            self._sync_extras()
            self._after_close()
        if self.ext and self._row_sig() != self._sig:
            self._render_rows()  # iPhone подключился по Wi-Fi — показать его имя
        cs = self._controllers()
        gs = tuple(c.state for c in cs)
        if gs != getattr(self, "_states", None):
            self._states = gs
            self.update_buttons()
        elif self._open_ids() and self.platform.get() == "Phones":
            self._update_bar()
        for g in self._groups():
            if g.state == "idle":
                g.limit_mb = self.limit()
        self.root.after(300, self.tick)

    def on_close(self):
        if self.busy:
            return
        if self._rec_active():
            messagebox.showinfo(APP, "Идёт запись. Остановите и сохраните её, потом закройте Takeone.")
            return
        for x in list(self.ext.values()):
            x.close()
        self.ext.clear()
        if self.android_wins:
            self._quitting = True
            for w in list(self.android_wins):
                w.close()
            return
        self.shutdown()

    def shutdown(self):
        try:
            self.save_settings()
        except Exception:  # noqa
            pass
        for x in list(self.ext.values()):
            x.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

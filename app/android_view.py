# -*- coding: utf-8 -*-
"""
Takeone — окно трансляции Android.

Подключается к телефону напрямую через scrcpy-server (тот же сервер, что у scrcpy),
показывает экран в своём окне с панелью управления, передаёт касания/клавиши,
проигрывает звук, делает скриншоты и пишет видео с настоящей паузой
(без перекодирования: пакеты H.264/AAC с телефона складываются в MKV,
при сохранении ffmpeg перепаковывает в MP4).
"""
import collections
import datetime
import os
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
NOWIN = 0x08000000 if IS_WIN else 0
DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"

# --- протокол scrcpy 5.x -------------------------------------------------------
CODEC_H264 = 0x68323634
CODEC_AAC = 0x00616163
FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY = 1 << 61
PTS_MASK = FLAG_KEY - 1

MSG_KEYCODE, MSG_TEXT, MSG_TOUCH, MSG_SCROLL, MSG_RESET_VIDEO = 0, 1, 2, 3, 17
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
POINTER_FINGER = (1 << 64) - 2  # SC_POINTER_ID_GENERIC_FINGER (-2)
KEY_HOME, KEY_BACK, KEY_APP_SWITCH = 3, 4, 187
KEYMAP = {  # Tk keysym -> Android keycode
    "Return": 66, "KP_Enter": 66, "BackSpace": 67, "Delete": 112, "Tab": 61,
    "Escape": KEY_BACK, "Left": 21, "Right": 22, "Up": 19, "Down": 20,
    "Home": 122, "End": 123, "Prior": 92, "Next": 93,
}


def msg_touch(action, x, y, w, h):
    pressure = 0 if action == ACTION_UP else 0xFFFF
    return struct.pack(">BBQiiHHHII", MSG_TOUCH, action, POINTER_FINGER, x, y, w, h, pressure, 0, 0)


def _i16_fixed(v):
    v = max(-1.0, min(1.0, v))
    return 0x7FFF if v >= 1.0 else int(v * 0x8000)


def msg_scroll(x, y, w, h, hscroll, vscroll):
    # сервер умножает декодированное значение на 16 (диапазон [-16, 16])
    return struct.pack(">BiiHHhhI", MSG_SCROLL, x, y, w, h,
                       _i16_fixed(hscroll / 16), _i16_fixed(vscroll / 16), 0)


def msg_key(action, keycode):
    return struct.pack(">BBiii", MSG_KEYCODE, action, keycode, 0, 0)


def msg_text(text):
    data = text.encode("utf-8")[:300]
    return struct.pack(">BI", MSG_TEXT, len(data)) + data


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("соединение закрыто")
        buf += chunk
    return bytes(buf)


def _tail(path, n=1500):
    try:
        with open(path, "rb") as f:
            return f.read()[-n:].decode("utf-8", "replace")
    except OSError:
        return ""


class AndroidLink:
    """Запуск scrcpy-server на телефоне и три сокета: видео, звук, управление."""

    def __init__(self, adb, serial, server_path, server_version, max_size, bitrate_mbps, audio, log_dir):
        self.adb, self.serial = adb, serial
        self.server_path, self.server_version = server_path, server_version
        self.max_size, self.bitrate, self.audio = max_size, bitrate_mbps, audio
        self.log = os.path.join(log_dir, "android_server.log")
        self.proc = None
        self.port = None
        self.video = self.audio_sock = self.control = None
        self.device_name = serial
        self._send_lock = threading.Lock()
        self._closing = False
        self._ended = False

    # --- запуск
    def _adb(self, *args, timeout=30):
        r = subprocess.run([self.adb, "-s", self.serial, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=NOWIN)
        if r.returncode:
            raise RuntimeError(f"adb {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-800:]}")
        return r.stdout

    def start(self):
        if not self.server_path or not os.path.exists(self.server_path):
            raise RuntimeError("Не найден scrcpy-server. Переустановите Takeone.")
        self._adb("push", self.server_path, DEVICE_SERVER_PATH, timeout=60)
        scid = random.randint(1, 0x7FFFFFFF)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self._adb("forward", f"tcp:{self.port}", f"localabstract:scrcpy_{scid:08x}")
        args = [f"scid={scid:08x}", "log_level=info", "tunnel_forward=true",
                f"video_bit_rate={int(self.bitrate * 1_000_000)}", "video_codec=h264",
                "control=true", "stay_awake=true", "clipboard_autosync=false",
                "power_off_on_close=false"]
        if self.max_size > 0:
            args.append(f"max_size={self.max_size}")
        args += ["audio=true", "audio_codec=aac"] if self.audio else ["audio=false"]
        cmd = [self.adb, "-s", self.serial, "shell", f"CLASSPATH={DEVICE_SERVER_PATH}", "app_process",
               "/", "com.genymobile.scrcpy.Server", self.server_version, *args]
        self._logf = open(self.log, "wb")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self._logf,
                                     stderr=subprocess.STDOUT, creationflags=NOWIN)
        self._connect()

    def _connect(self, host="127.0.0.1"):
        deadline = time.time() + 15
        while True:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError("Сервер на телефоне не запустился.\n\n" + _tail(self.log))
            s = None
            try:
                s = socket.create_connection((host, self.port), timeout=2)
                s.settimeout(2)
                if s.recv(1) == b"\x00":  # «пустой» байт = сервер действительно на связи
                    break
            except OSError:
                pass
            if s:
                s.close()
            if time.time() > deadline:
                raise RuntimeError("Телефон не ответил. Разблокируйте экран и попробуйте ещё раз.\n\n"
                                   + _tail(self.log))
            time.sleep(0.1)
        self.video = s
        if self.audio:
            self.audio_sock = socket.create_connection((host, self.port), timeout=5)
        self.control = socket.create_connection((host, self.port), timeout=5)
        self.control.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.video.settimeout(10)
        name = recv_exact(self.video, 64).split(b"\0", 1)[0].decode("utf-8", "replace")
        self.device_name = name or self.serial
        for so in (self.video, self.audio_sock, self.control):
            if so:
                so.settimeout(None)

    # --- потоки чтения
    def run(self, on_session, on_video, on_audio_config, on_audio, on_ended):
        self.cb = dict(session=on_session, video=on_video, aconf=on_audio_config, audio=on_audio,
                       ended=on_ended)
        threading.Thread(target=self._video_loop, daemon=True).start()
        if self.audio_sock:
            threading.Thread(target=self._audio_loop, daemon=True).start()
        threading.Thread(target=self._control_drain, daemon=True).start()

    def _end(self, reason):
        if not self._ended and not self._closing:
            self._ended = True
            self.cb["ended"](reason)

    def _video_loop(self):
        try:
            codec = struct.unpack(">I", recv_exact(self.video, 4))[0]
            if codec in (0, 1):
                raise RuntimeError("телефон не смог запустить видеопоток")
            if codec != CODEC_H264:
                raise RuntimeError(f"неподдерживаемый видеокодек {codec:#x}")
            config = b""
            pending = b""
            while True:
                hdr = recv_exact(self.video, 12)
                if hdr[0] & 0x80:  # метаданные сессии: новый размер кадра
                    _, w, h = struct.unpack(">III", hdr)
                    self.cb["session"](w, h)
                    continue
                ptsf, size = struct.unpack(">QI", hdr)
                data = recv_exact(self.video, size)
                if ptsf & FLAG_CONFIG:
                    config = pending = data
                    continue
                if pending:
                    data, pending = pending + data, b""
                self.cb["video"](data, ptsf & PTS_MASK, bool(ptsf & FLAG_KEY), config)
        except Exception as e:  # noqa
            self._end(str(e))

    def _audio_loop(self):
        try:
            codec = struct.unpack(">I", recv_exact(self.audio_sock, 4))[0]
            if codec == 0:
                return  # телефон не может отдать звук (Android < 11) — работаем без него
            if codec == 1:
                raise RuntimeError("ошибка звукового потока")
            if codec != CODEC_AAC:
                return
            while True:
                ptsf, size = struct.unpack(">QI", recv_exact(self.audio_sock, 12))
                data = recv_exact(self.audio_sock, size)
                if ptsf & FLAG_CONFIG:
                    self.cb["aconf"](data)
                else:
                    self.cb["audio"](data, ptsf & PTS_MASK)
        except Exception:  # noqa
            pass  # звук не критичен; конец сессии ловит видеопоток

    def _control_drain(self):
        try:
            while self.control.recv(4096):
                pass
        except OSError:
            pass

    # --- управление
    def send(self, data):
        if self._ended or not self.control:
            return
        try:
            with self._send_lock:
                self.control.sendall(data)
        except OSError:
            pass

    def reset_video(self):
        self.send(bytes([MSG_RESET_VIDEO]))

    def key(self, keycode):
        self.send(msg_key(ACTION_DOWN, keycode) + msg_key(ACTION_UP, keycode))

    def stop(self):
        self._closing = True
        for so in (self.video, self.audio_sock, self.control):
            if so:
                try:
                    so.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                so.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.port:
            try:
                subprocess.run([self.adb, "-s", self.serial, "forward", "--remove", f"tcp:{self.port}"],
                               capture_output=True, timeout=5, creationflags=NOWIN)
            except Exception:  # noqa
                pass
        try:
            self._logf.close()
        except Exception:  # noqa
            pass


# --- декодирование и звук ----------------------------------------------------------
class VideoDecoder:
    def __init__(self):
        import av
        self.av = av
        self.ctx = av.CodecContext.create("h264", "r")
        try:
            self.ctx.thread_type = "SLICE"  # без задержки на кадр, как у frame-threading
        except Exception:  # noqa
            pass
        self.lock = threading.Lock()
        self.frame = None
        self.seq = 0

    def decode(self, data):
        try:
            frames = self.ctx.decode(self.av.Packet(data))
        except self.av.error.FFmpegError:
            return
        for f in frames:
            with self.lock:
                self.frame = f
                self.seq += 1

    def latest(self):
        with self.lock:
            return self.frame, self.seq


class AudioPlayer:
    """AAC с телефона -> PCM 48 кГц -> динамики компьютера. Буфер не больше ~0.3 с."""
    RATE = 48000

    def __init__(self):
        import av
        import numpy as np
        import sounddevice as sd
        self.av, self.np = av, np
        self.dec = None
        self.res = av.AudioResampler(format="s16", layout="stereo", rate=self.RATE)
        self.buf = collections.deque()
        self.avail = 0
        self.lock = threading.Lock()
        self.muted = False
        self.priming = True
        self.stream = sd.OutputStream(samplerate=self.RATE, channels=2, dtype="int16",
                                      callback=self._cb, latency="low")
        self.stream.start()

    def set_config(self, asc):
        self.dec = self.av.CodecContext.create("aac", "r")
        self.dec.extradata = asc

    def push(self, data):
        if self.muted or self.dec is None:
            return
        try:
            frames = self.dec.decode(self.av.Packet(data))
        except self.av.error.FFmpegError:
            return
        for f in frames:
            for rf in self.res.resample(f):
                arr = rf.to_ndarray().reshape(-1, 2)
                with self.lock:
                    self.buf.append(arr)
                    self.avail += len(arr)
                    while self.avail > self.RATE * 0.3 and len(self.buf) > 1:
                        self.avail -= len(self.buf.popleft())

    def _cb(self, out, frames, time_info, status):
        with self.lock:
            if self.muted:
                self.buf.clear()
                self.avail = 0
            if self.priming and self.avail < self.RATE * 0.06:
                out.fill(0)
                return
            self.priming = False
            pos = 0
            while pos < frames and self.buf:
                chunk = self.buf[0]
                n = min(frames - pos, len(chunk))
                out[pos:pos + n] = chunk[:n]
                pos += n
                if n == len(chunk):
                    self.buf.popleft()
                else:
                    self.buf[0] = chunk[n:]
                self.avail -= n
            if pos < frames:
                out[pos:].fill(0)
                self.priming = True

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa
            pass


# --- запись: MKV без перекодирования --------------------------------------------------
def split_nals(data):
    starts = []
    i = 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        starts.append(j + 3)
        i = j + 3
    if not starts:
        return [data] if data else []
    nals = []
    for k, s in enumerate(starts):
        last = k + 1 == len(starts)
        nal = data[s:] if last else data[s:starts[k + 1] - 3].rstrip(b"\x00")
        if nal:
            nals.append(nal)
    return nals


def avcc_from_config(config):
    nals = split_nals(config)
    sps = [n for n in nals if n[0] & 0x1F == 7]
    pps = [n for n in nals if n[0] & 0x1F == 8]
    if not sps or not pps:
        return None
    out = bytes([1, sps[0][1], sps[0][2], sps[0][3], 0xFF, 0xE0 | len(sps)])
    for s in sps:
        out += len(s).to_bytes(2, "big") + s
    out += bytes([len(pps)])
    for p in pps:
        out += len(p).to_bytes(2, "big") + p
    return out


def _el(eid, payload):
    return eid + b"\x01" + len(payload).to_bytes(7, "big") + payload


def _uint(eid, v):
    return _el(eid, v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big"))


def _str(eid, s):
    return _el(eid, s.encode())


class MkvWriter:
    """Минимальный потоковый Matroska: дорожка 1 — H.264, дорожка 2 — AAC.
    Кластеры пишутся раз в секунду, поэтому при сбое теряется не больше секунды."""

    def __init__(self, path, avcc, width, height, asc):
        self.f = open(path, "wb")
        self.has_audio = asc is not None
        self.f.write(_el(b"\x1A\x45\xDF\xA3",
                         _uint(b"\x42\x86", 1) + _uint(b"\x42\xF7", 1) + _uint(b"\x42\xF2", 4) +
                         _uint(b"\x42\xF3", 8) + _str(b"\x42\x82", "matroska") +
                         _uint(b"\x42\x87", 4) + _uint(b"\x42\x85", 2)))
        self.f.write(b"\x18\x53\x80\x67\x01\xFF\xFF\xFF\xFF\xFF\xFF\xFF")  # Segment неизвестной длины
        self.f.write(_el(b"\x15\x49\xA9\x66", _uint(b"\x2A\xD7\xB1", 1_000_000) +
                         _str(b"\x4D\x80", "Takeone") + _str(b"\x57\x41", "Takeone")))
        video = _el(b"\xAE", _uint(b"\xD7", 1) + _uint(b"\x73\xC5", 1) + _uint(b"\x83", 1) +
                    _uint(b"\x9C", 0) + _str(b"\x86", "V_MPEG4/ISO/AVC") + _el(b"\x63\xA2", avcc) +
                    _el(b"\xE0", _uint(b"\xB0", width) + _uint(b"\xBA", height)))
        tracks = video
        if self.has_audio:
            tracks += _el(b"\xAE", _uint(b"\xD7", 2) + _uint(b"\x73\xC5", 2) + _uint(b"\x83", 2) +
                          _uint(b"\x9C", 0) + _str(b"\x86", "A_AAC") + _el(b"\x63\xA2", asc) +
                          _el(b"\xE1", _el(b"\xB5", struct.pack(">d", 48000.0)) + _uint(b"\x9F", 2)))
        self.f.write(_el(b"\x16\x54\xAE\x6B", tracks))
        self.cluster_ts = None
        self.blocks = []
        self.frames = 0

    def _flush(self):
        if self.blocks:
            self.f.write(_el(b"\x1F\x43\xB6\x75", _uint(b"\xE7", self.cluster_ts) + b"".join(self.blocks)))
            self.f.flush()
        self.blocks = []
        self.cluster_ts = None

    def _block(self, track, ts_ms, key, payload):
        if self.cluster_ts is not None:
            rel = ts_ms - self.cluster_ts
            if rel > 1000 or rel < -30000 or (key and track == 1 and rel > 0):
                self._flush()
        if self.cluster_ts is None:
            self.cluster_ts = max(0, ts_ms)
        rel = ts_ms - self.cluster_ts
        self.blocks.append(_el(b"\xA3", bytes([0x80 | track]) + struct.pack(">hB", rel, 0x80 if key else 0)
                               + payload))

    def video(self, ts_us, key, annexb):
        payload = b"".join(len(n).to_bytes(4, "big") + n for n in split_nals(annexb))
        self._block(1, ts_us // 1000, key, payload)
        self.frames += 1

    def audio(self, ts_us, data):
        if self.has_audio:
            self._block(2, ts_us // 1000, True, data)

    def close(self):
        self._flush()
        self.f.close()


class Recorder:
    """Пауза = пакеты не пишутся; после «Продолжить» время сдвигается так, чтобы паузы не было видно.
    Каждый отрезок начинается с ключевого кадра (окно просит телефон выдать его сразу)."""
    FRAME_US = 16_667

    def __init__(self, path, size, audio_asc):
        self.path, self.size, self.asc = path, size, audio_asc
        self.lock = threading.Lock()
        self.mkv = None
        self.active = False
        self.waiting_key = True
        self.offset = 0
        self.last_rec = None
        self.audio_from = None
        self.prev_pts = None
        self.gap = self.FRAME_US  # реальный интервал между кадрами — им же «сшиваем» паузу

    def start(self):
        with self.lock:
            self.active, self.waiting_key = True, True

    def pause(self):
        with self.lock:
            self.active = False

    def resume(self):
        with self.lock:
            self.active, self.waiting_key = True, True

    def video(self, data, pts, key, config):
        with self.lock:
            if self.prev_pts is not None and 1000 <= pts - self.prev_pts <= 100_000:
                self.gap = pts - self.prev_pts
            self.prev_pts = pts
            if not self.active:
                return
            if self.waiting_key:
                if not key:
                    return
                if self.mkv is None:
                    avcc = avcc_from_config(config)
                    if avcc is None:
                        return
                    self.mkv = MkvWriter(self.path, avcc, self.size[0], self.size[1], self.asc)
                self.offset = pts - (0 if self.last_rec is None else self.last_rec + self.gap)
                self.audio_from = pts
                self.waiting_key = False
            rec = pts - self.offset
            if self.last_rec is not None and rec <= self.last_rec:
                return
            self.mkv.video(rec, key, data)
            self.last_rec = rec

    def audio(self, data, pts):
        with self.lock:
            if not self.active or self.waiting_key or self.mkv is None or pts < self.audio_from:
                return
            rec = pts - self.offset
            if rec >= 0:
                self.mkv.audio(rec, data)

    def stop(self):
        with self.lock:
            self.active = False
            if self.mkv:
                self.mkv.close()
            return self.mkv is not None and self.mkv.frames > 0


# --- интерфейс -------------------------------------------------------------------
BAR = "#5B5FD6"
BAR_HOVER = "#6E72E2"
BAR_PRESS = "#4B4FC2"
ICON = "#FFFFFF"
ICON_OFF = "#A9ACEB"
REC_RED = "#FF4A4A"
VIDEO_BG = "#101014"


def _rounded(c, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


def ico_record(c, x, y, col):
    c.create_oval(x - 9, y - 9, x + 9, y + 9, fill=REC_RED if col == ICON else BAR_HOVER, outline=col, width=2)


def ico_pause(c, x, y, col):
    _rounded(c, x - 7, y - 8, x - 2, y + 8, 2, fill=col, outline="")
    _rounded(c, x + 2, y - 8, x + 7, y + 8, 2, fill=col, outline="")


def ico_play(c, x, y, col):
    c.create_polygon(x - 5, y - 9, x - 5, y + 9, x + 9, y, fill=col, outline=col, width=2, joinstyle="round")


def ico_stop(c, x, y, col):
    _rounded(c, x - 7, y - 7, x + 7, y + 7, 3, fill=col, outline="")


def ico_camera(c, x, y, col):
    _rounded(c, x - 11, y - 6, x + 11, y + 9, 4, fill="", outline=col, width=2)
    c.create_polygon(x - 5, y - 6, x - 3, y - 10, x + 3, y - 10, x + 5, y - 6, fill=col, outline=col)
    c.create_oval(x - 4, y - 2, x + 4, y + 6, outline=col, width=2)


def _speaker(c, x, y, col):
    c.create_polygon(x - 11, y - 4, x - 6, y - 4, x, y - 10, x, y + 10, x - 6, y + 4, x - 11, y + 4,
                     fill=col, outline=col, joinstyle="round")


def ico_sound(c, x, y, col):
    _speaker(c, x - 1, y, col)
    c.create_arc(x - 3, y - 6, x + 7, y + 6, start=-55, extent=110, style="arc", outline=col, width=2)
    c.create_arc(x - 3, y - 11, x + 12, y + 11, start=-55, extent=110, style="arc", outline=col, width=2)


def ico_muted(c, x, y, col):
    _speaker(c, x - 3, y, col)
    c.create_line(x + 4, y - 5, x + 12, y + 5, fill=col, width=2, capstyle="round")
    c.create_line(x + 4, y + 5, x + 12, y - 5, fill=col, width=2, capstyle="round")


def ico_back(c, x, y, col):
    c.create_line(x + 4, y - 9, x - 5, y, x + 4, y + 9, fill=col, width=3, capstyle="round", joinstyle="round")


def ico_home(c, x, y, col):
    c.create_oval(x - 9, y - 9, x + 9, y + 9, outline=col, width=2.5)


def ico_recents(c, x, y, col):
    for dy in (-6, 0, 6):
        c.create_line(x - 10, y + dy, x + 10, y + dy, fill=col, width=2.5, capstyle="round")


class IconButton(tk.Canvas):
    def __init__(self, parent, icon, command, tip="", size=40):
        super().__init__(parent, width=size, height=size, bg=BAR, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.icon, self.command, self.tip, self.size = icon, command, tip, size
        self.enabled = True
        self.hover = False
        self._tipwin = None
        self._tipjob = None
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", lambda e: self._draw(BAR_PRESS))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def set(self, icon=None, enabled=None, tip=None):
        if icon is not None:
            self.icon = icon
        if enabled is not None:
            self.enabled = enabled
        if tip is not None:
            self.tip = tip
        self._draw()

    def _draw(self, bg=None):
        self.delete("all")
        s = self.size
        if bg is None and self.hover and self.enabled:
            bg = BAR_HOVER
        if bg and self.enabled:
            _rounded(self, 2, 2, s - 2, s - 2, 9, fill=bg, outline="")
        self.icon(self, s // 2, s // 2, ICON if self.enabled else ICON_OFF)

    def _enter(self, _):
        self.hover = True
        self._draw()
        if self.tip:
            self._tipjob = self.after(600, self._show_tip)

    def _leave(self, _):
        self.hover = False
        self._draw()
        if self._tipjob:
            self.after_cancel(self._tipjob)
            self._tipjob = None
        if self._tipwin:
            self._tipwin.destroy()
            self._tipwin = None

    def _show_tip(self):
        self._tipwin = tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tk.Label(tw, text=self.tip, bg="#2B2B33", fg="white", padx=8, pady=4,
                 font=("TkDefaultFont", 11)).pack()
        tw.update_idletasks()
        x = self.winfo_rootx() + self.size // 2 - tw.winfo_width() // 2
        tw.wm_geometry(f"+{max(0, x)}+{self.winfo_rooty() + self.size + 4}")

    def _release(self, e):
        self._draw()
        if self.enabled and 0 <= e.x < self.size and 0 <= e.y < self.size:
            self.command()


def open_folder(path):
    try:
        if IS_WIN:
            os.startfile(path)  # noqa
        elif IS_MAC:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:  # noqa
        pass


class DeviceWindow(tk.Toplevel):
    BAR_H = 48

    def __init__(self, master, link, ffmpeg, shots_dir, video_dir, tmp_root, on_closed, app_name="Takeone"):
        super().__init__(master, bg=VIDEO_BG)
        self.link, self.ffmpeg = link, ffmpeg
        self.shots_dir, self.video_dir, self.tmp_root = shots_dir, video_dir, tmp_root
        self.on_closed, self.app_name = on_closed, app_name
        self.title(f"{link.device_name} — {app_name}")
        self.minsize(260, 360)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.decoder = VideoDecoder()
        self.player = None
        if link.audio:
            try:
                self.player = AudioPlayer()
            except Exception:  # noqa — нет звуковой карты/драйвера
                self.player = None
        self.asc = None
        self.dev_size = None
        self.sized_for = None
        self.shown_seq = -1
        self.photo = None
        self.img_rect = None
        self.touching = False
        self.ended = None
        self.recorder = None
        self.rec_state = "idle"  # idle | recording | paused | saving
        self.rec_elapsed = 0.0
        self.rec_since = None
        self.rec_tmp = None
        self._toast_job = None
        self._closing = False

        self._build()
        self._bind_input()
        link.run(self._on_session, self._on_video, self._on_audio_config, self._on_audio, self._on_ended)
        self.after(10, self._tick)

    # --- построение окна
    def _build(self):
        top = tk.Frame(self, bg=BAR, height=self.BAR_H)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        left = tk.Frame(top, bg=BAR)
        left.pack(side="left", padx=6)
        self.b_rec = IconButton(left, ico_record, self.rec_or_pause, "Начать запись  (Ctrl+R)")
        self.b_stop = IconButton(left, ico_stop, self.stop_rec, "Остановить и сохранить  (Ctrl+S)")
        self.b_rec.pack(side="left")
        self.b_stop.pack(side="left")
        self.rec_dot = tk.Label(left, text="●", bg=BAR, fg=BAR, font=("TkDefaultFont", 12))
        self.rec_dot.pack(side="left", padx=(4, 0))
        self.rec_time = tk.Label(left, text="", bg=BAR, fg=ICON, font=("TkFixedFont", 13, "bold"))
        self.rec_time.pack(side="left", padx=(2, 8))
        sep = tk.Frame(left, bg=BAR_HOVER, width=1, height=24)
        sep.pack(side="left", padx=4)
        self.b_shot = IconButton(left, ico_camera, self.screenshot, "Скриншот  (Ctrl+Shift+S)")
        self.b_shot.pack(side="left")
        self.b_sound = IconButton(left, ico_sound, self.toggle_sound, "Звук на компьютере  (Ctrl+M)")
        self.b_sound.pack(side="left")
        self.name = tk.Label(top, text=self.link.device_name, bg=BAR, fg=ICON,
                             font=("TkDefaultFont", 13, "bold"))
        self.name.pack(side="right", padx=12)

        bottom = tk.Frame(self, bg=BAR, height=self.BAR_H)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)
        nav = tk.Frame(bottom, bg=BAR)
        nav.place(relx=0.5, rely=0.5, anchor="center")
        for icon, code, tip in ((ico_back, KEY_BACK, "Назад  (Esc / правая кнопка мыши)"),
                                (ico_home, KEY_HOME, "Домой  (средняя кнопка мыши)"),
                                (ico_recents, KEY_APP_SWITCH, "Недавние приложения")):
            IconButton(nav, icon, lambda c=code: self.link.key(c), tip).pack(side="left", padx=22)

        self.canvas = tk.Canvas(self, bg=VIDEO_BG, highlightthickness=0, bd=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.img_item = self.canvas.create_image(0, 0, anchor="nw")
        self.msg_item = self.canvas.create_text(0, 0, text="Подключение…", fill="#9EA0B8",
                                                font=("TkDefaultFont", 14), justify="center")
        self.flash_item = None
        self.canvas.bind("<Configure>", lambda e: self._place_msg())
        self._update_controls()
        self.geometry("420x860")

    def _place_msg(self):
        self.canvas.coords(self.msg_item, self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2)
        self.shown_seq = -1  # перерисовать кадр под новый размер

    def _update_controls(self):
        s = self.rec_state
        ok = self.ended is None
        if s == "recording":
            self.b_rec.set(ico_pause, ok, "Пауза  (Ctrl+P)")
        elif s == "paused":
            self.b_rec.set(ico_play, ok, "Продолжить запись  (Ctrl+P)")
        else:
            self.b_rec.set(ico_record, ok and s == "idle", "Начать запись  (Ctrl+R)")
        self.b_stop.set(enabled=s in ("recording", "paused"))
        self.b_shot.set(enabled=ok)
        if self.player is None:
            self.b_sound.set(ico_muted, False, "Звук недоступен (нужен Android 11+ и включённый звук)")
        else:
            self.b_sound.set(ico_muted if self.player.muted else ico_sound, ok)

    # --- данные от телефона (потоки)
    def _on_session(self, w, h):
        self.dev_size = (w, h)

    def _on_video(self, data, pts, key, config):
        rec = self.recorder
        if rec:
            rec.video(data, pts, key, config)
        self.decoder.decode(data)

    def _on_audio_config(self, asc):
        self.asc = asc
        if self.player:
            self.player.set_config(asc)

    def _on_audio(self, data, pts):
        rec = self.recorder
        if rec:
            rec.audio(data, pts)
        if self.player:
            self.player.push(data)

    def _on_ended(self, reason):
        self.ended = reason or "соединение закрыто"

    # --- главный цикл окна
    def _tick(self):
        if self._closing:
            return
        if self.dev_size and self.dev_size != self.sized_for:
            self._fit_window()
        frame, seq = self.decoder.latest()
        if frame is not None and seq != self.shown_seq:
            self._show(frame)
            self.shown_seq = seq
        self._update_timer()
        if self.ended is not None and self.ended is not True:
            self.ended_reason, self.ended = self.ended, True
            self.canvas.itemconfigure(self.msg_item, text="Устройство отключено")
            self.canvas.itemconfigure(self.img_item, state="hidden")
            self.canvas.tag_raise(self.msg_item)
            self._update_controls()
            if self.rec_state in ("recording", "paused"):
                self.stop_rec()
        self.after(8, self._tick)

    def _fit_window(self):
        w, h = self.dev_size
        first = self.sized_for is None
        self.sized_for = self.dev_size
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        max_h = int(sh * 0.86) - 2 * self.BAR_H - 40
        max_w = int(sw * 0.86)
        if first or (w > h) != (self.canvas.winfo_width() > self.canvas.winfo_height()):
            if h >= w:
                vh = min(max_h, h)
                vw = vh * w // h
            else:
                vw = min(max_w, w)
                vh = vw * h // w
                if vh > max_h:
                    vh = max_h
                    vw = vh * w // h
            self.geometry(f"{max(vw, 300)}x{vh + 2 * self.BAR_H}")

    def _show(self, frame):
        from PIL import ImageTk
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            return
        fw, fh = frame.width, frame.height
        scale = min(cw / fw, ch / fh)
        w, h = max(2, int(fw * scale)) & ~1, max(2, int(fh * scale)) & ~1
        img = frame.to_image(width=w, height=h)
        if self.photo is not None and (self.photo.width(), self.photo.height()) == (w, h):
            self.photo.paste(img)
        else:
            self.photo = ImageTk.PhotoImage(img)
            self.canvas.itemconfigure(self.img_item, image=self.photo)
        x0, y0 = (cw - w) // 2, (ch - h) // 2
        self.canvas.coords(self.img_item, x0, y0)
        self.img_rect = (x0, y0, w, h)
        if self.canvas.itemcget(self.msg_item, "text") == "Подключение…":
            self.canvas.itemconfigure(self.msg_item, text="")

    # --- ввод
    def _bind_input(self):
        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._motion)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Button-2>" if not IS_MAC else "<Button-3>", lambda e: self.link.key(KEY_HOME))
        c.bind("<Button-3>" if not IS_MAC else "<Button-2>", lambda e: self.link.key(KEY_BACK))
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", lambda e: self._scroll_at(e, 1))
        c.bind("<Button-5>", lambda e: self._scroll_at(e, -1))
        self.bind("<KeyPress>", self._key)
        mods = ["Control"] + (["Command"] if IS_MAC else [])
        for m in mods:
            for seq, fn in (("r", self.start_rec), ("p", self.toggle_pause), ("s", self.stop_rec),
                            ("S", self.screenshot), ("m", self.toggle_sound)):
                try:
                    self.bind(f"<{m}-{seq}>", lambda e, f=fn: (f(), "break")[1])
                except tk.TclError:
                    pass

    def _to_device(self, ex, ey, clamp=False):
        if not self.img_rect or not self.dev_size:
            return None
        x0, y0, w, h = self.img_rect
        dw, dh = self.dev_size
        rx, ry = (ex - x0) / w, (ey - y0) / h
        if clamp:
            rx, ry = min(max(rx, 0.0), 0.9999), min(max(ry, 0.0), 0.9999)
        elif not (0 <= rx < 1 and 0 <= ry < 1):
            return None
        return int(rx * dw), int(ry * dh), dw, dh

    def _press(self, e):
        self.canvas.focus_set()
        if self._on_toast():
            return
        p = self._to_device(e.x, e.y)
        if p and self.ended is None:
            self.touching = True
            self.link.send(msg_touch(ACTION_DOWN, *p))

    def _motion(self, e):
        if self.touching:
            self.link.send(msg_touch(ACTION_MOVE, *self._to_device(e.x, e.y, clamp=True)))

    def _release(self, e):
        if self.touching:
            self.touching = False
            self.link.send(msg_touch(ACTION_UP, *self._to_device(e.x, e.y, clamp=True)))

    def _wheel(self, e):
        d = e.delta / 120 if IS_WIN else e.delta
        if d:
            self._scroll_at(e, max(-16, min(16, d)))

    def _scroll_at(self, e, v):
        p = self._to_device(e.x, e.y)
        if p:
            self.link.send(msg_scroll(*p, 0, v))

    def _key(self, e):
        if e.state & 0x4 or (IS_MAC and e.state & 0x8):
            return  # Ctrl/Cmd — это горячие клавиши окна
        code = KEYMAP.get(e.keysym)
        if code is not None:
            self.link.key(code)
        elif e.char and e.char.isprintable():
            self.link.send(msg_text(e.char))

    # --- запись
    def rec_or_pause(self):
        if self.rec_state == "idle":
            self.start_rec()
        else:
            self.toggle_pause()

    def start_rec(self):
        if self.rec_state != "idle" or self.ended is not None or not self.dev_size:
            return
        self.rec_tmp = tempfile.mkdtemp(prefix="takeone_rec_", dir=self.tmp_root)
        size = self.dev_size
        self.recorder = Recorder(os.path.join(self.rec_tmp, "record.mkv"), size, self.asc)
        self.recorder.start()
        self.link.reset_video()  # телефон сразу присылает ключевой кадр
        self.rec_state, self.rec_elapsed, self.rec_since = "recording", 0.0, time.monotonic()
        self._update_controls()
        self._toast("Запись началась")

    def toggle_pause(self):
        if self.rec_state == "recording":
            self.recorder.pause()
            self.rec_elapsed += time.monotonic() - self.rec_since
            self.rec_since = None
            self.rec_state = "paused"
            self._toast("Пауза")
        elif self.rec_state == "paused" and self.ended is None:
            self.recorder.resume()
            self.link.reset_video()
            self.rec_state, self.rec_since = "recording", time.monotonic()
            self._toast("Запись продолжается")
        self._update_controls()

    def stop_rec(self, then=None):
        if self.rec_state not in ("recording", "paused"):
            return
        if self.rec_since is not None:
            self.rec_elapsed += time.monotonic() - self.rec_since
            self.rec_since = None
        rec, self.recorder = self.recorder, None
        has_data = rec.stop()
        self.rec_state = "saving"
        self._update_controls()
        if not has_data:
            shutil.rmtree(self.rec_tmp, ignore_errors=True)
            self._rec_done("Запись пустая — нечего сохранять")
            if then:
                then()
            return
        name = f"{self.app_name}_" + datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        while True:
            dest = filedialog.asksaveasfilename(
                parent=self, title="Сохранить запись", initialdir=self.video_dir, initialfile=name,
                defaultextension=".mp4", filetypes=[("MP4", "*.mp4"), ("Matroska MKV", "*.mkv")])
            if dest:
                break
            if messagebox.askyesno(self.app_name, "Удалить эту запись без сохранения?", parent=self):
                shutil.rmtree(self.rec_tmp, ignore_errors=True)
                self._rec_done("Запись удалена")
                if then:
                    then()
                return
        src = rec.path
        self._toast("Сохраняю…", sticky=True)

        def work():
            err = None
            try:
                ext = os.path.splitext(dest)[1].lower()
                if self.ffmpeg:
                    cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                           "-map", "0", "-c", "copy"]
                    if ext in (".mp4", ".mov"):
                        cmd += ["-movflags", "+faststart"]
                    r = subprocess.run(cmd + [dest], capture_output=True, text=True, creationflags=NOWIN)
                    if r.returncode:
                        raise RuntimeError(r.stderr[-800:])
                else:
                    shutil.move(src, os.path.splitext(dest)[0] + ".mkv")
                shutil.rmtree(self.rec_tmp, ignore_errors=True)
            except Exception as e:  # noqa
                err = f"{e}\n\nИсходная запись сохранена здесь:\n{src}"
            self.after(0, lambda: finish(err))

        def finish(err):
            if err:
                messagebox.showerror(self.app_name, "Не удалось сохранить видео:\n" + err, parent=self)
                self._rec_done("Ошибка сохранения")
            else:
                self._rec_done(f"Сохранено: {os.path.basename(dest)}", folder=os.path.dirname(dest))
            if then:
                then()

        threading.Thread(target=work, daemon=True).start()

    def _rec_done(self, msg, folder=None):
        self.rec_state = "idle"
        self.rec_elapsed, self.rec_since = 0.0, None
        self._update_controls()
        self._toast(msg, folder=folder)

    def _update_timer(self):
        s = self.rec_state
        if s in ("recording", "paused"):
            t = self.rec_elapsed + (time.monotonic() - self.rec_since if self.rec_since else 0)
            t = int(t)
            self.rec_time.configure(text=f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}")
            blink = s == "paused" or int(time.monotonic() * 2) % 2 == 0
            self.rec_dot.configure(fg=(REC_RED if s == "recording" else "#FFB547") if blink else BAR)
        else:
            self.rec_time.configure(text="")
            self.rec_dot.configure(fg=BAR)

    # --- скриншот и звук
    def screenshot(self):
        frame, _ = self.decoder.latest()
        if frame is None or self.ended is not None:
            return
        img = frame.to_image()
        os.makedirs(self.shots_dir, exist_ok=True)
        path = os.path.join(self.shots_dir,
                            f"{self.app_name}_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.png")
        self._flash()

        def work():
            try:
                img.save(path)
                self.after(0, lambda: self._toast("Скриншот сохранён — открыть папку", folder=self.shots_dir))
            except Exception as e:  # noqa
                err = str(e)
                self.after(0, lambda: self._toast(f"Скриншот не сохранён: {err}"))

        threading.Thread(target=work, daemon=True).start()

    def _flash(self):
        if not self.img_rect:
            return
        x0, y0, w, h = self.img_rect
        item = self.canvas.create_rectangle(x0, y0, x0 + w, y0 + h, fill="white", outline="",
                                            stipple="gray50" if not IS_MAC else "")
        self.after(90, lambda: self.canvas.delete(item))

    def toggle_sound(self):
        if not self.player:
            return
        self.player.muted = not self.player.muted
        self._update_controls()
        self._toast("Звук на компьютере выключен" if self.player.muted else "Звук на компьютере включён")

    def _toast(self, text, folder=None, sticky=False):
        """Всплывающая плашка внизу видео; со ссылкой на папку — кликабельная."""
        if self._toast_job:
            self.after_cancel(self._toast_job)
            self._toast_job = None
        c = self.canvas
        c.delete("toast")
        if not text:
            return
        cw, ch = max(c.winfo_width(), 200), max(c.winfo_height(), 200)
        t = c.create_text(cw / 2, ch - 36, text=text, fill="white", width=cw - 70, justify="center",
                          font=("TkDefaultFont", 12, "underline" if folder else "normal"), tags=("toast",))
        x0, y0, x1, y1 = c.bbox(t)
        bg = _rounded(c, x0 - 16, y0 - 9, x1 + 16, y1 + 9, 14, fill="#23242C", outline="", tags=("toast",))
        c.tag_lower(bg, t)
        c.tag_unbind("toast", "<ButtonRelease-1>")
        if folder:
            c.tag_bind("toast", "<ButtonRelease-1>", lambda e: open_folder(folder))
            c.tag_bind("toast", "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind("toast", "<Leave>", lambda e: c.configure(cursor=""))
        if not sticky:
            self._toast_job = self.after(4500 if folder else 2000, lambda: c.delete("toast"))

    def _on_toast(self):
        return "toast" in self.canvas.gettags("current")

    # --- закрытие
    def close(self):
        if self._closing or self.rec_state == "saving":
            return
        if self.rec_state in ("recording", "paused"):
            ans = messagebox.askyesnocancel(self.app_name, "Идёт запись. Сохранить её перед закрытием?",
                                            parent=self)
            if ans is None:
                return
            if ans:
                self.stop_rec(then=self._shutdown)
                return
            self.recorder.stop()
            shutil.rmtree(self.rec_tmp, ignore_errors=True)
        self._shutdown()

    def _shutdown(self):
        self._closing = True
        threading.Thread(target=self.link.stop, daemon=True).start()
        if self.player:
            self.player.close()
        self.destroy()
        self.on_closed()

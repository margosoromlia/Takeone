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


class Timeline:
    """Общие часы для нескольких телефонов: время записи без пауз, по часам компьютера."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.paused_total = 0.0
        self.pause_at = None

    def pause(self):
        if self.pause_at is None:
            self.pause_at = time.monotonic()

    def resume(self):
        if self.pause_at is not None:
            self.paused_total += time.monotonic() - self.pause_at
            self.pause_at = None

    def now_us(self):
        t = self.pause_at if self.pause_at is not None else time.monotonic()
        return int((t - self.t0 - self.paused_total) * 1_000_000)


class Recorder:
    """Пауза = пакеты не пишутся. Каждый отрезок начинается с ключевого кадра (окно просит телефон
    выдать его сразу). Один телефон: после паузы время «сшивается» встык. Несколько телефонов:
    каждый отрезок привязывается к общим часам Timeline, чтобы экраны шли синхронно."""
    FRAME_US = 16_667

    def __init__(self, path, size, audio_asc, timeline=None):
        self.path, self.size, self.asc, self.timeline = path, size, audio_asc, timeline
        self.lock = threading.Lock()
        self.mkv = None
        self.active = False
        self.waiting_key = True
        self.offset = 0
        self.last_rec = None
        self.audio_from = None
        self.prev_pts = None
        self.gap = self.FRAME_US  # реальный интервал между кадрами

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
                base = 0 if self.last_rec is None else self.last_rec + self.gap
                if self.timeline is not None:
                    base = max(base, self.timeline.now_us())
                self.offset = pts - base
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


# --- сохранение: перепаковка, сжатие до лимита, склейка нескольких телефонов ---------------
def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NOWIN)


def probe(ffmpeg, path):
    """(длительность в секундах, ширина, высота) по выводу «ffmpeg -i» — ffprobe не нужен."""
    err = _run([ffmpeg, "-hide_banner", "-i", path]).stderr
    import re
    d = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return dur, w, h


def run_ffmpeg(cmd, duration, progress=None, span=(0.0, 1.0)):
    """Запуск ffmpeg с прогрессом (0..1) по out_time."""
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", creationflags=NOWIN)
    err = []
    t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    t.start()
    for line in p.stdout:
        if progress and duration > 0 and line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                v = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            progress(span[0] + (span[1] - span[0]) * min(0.999, max(0.0, v / duration)))
    p.wait()
    t.join(5)
    if p.returncode:
        raise RuntimeError("ffmpeg: " + "".join(err)[-1200:])


def target_bitrate(limit_mb, duration, audio_kbps):
    total = limit_mb * 1_000_000 * 8 * 0.94 / max(duration, 1.0)
    return max(120_000, int(total - audio_kbps * 1000))


def height_for_bitrate(vb_per_stream, h):
    for height, need in ((1920, 4_000_000), (1600, 2_800_000), (1280, 1_700_000), (960, 900_000)):
        if h >= height and vb_per_stream >= need:
            return min(h, height)
    return min(h, 720)


def encode(ffmpeg, in_args, filt, dest, duration, limit_mb, audio, progress, tmpdir):
    """Перекодирование в H.264: без лимита — по качеству (CRF), с лимитом — в два прохода под размер."""
    base = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + in_args + filt
    out_opts = ["-movflags", "+faststart"] if dest.lower().endswith((".mp4", ".mov")) else []
    aud = ["-c:a", "aac", "-b:a", "128k"] if audio else ["-an"]
    x264 = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if not limit_mb:
        run_ffmpeg(base + x264 + ["-crf", "20"] + aud + out_opts + [dest], duration, progress)
        return
    vb = target_bitrate(limit_mb, duration, 128 if audio else 0)
    log = os.path.join(tmpdir, "x264pass")
    for attempt in range(2):
        rate = ["-b:v", str(vb), "-maxrate", str(int(vb * 1.5)), "-bufsize", str(int(vb * 2))]
        run_ffmpeg(base + x264 + rate + ["-pass", "1", "-passlogfile", log, "-an", "-f", "null", "-"],
                   duration, progress, (0.0, 0.45))
        run_ffmpeg(base + x264 + rate + ["-pass", "2", "-passlogfile", log] + aud + out_opts + [dest],
                   duration, progress, (0.45, 1.0))
        if os.path.getsize(dest) <= limit_mb * 1_000_000:
            return
        vb = int(vb * 0.85)


def save_single(ffmpeg, src_mkv, dest, limit_mb, progress, tmpdir):
    """Один телефон: перепаковка без потерь; если файл больше лимита — сжатие до лимита."""
    ext = os.path.splitext(dest)[1].lower() or ".mp4"
    remux = os.path.join(tmpdir, "remux" + ext)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", src_mkv, "-map", "0", "-c", "copy"]
    if ext in (".mp4", ".mov"):
        cmd += ["-movflags", "+faststart"]
    r = _run(cmd + [remux])
    if r.returncode:
        raise RuntimeError(r.stderr[-1200:])
    if limit_mb and os.path.getsize(remux) > limit_mb * 1_000_000:
        fit_file(ffmpeg, remux, dest, limit_mb, progress, tmpdir)
    else:
        shutil.move(remux, dest)


def fit_file(ffmpeg, src, dest, limit_mb, progress, tmpdir):
    """Сжать готовое видео до лимита (используется и для записей iPhone)."""
    dur, w, h = probe(ffmpeg, src)
    has_audio = "Audio:" in _run([ffmpeg, "-hide_banner", "-i", src]).stderr
    vb = target_bitrate(limit_mb, dur, 128 if has_audio else 0)
    portrait_h = max(w, h)
    target = height_for_bitrate(vb, portrait_h)
    vf = []
    if target < portrait_h:
        vf = ["-vf", (f"scale=-2:{target}" if h >= w else f"scale={target}:-2") + ":flags=lanczos"]
    encode(ffmpeg, ["-i", src], vf, dest, dur, limit_mb, has_audio, progress, tmpdir)


def save_combined(ffmpeg, paths, sizes, dest, duration, limit_mb, progress, tmpdir):
    """Несколько телефонов в одно видео: рядом слева направо, одной высоты, без звука, 30 к/с.
    Файлы записаны по общим часам, поэтому -copyts сохраняет синхронность."""
    n = len(paths)
    h = min(min(s[1] for s in sizes), 1920)
    if limit_mb:
        h = height_for_bitrate(target_bitrate(limit_mb, duration, 0) / n, h)
    h -= h % 2
    gap = max(8, h // 120) & ~1
    parts = []
    for i in range(n):
        f = f"[{i}:v]fps=30,scale=-2:{h}:flags=lanczos,setsar=1"
        if i < n - 1:
            f += f",pad=iw+{gap}:ih:0:0:color=black"
        parts.append(f + f"[v{i}]")
    if n > 1:
        parts.append("".join(f"[v{i}]" for i in range(n)) + f"hstack=inputs={n}:shortest=0[v]")
    else:
        parts[0] = parts[0].replace("[v0]", "[v]")
    in_args = ["-copyts", "-start_at_zero"]
    for p in paths:
        in_args += ["-i", p]
    filt = ["-filter_complex", ";".join(parts), "-map", "[v]"]
    encode(ffmpeg, in_args, filt, dest, duration, limit_mb, False, progress, tmpdir)


# --- оформление ------------------------------------------------------------------------
C = {
    "bar": "#1A1B20", "line": "#2A2C34", "hover": "#2C2E37", "press": "#373A45",
    "icon": "#ECEDF2", "dim": "#5C5F6C", "red": "#E5484D", "red_hover": "#EE5F64",
    "amber": "#F5A524", "amber_bg": "#3D3220", "pill": "#2A2C34", "pill_hover": "#33363F",
    "video": "#0B0B0E", "toast": "#24252D",
}
TOP_H, BOT_H = 46, 42
MIN_W = 296

# Иконки в сетке 24×24 (в духе Lucide): ("line", точки, толщина) | ("poly", точки) |
# ("circle", cx, cy, r, толщина|None) | ("rrect", x0, y0, x1, y1, r, толщина|None) | ("arc", cx, cy, r, нач, длина, толщина)
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


def _rounded(c, x0, y0, x1, y1, r, **kw):
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
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


def _pil_render(w, h, shapes, ss=4):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def S(v):
        return v * ss

    for sh in shapes:
        t = sh[0]
        if t == "line":
            pts = [(S(x), S(y)) for x, y in sh[1]]
            col, wd = _rgba(sh[2]), max(1, round(S(sh[3])))
            d.line(pts, fill=col, width=wd, joint="curve")
            for x, y in (pts[0], pts[-1]):
                d.ellipse((x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2), fill=col)
        elif t == "poly":
            pts = [(S(x), S(y)) for x, y in sh[1]]
            col = _rgba(sh[2])
            d.polygon(pts, fill=col)
            d.line(pts + [pts[0]], fill=col, width=round(S(1.4)), joint="curve")
        elif t == "circle":
            _, cx, cy, r, col, wd = sh
            box = (S(cx - r), S(cy - r), S(cx + r), S(cy + r))
            if wd:
                d.ellipse(box, outline=_rgba(col), width=max(1, round(S(wd))))
            else:
                d.ellipse(box, fill=_rgba(col))
        elif t == "rrect":
            _, x0, y0, x1, y1, r, col, wd = sh
            box = (S(x0), S(y0), S(x1), S(y1))
            if wd:
                d.rounded_rectangle(box, radius=S(r), outline=_rgba(col), width=max(1, round(S(wd))))
            else:
                d.rounded_rectangle(box, radius=S(r), fill=_rgba(col))
        elif t == "arc":
            _, cx, cy, r, start, ext, col, wd = sh
            d.arc((S(cx - r), S(cy - r), S(cx + r), S(cy + r)), start=start, end=start + ext,
                  fill=_rgba(col), width=max(1, round(S(wd))))
    return img.resize((w, h), Image.LANCZOS)


_IMG_CACHE = {}


def paint(c, w, h, shapes, tag="paint"):
    """Векторно на Mac (Tk там сглаживает и рисует чётко на Retina), иначе — сглаженной картинкой PIL."""
    c.delete(tag)
    if IS_MAC:
        before = set(c.find_all())
        for sh in shapes:
            _tk_shape(c, sh)
        for item in set(c.find_all()) - before:
            c.addtag_withtag(tag, item)
        return
    from PIL import ImageTk
    key = (w, h, repr(shapes))
    img = _IMG_CACHE.get(key)
    if img is None:
        img = _IMG_CACHE[key] = ImageTk.PhotoImage(_pil_render(w, h, shapes), master=c)
    c.create_image(0, 0, anchor="nw", image=img, tags=(tag,))


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
        text = self.text_fn()
        if not text:
            return
        self.win = tw = tk.Toplevel(self.w)
        tw.wm_overrideredirect(True)
        tk.Label(tw, text=text, bg="#2B2C33", fg="#ECEDF2", padx=9, pady=5,
                 font=("TkDefaultFont", 11)).pack()
        tw.update_idletasks()
        x = self.w.winfo_rootx() + self.w.winfo_width() // 2 - tw.winfo_width() // 2
        tw.wm_geometry(f"+{max(0, x)}+{self.w.winfo_rooty() + self.w.winfo_height() + 6}")


class IconButton(tk.Canvas):
    SIZE = 34
    GLYPH = 22

    def __init__(self, parent, icon, command, tip=""):
        s = self.SIZE
        super().__init__(parent, width=s, height=s, bg=C["bar"], highlightthickness=0, bd=0)
        self.icon, self.command, self.tip = icon, command, tip
        self.enabled = True
        self.hover = self.pressed = False
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        Tooltip(self, lambda: self.tip)
        self._draw()

    def set(self, icon=None, enabled=None, tip=None):
        changed = False
        for k, v in (("icon", icon), ("enabled", enabled), ("tip", tip)):
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
        s, g = self.SIZE, self.GLYPH
        shapes = []
        if self.enabled and (self.hover or self.pressed):
            shapes.append(("rrect", 1, 1, s - 1, s - 1, 9, C["press"] if self.pressed else C["hover"], None))
        shapes += icon_shapes(self.icon, (s - g) / 2, (s - g) / 2, g, C["icon"] if self.enabled else C["dim"])
        self.configure(cursor="hand2" if self.enabled else "")
        paint(self, s, s, shapes)

    def _release(self, e):
        self._set(pressed=False)
        if self.enabled and 0 <= e.x < self.SIZE and 0 <= e.y < self.SIZE:
            self.command()


class RecPill(tk.Canvas):
    """Плашка записи: «● Запись» → красная с таймером → янтарная «Пауза»."""
    W, H = 118, 30

    def __init__(self, parent, command):
        super().__init__(parent, width=self.W, height=self.H, bg=C["bar"], highlightthickness=0, bd=0)
        self.command = command
        self.state, self.text, self.enabled, self.hover, self.blink = "idle", "Запись", True, False, True
        self.bind("<Enter>", lambda e: self._h(True))
        self.bind("<Leave>", lambda e: self._h(False))
        self.bind("<ButtonRelease-1>", lambda e: self.enabled and self.state in ("idle", "paused") and self.command())
        Tooltip(self, lambda: {"idle": "Начать запись  (Ctrl+R)", "paused": "Продолжить запись  (Ctrl+P)"}
                .get(self.state, ""))
        self._draw()

    def _h(self, v):
        self.hover = v
        self._draw()

    def set(self, state, text, enabled=True, blink=True):
        if (state, text, enabled, blink) != (self.state, self.text, self.enabled, self.blink):
            self.state, self.text, self.enabled, self.blink = state, text, enabled, blink
            self._draw()

    def _draw(self):
        W, H, st = self.W, self.H, self.state
        clickable = self.enabled and st in ("idle", "paused")
        if st == "recording":
            bg, dot, fg = C["red"], ("#FFFFFF" if self.blink else C["red"]), "#FFFFFF"
        elif st == "paused":
            bg, dot, fg = C["amber_bg"], None, C["amber"]
        elif st == "saving":
            bg, dot, fg = C["pill"], None, C["icon"]
        else:
            bg = C["pill_hover"] if (self.hover and clickable) else C["pill"]
            dot, fg = (C["red"] if self.enabled else C["dim"]), (C["icon"] if self.enabled else C["dim"])
        shapes = [("rrect", 0.5, 0.5, W - 0.5, H - 0.5, H / 2, bg, None)]
        if st == "paused":
            shapes += [("rrect", 13, 10, 16.5, 20, 1.2, C["amber"], None),
                       ("rrect", 19.5, 10, 23, 20, 1.2, C["amber"], None)]
        elif dot:
            shapes.append(("circle", 18, H / 2, 5, dot, None))
        paint(self, W, H, shapes)
        self.delete("txt")
        mono = st in ("recording", "paused")
        self.create_text(31, H / 2, text=self.text, anchor="w", fill=fg, tags=("txt",),
                         font=("TkFixedFont" if mono else "TkDefaultFont", 12, "bold"))
        self.configure(cursor="hand2" if clickable else "")


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


def fmt_hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


# --- запись одного или нескольких окон ---------------------------------------------------
class RecordGroup:
    """Управляет записью для одного окна или для нескольких связанных окон (общая запись)."""

    def __init__(self, ffmpeg, video_dir, tmp_root, limit_mb, app_name):
        self.ffmpeg, self.video_dir, self.tmp_root = ffmpeg, video_dir, tmp_root
        self.limit_mb, self.app_name = limit_mb, app_name
        self.members = []
        self.state = "idle"  # idle | recording | paused | saving
        self.elapsed, self.since = 0.0, None
        self.timeline = None
        self.tmp = None
        self.arranged = False
        self.multi_rec = False

    @property
    def multi(self):
        return len(self.members) > 1

    def add(self, w):
        self.members.append(w)

    def remove(self, w):
        if w in self.members:
            self.members.remove(w)
        self.refresh()

    def time(self):
        return self.elapsed + (time.monotonic() - self.since if self.since else 0.0)

    def refresh(self):
        for w in self.members:
            w._update_controls()

    def toast(self, text, folder=None, sticky=False):
        for w in self.members:
            w._toast(text, folder, sticky)

    def ready(self):
        return bool(self.members) and all(w.ready() for w in self.members)

    def arrange(self):
        """Окна нескольких телефонов — рядом по центру экрана (один раз)."""
        if self.arranged or not self.multi or not all(w.sized_for for w in self.members):
            return
        self.arranged = True
        ws = self.members
        for w in ws:
            w.update_idletasks()
        widths = [w.winfo_width() for w in ws]
        gap = 14
        total = sum(widths) + gap * (len(ws) - 1)
        x = max(0, (ws[0].winfo_screenwidth() - total) // 2)
        for w, wd in zip(ws, widths):
            w.geometry(f"+{x}+30")
            x += wd + gap

    # --- кнопки
    def start(self):
        if self.state != "idle" or not self.ready():
            return
        self.tmp = tempfile.mkdtemp(prefix="takeone_rec_", dir=self.tmp_root)
        self.multi_rec = self.multi
        self.timeline = Timeline() if self.multi_rec else None
        for i, w in enumerate(self.members):
            asc = None if self.multi_rec else w.asc  # общая запись — без звука
            w.recorder = Recorder(os.path.join(self.tmp, f"dev{i}.mkv"), w.dev_size, asc, self.timeline)
            w.recorder.start()
            w.link.reset_video()  # телефон сразу присылает ключевой кадр
        self.state, self.elapsed, self.since = "recording", 0.0, time.monotonic()
        self.refresh()
        self.toast("Общая запись началась" if self.multi_rec else "Запись началась")

    def toggle_pause(self):
        if self.state == "recording":
            if self.timeline:
                self.timeline.pause()
            for w in self.members:
                if w.recorder:
                    w.recorder.pause()
            self.elapsed += time.monotonic() - self.since
            self.since, self.state = None, "paused"
            self.toast("Пауза")
        elif self.state == "paused":
            if any(w.ended for w in self.members):
                return
            if self.timeline:
                self.timeline.resume()
            for w in self.members:
                if w.recorder:
                    w.recorder.resume()
                    w.link.reset_video()
            self.state, self.since = "recording", time.monotonic()
            self.toast("Запись продолжается")
        self.refresh()

    def _collect(self):
        if self.since is not None:
            self.elapsed += time.monotonic() - self.since
            self.since = None
        out = []
        for w in self.members:
            rec, w.recorder = w.recorder, None
            if rec is not None and rec.stop():
                out.append((rec.path, rec.size))
        return out

    def discard(self):
        if self.state in ("recording", "paused"):
            self._collect()
            shutil.rmtree(self.tmp, ignore_errors=True)
            self._done("Запись удалена")

    def stop(self, then=None, parent=None):
        if self.state not in ("recording", "paused"):
            if then:
                then()
            return
        files = self._collect()
        duration = self.elapsed
        self.state = "saving"
        self.refresh()
        parent = parent or (self.members[0] if self.members else None)
        if not files:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self._done("Запись пустая — нечего сохранять")
            return then and then()
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        name = f"{self.app_name}_{'вместе_' if self.multi_rec else ''}{stamp}"
        while True:
            dest = filedialog.asksaveasfilename(
                parent=parent, title="Сохранить запись", initialdir=self.video_dir, initialfile=name,
                defaultextension=".mp4", filetypes=[("MP4", "*.mp4"), ("Matroska MKV", "*.mkv")])
            if dest:
                break
            if messagebox.askyesno(self.app_name, "Удалить эту запись без сохранения?", parent=parent):
                shutil.rmtree(self.tmp, ignore_errors=True)
                self._done("Запись удалена")
                return then and then()
        self.toast("Сохраняю…", sticky=True)
        tmp, multi, limit = self.tmp, self.multi_rec, self.limit_mb
        anchor = parent

        def progress(f):
            anchor.after(0, lambda: self.toast(f"Сохраняю… {int(f * 100)}%", sticky=True))

        def work():
            err = None
            try:
                if not self.ffmpeg:
                    raise RuntimeError("не найден ffmpeg")
                if multi:
                    save_combined(self.ffmpeg, [p for p, _ in files], [s for _, s in files], dest,
                                  duration, limit, progress, tmp)
                else:
                    save_single(self.ffmpeg, files[0][0], dest, limit, progress, tmp)
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception as e:  # noqa
                err = f"{e}\n\nИсходные записи остались здесь:\n{tmp}"
            anchor.after(0, lambda: finish(err))

        def finish(err):
            if err:
                messagebox.showerror(self.app_name, "Не удалось сохранить видео:\n" + err, parent=anchor)
                self._done("Ошибка сохранения")
            else:
                mb = os.path.getsize(dest) / 1_000_000
                self._done(f"Сохранено: {os.path.basename(dest)} · {mb:.1f} МБ", folder=os.path.dirname(dest))
            if then:
                then()

        threading.Thread(target=work, daemon=True).start()

    def _done(self, msg, folder=None):
        self.state, self.elapsed, self.since, self.timeline = "idle", 0.0, None, None
        self.refresh()
        self.toast(msg, folder)

    def member_ended(self, w):
        if self.state in ("recording", "paused"):
            self.stop(parent=w)


# --- окно телефона -------------------------------------------------------------------------
class DeviceWindow(tk.Toplevel):
    def __init__(self, master, link, group, shots_dir, on_closed, size_frac=0.86, app_name="Takeone"):
        super().__init__(master, bg=C["video"])
        self.link, self.group, self.shots_dir = link, group, shots_dir
        self.on_closed, self.size_frac, self.app_name = on_closed, size_frac, app_name
        group.add(self)
        self.title(f"{link.device_name} — {app_name}")
        self.resizable(False, False)
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
        self.ended_handled = False
        self.recorder = None
        self._toast_job = None
        self._closing = False

        self._build()
        self._bind_input()
        link.run(self._on_session, self._on_video, self._on_audio_config, self._on_audio, self._on_ended)
        self.after(10, self._tick)

    # --- построение окна
    def _build(self):
        top = tk.Frame(self, bg=C["bar"], height=TOP_H)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        tk.Frame(self, bg=C["line"], height=1).pack(side="top", fill="x")
        left = tk.Frame(top, bg=C["bar"])
        left.pack(side="left", padx=(8, 0), pady=(TOP_H - RecPill.H) // 2)
        self.pill = RecPill(left, self._rec_click)
        self.pill.pack(side="left")
        self.b_pause = IconButton(left, "pause", self.group.toggle_pause, "Пауза  (Ctrl+P)")
        self.b_stop = IconButton(left, "stop", self.stop_rec, "Остановить и сохранить  (Ctrl+S)")
        self.b_pause.pack(side="left", padx=(6, 0))
        self.b_stop.pack(side="left")
        right = tk.Frame(top, bg=C["bar"])
        right.pack(side="right", padx=(0, 6))
        self.b_sound = IconButton(right, "sound", self.toggle_sound, "Звук на компьютере  (Ctrl+M)")
        self.b_shot = IconButton(right, "camera", self.screenshot, "Скриншот  (Ctrl+Shift+S)")
        self.b_sound.pack(side="right")
        self.b_shot.pack(side="right")

        bottom = tk.Frame(self, bg=C["bar"], height=BOT_H)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)
        tk.Frame(self, bg=C["line"], height=1).pack(side="bottom", fill="x")
        nav = tk.Frame(bottom, bg=C["bar"])
        nav.place(relx=0.5, rely=0.5, anchor="center")
        for icon, code, tip in (("back", KEY_BACK, "Назад  (Esc, правая кнопка мыши)"),
                                ("home", KEY_HOME, "Домой  (средняя кнопка мыши)"),
                                ("recents", KEY_APP_SWITCH, "Недавние приложения")):
            IconButton(nav, icon, lambda c=code: self.link.key(c), tip).pack(side="left", padx=18)

        self.canvas = tk.Canvas(self, bg=C["video"], highlightthickness=0, bd=0, width=MIN_W, height=640)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.img_item = self.canvas.create_image(0, 0, anchor="nw")
        self.msg_item = self.canvas.create_text(MIN_W / 2, 320, text="Подключение…", fill="#8E90A0",
                                                font=("TkDefaultFont", 14), justify="center")
        self._update_controls()

    def _rec_click(self):
        if self.group.state == "idle":
            self.group.start()
        else:
            self.group.toggle_pause()

    def ready(self):
        return self.dev_size is not None and self.ended is None and self.shown_seq >= 0

    def _update_controls(self):
        g = self.group
        s = g.state
        alive = self.ended is None
        t = fmt_hms(g.time())
        both = f" ×{len(g.members)}" if g.multi and s in ("recording", "paused") else ""
        if s == "recording":
            self.pill.set("recording", t + both, blink=int(time.monotonic() * 2) % 2 == 0)
        elif s == "paused":
            self.pill.set("paused", t + both)
        elif s == "saving":
            self.pill.set("saving", "Сохранение", enabled=False)
        else:
            self.pill.set("idle", "Запись вместе" if g.multi else "Запись", enabled=alive and g.ready())
        self.b_pause.set("play" if s == "paused" else "pause", s in ("recording", "paused"),
                         "Продолжить  (Ctrl+P)" if s == "paused" else "Пауза  (Ctrl+P)")
        self.b_stop.set(enabled=s in ("recording", "paused"))
        self.b_shot.set(enabled=alive and self.shown_seq >= 0)
        if self.player is None:
            self.b_sound.set("muted", False, "Звук недоступен (нужен Android 11+ и включённый звук)")
        else:
            self.b_sound.set("muted" if self.player.muted else "sound", alive,
                             "Включить звук  (Ctrl+M)" if self.player.muted else "Выключить звук  (Ctrl+M)")

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
        self._update_controls()
        if self.ended is not None and not self.ended_handled:
            self.ended_handled = True
            self.canvas.itemconfigure(self.msg_item, text="Устройство отключено")
            self.canvas.itemconfigure(self.img_item, state="hidden")
            self.canvas.tag_raise(self.msg_item)
            self.group.member_ended(self)
        self.after(15, self._tick)

    def _fit_window(self):
        """Окно ровно по пропорциям экрана телефона; растягивать вручную нельзя.
        При повороте телефона окно поворачивается вместе с ним."""
        w, h = self.dev_size
        self.sized_for = self.dev_size
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        n = max(1, len(self.group.members))
        max_vh = int(sh * self.size_frac) - TOP_H - BOT_H - 2 - 30
        max_vw = int(sw * 0.94 / n) - 14
        scale = min(max_vh / h, max_vw / w, 1.0)
        if w * scale < MIN_W:
            scale = MIN_W / w
        vw = int(round(w * scale))
        vh = int(round(h * vw / w))  # высота строго по пропорции экрана — без полосок
        self.canvas.configure(width=vw, height=vh)
        self.geometry(f"{vw}x{vh + TOP_H + BOT_H + 2}")
        self.canvas.coords(self.msg_item, vw / 2, vh / 2)
        self.shown_seq = -1 if self.shown_seq >= 0 else self.shown_seq
        self.after(50, self.group.arrange)

    def _show(self, frame):
        from PIL import ImageTk
        cw, ch = int(self.canvas.cget("width")), int(self.canvas.cget("height"))
        rw, rh = self.canvas.winfo_width(), self.canvas.winfo_height()
        if rw > 10 and rh > 10:
            cw, ch = rw, rh
        fw, fh = frame.width, frame.height
        scale = min(cw / fw, ch / fh)
        w, h = max(2, round(fw * scale)), max(2, round(fh * scale))
        img = frame.to_image(width=w, height=h)
        if self.photo is not None and (self.photo.width(), self.photo.height()) == (w, h):
            self.photo.paste(img)
        else:
            self.photo = ImageTk.PhotoImage(img, master=self)
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
            for seq, fn in (("r", self.group.start), ("p", self.group.toggle_pause), ("s", self.stop_rec),
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

    def _on_toast(self):
        return "toast" in self.canvas.gettags("current")

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
            return  # Ctrl/Cmd — горячие клавиши окна
        code = KEYMAP.get(e.keysym)
        if code is not None:
            self.link.key(code)
        elif e.char and e.char.isprintable():
            self.link.send(msg_text(e.char))

    # --- запись (через группу)
    def stop_rec(self, then=None):
        self.group.stop(then=then, parent=self)

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
                                            stipple="" if IS_MAC else "gray50")
        self.after(90, lambda: self.canvas.delete(item))

    def toggle_sound(self):
        if not self.player:
            return
        self.player.muted = not self.player.muted
        self._update_controls()
        self._toast("Звук на компьютере выключен" if self.player.muted else "Звук на компьютере включён")

    def _toast(self, text, folder=None, sticky=False):
        """Плашка внизу видео; со ссылкой на папку — кликабельная."""
        if self._closing:
            return
        if self._toast_job:
            self.after_cancel(self._toast_job)
            self._toast_job = None
        c = self.canvas
        c.delete("toast")
        if not text:
            return
        cw = max(c.winfo_width(), int(c.cget("width")))
        ch = max(c.winfo_height(), int(c.cget("height")))
        t = c.create_text(cw / 2, ch - 34, text=text, fill="#F2F3F7", width=cw - 64, justify="center",
                          font=("TkDefaultFont", 12, "underline" if folder else "normal"), tags=("toast",))
        x0, y0, x1, y1 = c.bbox(t)
        bg = _rounded(c, x0 - 14, y0 - 8, x1 + 14, y1 + 8, 12, fill=C["toast"], outline="", tags=("toast",))
        c.tag_lower(bg, t)
        c.tag_unbind("toast", "<ButtonRelease-1>")
        if folder:
            c.tag_bind("toast", "<ButtonRelease-1>", lambda e: open_folder(folder))
            c.tag_bind("toast", "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind("toast", "<Leave>", lambda e: c.configure(cursor=""))
        if not sticky:
            self._toast_job = self.after(4500 if folder else 2000, lambda: c.delete("toast"))

    # --- закрытие
    def close(self):
        if self._closing or self.group.state == "saving":
            return
        if self.group.state in ("recording", "paused"):
            ans = messagebox.askyesnocancel(
                self.app_name, ("Идёт общая запись. " if self.group.multi else "Идёт запись. ")
                + "Сохранить её перед закрытием?", parent=self)
            if ans is None:
                return
            if ans:
                self.group.stop(then=self._shutdown, parent=self)
                return
            self.group.discard()
        self._shutdown()

    def _shutdown(self):
        if self._closing:
            return
        self._closing = True
        self.group.remove(self)
        threading.Thread(target=self.link.stop, daemon=True).start()
        if self.player:
            self.player.close()
        self.destroy()
        self.on_closed(self)

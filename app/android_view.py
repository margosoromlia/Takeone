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
from tkinter import messagebox

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
NOWIN = 0x08000000 if IS_WIN else 0
DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"

# --- протокол scrcpy 5.x -------------------------------------------------------
CODEC_H264 = 0x68323634
CODEC_AAC = 0x00616163
CODEC_OPUS = 0x6F707573
AUDIO_CODECS = {CODEC_AAC: "aac", CODEC_OPUS: "opus"}
_USED_PORTS = set()
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


def msg_paste(text):
    """Положить текст в буфер обмена телефона и сразу вставить (TYPE_SET_CLIPBOARD, paste=true)."""
    data = text.encode("utf-8")
    return struct.pack(">BQBI", 9, 0, 1, len(data)) + data


def msg_text(text):
    data = text.encode("utf-8")[:300]
    return struct.pack(">BI", MSG_TEXT, len(data)) + data


SOFT_ENCODERS = ("c2.android.avc.encoder", "OMX.google.h264.encoder")
VIDEO_FAIL_MARKS = ("Video encoding error", "Capture/encoding error", "Could not create default video encoder",
                    "Could not create video encoder")


def input_help(manufacturer):
    """Что включить на телефоне, если касания и клавиши не доходят (известно по scrcpy).
    Возвращает {title, intro, steps, note} для подсказки в окне телефона."""
    m = (manufacturer or "").lower()
    title = "Касания не доходят до телефона"
    if m in ("xiaomi", "redmi", "poco", "blackshark"):
        return dict(title=title, intro="На Xiaomi, Redmi и POCO включите одну настройку и перезагрузите телефон:",
                    steps=["Настройки → Расширенные настройки", "Для разработчиков",
                           "«Отладка по USB (настройки безопасности)»"],
                    note="Пункт требует вход в Mi-аккаунт и SIM-карту в телефоне.")
    if m in ("oppo", "realme", "oneplus"):
        return dict(title=title, intro="На OPPO, realme и OnePlus включите одну настройку и перезагрузите телефон:",
                    steps=["Настройки → Для разработчиков",
                           "«Отключить мониторинг разрешений» (Disable permission monitoring)"], note="")
    if m in ("vivo", "iqoo"):
        return dict(title=title, intro="На vivo и iQOO включите одну настройку и перезагрузите телефон:",
                    steps=["Настройки → Для разработчиков", "«Отладка по USB (настройки безопасности)»"], note="")
    return dict(title=title, intro="В «Для разработчиков» включите один из пунктов и перезагрузите телефон:",
                steps=["«Отладка по USB (настройки безопасности)» / «USB debugging (Security settings)»",
                       "или «Отключить мониторинг разрешений»"], note="")


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
        self.log_dir = log_dir
        self.encoder = None  # None = кодировщик по умолчанию; иначе имя (например, программный)
        self.props = {}
        self._log_pos = 0
        safe = "".join(ch if ch.isalnum() else "_" for ch in serial)
        self.log = os.path.join(log_dir, f"android_server_{safe}.log")  # у каждого телефона свой лог
        self.started_at = None
        self.vpackets = 0      # сколько видеопакетов пришло (для диагностики «нет картинки»)
        self.vsize = None
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
        self.props = self._props()
        self._adb("push", self.server_path, DEVICE_SERVER_PATH, timeout=60)
        scid = random.randint(1, 0x7FFFFFFF)
        self.port = self._free_port()
        self._adb("forward", f"tcp:{self.port}", f"localabstract:scrcpy_{scid:08x}")
        args = [f"scid={scid:08x}", "log_level=info", "tunnel_forward=true",
                f"video_bit_rate={int(self.bitrate * 1_000_000)}", "video_codec=h264",
                "control=true", "stay_awake=true", "clipboard_autosync=false",
                "power_off_on_close=false"]
        if self.max_size > 0:
            args.append(f"max_size={self.max_size}")
        if self.encoder:
            args.append(f"video_encoder={self.encoder}")
        args += ["audio=true", "audio_codec=opus"] if self.audio else ["audio=false"]
        cmd = [self.adb, "-s", self.serial, "shell", f"CLASSPATH={DEVICE_SERVER_PATH}", "app_process",
               "/", "com.genymobile.scrcpy.Server", self.server_version, *args]
        self._logf = open(self.log, "wb")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self._logf,
                                     stderr=subprocess.STDOUT, creationflags=NOWIN)
        self._connect()
        self.started_at = time.monotonic()

    @staticmethod
    def _free_port():
        """Свой порт на каждый телефон: из диапазона scrcpy, не занятый ни системой, ни нами."""
        for _ in range(200):
            p = random.randint(27183, 28999)
            if p in _USED_PORTS:
                continue
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    continue
            _USED_PORTS.add(p)
            return p
        raise RuntimeError("Не нашёлся свободный сетевой порт для подключения телефона")

    def clone(self, audio=None, max_size=None, encoder=None):
        n = AndroidLink(self.adb, self.serial, self.server_path, self.server_version,
                        self.max_size if max_size is None else max_size,
                        self.bitrate, self.audio if audio is None else audio, self.log_dir)
        n.encoder = self.encoder if encoder is None else encoder
        return n

    def _props(self):
        try:
            out = self._adb("shell", "getprop ro.product.manufacturer; getprop ro.build.version.sdk; "
                                     "getprop ro.product.model", timeout=10).splitlines()
        except Exception:  # noqa
            return {}
        out += ["", "", ""]
        try:
            sdk = int(out[1].strip())
        except ValueError:
            sdk = 0
        return {"manufacturer": out[0].strip().lower(), "sdk": sdk, "model": out[2].strip()}

    def new_log(self):
        """Текст, который сервер дописал в лог с прошлого вызова."""
        try:
            with open(self.log, "rb") as f:
                f.seek(self._log_pos)
                data = f.read()
                self._log_pos = f.tell()
            return data.decode("utf-8", "replace")
        except OSError:
            return ""

    def log_text(self):
        return _tail(self.log, 20000)

    def note(self, text):
        """Наша пометка в лог телефона — чтобы по логу было видно, что происходило на компьютере."""
        try:
            with open(self.log, "ab") as f:
                f.write(f"[takeone] {time.strftime('%H:%M:%S')} {text}\n".encode("utf-8"))
        except OSError:
            pass

    def error_hint(self):
        """Последняя ошибка, которую сервер на телефоне написал в лог."""
        lines = [ln.strip() for ln in _tail(self.log, 6000).splitlines()]
        errs = [ln.split("ERROR:", 1)[1].strip() for ln in lines if "ERROR:" in ln]
        if errs:
            return errs[-1][:160]
        exc = [ln for ln in lines if "Exception" in ln or "Error:" in ln]
        return exc[-1][:160] if exc else ""

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
    def run(self, on_session, on_video, on_audio_config, on_audio, on_ended, on_audio_none=None):
        self.cb = dict(session=on_session, video=on_video, aconf=on_audio_config, audio=on_audio,
                       ended=on_ended, anone=on_audio_none or (lambda code: None))
        threading.Thread(target=self._video_loop, daemon=True).start()
        if self.audio_sock:
            threading.Thread(target=self._audio_loop, daemon=True).start()
        threading.Thread(target=self._control_drain, daemon=True).start()

    def _end(self, reason):
        if not self._ended and not self._closing:
            self._ended = True
            try:
                with open(self.log, "ab") as f:
                    f.write(f"\n[takeone] соединение завершено: {reason}\n".encode("utf-8"))
            except OSError:
                pass
            self.cb["ended"](reason)

    def _safe(self, name, *args):
        """Ошибка в обработке одного пакета не должна обрывать трансляцию."""
        try:
            self.cb[name](*args)
        except Exception as e:  # noqa
            if not getattr(self, "_cb_err_logged", False):
                self._cb_err_logged = True
                try:
                    with open(self.log, "ab") as f:
                        f.write(f"\n[takeone] ошибка обработки ({name}): {e!r}\n".encode("utf-8"))
                except OSError:
                    pass

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
                    self.vsize = (w, h)
                    self._safe("session", w, h)
                    continue
                ptsf, size = struct.unpack(">QI", hdr)
                data = recv_exact(self.video, size)
                self.vpackets += 1
                if ptsf & FLAG_CONFIG:
                    config = pending = data
                    continue
                if pending:
                    data, pending = pending + data, b""
                self._safe("video", data, ptsf & PTS_MASK, bool(ptsf & FLAG_KEY), config)
        except Exception as e:  # noqa
            self._end(str(e))

    def _audio_loop(self):
        try:
            codec = struct.unpack(">I", recv_exact(self.audio_sock, 4))[0]
            if codec in (0, 1):
                self._safe("anone", codec)  # телефон не может отдать звук — работаем без него
                return
            name = AUDIO_CODECS.get(codec)
            if name is None:
                return
            while True:
                ptsf, size = struct.unpack(">QI", recv_exact(self.audio_sock, 12))
                data = recv_exact(self.audio_sock, size)
                if ptsf & FLAG_CONFIG:
                    self._safe("aconf", (name, data))
                else:
                    self._safe("audio", data, ptsf & PTS_MASK)
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
        _USED_PORTS.discard(self.port)
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

    def set_config(self, aconf):
        codec, extradata = aconf
        self.dec = self.av.CodecContext.create(codec, "r")
        self.dec.extradata = extradata

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
        self.has_audio = asc is not None  # asc = (кодек, extradata)
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
                          _uint(b"\x9C", 0) + _str(b"\x86", "A_OPUS" if asc[0] == "opus" else "A_AAC") +
                          _el(b"\x63\xA2", asc[1]) +
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
def _run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NOWIN, timeout=timeout)


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


_HW = None
HW_MIN_BITRATE = 2_500_000  # ниже этого аппаратный кодировщик заметно мылит — тогда x264


def hw_encoder(ffmpeg):
    """Аппаратный кодировщик H.264, который реально работает на этом компьютере ("" — нет такого).
    Mac: VideoToolbox; Windows: NVIDIA, Intel, AMD или Media Foundation. Проверяется один раз."""
    global _HW
    if _HW is not None:
        return _HW
    _HW = ""
    cands = ["h264_videotoolbox"] if IS_MAC else (["h264_nvenc", "h264_qsv", "h264_amf", "h264_mf"] if IS_WIN else [])
    try:
        listed = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=20).stdout
    except Exception:  # noqa
        return _HW
    for enc in cands:
        if enc not in listed:
            continue
        try:
            r = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                      "-i", "testsrc2=s=640x360:r=30:d=0.5", "-c:v", enc, "-b:v", "2M", "-f", "null", "-"], timeout=20)
        except Exception:  # noqa
            continue
        if r.returncode == 0:
            _HW = enc
            break
    return _HW


def _hw_args(enc, vb):
    rate = ["-b:v", str(vb)]
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p4", "-rc", "vbr"] + rate + ["-maxrate", str(int(vb * 1.3)),
                                                                       "-bufsize", str(vb * 2)]
    if enc == "h264_amf":
        return ["-c:v", enc, "-rc", "vbr_peak"] + rate + ["-maxrate", str(int(vb * 1.3))]
    if enc == "h264_qsv":
        return ["-c:v", enc] + rate + ["-maxrate", str(int(vb * 1.3))]
    return ["-c:v", enc] + rate


def encode(ffmpeg, in_args, filt, dest, duration, limit_mb, audio, progress, tmpdir):
    """Перекодирование в H.264 за один проход.
    Быстрый путь — аппаратный кодировщик (видеокарта/чип), если бюджет битрейта позволяет;
    иначе x264 с ограниченным CRF: качество по CRF, но не больше битрейта под лимит.
    Если быстрый путь не сработал или файл вышел больше лимита — повтор на x264."""
    out_opts = ["-movflags", "+faststart"] if dest.lower().endswith((".mp4", ".mov")) else []
    aud = ["-c:a", "aac", "-b:a", "128k"] if audio else ["-an"]
    vb = target_bitrate(limit_mb, duration, 128 if audio else 0) if limit_mb else 10_000_000
    hw = hw_encoder(ffmpeg) if vb >= HW_MIN_BITRATE else ""

    def cmd(video, hwaccel):
        ins = []
        for a in in_args:  # -hwaccel auto перед каждым входом: декодирование тоже на видеокарте
            if a == "-i" and hwaccel:
                ins += ["-hwaccel", "auto"]
            ins.append(a)
        return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + ins + filt + video + aud + out_opts + [dest]

    def fits():
        return not limit_mb or os.path.getsize(dest) <= limit_mb * 1_000_000

    if hw:
        try:
            run_ffmpeg(cmd(_hw_args(hw, int(vb * 0.92)), True), duration, progress)
            if fits():
                return
        except RuntimeError:
            pass
        if progress:
            progress(0.0)
    x264 = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if not limit_mb:
        run_ffmpeg(cmd(x264 + ["-crf", "20"], False), duration, progress)
        return
    for _ in range(3):
        run_ffmpeg(cmd(x264 + ["-crf", "21", "-maxrate", str(vb), "-bufsize", str(vb)], False), duration, progress)
        if fits():
            return
        vb = int(vb * 0.85)
        if progress:
            progress(0.0)


LAST_VIDEO_FILE = os.path.join(os.path.expanduser("~"), ".takeone", "last_video.txt")


def remember_video(path):
    """Запомнить последнее сохранённое видео — для кнопки «Последняя запись» во вкладке «Монтаж»."""
    try:
        os.makedirs(os.path.dirname(LAST_VIDEO_FILE), exist_ok=True)
        with open(LAST_VIDEO_FILE, "w", encoding="utf-8") as f:
            f.write(path)
    except OSError:
        pass


def last_video():
    try:
        with open(LAST_VIDEO_FILE, encoding="utf-8") as f:
            p = f.read().strip()
        return p if p and os.path.isfile(p) else None
    except OSError:
        return None


def fmt_mb(v):
    return (f"{v:.0f}" if v >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")).replace(".", ",")


def save_single(ffmpeg, src_mkv, dest, limit_mb, progress, tmpdir):
    """Один телефон: перепаковка без потерь; если файл больше лимита — сжатие до лимита."""
    ext = os.path.splitext(dest)[1].lower() or ".mp4"
    remux = os.path.join(tmpdir, "remux" + ext)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", src_mkv, "-map", "0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
    if ext in (".mp4", ".mov"):
        cmd += ["-movflags", "+faststart"]
    r = _run(cmd + [remux])
    if r.returncode:
        raise RuntimeError(r.stderr[-1200:])
    if limit_mb and os.path.getsize(remux) > limit_mb * 1_000_000:
        if progress:
            progress(0.0, f"Сжимаю до {fmt_mb(limit_mb)} МБ",
                     f"Записалось {fmt_mb(os.path.getsize(remux) / 1_000_000)} МБ — это больше лимита. "
                     "Трансляция продолжается.")
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
    chain = []
    if vb < 3_000_000:
        chain.append("fps=30")  # мало битов — лучше 30 чётких кадров в секунду, чем 60 мыльных
    if target < portrait_h:
        chain.append((f"scale=-2:{target}" if h >= w else f"scale={target}:-2") + ":flags=bicubic")
    vf = ["-vf", ",".join(chain)] if chain else []
    encode(ffmpeg, ["-i", src], vf, dest, dur, limit_mb, has_audio, progress, tmpdir)


def export_cut(ffmpeg, src, dest, segments, limit_mb, progress, tmpdir):
    """Монтаж: склеить куски [(начало, конец), …] исходного видео в один файл.
    Каждый кусок читается с точной перемоткой (-ss перед -i при перекодировании — с точностью до кадра),
    затем concat; если задан лимит — сжатие под него, как при обычном сохранении."""
    _, w, h = probe(ffmpeg, src)
    has_audio = "Audio:" in _run([ffmpeg, "-hide_banner", "-i", src]).stderr
    segments = [(a, b) for a, b in segments if b - a > 0.02]
    if not segments:
        raise RuntimeError("Нечего сохранять: всё видео вырезано.")
    total = sum(b - a for a, b in segments)
    ins = []
    for a, b in segments:
        ins += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", src]
    chain = []
    if limit_mb:
        vb = target_bitrate(limit_mb, total, 128 if has_audio else 0)
        if vb < 3_000_000:
            chain.append("fps=30")
        portrait_h = max(w, h)
        target = height_for_bitrate(vb, portrait_h)
        if portrait_h and target < portrait_h:
            chain.append((f"scale=-2:{target}" if h >= w else f"scale={target}:-2") + ":flags=bicubic")
    k = len(segments)
    inputs = "".join(f"[{i}:v]" + (f"[{i}:a]" if has_audio else "") for i in range(k))
    graph = f"{inputs}concat=n={k}:v=1:a={1 if has_audio else 0}[vc]" + ("[a]" if has_audio else "")
    graph += f";[vc]{','.join(chain) or 'null'}[v]"
    filt = ["-filter_complex", graph, "-map", "[v]"] + (["-map", "[a]"] if has_audio else [])
    encode(ffmpeg, ins, filt, dest, total, limit_mb, has_audio, progress, tmpdir)


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
        f = f"[{i}:v]fps=30,scale=-2:{h}:flags=bicubic,setsar=1"
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


# --- оформление (общий набор виджетов «Студийный графит») ---------------------------------
from ui_kit import (C, IconButton, RecPill, Button, box, paint, font, measure, icon_shapes,  # noqa: E402
                    fmt_hms, plural)

TOP_H, BOT_H = 48, 44
MIN_W = 296


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


# --- раскладка окон телефонов -----------------------------------------------------------
class WindowLayout:
    """Все открытые окна телефонов (и общей, и раздельной записи) — чтобы ставить их рядом."""

    def __init__(self):
        self.windows = []
        self.arranged = False

    def add(self, w):
        self.windows.append(w)
        self.arranged = False

    def remove(self, w):
        if w in self.windows:
            self.windows.remove(w)

    def arrange(self):
        """Окна телефонов — рядом друг с другом; если главное окно открыто — справа от него,
        чтобы оно оставалось под рукой (можно добавить ещё телефон)."""
        ws = self.windows
        if self.arranged or not ws or not all(w.sized_for for w in ws):
            return
        main = ws[0].master
        main_shown = False
        try:
            main_shown = bool(main.winfo_viewable())
        except tk.TclError:
            pass
        if len(ws) < 2 and not main_shown:
            return
        self.arranged = True
        for w in ws:
            w.update_idletasks()
        widths = [w.winfo_width() for w in ws]
        gap = 14
        total = sum(widths) + gap * (len(ws) - 1)
        sw = ws[0].winfo_screenwidth()
        x = max(0, (sw - total) // 2)
        if main_shown:
            right = main.winfo_rootx() + main.winfo_width() + gap
            if right + total <= sw:
                x = right
            elif total + main.winfo_width() + gap <= sw:
                x = sw - total  # не влезает справа от главного окна — прижать к правому краю
        for w, wd in zip(ws, widths):
            w.geometry(f"+{x}+30")
            x += wd + gap


# --- запись одного или нескольких окон ---------------------------------------------------
class RecordGroup:
    """Управляет записью для одного окна или для нескольких связанных окон (общая запись)."""

    def __init__(self, ffmpeg, video_dir, tmp_root, limit_mb, app_name):
        self.ffmpeg, self.video_dir, self.tmp_root = ffmpeg, video_dir, tmp_root
        self.limit_mb, self.app_name = limit_mb, app_name
        if _HW is None and ffmpeg:  # заранее узнать, есть ли аппаратный кодировщик, чтобы не ждать при сохранении
            threading.Thread(target=hw_encoder, args=(ffmpeg,), daemon=True).start()
        self.members = []
        self.state = "idle"  # idle | recording | paused | saving
        self.elapsed, self.since = 0.0, None
        self.timeline = None
        self.tmp = None
        self.layout = WindowLayout()  # раскладка по умолчанию, если окну не дали общую
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

    def alive(self):
        return [w for w in self.members if w.ended is None]

    def ready(self):
        alive = self.alive()
        return bool(alive) and all(w.ready() for w in alive)

    # --- кнопки
    def start(self):
        if self.state != "idle" or not self.ready():
            return
        self.tmp = tempfile.mkdtemp(prefix="takeone_rec_", dir=self.tmp_root)
        rec_members = self.alive()
        self.multi_rec = len(rec_members) > 1
        self.timeline = Timeline() if self.multi_rec else None
        for i, w in enumerate(rec_members):
            asc = None if self.multi_rec else w.aconf  # общая запись — без звука
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
            self.toast("Пауза — эта часть не попадёт в видео")
        elif self.state == "paused":
            if not any(w.recorder and w.ended is None for w in self.members):
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
        if self.multi_rec:
            name = f"{self.app_name}_вместе_{stamp}"
        else:
            ln = self.members[0].link if self.members else None
            model = ((ln.props.get("model") or ln.device_name) if ln else "") or ""
            model = "".join(ch if ch.isalnum() or ch in "-" else "_" for ch in model).strip("_")[:30]
            name = f"{self.app_name}_{model + '_' if model else ''}{stamp}"
        import save_dialog
        folder = save_dialog.last_dir(self.video_dir)
        ext = save_dialog.load_prefs().get("ext_android", "mp4")
        who = (f"{len(files)} {plural(len(files), 'телефон', 'телефона', 'телефонов')} вместе, без звука"
               if self.multi_rec else (self.members[0].link.props.get("model") or self.members[0].link.device_name
                                       if self.members else ""))
        res, dest = save_dialog.ask(parent, self.app_name, save_dialog.unique_name(folder, name, ext),
                                    kind="android", folder=folder,
                                    info=" · ".join(v for v in (f"Длительность {fmt_hms(duration)}", who) if v))
        if res != "save":
            shutil.rmtree(self.tmp, ignore_errors=True)
            self._done("Запись удалена")
            return then and then()
        tmp, multi, limit = self.tmp, self.multi_rec, self.limit_mb
        anchor = parent
        fname = os.path.basename(dest)
        if multi:
            title = "Склеиваю видео" + (f" · до {fmt_mb(limit)} МБ" if limit else "")
            note = f"{len(files)} {plural(len(files), 'экран', 'экрана', 'экранов')} рядом, без звука. " \
                   "Трансляция продолжается."
        else:
            title, note = "Сохраняю видео", "Трансляция продолжается."
        info = {"title": title, "note": note, "frac": 0.0, "file": fname}
        self.save_card(info)

        def progress(f, new_title=None, new_note=None):
            def upd():
                info["frac"] = f
                if new_title:
                    info["title"] = new_title
                if new_note:
                    info["note"] = new_note
                if self.state == "saving":
                    self.save_card(info)
            anchor.after(0, upd)

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
                remember_video(dest)
                self._done(f"Сохранено: {os.path.basename(dest)} · {mb:.1f} МБ", folder=os.path.dirname(dest))
            if then:
                then()

        threading.Thread(target=work, daemon=True).start()

    def save_card(self, info):
        for w in self.members:
            w._save_card(info)

    def _done(self, msg, folder=None):
        self.state, self.elapsed, self.since, self.timeline = "idle", 0.0, None, None
        self.save_card(None)
        self.refresh()
        self.toast(msg, folder)

    def member_ended(self, w):
        if self.state in ("recording", "paused"):
            self.stop(parent=w)


# --- окно телефона -------------------------------------------------------------------------
class DeviceWindow(tk.Toplevel):
    def __init__(self, master, link, group, shots_dir, on_closed, size_frac=0.86, app_name="Takeone",
                 layout=None, sharp=True):
        super().__init__(master, bg=C["video"])
        self.link, self.group, self.shots_dir = link, group, shots_dir
        self.on_closed, self.size_frac, self.app_name = on_closed, size_frac, app_name
        self.layout = layout or group.layout
        self.layout.add(self)
        self.sharp = sharp and IS_MAC  # чёткий показ через Core Animation (только Mac)
        self.native = None
        self.native_tried = False
        self._ov_rects = {}
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
        self.aconf = None
        self.dev_size = None
        self.retried_no_audio = False
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
        self.video_retries = 0
        self.audio_none = None
        self.input_warned = False
        self.got_frame = self.noframe_warned = self.noframe_shown = False
        link.run(self._on_session, self._on_video, self._on_audio_config, self._on_audio, self._on_ended,
                 self._on_audio_none)
        self.after(10, self._tick)
        self.after(1000, self._watch_log)

    # --- построение окна
    def _build(self):
        bar = C["panel"]
        top = tk.Frame(self, bg=bar, height=TOP_H)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        tk.Frame(self, bg=C["line"], height=1).pack(side="top", fill="x")
        left = tk.Frame(top, bg=bar)
        left.pack(side="left", padx=(10, 0), pady=(TOP_H - RecPill.H) // 2)
        self.pill = RecPill(left, self._rec_click, bg=bar)
        self.pill.pack(side="left")
        self.b_pause = IconButton(left, "pause", lambda: self.group.toggle_pause(), "Пауза  (Ctrl+P)", bg=bar)
        self.b_stop = IconButton(left, "stop", self.stop_rec, "Остановить и сохранить  (Ctrl+S)", bg=bar)
        self.b_pause.pack(side="left", padx=(4, 0))
        self.b_stop.pack(side="left", padx=(4, 0))
        right = tk.Frame(top, bg=bar)
        right.pack(side="right", padx=(0, 8))
        self.b_sound = IconButton(right, "sound", self.toggle_sound, "Звук на компьютере  (Ctrl+M)", bg=bar)
        self.b_shot = IconButton(right, "camera", self.screenshot, "Скриншот  (Ctrl+Shift+S)", bg=bar)
        self.b_sound.pack(side="right")
        self.b_shot.pack(side="right", padx=(0, 2))

        bottom = tk.Frame(self, bg=bar, height=BOT_H)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)
        tk.Frame(self, bg=C["line"], height=1).pack(side="bottom", fill="x")
        nav = tk.Frame(bottom, bg=bar)
        nav.place(relx=0.5, rely=0.5, anchor="center")
        self.nav_buttons = []
        for icon, code, tip in (("back", KEY_BACK, "Назад  (Esc, правая кнопка мыши)"),
                                ("home", KEY_HOME, "Домой  (средняя кнопка мыши)"),
                                ("recents", KEY_APP_SWITCH, "Недавние приложения")):
            b = IconButton(nav, icon, lambda c=code: self.link.key(c), tip, bg=bar)
            b.pack(side="left", padx=20)
            self.nav_buttons.append(b)

        self.canvas = tk.Canvas(self, bg=C["video"], highlightthickness=0, bd=0, width=MIN_W, height=640)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.img_item = self.canvas.create_image(0, 0, anchor="nw")
        self.msg_item = self.canvas.create_text(MIN_W / 2, 320, text="Подключение…", fill=C["muted"],
                                                font=font(14), justify="center")
        self._frame_state = None
        self._save_info = None
        self._disc_btn = None
        self._update_controls()

    def set_group(self, group):
        """Перенести окно в другую группу записи (только когда запись не идёт)."""
        if group is self.group:
            return
        old = self.group
        if self in old.members:
            old.members.remove(self)
        self.group = group
        group.add(self)
        old.refresh()
        group.refresh()

    # --- чёткий показ на Mac
    def _native_start(self):
        try:
            import mac_video
            self.update_idletasks()
            self.native = mac_video.MacVideo(self, self.canvas)
            self.canvas.bind("<Configure>", lambda e: self._native_place(), add="+")
            self.link.note(f"чёткий показ включён (Retina ×{self.native.scale:g})")
            self._draw_frame()
            for tag in ("toast", "savecard", "banner"):
                self._mirror(tag)
        except Exception as e:  # noqa
            self.native = None
            self.link.note(f"чёткий показ недоступен: {e!r}")

    def _native_stop(self, err=None):
        if self.native is not None:
            if err is not None:
                self.link.note(f"чёткий показ выключен из-за ошибки: {err!r}")
            self.native.close()
            self.native = None

    def _native_place(self):
        if self.native is not None:
            try:
                self.native.place()
                for tag in ("toast", "savecard", "banner"):
                    self._mirror(tag)
            except Exception as e:  # noqa
                self._native_stop(e)

    def _mirror(self, tag):
        """Повторить подсказку (плашку, карточку, предупреждение) поверх нативного видео."""
        if self.native is None:
            return
        try:
            import mac_video
            from ui_kit import _families
            prims = []
            if self.canvas.find_withtag(tag):
                prims = list(self._ov_rects.get(tag, [])) + mac_video.text_prims(self.canvas, tag,
                                                                                  _families()["mono"])
            self.native.set_overlay(tag, prims)
        except Exception as e:  # noqa
            self._native_stop(e)
            self.canvas.itemconfigure(self.img_item, state="normal")

    def _ov_clear(self, tag):
        self.canvas.delete(tag)
        self._mirror(tag)

    def _csize(self):
        c = self.canvas
        return max(c.winfo_width(), int(c.cget("width"))), max(c.winfo_height(), int(c.cget("height")))

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
        both = f" ×{sum(1 for m in g.members if m.recorder)}" if g.multi_rec and s in ("recording", "paused") else ""
        if s == "recording":
            self.pill.set("recording", t + both, blink=int(time.monotonic() * 2) % 2 == 0)
        elif s == "paused":
            self.pill.set("paused", t + both)
        elif s == "saving":
            self.pill.set("saving", "Сохранение", enabled=False, spin=int(time.monotonic() * 12) % 12 * 30)
        else:
            n = len(g.alive())
            self.pill.set("idle", f"Запись ×{n}" if n > 1 else "Запись", enabled=alive and g.ready())
        active = s in ("recording", "paused")
        self.b_pause.set("play" if s == "paused" else "pause", active,
                         "Продолжить  (Ctrl+P)" if s == "paused" else "Пауза  (Ctrl+P)")
        self.b_stop.set(enabled=active, tone="raised" if active else "")
        self.b_shot.set(enabled=alive and self.shown_seq >= 0)
        if self.player is None:
            self.b_sound.set("muted", False, "Звук недоступен (нужен Android 11+ и включённый звук)", tone="")
        else:
            muted = self.player.muted
            self.b_sound.set("muted" if muted else "sound", alive,
                             "Включить звук  (Ctrl+M)" if muted else "Выключить звук  (Ctrl+M)",
                             tone="warn" if muted else "")
        for b in self.nav_buttons:
            b.set(enabled=alive)
        fs = s if (active and self.recorder is not None and alive) else None
        if fs != self._frame_state:
            self._frame_state = fs
            self._draw_frame()

    def _draw_frame(self):
        """Тонкая рамка вокруг экрана: красная — идёт запись, янтарная — пауза."""
        c = self.canvas
        c.delete("recframe")
        col = None
        if self._frame_state:
            col = C["rec"] if self._frame_state == "recording" else C["paused_frame"]
        if self.native is not None:
            try:
                self.native.set_border(col)
            except Exception as e:  # noqa
                self._native_stop(e)
        if not col:
            return
        cw, ch = self._csize()
        c.create_rectangle(1, 1, cw - 1, ch - 1, outline=col, width=2, tags=("recframe",))
        c.tag_lower("recframe")
        c.tag_raise("recframe", self.img_item)

    # --- данные от телефона (потоки)
    def _on_session(self, w, h):
        self.dev_size = (w, h)

    def _on_video(self, data, pts, key, config):
        rec = self.recorder
        if rec:
            rec.video(data, pts, key, config)
        self.decoder.decode(data)

    def _on_audio_config(self, aconf):
        self.aconf = aconf
        if self.player:
            self.player.set_config(aconf)

    def _on_audio(self, data, pts):
        rec = self.recorder
        if rec:
            rec.audio(data, pts)
        if self.player:
            self.player.push(data)

    def _on_audio_none(self, code):
        self.audio_none = code

    def _watch_log(self):
        """Следим за логом сервера: известные проблемы показываем понятным текстом."""
        if self._closing:
            return
        text = self.link.new_log()
        ln = self.link
        if (self.ended is None and not self.got_frame and not self.noframe_warned and ln.started_at
                and not getattr(self, "_reconnecting", False) and time.monotonic() - ln.started_at > 8):
            self.noframe_warned = True
            _, seq = self.decoder.latest()
            ln.note(f"нет изображения через 8 с: видеопакетов {ln.vpackets}, размер {ln.vsize}, "
                    f"кадров декодировано {seq + 1}, max_size {ln.max_size}, кодировщик {ln.encoder or 'по умолчанию'}")
            self.noframe_shown = True
            model = ln.props.get("model") or ln.device_name
            sdk = ln.props.get("sdk") or "?"
            self._show_disconnected(
                "Нет изображения",
                "Телефон подключён, но картинка не приходит. Разблокируйте экран телефона и подождите "
                "пару секунд. Не помогло — попробуйте упрощённый режим видео.",
                detail=f"{model} · API {sdk} · видеопакетов: {ln.vpackets}",
                btn_text="Упрощённый режим", btn_cmd=self._simple_video, icon="phone")
        if "INJECT_EVENTS" in text and not self.input_warned:
            self.input_warned = True
            self._banner(input_help(self.link.props.get("manufacturer")))
        if self.audio_none is not None and self.audio_none is not True:
            self.audio_none = True
            if self.player:
                self.player.close()
                self.player = None
            sdk = self.link.props.get("sdk", 0)
            if 0 < sdk < 30:
                ver = {21: "5", 22: "5.1", 23: "6", 24: "7", 25: "7.1", 26: "8", 27: "8.1", 28: "9", 29: "10"}
                self._toast(f"Звука нет: нужен Android 11+, на телефоне Android {ver.get(sdk, sdk)}")
            elif sdk == 30:
                self._toast("Звука нет: на Android 11 разблокируйте экран и нажмите сюда",
                            action=lambda: self.reconnect(audio=True))
            else:
                self._toast("Телефон не отдал звук — работаю без звука")
        self.after(1000, self._watch_log)

    def _banner(self, info):
        """Постоянная подсказка вверху видео (закрывается крестиком)."""
        if isinstance(info, str):
            info = dict(title=info, intro="", steps=[], note="")
        c = self.canvas
        self._ov_clear("banner")
        cw, _ = self._csize()
        x0, x1, top, pad = 12, cw - 12, 12, 14
        inner = x1 - x0 - 2 * pad
        tx, y = x0 + pad, top + pad
        tags = ("banner", "banner_fg")
        paint(c, 18, 18, icon_shapes("alert", 0, 0, 18, C["amber_text"]), tag=tags, x=tx, y=y, clear=False)
        title = c.create_text(tx + 28, y - 1, text=info["title"], anchor="nw", fill=C["hint_title"],
                              width=inner - 28 - 30, font=font(14, "semibold"), tags=tags)
        paint(c, 28, 28, icon_shapes("close", 6, 6, 16, C["hint_title"]), tag=tags + ("banner_x",),
              x=x1 - pad - 24, y=y - 6, clear=False)
        y = max(c.bbox(title)[3], y + 18) + 8
        parts = []  # (текст, цвет, размер, отступ сверху)
        if info.get("intro"):
            parts.append((info["intro"], C["hint_text"], 13, 0))
        for i, step in enumerate(info.get("steps") or [], 1):
            parts.append((f"{i}.  {step}", C["hint_title"], 13, 8 if i == 1 else 3))
        if info.get("note"):
            parts.append((info["note"], C["hint_note"], 12, 10))
        for text, col, size, gap in parts:
            item = c.create_text(tx, y + gap, text=text, anchor="nw", fill=col, width=inner, font=font(size),
                                 tags=tags)
            y = c.bbox(item)[3]
        bottom = y + pad + 2
        paint(c, x1 - x0, bottom - top, box(0, 0, x1 - x0, bottom - top, 14, C["amber_bg"], C["amber_line"]),
              tag=("banner", "banner_bg"), x=x0, y=top, clear=False)
        c.tag_lower("banner_bg", "banner_fg")
        self._ov_rects["banner"] = [
            {"rect": (x0, top, x1 - x0, bottom - top), "r": 14, "fill": C["amber_bg"], "line": C["amber_line"]},
            {"text": "⚠", "rect": (tx - 1, top + pad - 2, 22, 20), "size": 15, "color": C["amber_text"]},
            {"text": "✕", "rect": (x1 - pad - 22, top + pad - 3, 22, 20), "size": 14, "color": C["hint_title"],
             "align": "center"}]
        self._mirror("banner")
        c.tag_bind("banner_x", "<ButtonRelease-1>", lambda e: self._ov_clear("banner"))
        c.tag_bind("banner_x", "<Enter>", lambda e: c.configure(cursor=""))
        c.tag_bind("banner_x", "<Leave>", lambda e: c.configure(cursor=""))

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
            if not self.got_frame:
                self.got_frame = True
                if self.noframe_shown:  # картинка всё-таки пришла — убрать экран «Нет изображения»
                    self.noframe_shown = False
                    self._clear_disc()
                    self.canvas.itemconfigure(self.img_item, state="normal")
                self.link.note(f"первый кадр через {time.monotonic() - (self.link.started_at or 0):.1f} с, "
                               f"размер {self.dev_size}")
            self._show(frame)
            self.shown_seq = seq
        self._update_controls()
        if self.ended is not None and not self.ended_handled:
            self.ended_handled = True
            self._handle_ended()
        self.after(15, self._tick)

    # --- отключение и переподключение
    def _handle_ended(self):
        link = self.link
        quick = link.started_at is not None and time.monotonic() - link.started_at < 10
        self.group.member_ended(self)
        log = link.log_text()
        if quick and any(m in log for m in VIDEO_FAIL_MARKS) and self.video_retries < 2:
            # Кодировщик видео на телефоне не справился: сначала меньше разрешение, потом программный кодировщик.
            ms, enc = self._video_fallback()
            self._show_disconnected("Переподключаюсь…", "Видеокодер телефона дал сбой — пробую упрощённый режим видео.",
                                    button=False)
            self.reconnect(max_size=ms, encoder=enc,
                           note=f"Видеокодер телефона дал сбой — показываю в {ms}p"
                                + (" (программное кодирование)" if enc else ""))
            return
        if quick and link.audio and not self.retried_no_audio:
            # Частая причина на некоторых телефонах — сбой захвата звука на стороне Android:
            # пробуем сразу ещё раз, но без звука.
            self.retried_no_audio = True
            self._show_disconnected("Переподключаюсь…", "Телефон не смог отдать звук — пробую без звука.",
                                    button=False)
            self.reconnect(audio=False, note="Звук с этого телефона не удалось получить — работаю без звука")
            return
        hint = link.error_hint()
        self._show_disconnected("Телефон отключён",
                                "Проверьте кабель и что экран телефона разблокирован, затем подключите снова.",
                                detail=hint or (self.ended if self.ended not in (None, "соединение закрыто") else ""))

    def _clear_disc(self):
        self.canvas.delete("disc")
        if self._disc_btn is not None:
            self._disc_btn.destroy()
            self._disc_btn = None

    def _video_fallback(self):
        """Следующая ступень «упрощённого» видео: меньше разрешение, затем программный кодировщик."""
        link = self.link
        self.video_retries += 1
        if self.video_retries == 1:
            return (1280 if (link.max_size == 0 or link.max_size > 1280) else 1024), None
        ms = min(link.max_size or 1280, 1280)
        return ms, (SOFT_ENCODERS[0] if link.props.get("sdk", 0) >= 29 else SOFT_ENCODERS[1])

    def _simple_video(self):
        ms, enc = self._video_fallback()
        self.link.note(f"упрощённый режим по кнопке: max_size {ms}, кодировщик {enc or 'по умолчанию'}")
        self.reconnect(max_size=ms, encoder=enc,
                       note=f"Упрощённый режим: {ms}p" + (" (программное кодирование)" if enc else ""))

    def _show_disconnected(self, title, text="", detail="", button=True, btn_text=None, btn_cmd=None,
                           icon=None):
        """Экран вместо видео: телефон отключён или идёт переподключение."""
        c = self.canvas
        self._clear_disc()
        c.delete("recframe")
        c.itemconfigure(self.img_item, state="hidden")
        if self.native is not None:
            self.native.set_visible(False)
        c.itemconfigure(self.msg_item, text="")
        cw, ch = self._csize()
        cx, maxw = cw / 2, min(300, cw - 48)
        tags = ("disc",)
        items = []  # (высота, функция размещения по y)

        def tile(y):
            paint(c, 56, 56, box(0, 0, 56, 56, 16, C["panel"], C["line"])
                  + icon_shapes(icon or ("phone_off" if button else "refresh"), 14, 14, 28, C["text2"]),
                  tag=tags, x=cx - 28, y=y, clear=False)
        items.append((56, tile))

        def text_item(t, f, col, width=maxw):
            it = c.create_text(cx, 0, text=t, anchor="n", fill=col, font=f, width=width, justify="center",
                               tags=tags)
            bb = c.bbox(it)
            items.append((bb[3] - bb[1], lambda y: c.coords(it, cx, y)))

        text_item(title, font(18, "semibold"), C["text"])
        if text:
            text_item(text, font(13), C["text2"])
        if detail:
            f = font(12, mono=True)
            it = c.create_text(cx, 0, text=detail, anchor="n", fill=C["muted"], font=f, width=maxw - 20,
                               justify="center", tags=tags)
            bb = c.bbox(it)
            bw, bh = bb[2] - bb[0] + 20, bb[3] - bb[1] + 16

            def place(y, it=it, bw=bw, bh=bh):
                c.coords(it, cx, y + 8)
                paint(c, bw, bh, box(0, 0, bw, bh, 8, C["mono_bg"], C["elev"]), tag=("disc", "disc_bg"),
                      x=cx - bw / 2, y=y, clear=False)
                c.tag_lower("disc_bg", it)
            items.append((bh, place))
        if button:
            self._disc_btn = Button(c, btn_text or "Подключить снова", btn_cmd or self.reconnect, kind="primary",
                                    icon="refresh", height=40, radius=10, size=14, bg=C["video"])

            def place_btn(y):
                c.create_window(cx, y + 4, anchor="n", window=self._disc_btn, tags=tags)
            items.append((44, place_btn))
        gap = 16
        total = sum(h for h, _ in items) + gap * (len(items) - 1)
        y = max(16, (ch - total) / 2)
        for h, place in items:
            place(y)
            y += h + gap
        c.tag_raise("disc")

    def reconnect(self, audio=None, note=None, max_size=None, encoder=None):
        if self._closing or getattr(self, "_reconnecting", False):
            return
        self._reconnecting = True
        self._clear_disc()
        self.canvas.itemconfigure(self.msg_item, text="Подключение…")
        if self.native is not None:
            self.native.set_visible(False)
        old = self.link
        new = old.clone(audio=audio, max_size=max_size, encoder=encoder)

        def work():
            old.stop()
            try:
                new.start()
                err = None
            except Exception as e:  # noqa
                err = str(e).split("\n")[0]
            self.after(0, lambda: done(err))

        def done(err):
            self._reconnecting = False
            if self._closing:
                threading.Thread(target=new.stop, daemon=True).start()
                return
            if err:
                self._show_disconnected("Не удалось подключиться",
                                        "Проверьте кабель и разрешение «Отладка по USB» на телефоне.", detail=err)
                return
            self.link = new
            self.decoder = VideoDecoder()
            if self.player and not new.audio:
                self.player.close()
                self.player = None
            self.aconf = None
            self.dev_size, self.img_rect, self.shown_seq = None, None, -1
            self.got_frame = self.noframe_warned = self.noframe_shown = False
            self.ended, self.ended_handled = None, False
            self.canvas.itemconfigure(self.img_item, state="normal")
            self.canvas.itemconfigure(self.msg_item, text="Подключение…")
            self.title(f"{new.device_name} — {self.app_name}")
            if self.player is None and new.audio:
                try:
                    self.player = AudioPlayer()
                except Exception:  # noqa
                    self.player = None
            self.audio_none = None
            new.run(self._on_session, self._on_video, self._on_audio_config, self._on_audio, self._on_ended,
                    self._on_audio_none)
            if note:
                self._toast(note)

        threading.Thread(target=work, daemon=True).start()

    def _fit_window(self):
        """Окно ровно по пропорциям экрана телефона; растягивать вручную нельзя.
        При повороте телефона окно поворачивается вместе с ним."""
        w, h = self.dev_size
        self.sized_for = self.dev_size
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        n = max(1, len(self.layout.windows))
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
        self.after_idle(self._draw_frame)
        if self._save_info:
            self.after_idle(lambda: self._save_card(self._save_info))
        self.after(50, self.layout.arrange)

    def _show(self, frame):
        from PIL import ImageTk
        cw, ch = int(self.canvas.cget("width")), int(self.canvas.cget("height"))
        rw, rh = self.canvas.winfo_width(), self.canvas.winfo_height()
        if rw > 10 and rh > 10:
            cw, ch = rw, rh
        fw, fh = frame.width, frame.height
        scale = min(cw / fw, ch / fh)
        w, h = max(2, round(fw * scale)), max(2, round(fh * scale))
        if self.sharp and not self.native_tried:
            self.native_tried = True
            self._native_start()
        if self.native is not None:
            try:
                k = self.native.scale
                pw, ph = max(2, round(w * k)) & ~1, max(2, round(h * k)) & ~1
                try:
                    arr = frame.to_ndarray(width=pw, height=ph, format="bgra",
                                           interpolation="AREA" if pw < fw else "BICUBIC")
                except TypeError:
                    arr = frame.to_ndarray(width=pw, height=ph, format="bgra")
                import numpy as np
                self.native.show(np.ascontiguousarray(arr))
                x0, y0 = (cw - w) // 2, (ch - h) // 2
                self.img_rect = (x0, y0, w, h)
                self.canvas.itemconfigure(self.img_item, state="hidden")
                if self.canvas.itemcget(self.msg_item, "text") == "Подключение…":
                    self.canvas.itemconfigure(self.msg_item, text="")
                return
            except Exception as e:  # noqa — что-то пошло не так: дальше показываем по-старому
                self._native_stop(e)
                self.canvas.itemconfigure(self.img_item, state="normal")
        try:  # AREA — честное усреднение при уменьшении: мелкий текст читается лучше
            img = frame.to_image(width=w, height=h, interpolation="AREA" if w < fw else "BICUBIC")
        except TypeError:
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
            for seq, fn in (("r", lambda: self.group.start()), ("p", lambda: self.group.toggle_pause()),
                            ("s", self.stop_rec),
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
        tags = self.canvas.gettags("current")
        if self._on_toast() or "banner" in tags or "savecard" in tags or "disc" in tags:
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
            # scrcpy умеет «печатать» только ASCII; кириллицу и прочее вставляем через буфер обмена
            self.link.send(msg_text(e.char) if e.char.isascii() else msg_paste(e.char))

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

    def _toast(self, text, folder=None, sticky=False, action=None):
        """Плашка внизу видео; со ссылкой на папку или действием — кликабельная."""
        if self._closing:
            return
        if self._toast_job:
            self.after_cancel(self._toast_job)
            self._toast_job = None
        c = self.canvas
        self._ov_clear("toast")
        if not text:
            return
        cw, ch = self._csize()
        bottom = ch - 20
        if self._save_info:  # не перекрывать карточку сохранения
            bb = c.bbox("savecard")
            if bb:
                bottom = bb[1] - 12
        link = bool(folder or action)
        t = c.create_text(cw / 2, bottom - 10, text=text, fill="#F2F3F7", width=cw - 80, justify="center",
                          anchor="s", font=font(13, underline=link), tags=("toast", "toast_fg"))
        x0, y0, x1, y1 = c.bbox(t)
        bw, bh = x1 - x0 + 28, y1 - y0 + 20
        paint(c, bw, bh, box(0, 0, bw, bh, 12, C["toast"]), tag=("toast", "toast_bg"), x=x0 - 14, y=y0 - 10,
              clear=False)
        c.tag_lower("toast_bg", t)
        self._ov_rects["toast"] = [{"rect": (x0 - 14, y0 - 10, bw, bh), "r": 12, "fill": C["toast"]}]
        self._mirror("toast")
        c.tag_unbind("toast", "<ButtonRelease-1>")
        if link:
            c.tag_bind("toast", "<ButtonRelease-1>",
                       lambda e: (self._ov_clear("toast"), action()) if action else open_folder(folder))
            c.tag_bind("toast", "<Enter>", lambda e: c.configure(cursor=""))
            c.tag_bind("toast", "<Leave>", lambda e: c.configure(cursor=""))
        if not sticky:
            self._toast_job = self.after(9000 if action else (4500 if folder else 2200),
                                         lambda: self._ov_clear("toast"))

    def _save_card(self, info):
        """Карточка внизу видео: что происходит с записью и сколько осталось."""
        if self._closing:
            return
        c = self.canvas
        self._ov_clear("savecard")
        self._save_info = dict(info) if info else None
        if not info:
            return
        self._ov_clear("toast")
        cw, ch = self._csize()
        x0, x1, pad = 16, cw - 16, 16
        W, inner = x1 - x0, x1 - x0 - 2 * pad
        frac = min(1.0, max(0.0, info.get("frac") or 0.0))
        tags = ("savecard", "save_fg")
        note = " · ".join(v for v in (info.get("file"), info.get("note")) if v)
        nt = c.create_text(x0 + pad, 0, text=note, anchor="nw", fill=C["text2"], width=inner, font=font(12),
                           tags=tags)
        nb = c.bbox(nt)
        nh = nb[3] - nb[1] if note else 0
        H = pad + 20 + 12 + 6 + (12 + nh if note else 0) + pad
        y0 = ch - 16 - H
        paint(c, W, H, box(0, 0, W, H, 14, C["card"], C["card_line"]), tag=("savecard", "save_bg"),
              x=x0, y=y0, clear=False)
        pf = font(14, "semibold", mono=True)
        pct = f"{int(frac * 100)}%"
        c.create_text(x1 - pad, y0 + pad + 10, text=pct, anchor="e", fill=C["text"], font=pf, tags=tags)
        tf = font(14, "semibold")
        c.create_text(x0 + pad, y0 + pad + 10, anchor="w", fill=C["text"], font=tf, tags=tags,
                      text=info.get("title") or "Сохраняю видео", width=inner - measure(pf, pct) - 12)
        by = y0 + pad + 20 + 12
        fill_w = round(inner * frac)
        paint(c, inner, 6, box(0, 0, inner, 6, 3, C["hover"]) + (box(0, 0, max(6, fill_w), 6, 3, C["text"])
                                                                if fill_w > 0 else []),
              tag=tags, x=x0 + pad, y=by, clear=False)
        c.coords(nt, x0 + pad, by + 6 + 12)
        c.tag_lower("save_bg", "save_fg")
        c.tag_raise("savecard")
        self._ov_rects["savecard"] = [
            {"rect": (x0, y0, W, H), "r": 14, "fill": C["card"], "line": C["card_line"]},
            {"rect": (x0 + pad, by, inner, 6), "r": 3, "fill": C["hover"]}]
        if fill_w > 0:
            self._ov_rects["savecard"].append({"rect": (x0 + pad, by, max(6, fill_w), 6), "r": 3, "fill": C["text"]})
        self._mirror("savecard")

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
        self._native_stop()
        self.group.remove(self)
        self.layout.remove(self)
        threading.Thread(target=self.link.stop, daemon=True).start()
        if self.player:
            self.player.close()
        self.destroy()
        self.on_closed(self)

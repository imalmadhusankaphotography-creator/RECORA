# -*- coding: utf-8 -*-
"""
RECORA - lightweight screen recorder for Windows
================================================

Why it stays light (and why JARVIS's voice won't shake):

* The screen is grabbed ON THE GPU (Windows Desktop Duplication API, FFmpeg
  "ddagrab") and encoded on the GPU's video engine (Intel Quick Sync, NVIDIA
  NVENC, AMD AMF or Windows Media Foundation). Frames never go through the CPU.
* FFmpeg runs at BELOW-NORMAL priority, so JARVIS (and its TTS playback)
  always wins the CPU if the two ever compete.
* System sound is copied with WASAPI loopback straight into a WAV file: no
  resampling, no effects, and nothing touches the playback path.
* Video and sound are joined into one MP4 only after you press Stop.

Hotkey: F9 = start / stop (works while the window is hidden).
"""

import os
import sys
import json
import time
import wave
import queue
import ctypes
import shutil
import datetime
import threading
import subprocess
import collections

try:
    import pyaudiowpatch as pyaudio          # WASAPI loopback (system sound)
except Exception:                             # pragma: no cover
    pyaudio = None

IS_WIN = os.name == "nt"

APP_NAME = "RECORA"
APP_VERSION = "1.0.0"

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


# ----------------------------------------------------------------- paths ----
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    return os.path.join(getattr(sys, "_MEIPASS", app_dir()), name)


def config_file():
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(root, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


def default_output_dir():
    return os.path.join(os.path.expanduser("~"), "Videos", APP_NAME)


def find_ffmpeg():
    exe = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    base = app_dir()
    for cand in (os.path.join(base, exe),
                 os.path.join(base, "ffmpeg", exe),
                 os.path.join(base, "ffmpeg", "bin", exe)):
        if os.path.isfile(cand):
            return cand
    return shutil.which("ffmpeg")


def ffmpeg_signature(path):
    try:
        return "%s|%d|%d" % (path, os.path.getsize(path), int(os.path.getmtime(path)))
    except OSError:
        return None


# ---------------------------------------------------------------- config ----
DEFAULTS = {
    "output_dir": default_output_dir(),
    "fps": 30,
    "monitor": 0,
    "system_audio": True,
    "mic": False,
    "hide_window": True,
    "countdown": True,
    "encoder": None,          # cached encoder id after a successful check
    "encoder_ffmpeg": None,   # which ffmpeg.exe that check was done with
    "audio_offset_ms": 0,     # sync nudge: + delays sound, - makes sound earlier
    "mic_volume": 1.0,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(config_file(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(config_file(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception:
        pass


# -------------------------------------------------------------- encoders ----
# Tried in this order; the first one that really encodes a test clip wins.
# "post" is appended to the ddagrab filter (GPU frames come out of ddagrab).
ENCODERS = [
    {"id": "nvenc", "label": "NVIDIA NVENC (GPU)", "gpu": True, "pre": [], "post": "",
     "enc": ["-c:v", "h264_nvenc", "-preset", "p3", "-rc", "vbr", "-cq", "24", "-b:v", "0"]},
    {"id": "qsv_vpp", "label": "Intel Quick Sync (GPU)", "gpu": True,
     "pre": ["-init_hw_device", "d3d11va=dx", "-filter_hw_device", "dx"],
     "post": ",hwmap=derive_device=qsv,format=qsv,vpp_qsv=format=nv12",
     "enc": ["-c:v", "h264_qsv", "-preset", "veryfast", "-q:v", "25"]},
    {"id": "qsv", "label": "Intel Quick Sync (GPU)", "gpu": True, "pre": [],
     "post": ",hwmap=derive_device=qsv,format=qsv",
     "enc": ["-c:v", "h264_qsv", "-preset", "veryfast", "-q:v", "25"]},
    {"id": "amf", "label": "AMD AMF (GPU)", "gpu": True, "pre": [], "post": "",
     "enc": ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "24", "-qp_p", "26"]},
    {"id": "mf", "label": "Windows Media Foundation (GPU)", "gpu": True, "pre": [],
     "post": ",hwdownload,format=bgra,format=nv12",
     "enc": ["-c:v", "h264_mf", "-hw_encoding", "1", "-b:v", "8M"]},
    {"id": "x264", "label": "CPU x264 (fallback, heavier)", "gpu": False, "pre": [],
     "post": ",hwdownload,format=bgra,format=yuv420p",
     "enc": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-threads", "2"]},
    {"id": "gdi_x264", "label": "CPU x264 + GDI (last resort)", "gpu": False, "gdi": True,
     "enc": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-threads", "2",
             "-pix_fmt", "yuv420p"]},
]
ENCODER_BY_ID = {e["id"]: e for e in ENCODERS}


def video_cmd(ffmpeg, enc, fps, monitor, out_path=None, test_frames=None):
    """FFmpeg command that grabs the screen and writes video only."""
    cmd = [ffmpeg, "-hide_banner", "-nostats",
           "-loglevel", "error" if test_frames else "info"]
    if enc.get("gdi"):
        cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1",
                "-i", "desktop", "-map", "0:v"]
    else:
        graph = "ddagrab=output_idx=%d:framerate=%d:draw_mouse=1%s[v]" % (
            int(monitor), int(fps), enc["post"])
        cmd += list(enc["pre"]) + ["-filter_complex", graph, "-map", "[v]"]
    if test_frames:
        cmd += ["-frames:v", str(test_frames)]
    cmd += list(enc["enc"]) + ["-g", str(int(fps) * 2)]
    if test_frames:
        cmd += ["-f", "null", "-"]
    else:
        cmd += ["-f", "matroska", "-y", out_path]
    return cmd


def mux_cmd(ffmpeg, video, system_wav, mic_wav, out_path, offset_ms=0, mic_volume=1.0):
    """Join video + sound(s). apad + -shortest = sound is cut/padded to video length."""
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-i", video]
    tracks = []
    for kind, path in (("sys", system_wav), ("mic", mic_wav)):
        if path and os.path.isfile(path) and os.path.getsize(path) > 4096:
            tracks.append(kind)
            cmd += ["-i", path]
    if not tracks:
        return cmd + ["-map", "0:v", "-c:v", "copy", "-movflags", "+faststart", out_path]

    # sync nudge: + delays the sound, - starts the sound earlier
    offset_ms = int(offset_ms or 0)
    if offset_ms > 0:
        shift = "adelay=delays=%d:all=1," % offset_ms
    elif offset_ms < 0:
        shift = "atrim=start=%.3f,asetpts=PTS-STARTPTS," % (-offset_ms / 1000.0)
    else:
        shift = ""
    vol = "volume=%.2f," % float(mic_volume) if abs(float(mic_volume) - 1.0) > 1e-3 else ""
    if len(tracks) == 1:
        graph = "[1:a]%s%sapad[a]" % (shift, vol if tracks[0] == "mic" else "")
    else:
        graph = ("[1:a]%sanull[s];[2:a]%s%sanull[m];"
                 "[s][m]amix=inputs=2:duration=longest:normalize=0,apad[a]" % (shift, shift, vol))
    return cmd + ["-filter_complex", graph, "-map", "0:v", "-map", "[a]",
                  "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                  "-shortest", "-movflags", "+faststart", out_path]


def popen_flags():
    return (CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS) if IS_WIN else 0


def tail_text(data, n=12):
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    lines = [l for l in (data or "").splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def detect_encoder(ffmpeg, monitor=0, progress=None):
    """Encode a tiny test clip with each encoder; return (encoder, log)."""
    log = []
    for enc in ENCODERS:
        if progress:
            progress(enc["label"])
        cmd = video_cmd(ffmpeg, enc, 30, monitor, test_frames=15)
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=25,
                               creationflags=popen_flags())
            if r.returncode == 0:
                log.append("%-9s OK" % enc["id"])
                return enc, log
            log.append("%-9s failed: %s" % (enc["id"], tail_text(r.stderr, 2)))
        except subprocess.TimeoutExpired:
            log.append("%-9s failed: timeout" % enc["id"])
        except OSError as exc:
            log.append("%-9s failed: %s" % (enc["id"], exc))
    return None, log


# ----------------------------------------------------------------- audio ----
class GapFillWavWriter(threading.Thread):
    """Writes PCM chunks to a WAV file on its own thread.

    WASAPI loopback sends NOTHING while the PC is silent, so a plain writer
    would squash the pauses and drift out of sync. Every chunk carries the
    time it arrived; when a chunk lands later than the audio written so far,
    the gap is filled with silence.
    """

    GAP_SECONDS = 0.20

    def __init__(self, path, channels, rate, sampwidth=2):
        super().__init__(daemon=True)
        self.path, self.channels, self.rate, self.sampwidth = path, channels, rate, sampwidth
        self.q = queue.Queue()
        self.t0 = None
        self.t_end = None
        self.frames_written = 0

    def push(self, t, data):
        self.q.put((t, data))

    def finish(self, t_end):
        self.t_end = t_end
        self.q.put(None)

    def _silence(self, wf, frames):
        frame_bytes = self.channels * self.sampwidth
        step = self.rate                      # write at most 1 s at a time
        while frames > 0:
            n = min(step, frames)
            wf.writeframesraw(b"\x00" * (n * frame_bytes))
            frames -= n
            self.frames_written += n

    def run(self):
        frame_bytes = self.channels * self.sampwidth
        gap_limit = int(self.rate * self.GAP_SECONDS)
        wf = wave.open(self.path, "wb")
        try:
            wf.setnchannels(self.channels)
            wf.setsampwidth(self.sampwidth)
            wf.setframerate(self.rate)
            while True:
                item = self.q.get()
                if item is None:
                    break
                t, data = item
                n = len(data) // frame_bytes
                if n <= 0:
                    continue
                if self.t0 is not None:
                    expected_start = int((t - self.t0) * self.rate) - n
                    gap = expected_start - self.frames_written
                    if gap > gap_limit:
                        self._silence(wf, gap)
                wf.writeframesraw(data[: n * frame_bytes])
                self.frames_written += n
            if self.t0 is not None and self.t_end is not None:
                gap = int((self.t_end - self.t0) * self.rate) - self.frames_written
                if gap > gap_limit:
                    self._silence(wf, gap)
        finally:
            wf.close()


class AudioCapture:
    """System sound (WASAPI loopback) and/or microphone, each to its own WAV."""

    def __init__(self, folder, want_system, want_mic):
        self.folder = folder
        self.want_system = want_system
        self.want_mic = want_mic
        self.pa = None
        self.streams = []        # (stream, writer)
        self.capturing = False
        self.system_wav = None
        self.mic_wav = None

    # ---- device lookup
    def _loopback_device(self):
        try:
            return self.pa.get_default_wasapi_loopback()
        except Exception:
            pass
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if speakers.get("isLoopbackDevice"):
            return speakers
        for dev in self.pa.get_loopback_device_info_generator():
            if speakers["name"] in dev["name"]:
                return dev
        raise RuntimeError("No loopback device for '%s'" % speakers["name"])

    def _mic_device(self):
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        idx = wasapi.get("defaultInputDevice", -1)
        if idx is None or idx < 0:
            raise RuntimeError("No microphone found")
        return self.pa.get_device_info_by_index(idx)

    def _open(self, dev, path):
        rate = int(dev["defaultSampleRate"])
        max_ch = int(dev["maxInputChannels"]) or 2
        last_err = None
        for ch in dict.fromkeys([max_ch, 2, 1]):
            writer = GapFillWavWriter(path, ch, rate)
            try:
                stream = self.pa.open(format=pyaudio.paInt16, channels=ch, rate=rate,
                                      input=True, input_device_index=dev["index"],
                                      frames_per_buffer=1024,
                                      stream_callback=self._callback(writer))
            except Exception as exc:
                last_err = exc
                continue
            writer.start()
            self.streams.append((stream, writer))
            return
        raise RuntimeError(str(last_err))

    def _callback(self, writer):
        def cb(in_data, frame_count, time_info, status):
            if self.capturing and in_data:
                writer.push(time.perf_counter(), in_data)
            return (None, pyaudio.paContinue)
        return cb

    # ---- public
    def open(self):
        """Open the streams (data is ignored until begin()). Returns warnings."""
        warnings = []
        self.pa = pyaudio.PyAudio()
        if self.want_system:
            try:
                dev = self._loopback_device()
                self.system_wav = os.path.join(self.folder, "system.wav")
                self._open(dev, self.system_wav)
            except Exception as exc:
                self.system_wav = None
                warnings.append("System sound not recorded (%s)" % exc)
        if self.want_mic:
            try:
                dev = self._mic_device()
                self.mic_wav = os.path.join(self.folder, "mic.wav")
                self._open(dev, self.mic_wav)
            except Exception as exc:
                self.mic_wav = None
                warnings.append("Microphone not recorded (%s)" % exc)
        return warnings

    def begin(self, t0):
        for _, writer in self.streams:
            writer.t0 = t0
        self.capturing = True

    def stop(self):
        t_end = time.perf_counter()
        self.capturing = False
        for stream, writer in self.streams:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            writer.finish(t_end)
        for _, writer in self.streams:
            writer.join(timeout=30)
        self.streams = []
        if self.pa is not None:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None


# --------------------------------------------------------------- session ----
class RecordingSession:
    def __init__(self, ffmpeg, enc, cfg):
        self.ffmpeg = ffmpeg
        self.enc = enc
        self.cfg = dict(cfg)
        out_dir = self.cfg["output_dir"]
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.output = os.path.join(out_dir, "RECORA_%s.mp4" % stamp)
        tmp_root = os.path.join(out_dir, ".recora_tmp")
        self.tmp = os.path.join(tmp_root, stamp)
        os.makedirs(self.tmp, exist_ok=True)
        if IS_WIN:
            try:
                ctypes.windll.kernel32.SetFileAttributesW(tmp_root, 0x02)   # hidden
            except Exception:
                pass
        self.tmp_root = tmp_root
        self.video_tmp = os.path.join(self.tmp, "video.mkv")
        self.proc = None
        self.audio = None
        self.t_start = None
        self.lines = collections.deque(maxlen=40)
        self.ready = threading.Event()
        self.warnings = []

    def _read_stderr(self):
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.lines.append(line)
            if "Press [q]" in line:
                self.ready.set()

    def start(self):
        want_sys = bool(self.cfg.get("system_audio"))
        want_mic = bool(self.cfg.get("mic"))
        if want_sys or want_mic:
            if pyaudio is None:
                self.warnings.append("Sound module (pyaudiowpatch) missing - video only")
            else:
                try:
                    self.audio = AudioCapture(self.tmp, want_sys, want_mic)
                    self.warnings += self.audio.open()
                except Exception as exc:          # never block the video because of sound
                    self._drop_audio()
                    self.warnings.append("Sound not recorded (%s)" % exc)

        cmd = video_cmd(self.ffmpeg, self.enc, self.cfg["fps"], self.cfg["monitor"],
                        out_path=self.video_tmp)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=popen_flags())
        threading.Thread(target=self._read_stderr, daemon=True).start()

        deadline = time.time() + 10
        while not self.ready.wait(0.03):
            if self.proc.poll() is not None:
                time.sleep(0.2)
                self._drop_audio()
                raise RuntimeError("FFmpeg could not start recording:\n" +
                                   "\n".join(list(self.lines)[-8:]))
            if time.time() > deadline:
                break
        # Video capture is running now - start keeping sound from this moment.
        if self.audio:
            self.audio.begin(time.perf_counter())
        self.t_start = time.time()

    def _drop_audio(self):
        if self.audio:
            self.audio.stop()
            self.audio = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop_and_save(self, progress=None):
        # 1) stop video cleanly ('q' lets FFmpeg finish the file)
        if self.proc is not None:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write(b"q")
                    self.proc.stdin.flush()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        # 2) stop sound (after video, so sound is never shorter than video)
        sys_wav = mic_wav = None
        if self.audio:
            self.audio.stop()
            sys_wav, mic_wav = self.audio.system_wav, self.audio.mic_wav

        if not os.path.isfile(self.video_tmp) or os.path.getsize(self.video_tmp) < 1024:
            raise RuntimeError("No video was written.\n" + "\n".join(list(self.lines)[-8:]))

        # 3) join into the final MP4
        if progress:
            progress("Saving MP4...")
        cmd = mux_cmd(self.ffmpeg, self.video_tmp, sys_wav, mic_wav, self.output,
                      offset_ms=int(self.cfg.get("audio_offset_ms") or 0),
                      mic_volume=float(self.cfg.get("mic_volume") or 1.0))
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=3600, creationflags=popen_flags())
        if r.returncode != 0 or not os.path.isfile(self.output):
            raise RuntimeError("Could not join video and sound. Raw files kept in:\n%s\n\n%s"
                               % (self.tmp, tail_text(r.stderr, 6)))
        shutil.rmtree(self.tmp, ignore_errors=True)
        try:
            os.rmdir(self.tmp_root)          # only if empty
        except OSError:
            pass
        return self.output


# ------------------------------------------------------- windows helpers ----
class HotkeyThread(threading.Thread):
    """Global hotkey (default F9) through RegisterHotKey - no extra libraries."""

    def __init__(self, callback, vk=0x78):
        super().__init__(daemon=True)
        self.callback = callback
        self.vk = vk
        self.ok = False
        self.tid = None
        self.ready = threading.Event()

    def run(self):
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        self.ok = bool(user32.RegisterHotKey(None, 1, 0x4000, self.vk))   # MOD_NOREPEAT
        self.ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:                                     # WM_HOTKEY
                self.callback()
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.tid:
            ctypes.windll.user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)   # WM_QUIT


def monitor_count():
    if IS_WIN:
        try:
            return max(1, int(ctypes.windll.user32.GetSystemMetrics(80)))  # SM_CMONITORS
        except Exception:
            pass
    return 1


def keep_awake(on):
    if IS_WIN:
        try:
            flags = 0x80000000 | (0x00000001 | 0x00000002 if on else 0)
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
        except Exception:
            pass


_MUTEX = None


def single_instance():
    global _MUTEX
    if not IS_WIN:
        return True
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\RECORA_single_instance")
    return ctypes.windll.kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS


def open_path(path):
    try:
        if IS_WIN:
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# -------------------------------------------------------------------- UI ----
BG = "#0e1014"
CARD = "#161a22"
LINE = "#262c3a"
FG = "#e9ecf2"
MUTED = "#8b93a5"
RED = "#ff3b4e"
RED_DARK = "#d92c3e"
STOP_BG = "#e9ecf2"
GREEN = "#34d399"
AMBER = "#fbbf24"
FONT = "Segoe UI" if IS_WIN else "Helvetica"


class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox
        self.tk, self.filedialog, self.messagebox = tk, filedialog, messagebox

        self.cfg = load_config()
        self.ffmpeg = find_ffmpeg()
        self.enc = None
        self.detect_log = []
        self.state = "idle"          # idle | detecting | countdown | starting | recording | saving
        self.session = None
        self.last_file = None
        self.close_after_save = False
        self.events = queue.Queue()
        self._countdown_job = None
        self._last_second = -1

        self.root = tk.Tk()
        self._build_ui()

        self.hotkey = None
        if IS_WIN:
            self.hotkey = HotkeyThread(lambda: self.events.put(("hotkey", None)))
            self.hotkey.start()
            self.hotkey.ready.wait(1.0)
            if not self.hotkey.ok:
                self._status("F9 is used by another app - use the button instead.", AMBER)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(150, self._poll)
        self._init_encoder()

    # ---------------------------------------------------------- widgets
    def _build_ui(self):
        tk = self.tk
        r = self.root
        r.title(APP_NAME)
        r.configure(bg=BG)
        r.resizable(False, False)
        try:
            r.iconbitmap(resource_path("recora.ico"))
        except Exception:
            pass

        self.v_mon = tk.StringVar(value=str(int(self.cfg["monitor"]) + 1))
        self.v_fps = tk.StringVar(value=str(self.cfg["fps"]))
        self.v_sys = tk.BooleanVar(value=bool(self.cfg["system_audio"]))
        self.v_mic = tk.BooleanVar(value=bool(self.cfg["mic"]))
        self.v_hide = tk.BooleanVar(value=bool(self.cfg["hide_window"]))
        self.v_cd = tk.BooleanVar(value=bool(self.cfg["countdown"]))
        self.lockable = []

        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 0))
        tk.Label(head, text=APP_NAME, bg=BG, fg=FG, font=(FONT, 16, "bold")).pack(side="left")
        tk.Label(head, text="  lite screen recorder", bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(side="left", pady=(7, 0))
        self.dot = tk.Label(head, text="●", bg=BG, fg=MUTED, font=(FONT, 13))
        self.dot.pack(side="right")

        self.timer = tk.Label(r, text="00:00:00", bg=BG, fg=FG, font=("Consolas", 30, "bold"))
        self.timer.pack(pady=(4, 2))

        self.btn = tk.Button(r, text="●   START    (F9)", command=self.toggle,
                             bg=RED, fg="white", activebackground=RED_DARK,
                             activeforeground="white", relief="flat", bd=0,
                             font=(FONT, 13, "bold"), cursor="hand2", pady=10)
        self.btn.pack(fill="x", padx=18, pady=(4, 12))

        card = tk.Frame(r, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="x", padx=18)

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(row, text="Screen", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left")
        mons = [str(i + 1) for i in range(monitor_count())]
        if self.v_mon.get() not in mons:
            self.v_mon.set("1")
        self._option(row, self.v_mon, mons).pack(side="left", padx=(6, 16))
        tk.Label(row, text="FPS", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left")
        self._option(row, self.v_fps, ["30", "60"]).pack(side="left", padx=6)

        self.chk_sys = self._check(card, "System sound   (JARVIS voice)", self.v_sys)
        self.chk_mic = self._check(card, "Microphone   (my voice)", self.v_mic)
        self._check(card, "Hide this window while recording", self.v_hide)
        self._check(card, "3-second countdown", self.v_cd)

        row2 = tk.Frame(card, bg=CARD)
        row2.pack(fill="x", padx=12, pady=(8, 10))
        tk.Label(row2, text="Save to", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left")
        self.lbl_dir = tk.Label(row2, text="", bg=CARD, fg=FG, font=(FONT, 9), anchor="w")
        self.lbl_dir.pack(side="left", padx=(6, 4), fill="x", expand=True)
        self._small(row2, "Open", lambda: open_path(self._out_dir()), CARD).pack(side="right")
        b = self._small(row2, "Change", self.choose_dir, CARD)
        b.pack(side="right", padx=4)
        self.lockable.append(b)
        self._show_dir()

        self.status = tk.Label(r, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="nw",
                               justify="left", wraplength=340, height=2)
        self.status.pack(fill="x", padx=18, pady=(10, 0))

        act = tk.Frame(r, bg=BG)
        act.pack(fill="x", padx=18, pady=(4, 0))
        self.btn_play = self._small(act, "▶  Play last", self.play_last, BG)
        self.btn_play.pack(side="left")
        self.btn_play.configure(state="disabled")
        self._small(act, "Folder", lambda: open_path(self._out_dir()), BG).pack(side="left", padx=6)

        foot = tk.Frame(r, bg=BG)
        foot.pack(fill="x", padx=18, pady=(10, 14))
        self.lbl_enc = tk.Label(foot, text="Encoder: -", bg=BG, fg=MUTED, font=(FONT, 9),
                                cursor="hand2", anchor="w")
        self.lbl_enc.pack(side="left")
        self.lbl_enc.bind("<Button-1>", lambda e: self.show_detect_log())
        b = self._small(foot, "Re-check", self.redetect, BG)
        b.pack(side="right")
        self.lockable.append(b)

        if pyaudio is None:
            self.v_sys.set(False)
            self.v_mic.set(False)
            self.chk_sys.configure(state="disabled")
            self.chk_mic.configure(state="disabled")
            self._status("pyaudiowpatch not installed - sound can't be recorded.", AMBER)

        r.minsize(380, 0)

    def _option(self, parent, var, values):
        m = self.tk.OptionMenu(parent, var, *values)
        m.configure(bg=LINE, fg=FG, activebackground="#323a4c", activeforeground=FG,
                    relief="flat", bd=0, highlightthickness=0, font=(FONT, 9), width=3)
        m["menu"].configure(bg=CARD, fg=FG, activebackground=RED, activeforeground="white", bd=0)
        self.lockable.append(m)
        return m

    def _check(self, parent, text, var):
        c = self.tk.Checkbutton(parent, text=text, variable=var, bg=CARD, fg=FG,
                                selectcolor=LINE, activebackground=CARD, activeforeground=FG,
                                font=(FONT, 10), anchor="w", bd=0, highlightthickness=0)
        c.pack(fill="x", padx=10, pady=1)
        self.lockable.append(c)
        return c

    def _small(self, parent, text, cmd, bg):
        return self.tk.Button(parent, text=text, command=cmd, bg=LINE, fg=FG,
                              activebackground="#323a4c", activeforeground=FG, relief="flat",
                              bd=0, font=(FONT, 9), cursor="hand2", padx=8, pady=2,
                              disabledforeground="#4b5263")

    # ---------------------------------------------------------- helpers
    def _status(self, text, color=MUTED):
        self.status.configure(text=text, fg=color)

    def _out_dir(self):
        d = self.cfg.get("output_dir") or default_output_dir()
        os.makedirs(d, exist_ok=True)
        return d

    def _show_dir(self):
        d = self.cfg.get("output_dir") or default_output_dir()
        self.lbl_dir.configure(text=d if len(d) <= 34 else "..." + d[-31:])

    def _collect_cfg(self):
        self.cfg["monitor"] = max(0, int(self.v_mon.get() or 1) - 1)
        self.cfg["fps"] = int(self.v_fps.get() or 30)
        self.cfg["system_audio"] = bool(self.v_sys.get())
        self.cfg["mic"] = bool(self.v_mic.get())
        self.cfg["hide_window"] = bool(self.v_hide.get())
        self.cfg["countdown"] = bool(self.v_cd.get())
        save_config(self.cfg)

    def _lock(self, locked):
        for w in self.lockable:
            try:
                w.configure(state="disabled" if locked else "normal")
            except Exception:
                pass
        if pyaudio is None:
            self.chk_sys.configure(state="disabled")
            self.chk_mic.configure(state="disabled")

    def _set_button(self, mode):
        if mode == "start":
            self.btn.configure(text="●   START    (F9)", bg=RED, fg="white",
                               activebackground=RED_DARK, state="normal")
        elif mode == "stop":
            self.btn.configure(text="■   STOP    (F9)", bg=STOP_BG, fg="#111",
                               activebackground="#cfd3dc", state="normal")
        elif mode == "cancel":
            self.btn.configure(text="Cancel    (F9)", bg=LINE, fg=FG,
                               activebackground="#323a4c", state="normal")
        else:
            self.btn.configure(text=mode, bg=LINE, fg=MUTED, state="disabled")

    def _restore_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    # ---------------------------------------------------------- encoder
    def _init_encoder(self):
        if not self.ffmpeg:
            self.lbl_enc.configure(text="Encoder: ffmpeg.exe not found", fg=RED)
            self._set_button("ffmpeg.exe missing")
            self._status("Put ffmpeg.exe next to RECORA.exe (see README).", RED)
            return
        sig = ffmpeg_signature(self.ffmpeg)
        cached = ENCODER_BY_ID.get(self.cfg.get("encoder") or "")
        if cached and self.cfg.get("encoder_ffmpeg") == sig:
            self._set_encoder(cached)
            return
        self.redetect()

    def redetect(self):
        if self.state not in ("idle",) or not self.ffmpeg:
            return
        self._collect_cfg()
        self.state = "detecting"
        self._set_button("Checking GPU...")
        self.lbl_enc.configure(text="Encoder: checking...", fg=MUTED)
        mon = self.cfg["monitor"]

        def work():
            enc, log = detect_encoder(self.ffmpeg, mon,
                                      progress=lambda l: self.events.put(("detect_progress", l)))
            self.events.put(("detect_done", (enc, log)))
        threading.Thread(target=work, daemon=True).start()

    def _set_encoder(self, enc):
        self.enc = enc
        if enc["gpu"]:
            self.lbl_enc.configure(text="Encoder: %s  ✓" % enc["label"], fg=GREEN)
        else:
            self.lbl_enc.configure(text="Encoder: %s" % enc["label"], fg=AMBER)
            self._status("No GPU encoder worked - CPU fallback is heavier. "
                         "Updating the graphics driver usually fixes this.", AMBER)

    def show_detect_log(self):
        text = "\n".join(self.detect_log) or "Using the encoder saved from the last check.\n" \
                                             "Press Re-check to test again."
        self.messagebox.showinfo("Encoder check", "ffmpeg: %s\n\n%s" % (self.ffmpeg, text))

    # ---------------------------------------------------------- record
    def toggle(self):
        if self.state == "idle":
            self.begin_start()
        elif self.state == "countdown":
            self.cancel_countdown()
        elif self.state == "recording":
            self.begin_stop()

    def begin_start(self):
        if not self.ffmpeg or not self.enc:
            return
        self._collect_cfg()
        self._lock(True)
        if self.cfg["countdown"]:
            self.state = "countdown"
            self._set_button("cancel")
            self._tick(3)
        else:
            self._launch()

    def _tick(self, n):
        if self.state != "countdown":
            return
        if n <= 0:
            self._launch()
            return
        self.timer.configure(text=str(n), fg=RED)
        self.root.title("%d... - %s" % (n, APP_NAME))
        self._countdown_job = self.root.after(1000, self._tick, n - 1)

    def cancel_countdown(self):
        if self._countdown_job:
            self.root.after_cancel(self._countdown_job)
            self._countdown_job = None
        self.state = "idle"
        self.timer.configure(text="00:00:00", fg=FG)
        self.root.title(APP_NAME)
        self._set_button("start")
        self._lock(False)

    def _launch(self):
        self.state = "starting"
        self._set_button("Starting...")
        self.timer.configure(text="00:00:00", fg=FG)
        self._status("Starting...")
        if self.cfg["hide_window"]:
            self.root.iconify()
        delay = 450 if self.cfg["hide_window"] else 50     # let the window get out of view

        def work():
            try:
                sess = RecordingSession(self.ffmpeg, self.enc, self.cfg)
                sess.start()
                self.events.put(("started", sess))
            except Exception as exc:
                self.events.put(("start_failed", str(exc)))
        self.root.after(delay, lambda: threading.Thread(target=work, daemon=True).start())

    def _on_started(self, sess):
        self.session = sess
        self.state = "recording"
        self._last_second = -1
        self._set_button("stop")
        self.dot.configure(fg=RED)
        keep_awake(True)
        if sess.warnings:
            self._status("Recording  •  " + "; ".join(sess.warnings), AMBER)
        else:
            self._status("Recording...  press F9 to stop.", RED)

    def _on_start_failed(self, msg):
        self.state = "idle"
        self._restore_window()
        self._set_button("start")
        self._lock(False)
        self.root.title(APP_NAME)
        self._status("Could not start. The encoder will be checked again.", RED)
        self.messagebox.showerror(APP_NAME, msg[-1500:])
        self.cfg["encoder"] = None
        save_config(self.cfg)
        self.redetect()

    def begin_stop(self):
        if self.state != "recording" or not self.session:
            return
        self.state = "saving"
        self._set_button("Saving...")
        self.dot.configure(fg=AMBER)
        self._status("Saving...")
        self._restore_window()
        self.root.title(APP_NAME)
        keep_awake(False)
        sess = self.session

        def work():
            try:
                path = sess.stop_and_save(progress=lambda t: self.events.put(("progress", t)))
                self.events.put(("saved", path))
            except Exception as exc:
                self.events.put(("save_failed", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _after_save(self):
        self.session = None
        self.state = "idle"
        self.dot.configure(fg=MUTED)
        self.timer.configure(fg=FG)
        self._set_button("start")
        self._lock(False)
        if self.close_after_save:
            self.on_close()

    # ---------------------------------------------------------- loop
    def _poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "hotkey":
                    self.toggle()
                elif kind == "detect_progress":
                    self.lbl_enc.configure(text="Encoder: trying %s..." % data, fg=MUTED)
                elif kind == "detect_done":
                    enc, log = data
                    self.detect_log = log
                    self.state = "idle"
                    if enc:
                        self.cfg["encoder"] = enc["id"]
                        self.cfg["encoder_ffmpeg"] = ffmpeg_signature(self.ffmpeg)
                        save_config(self.cfg)
                        self._set_encoder(enc)
                        self._set_button("start")
                    else:
                        self.lbl_enc.configure(text="Encoder: none worked (click for details)",
                                               fg=RED)
                        self._set_button("No encoder")
                elif kind == "started":
                    self._on_started(data)
                elif kind == "start_failed":
                    self._on_start_failed(data)
                elif kind == "progress":
                    self._status(data)
                elif kind == "saved":
                    self.last_file = data
                    self.btn_play.configure(state="normal")
                    self._status("Saved ✓  " + os.path.basename(data), GREEN)
                    self._after_save()
                elif kind == "save_failed":
                    self._status("Saving failed.", RED)
                    self.messagebox.showerror(APP_NAME, data[-1500:])
                    self._after_save()
        except queue.Empty:
            pass

        if self.state == "recording" and self.session:
            if not self.session.alive():
                self._status("Recording stopped by FFmpeg - saving what was recorded.", AMBER)
                self.begin_stop()
            else:
                secs = int(time.time() - self.session.t_start)
                if secs != self._last_second:
                    self._last_second = secs
                    t = "%02d:%02d:%02d" % (secs // 3600, (secs // 60) % 60, secs % 60)
                    self.timer.configure(text=t, fg=RED)
                    self.root.title("● REC %s - %s" % (t, APP_NAME))
        self.root.after(200, self._poll)

    # ---------------------------------------------------------- misc
    def choose_dir(self):
        d = self.filedialog.askdirectory(initialdir=self._out_dir(), title="Save recordings to")
        if d:
            self.cfg["output_dir"] = os.path.normpath(d)
            save_config(self.cfg)
            self._show_dir()

    def play_last(self):
        if self.last_file and os.path.isfile(self.last_file):
            open_path(self.last_file)

    def on_close(self):
        if self.state == "recording":
            if self.messagebox.askyesno(APP_NAME, "Recording is running. Stop and save it?"):
                self.close_after_save = True
                self.begin_stop()
            return
        if self.state in ("saving", "starting"):
            self.close_after_save = self.state == "saving"
            return
        if self.state == "countdown":
            self.cancel_countdown()
        self._collect_cfg()
        if self.hotkey:
            self.hotkey.stop()
        keep_awake(False)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    if not single_instance():
        import tkinter as tk
        from tkinter import messagebox
        t = tk.Tk()
        t.withdraw()
        messagebox.showinfo(APP_NAME, "RECORA is already running.")
        return
    App().run()


if __name__ == "__main__":
    main()

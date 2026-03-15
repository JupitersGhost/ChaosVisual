#!/usr/bin/env python3
"""
chaosmain.py  v2.0.0
ChaosVisual — Cross-Platform Entropy Harvester Backend

Self-contained entropy harvester with cross-platform support.
Can be run standalone (CLI) or imported by chaosgui.py.

Sources:
  1. Screen capture        → frame diff, LSB, edge noise (mss)
  2. Audio loopback        → WASAPI (Win) / PulseAudio monitor (Linux)
  3. Microphone input      → ADC noise channel
  4. OS RNG                → CryptGenRandom / getrandom / /dev/urandom
  5. CPU timing jitter     → nanosecond instruction noise
  6. Mouse delta           → human movement randomness
  7. Keyboard state        → XOR-diff VK/evdev polling (privacy-safe)

NIST SP 800-90B Pipeline:
  raw → LSB extract → Von Neumann debias (biased sources only)
  → XOR fold → SHA3-256 conditioning → health check → TCP to server

Platform support:
  Windows 11:  pyaudiowpatch (WASAPI), ctypes/user32 mouse+keyboard
  Linux (Mint/Debian/openSUSE): pyaudio (PulseAudio/PipeWire/ALSA),
                                 evdev or pynput keyboard, Xlib mouse
  macOS:       pyaudio (CoreAudio), pynput keyboard/mouse
"""

import hashlib
import json
import logging
import math
import os
import platform
import socket
import struct
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────
# PLATFORM DETECTION
# ─────────────────────────────────────────────

PLATFORM = platform.system()
IS_WINDOWS = PLATFORM == "Windows"
IS_LINUX = PLATFORM == "Linux"
IS_MACOS = PLATFORM == "Darwin"

PLATFORM_INFO: Dict[str, str] = {
    "os": PLATFORM,
    "release": platform.release(),
    "distro": "unknown",
    "display_server": "unknown",
    "audio_backend": "unknown",
}

if IS_LINUX:
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    PLATFORM_INFO["distro"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except Exception:
        pass
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type:
        PLATFORM_INFO["display_server"] = session_type
    elif os.environ.get("WAYLAND_DISPLAY"):
        PLATFORM_INFO["display_server"] = "wayland"
    elif os.environ.get("DISPLAY"):
        PLATFORM_INFO["display_server"] = "x11"
elif IS_WINDOWS:
    PLATFORM_INFO["distro"] = f"Windows {platform.version()}"
    PLATFORM_INFO["display_server"] = "win32"
    PLATFORM_INFO["audio_backend"] = "wasapi"

# ─────────────────────────────────────────────
# OPTIONAL DEPS (cross-platform)
# ─────────────────────────────────────────────

NUMPY_OK = False
try:
    import numpy as np
    NUMPY_OK = True
except ImportError:
    print("[WARN] numpy missing: pip install numpy")

MSS_OK = False
try:
    import mss
    MSS_OK = True
except ImportError:
    print("[WARN] mss missing: pip install mss")

# Audio backend: try pyaudiowpatch (Windows WASAPI) first, then standard pyaudio
_pyaudio = None
_PA_GLOBAL = None
PYAUDIO_OK = False
AUDIO_BACKEND_NAME = "none"

if IS_WINDOWS:
    try:
        import pyaudiowpatch as _pyaudio
        PYAUDIO_OK = True
        AUDIO_BACKEND_NAME = "pyaudiowpatch (WASAPI)"
    except ImportError:
        pass

if not PYAUDIO_OK:
    try:
        import pyaudio as _pyaudio
        PYAUDIO_OK = True
        AUDIO_BACKEND_NAME = "pyaudio (ALSA/PulseAudio)"
    except ImportError:
        pass

if PYAUDIO_OK:
    try:
        _PA_GLOBAL = _pyaudio.PyAudio()
    except Exception:
        PYAUDIO_OK = False
        AUDIO_BACKEND_NAME = "none (init failed)"

if IS_LINUX and PYAUDIO_OK:
    PLATFORM_INFO["audio_backend"] = AUDIO_BACKEND_NAME

# Keyboard backend
CTYPES_OK = False
EVDEV_OK = False
PYNPUT_OK = False
_user32 = None
_GetAsyncKeyState = None

if IS_WINDOWS:
    try:
        import ctypes
        import ctypes.wintypes
        _user32 = ctypes.windll.user32
        _GetAsyncKeyState = _user32.GetAsyncKeyState
        _GetAsyncKeyState.argtypes = [ctypes.c_int]
        _GetAsyncKeyState.restype = ctypes.c_short
        CTYPES_OK = True
    except Exception:
        pass

if IS_LINUX:
    try:
        import evdev as _evdev_mod
        EVDEV_OK = True
    except ImportError:
        pass

try:
    from pynput import keyboard as _pynput_kb
    PYNPUT_OK = True
except ImportError:
    _pynput_kb = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ChaosVisual] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("chaosvisual")


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

def _default_config_path() -> str:
    if IS_LINUX or IS_MACOS:
        config_dir = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        ) / "chaosvisual"
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            return str(config_dir / "config.json")
        except OSError:
            pass
    return "config.json"


DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8213,
        "send_interval_sec": 2.0,
        "retry_delay_sec": 15.0,
    },
    "sources": {
        "screen": {
            "enabled": True,
            "fps": 4,
            "monitor": 1,
            "resize_width": 320,
            "resize_height": 180,
        },
        "loopback": {
            "enabled": True,
            "device_index": None,
            "channels": None,
            "sample_rate": 44100,
            "chunk_duration_ms": 100,
            "lsb_bits": 4,
            "exclusive_mode": False,
        },
        "mic": {
            "enabled": True,
            "device_index": None,
            "channels": 1,
            "sample_rate": 44100,
            "chunk_duration_ms": 100,
            "lsb_bits": 4,
        },
        "os_rng": {
            "enabled": True,
            "bytes_per_sample": 256,
        },
        "timing_jitter": {
            "enabled": True,
            "iterations": 200,
        },
        "mouse": {
            "enabled": True,
        },
        "keyboard": {
            "enabled": True,
        },
    },
    "processing": {
        "output_bytes": 64,
        "min_shannon_threshold": 6.0,
        "stats_interval_sec": 30.0,
    },
    "debug": {
        "dry_run": False,
        "verbose": False,
        "save_local_fallback": True,
        "fallback_path": "entropy_fallback.bin",
    },
}


def load_config(path: str = "") -> dict:
    if not path:
        path = _default_config_path()
    p = Path(path)
    if not p.exists():
        log.info(f"Creating default config: {p.absolute()}")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return _deep_merge({}, DEFAULT_CONFIG)
    try:
        with open(p) as f:
            user = json.load(f)
        # Backward compat: migrate legacy "akari" key → "server"
        if "akari" in user and "server" not in user:
            user["server"] = user.pop("akari")
        return _deep_merge(DEFAULT_CONFIG, user)
    except Exception as e:
        log.error(f"Config error: {e} — using defaults")
        return _deep_merge({}, DEFAULT_CONFIG)


def _deep_merge(base: dict, override: dict) -> dict:
    r = base.copy()
    for k, v in override.items():
        r[k] = _deep_merge(r[k], v) if (k in r and isinstance(r[k], dict)
                                         and isinstance(v, dict)) else v
    return r


# ─────────────────────────────────────────────
# MATH / NIST
# ─────────────────────────────────────────────

def _byte_freq(data: bytes) -> list:
    freq = [0] * 256
    for b in data:
        freq[b] += 1
    return freq


def shannon_naive(data: bytes) -> float:
    if not data:
        return 0.0
    freq = _byte_freq(data)
    n = len(data)
    h = 0.0
    for c in freq:
        if c:
            p = c / n
            h -= p * math.log2(p)
    return h


def shannon_miller_madow(data: bytes) -> float:
    """Miller-Madow bias-corrected Shannon estimator (Miller 1955)."""
    if not data:
        return 0.0
    freq = _byte_freq(data)
    n = len(data)
    h = 0.0
    distinct = 0
    for c in freq:
        if c:
            distinct += 1
            p = c / n
            h -= p * math.log2(p)
    correction = (distinct - 1) / (2 * n) if n > 0 else 0.0
    return min(h + correction, 8.0)


def min_entropy(data: bytes) -> float:
    """Min-entropy H∞ = −log₂(p_max). NIST SP 800-90B primary metric."""
    if not data:
        return 0.0
    freq = _byte_freq(data)
    p_max = max(freq) / len(data)
    return -math.log2(p_max) if p_max > 0 else 0.0


def extract_lsb(data: bytes, bits: int = 4) -> bytes:
    mask = (1 << bits) - 1
    return bytes(b & mask for b in data)


def von_neumann_debias(data: bytes) -> bytes:
    """SP 800-90B §4.4.1 Von Neumann debiasing. For biased sources only."""
    bits = []
    for byte_val in data:
        for i in range(0, 8, 2):
            b1 = (byte_val >> (7 - i)) & 1
            b2 = (byte_val >> (6 - i)) & 1
            if b1 != b2:
                bits.append(b1)
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        v = 0
        for j in range(8):
            v = (v << 1) | bits[i + j]
        out.append(v)
    return bytes(out)


def xor_fold(data: bytes, out_len: int = 64) -> bytes:
    folded = bytearray(out_len)
    for i, b in enumerate(data):
        folded[i % out_len] ^= b
    return bytes(folded)


def sha3_condition(data: bytes, n: int = 64) -> bytes:
    result = b""
    ctr = 0
    while len(result) < n:
        result += hashlib.sha3_256(data + ctr.to_bytes(4, "big")).digest()
        ctr += 1
    return result[:n]


def health_label(sh_mm: float, min_ent: float) -> str:
    if sh_mm >= 7.9 and min_ent >= 7.0:
        return "excellent"
    if sh_mm >= 7.5 and min_ent >= 6.5:
        return "healthy"
    if sh_mm >= 6.0:
        return "acceptable"
    if sh_mm >= 4.0:
        return "degraded"
    return "poor"


# ─────────────────────────────────────────────
# MOUSE POSITION (cross-platform)
# ─────────────────────────────────────────────

def _mouse_pos() -> Tuple[int, int]:
    """Get current mouse cursor position. Cross-platform."""
    if IS_WINDOWS:
        try:
            import ctypes as _ct
            class _PT(_ct.Structure):
                _fields_ = [("x", _ct.c_long), ("y", _ct.c_long)]
            pt = _PT()
            _ct.windll.user32.GetCursorPos(_ct.byref(pt))
            return (pt.x, pt.y)
        except Exception:
            return (0, 0)
    elif IS_LINUX:
        # Try Xlib first (fast, no subprocess)
        try:
            from Xlib import display as xdisplay
            d = xdisplay.Display()
            data = d.screen().root.query_pointer()._data
            return (data["root_x"], data["root_y"])
        except Exception:
            pass
        # Fallback: xdotool
        try:
            out = subprocess.check_output(
                ["xdotool", "getmouselocation", "--shell"],
                timeout=1, stderr=subprocess.DEVNULL
            ).decode()
            x = y = 0
            for line in out.splitlines():
                if line.startswith("X="):
                    x = int(line[2:])
                elif line.startswith("Y="):
                    y = int(line[2:])
            return (x, y)
        except Exception:
            return (0, 0)
    else:
        return (0, 0)


# ─────────────────────────────────────────────
# AUDIO HELPERS (cross-platform)
# ─────────────────────────────────────────────

def _find_pulse_monitor_sources() -> List[dict]:
    """
    Query PulseAudio/PipeWire for monitor sources directly via pactl.
    This catches sources that PyAudio misses on some Linux distros.
    Returns a list of dicts with name, description, sample_rate, channels.
    """
    monitors = []
    if not IS_LINUX:
        return monitors
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[1]
                if ".monitor" in name.lower() or "monitor" in name.lower():
                    monitors.append({
                        "name": name,
                        "description": parts[1],
                    })
    except Exception:
        pass
    return monitors


def _find_pulse_device_index_for_monitor(monitor_name: str) -> Optional[int]:
    """
    Given a PulseAudio monitor source name (e.g. 'alsa_output.pci-..monitor'),
    find its device index in PyAudio by matching names.
    """
    if not PYAUDIO_OK or _PA_GLOBAL is None:
        return None
    try:
        for i in range(_PA_GLOBAL.get_device_count()):
            info = _PA_GLOBAL.get_device_info_by_index(i)
            dev_name = info.get("name", "")
            if monitor_name in dev_name or dev_name in monitor_name:
                if int(info.get("maxInputChannels", 0)) > 0:
                    return i
    except Exception:
        pass
    return None


def scan_audio_devices() -> Tuple[List[dict], List[dict]]:
    """
    Returns (loopback_list, input_list). Cross-platform.

    Windows: pyaudiowpatch isLoopbackDevice flag.
    Linux:   PyAudio device scan + PulseAudio monitor source fallback.
             Many Linux distros (including Mint) don't expose monitor
             sources through PyAudio's default ALSA enumeration. We
             additionally query pactl to find them and try to match
             them back to PyAudio device indices.
    """
    lb_list: List[dict] = []
    mic_list: List[dict] = []

    if not PYAUDIO_OK or _PA_GLOBAL is None:
        return lb_list, mic_list

    seen_indices = set()

    try:
        for i in range(_PA_GLOBAL.get_device_count()):
            try:
                info = _PA_GLOBAL.get_device_info_by_index(i)
                max_in = int(info.get("maxInputChannels", 0))
                max_out = int(info.get("maxOutputChannels", 0))
                sr = int(info.get("defaultSampleRate", 44100))
                name = str(info.get("name", f"Device {i}"))

                host_api_idx = int(info.get("hostApi", 0))
                try:
                    host_api = _PA_GLOBAL.get_host_api_info_by_index(host_api_idx)
                    host_api_name = host_api.get("name", "")
                except Exception:
                    host_api_name = ""

                # Loopback detection
                is_lb = False
                if IS_WINDOWS:
                    is_lb = bool(info.get("isLoopbackDevice", False))
                elif IS_LINUX:
                    is_lb = "monitor" in name.lower()

                if is_lb:
                    if IS_WINDOWS:
                        ch = max(1, min(max_out if max_out > 0 else max_in, 2))
                    else:
                        ch = max(1, min(max_in if max_in > 0 else 2, 2))
                    lb_list.append(dict(
                        index=i, name=name, channels=ch, sr=sr,
                        max_in=max_in, max_out=max_out,
                        host_api=host_api_name,
                        label=f"{name}  [{ch}ch {sr}Hz]  idx={i}",
                    ))
                    seen_indices.add(i)
                elif max_in > 0:
                    ch = max(1, min(max_in, 2))
                    mic_list.append(dict(
                        index=i, name=name, channels=ch, sr=sr,
                        max_in=max_in, max_out=max_out,
                        host_api=host_api_name,
                        label=f"{name}  [{ch}ch {sr}Hz]  idx={i}",
                    ))
                    seen_indices.add(i)
            except Exception:
                pass
    except Exception as exc:
        log.debug(f"Device scan: {exc}")

    # ── Linux fallback: query pactl for monitor sources not seen by PyAudio ──
    if IS_LINUX and len(lb_list) == 0:
        pulse_monitors = _find_pulse_monitor_sources()
        if pulse_monitors:
            log.info(f"pactl found {len(pulse_monitors)} monitor sources "
                     f"not visible to PyAudio")
            for pm in pulse_monitors:
                idx = _find_pulse_device_index_for_monitor(pm["name"])
                if idx is not None and idx not in seen_indices:
                    try:
                        info = _PA_GLOBAL.get_device_info_by_index(idx)
                        max_in = int(info.get("maxInputChannels", 0))
                        sr = int(info.get("defaultSampleRate", 44100))
                        ch = max(1, min(max_in, 2))
                        lb_list.append(dict(
                            index=idx, name=pm["name"], channels=ch, sr=sr,
                            max_in=max_in, max_out=0,
                            host_api="PulseAudio",
                            label=f"{pm['name']}  [{ch}ch {sr}Hz]  idx={idx}",
                        ))
                        seen_indices.add(idx)
                    except Exception:
                        pass

            # If still nothing matched in PyAudio, add them as info-only
            # with a None index so the GUI can show them and the user
            # can debug. We also try opening by name via pulse.
            if len(lb_list) == 0 and pulse_monitors:
                log.warning(
                    "PulseAudio has monitor sources but PyAudio can't see them. "
                    "This is common on Linux Mint. Possible fixes:\n"
                    "  1. pip install pyaudio  (make sure it's built against pulse)\n"
                    "  2. sudo apt install portaudio19-dev && pip install --force-reinstall pyaudio\n"
                    "  3. Set PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native before running"
                )
                for pm in pulse_monitors:
                    lb_list.append(dict(
                        index=None, name=f"[pactl] {pm['name']}", channels=2,
                        sr=44100, max_in=2, max_out=0,
                        host_api="PulseAudio (pactl only)",
                        label=f"[pactl] {pm['name']}  [2ch 44100Hz]  (not in PyAudio)",
                    ))

    return lb_list, mic_list


def _open_stream(device_index: Optional[int],
                 channels: Optional[int],
                 sample_rate: int,
                 chunk_samples: int,
                 is_loopback: bool):
    """
    Open an audio stream. Cross-platform.

    Windows: pyaudiowpatch with WASAPI loopback auto-detection.
    Linux:   standard pyaudio with PulseAudio/PipeWire backend.
    """
    pya = _pyaudio.PyAudio()
    try:
        if device_index is None and is_loopback:
            if IS_WINDOWS:
                # Auto-detect WASAPI loopback of default output
                try:
                    wasapi = pya.get_host_api_info_by_type(_pyaudio.paWASAPI)
                    default_out = wasapi["defaultOutputDevice"]
                    default_info = pya.get_device_info_by_index(default_out)
                    for i in range(pya.get_device_count()):
                        d = pya.get_device_info_by_index(i)
                        if d["name"] == default_info["name"] and d.get("isLoopbackDevice"):
                            device_index = i
                            log.info(f"Loopback auto: '{d['name']}' idx={i}")
                            break
                    if device_index is None:
                        device_index = default_out
                        log.warning(f"No loopback variant found, using output idx={device_index}")
                except Exception as e:
                    log.warning(f"WASAPI loopback detect failed: {e}")
            elif IS_LINUX:
                # Auto-detect PulseAudio monitor source
                for i in range(pya.get_device_count()):
                    try:
                        d = pya.get_device_info_by_index(i)
                        name = d.get("name", "").lower()
                        if "monitor" in name and int(d.get("maxInputChannels", 0)) > 0:
                            device_index = i
                            log.info(f"Loopback auto (monitor): '{d['name']}' idx={i}")
                            break
                    except Exception:
                        pass
                if device_index is None:
                    # Try via pactl
                    monitors = _find_pulse_monitor_sources()
                    for pm in monitors:
                        idx = _find_pulse_device_index_for_monitor(pm["name"])
                        if idx is not None:
                            device_index = idx
                            log.info(f"Loopback auto (pactl→PyAudio): '{pm['name']}' idx={idx}")
                            break
                    if device_index is None and monitors:
                        log.warning(
                            f"Found {len(monitors)} PulseAudio monitors but none visible "
                            f"to PyAudio. Try rebuilding pyaudio against portaudio19."
                        )

        # Determine channels from device metadata
        if channels is None and device_index is not None:
            info = pya.get_device_info_by_index(device_index)
            if is_loopback and IS_WINDOWS:
                ch = int(info.get("maxOutputChannels", 0))
                if ch == 0:
                    ch = int(info.get("maxInputChannels", 1))
            else:
                ch = int(info.get("maxInputChannels", 1))
            channels = max(1, min(ch, 2))
            log.info(f"  auto channels={channels} "
                     f"(maxOut={info.get('maxOutputChannels', 0)} "
                     f"maxIn={info.get('maxInputChannels', 0)})")

        channels = channels or 1

        if device_index is None:
            raise RuntimeError("No audio device found for this source")

        stream = pya.open(
            format=_pyaudio.paInt16,
            channels=channels,
            rate=sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=chunk_samples,
        )
        kind = "loopback" if is_loopback else "mic"
        log.info(f"Audio stream [{kind}]: idx={device_index} "
                 f"{channels}ch {sample_rate}Hz")
        return pya, stream, channels

    except Exception:
        try:
            pya.terminate()
        except Exception:
            pass
        raise


def _audio_extract(stream, chunk_samples: int, channels: int,
                   lsb_bits: int) -> Optional[bytes]:
    """Extract entropy from one audio chunk."""
    if not NUMPY_OK:
        return None
    try:
        raw = stream.read(chunk_samples, exception_on_overflow=False)
    except Exception:
        return None

    samples = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        # Truncate to exact multiple of channels before reshape
        trim = len(samples) - (len(samples) % channels)
        samples = samples[:trim].reshape(-1, channels).mean(axis=1).astype(np.int16)

    srcs = []
    # 1. ADC LSBs → Von Neumann
    lsb = extract_lsb(samples.tobytes(), lsb_bits)
    vn = von_neumann_debias(lsb)
    if vn:
        srcs.append(hashlib.sha3_256(vn).digest())
    # 2. FFT phase spectrum
    fft = np.fft.rfft(samples.astype(np.float32))
    srcs.append(hashlib.sha3_256(np.angle(fft).astype(np.float32).tobytes()).digest())
    # 3. High-freq magnitude noise floor
    mag = np.abs(fft).astype(np.float32)
    srcs.append(hashlib.sha3_256(mag[len(mag)//2:].tobytes()).digest())
    # 4. Timestamp + amplitude variance
    srcs.append(hashlib.sha3_256(
        struct.pack(">Q", time.time_ns())
        + struct.pack(">f", float(np.std(samples)))
        + samples.tobytes()[:64]
    ).digest())

    result = bytearray(32)
    for s in srcs:
        for i, b in enumerate(s):
            result[i % 32] ^= b
    return bytes(result)


# ─────────────────────────────────────────────
# SOURCES
# ─────────────────────────────────────────────

class ScreenSource:
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True) and MSS_OK
        self.fps = cfg.get("fps", 4)
        self.monitor_idx = cfg.get("monitor", 1)
        self.w = cfg.get("resize_width", 320)
        self.h = cfg.get("resize_height", 180)
        self._prev: Optional[bytes] = None
        if self.enabled:
            log.info(f"Screen: {self.w}x{self.h} @{self.fps}fps mon={self.monitor_idx}")
        else:
            log.warning("Screen: DISABLED (mss not available or disabled)")

    def collect(self) -> Optional[bytes]:
        if not self.enabled:
            return None
        try:
            with mss.mss() as sct:
                mons = sct.monitors
                mon = mons[self.monitor_idx] if self.monitor_idx < len(mons) else mons[1]
                raw_img = sct.grab(mon)
                fw, fh = raw_img.width, raw_img.height
                sx, sy = max(1, fw // self.w), max(1, fh // self.h)
                img = bytes(raw_img.raw)
                pixels = bytearray()
                for row in range(0, fh, sy):
                    for col in range(0, fw, sx):
                        idx = (row * fw + col) * 4
                        if idx + 2 < len(img):
                            pixels.append(img[idx + 2])
                pixels = bytes(pixels[:self.w * self.h])

            srcs = []
            vn = von_neumann_debias(extract_lsb(pixels, 4))
            if vn:
                srcs.append(hashlib.sha3_256(vn).digest())
            if self._prev and len(self._prev) == len(pixels):
                diff = bytes(a ^ b for a, b in zip(pixels, self._prev))
                srcs.append(hashlib.sha3_256(diff).digest())
            seed = int.from_bytes(os.urandom(4), "big")
            blocks = bytearray()
            for k in range(16):
                pos = (seed * (k + 1) * 6364136223846793005) % max(1, len(pixels) - 64)
                blocks.extend(pixels[pos:pos + 64])
            srcs.append(hashlib.sha3_256(bytes(blocks)).digest())
            srcs.append(hashlib.sha3_256(
                struct.pack(">Q", time.time_ns()) + pixels[:64]
            ).digest())
            self._prev = pixels

            result = bytearray(32)
            for s in srcs:
                for i, b in enumerate(s):
                    result[i % 32] ^= b
            return bytes(result)
        except Exception as e:
            log.debug(f"Screen error: {e}")
            return None


class LoopbackAudioSource:
    """System audio loopback — WASAPI (Win) / PulseAudio monitor (Linux)."""
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True) and PYAUDIO_OK and NUMPY_OK
        self._dev_idx = cfg.get("device_index", None)
        self._channels = cfg.get("channels", None)
        self.sample_rate = cfg.get("sample_rate", 44100)
        self.chunk_ms = cfg.get("chunk_duration_ms", 100)
        self.lsb_bits = cfg.get("lsb_bits", 4)
        self.chunk_samples = int(self.sample_rate * self.chunk_ms / 1000)
        self._pya = None
        self._stream = None
        self._ch = 1

        if self.enabled:
            self._setup()
        else:
            log.warning("Loopback: DISABLED")

    def _setup(self):
        try:
            self._pya, self._stream, self._ch = _open_stream(
                self._dev_idx, self._channels,
                self.sample_rate, self.chunk_samples, is_loopback=True)
        except Exception as e:
            log.warning(f"Loopback setup failed: {e}")
            self.enabled = False

    def collect(self) -> Optional[bytes]:
        if not self.enabled or not self._stream:
            return None
        try:
            return _audio_extract(self._stream, self.chunk_samples, self._ch, self.lsb_bits)
        except Exception as e:
            log.debug(f"Loopback collect: {e}")
            return None

    def close(self):
        for obj in [self._stream, self._pya]:
            if obj:
                try:
                    getattr(obj, "stop_stream", lambda: None)()
                    getattr(obj, "close", lambda: None)()
                    getattr(obj, "terminate", lambda: None)()
                except Exception:
                    pass


class MicAudioSource:
    """Microphone / input device — ADC quantization noise."""
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True) and PYAUDIO_OK and NUMPY_OK
        self._dev_idx = cfg.get("device_index", None)
        self._channels = cfg.get("channels", 1)
        self.sample_rate = cfg.get("sample_rate", 44100)
        self.chunk_ms = cfg.get("chunk_duration_ms", 100)
        self.lsb_bits = cfg.get("lsb_bits", 4)
        self.chunk_samples = int(self.sample_rate * self.chunk_ms / 1000)
        self._pya = None
        self._stream = None
        self._ch = 1

        if self.enabled:
            self._setup()
        else:
            log.warning("Mic: DISABLED")

    def _setup(self):
        try:
            self._pya, self._stream, self._ch = _open_stream(
                self._dev_idx, self._channels,
                self.sample_rate, self.chunk_samples, is_loopback=False)
        except Exception as e:
            log.warning(f"Mic setup failed: {e}")
            self.enabled = False

    def collect(self) -> Optional[bytes]:
        if not self.enabled or not self._stream:
            return None
        try:
            return _audio_extract(self._stream, self.chunk_samples, self._ch, self.lsb_bits)
        except Exception as e:
            log.debug(f"Mic collect: {e}")
            return None

    def close(self):
        for obj in [self._stream, self._pya]:
            if obj:
                try:
                    getattr(obj, "stop_stream", lambda: None)()
                    getattr(obj, "close", lambda: None)()
                    getattr(obj, "terminate", lambda: None)()
                except Exception:
                    pass


class OsRngSource:
    """os.urandom — CryptGenRandom (Win) / getrandom (Linux)."""
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        self.nbytes = max(128, cfg.get("bytes_per_sample", 256))
        if self.enabled:
            rng_name = "CryptGenRandom" if IS_WINDOWS else "getrandom/urandom"
            log.info(f"OS RNG: {self.nbytes} bytes/sample ({rng_name})")

    def collect(self) -> Optional[bytes]:
        if not self.enabled:
            return None
        try:
            raw = os.urandom(self.nbytes)
            ts = struct.pack(">Q", time.time_ns())
            return raw + hashlib.sha3_256(raw + ts).digest()
        except Exception as e:
            log.debug(f"OS RNG error: {e}")
            return None


class TimingJitterSource:
    """CPU timing jitter — branch prediction, cache misses, thermal noise."""
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        self.iters = cfg.get("iterations", 200)
        if self.enabled:
            log.info(f"Timing jitter: {self.iters} iterations")

    def collect(self) -> Optional[bytes]:
        if not self.enabled:
            return None
        try:
            timings = bytearray()
            prev = time.perf_counter_ns()
            for _ in range(self.iters):
                now = time.perf_counter_ns()
                delta = now - prev
                timings += bytes([delta & 0xFF, (delta >> 8) & 0xFF])
                prev = now
                _ = hashlib.md5(timings[-4:]).digest()
            vn = von_neumann_debias(bytes(timings))
            seed = vn if vn else bytes(timings)
            return hashlib.sha3_256(seed + struct.pack(">Q", time.time_ns())).digest()
        except Exception as e:
            log.debug(f"Timing error: {e}")
            return None


class MouseSource:
    """Mouse delta — human entropy. Accumulates 64B before yielding."""
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        self._prev = (0, 0)
        self._acc = bytearray()
        if self.enabled:
            log.info("Mouse: enabled")

    def collect(self) -> Optional[bytes]:
        if not self.enabled:
            return None
        try:
            pos = _mouse_pos()
            dx, dy = pos[0] - self._prev[0], pos[1] - self._prev[1]
            self._prev = pos
            if dx == 0 and dy == 0:
                return None
            self._acc.extend(struct.pack(">hhiiQ", dx, dy, pos[0], pos[1], time.time_ns()))
            if len(self._acc) >= 64:
                result = hashlib.sha3_256(bytes(self._acc)).digest()
                self._acc = bytearray()
                return result
            return None
        except Exception as e:
            log.debug(f"Mouse error: {e}")
            return None


class KeyboardSource:
    """
    Keyboard entropy — privacy-safe XOR-diff state polling.
    Does NOT log keystrokes. Only state transitions + timestamps.
    """
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        self._prev_state = bytearray(256)
        self._acc = bytearray()
        self._method = "none"

        if not self.enabled:
            return

        if IS_WINDOWS and CTYPES_OK:
            self._method = "ctypes"
        elif IS_LINUX and EVDEV_OK:
            self._method = "evdev"
        elif PYNPUT_OK:
            self._method = "pynput"
        else:
            log.warning("Keyboard: no backend available")
            self.enabled = False
            return
        log.info(f"Keyboard: enabled (method: {self._method})")

    def collect(self) -> Optional[bytes]:
        if not self.enabled:
            return None

        if self._method == "ctypes":
            return self._collect_ctypes()
        elif self._method == "evdev":
            return self._collect_timing()  # evdev needs its own thread; use timing here
        elif self._method == "pynput":
            return self._collect_timing()  # same — pynput is callback-based
        return None

    def _collect_ctypes(self) -> Optional[bytes]:
        """Windows: GetAsyncKeyState VK polling."""
        try:
            current = bytearray(256)
            for vk in range(0x08, 0xFF):
                state = _GetAsyncKeyState(vk)
                current[vk] = 1 if (state & 0x8000) else 0

            diff = bytearray(a ^ b for a, b in zip(current, self._prev_state))
            self._prev_state = current

            if any(diff):
                ts = time.perf_counter_ns()
                ts_bytes = ts.to_bytes(8, "little")
                entropy = bytes(b for b in diff if b) + ts_bytes
                return hashlib.sha3_256(entropy).digest()
            return None
        except Exception:
            return None

    def _collect_timing(self) -> Optional[bytes]:
        """Fallback: timing-based entropy from perf_counter jitter."""
        try:
            ts = time.perf_counter_ns()
            self._acc.extend(ts.to_bytes(8, "little"))
            if len(self._acc) >= 64:
                result = hashlib.sha3_256(bytes(self._acc)).digest()
                self._acc = bytearray()
                return result
            return None
        except Exception:
            return None


# ─────────────────────────────────────────────
# MIXER (NIST SP 800-90B)
# ─────────────────────────────────────────────

class EntropyMixer:
    """Multi-source mixing pipeline with Miller-Madow Shannon + NIST min-entropy."""
    NIST_MULT = 0.85

    def __init__(self, cfg: dict):
        self.out_bytes = cfg.get("output_bytes", 64)
        self.min_shannon = cfg.get("min_shannon_threshold", 6.0)

    def mix(self, chunks: Dict[str, Optional[bytes]]) -> Dict[str, Any]:
        ts_ns = time.time_ns()
        ts_sec = ts_ns // 1_000_000_000

        active, src_health, combined = [], [], bytearray()

        for name, data in chunks.items():
            if not data:
                continue
            active.append(name)
            combined.extend(data)
            sh = shannon_miller_madow(data)
            me = min_entropy(data)
            src_health.append({
                "source": name,
                "bytes": len(data),
                "shannon_mm": round(sh, 4),
                "min_entropy": round(me, 4),
                "status": health_label(sh, me),
            })

        if not combined:
            return {"success": False, "error": "no sources", "timestamp": ts_sec}

        combined.extend(struct.pack(">Q", ts_ns))
        combined.extend(os.urandom(8))

        raw_sh = shannon_miller_madow(bytes(combined))
        raw_me = min_entropy(bytes(combined))

        folded = xor_fold(bytes(combined), 64)
        conditioned = sha3_condition(folded, self.out_bytes)

        nist_adj = min(raw_me * len(combined), self.out_bytes * 8) * self.NIST_MULT
        vial = hashlib.sha3_256(conditioned).hexdigest()[:12]

        node_name = f"chaosvisual@{PLATFORM_INFO.get('distro', PLATFORM)}"

        return {
            "success": True,
            "vial_id": f"CVS-{vial}",
            "timestamp": ts_sec,
            "timestamp_ns": ts_ns,
            "node": node_name,
            "sources_list": active,
            "sources_str": "+".join(active),
            "sources_count": len(active),
            "source_health": src_health,
            "raw_bytes": len(combined),
            "raw_shannon": round(raw_sh, 4),
            "raw_min_entropy": round(raw_me, 4),
            "conditioned_bytes": len(conditioned),
            "conditioned_data": conditioned,
            "nist_adjusted_entropy_bits": round(nist_adj, 2),
            "health_status": health_label(raw_sh, raw_me),
        }


# ─────────────────────────────────────────────
# NETWORK
# ─────────────────────────────────────────────

class EntropySender:
    """TCP sender for conditioned entropy packets."""
    MAGIC = b"CHVS"

    def __init__(self, host: str, port: int, retry_delay: float = 15.0):
        self.host, self.port = host, port
        self.retry_delay = retry_delay
        self._sock = None
        self._connected = False
        self.packets_sent = 0
        self.failures = 0
        log.info(f"Sender: {host}:{port}")

    def connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10.0)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.connect((self.host, self.port))
            self._sock = s
            self._connected = True
            self.failures = 0
            log.info(f"Connected to server {self.host}:{self.port}")
            return True
        except Exception as e:
            log.warning(f"Connect failed: {e}")
            self._connected = False
            return False

    def send(self, meta: Dict[str, Any]) -> bool:
        if not self._connected:
            self.connect()
        if not self._connected:
            return False
        try:
            conditioned = meta.pop("conditioned_data", b"")
            clean = {k: v for k, v in meta.items()
                     if k not in ("conditioned_data", "source_health")}
            mj = json.dumps(clean).encode()
            payload = struct.pack(">H", len(mj)) + mj + conditioned
            frame = self.MAGIC + struct.pack(">I", len(payload)) + payload + struct.pack(">Q", time.time_ns())
            self._sock.sendall(frame)
            self.packets_sent += 1
            return True
        except Exception as e:
            log.warning(f"Send error: {e}")
            self._connected = False
            self.failures += 1
            return False

    def disconnect(self):
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._connected = False


def save_fallback(meta: Dict[str, Any], path: str):
    try:
        data = meta.get("conditioned_data", b"")
        with open(path, "ab") as f:
            f.write(struct.pack(">QH", meta.get("timestamp_ns", time.time_ns()),
                                len(data)) + data)
    except Exception:
        pass


# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────

class StatsTracker:
    def __init__(self):
        self.samples = self.sent = self.fallback = 0
        self.health_counts: Dict[str, int] = {}
        self.shannon_history: deque = deque(maxlen=60)
        self.t0 = time.time()

    def record(self, meta: Dict[str, Any], sent: bool):
        self.samples += 1
        if sent:
            self.sent += 1
        else:
            self.fallback += 1
        h = meta.get("health_status", "?")
        self.health_counts[h] = self.health_counts.get(h, 0) + 1
        self.shannon_history.append(meta.get("raw_shannon", 0.0))

    def print_summary(self):
        uptime = time.time() - self.t0
        avg = (sum(self.shannon_history) / len(self.shannon_history)
               if self.shannon_history else 0.0)
        log.info(f"-- {uptime/60:.1f}min | samples={self.samples} "
                 f"sent={self.sent} fallback={self.fallback} "
                 f"avg_shannon={avg:.3f} {self.health_counts}")


# ─────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────

class ChaosVisual:
    """Core entropy harvester engine. Used by both CLI and GUI."""

    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.running = False

        src = cfg.get("sources", {})
        proc = cfg.get("processing", {})
        # "akari" fallback supports legacy config files
        net = cfg.get("server", cfg.get("akari", {}))
        dbg = cfg.get("debug", {})

        self.sources: Dict[str, Any] = {
            "screen":        ScreenSource(src.get("screen", {})),
            "loopback":      LoopbackAudioSource(src.get("loopback", {})),
            "mic":           MicAudioSource(src.get("mic", {})),
            "os_rng":        OsRngSource(src.get("os_rng", {})),
            "timing_jitter": TimingJitterSource(src.get("timing_jitter", {})),
            "mouse":         MouseSource(src.get("mouse", {})),
            "keyboard":      KeyboardSource(src.get("keyboard", {})),
        }

        self.mixer = EntropyMixer(proc)
        self.sender = EntropySender(net.get("host", "127.0.0.1"),
                                    net.get("port", 8213),
                                    net.get("retry_delay_sec", 15.0))
        self.send_interval = net.get("send_interval_sec", 2.0)
        self.stats = StatsTracker()
        self.stats_interval = proc.get("stats_interval_sec", 30.0)
        self.save_fb = dbg.get("save_local_fallback", True)
        self.fb_path = dbg.get("fallback_path", "entropy_fallback.bin")
        self._screen_ivl = 1.0 / max(1, src.get("screen", {}).get("fps", 4))
        self._last_screen = 0.0
        self._last_stats = 0.0
        log.info(f"ChaosVisual v2.0 ready ({PLATFORM_INFO.get('distro', PLATFORM)})")

    def collect_all(self) -> Dict[str, Optional[bytes]]:
        now = time.time()
        chunks: Dict[str, Optional[bytes]] = {}
        if (now - self._last_screen) >= self._screen_ivl:
            chunks["screen"] = self.sources["screen"].collect()
            self._last_screen = now
        else:
            chunks["screen"] = None
        chunks["loopback"] = self.sources["loopback"].collect()
        chunks["mic"] = self.sources["mic"].collect()
        chunks["os_rng"] = self.sources["os_rng"].collect()
        chunks["timing_jitter"] = self.sources["timing_jitter"].collect()
        chunks["mouse"] = self.sources["mouse"].collect()
        chunks["keyboard"] = self.sources["keyboard"].collect()
        return chunks

    def run(self):
        self.running = True
        # "akari" fallback supports legacy config files
        net = self.cfg.get("server", self.cfg.get("akari", {}))
        log.info("=" * 40)
        log.info(f"  Target: {net.get('host')}:{net.get('port')}")
        for n, s in self.sources.items():
            log.info(f"  {'Y' if getattr(s, 'enabled', True) else 'N'} {n}")
        log.info("=" * 40)

        if not self.dry_run:
            self.sender.connect()

        last_send = time.time()
        try:
            while self.running:
                now = time.time()
                chunks = self.collect_all()

                if (now - last_send) >= self.send_interval:
                    meta = self.mixer.mix(chunks)
                    last_send = now

                    if not meta.get("success"):
                        log.warning(f"Mix failed: {meta.get('error')}")
                        continue
                    if meta["raw_shannon"] < self.mixer.min_shannon:
                        log.warning(f"Low shannon {meta['raw_shannon']:.3f}")
                        continue

                    if self.dry_run:
                        sent = True
                        log.info(f"DRY {meta['vial_id']} "
                                 f"sh={meta['raw_shannon']:.3f} "
                                 f"me={meta['raw_min_entropy']:.3f} "
                                 f"[{meta['sources_str']}]")
                    else:
                        sent = self.sender.send(meta)
                        if not sent and self.save_fb:
                            save_fallback(meta, self.fb_path)

                    self.stats.record(meta, sent)

                if (now - self._last_stats) >= self.stats_interval:
                    self.stats.print_summary()
                    self._last_stats = now

                time.sleep(min(0.05, self.send_interval * 0.05))

        except KeyboardInterrupt:
            log.info("Stopped")
        finally:
            self.running = False
            self.sources["loopback"].close()
            self.sources["mic"].close()
            if not self.dry_run:
                self.sender.disconnect()
            self.stats.print_summary()


def main():
    import argparse
    p = argparse.ArgumentParser(description="ChaosVisual Entropy Harvester")
    p.add_argument("--config", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--list-devices", action="store_true",
                   help="List all detected audio devices and exit")
    args = p.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_devices:
        print(f"\nPlatform: {PLATFORM_INFO.get('distro', PLATFORM)}")
        print(f"Audio backend: {AUDIO_BACKEND_NAME}")
        lb, mic = scan_audio_devices()
        print(f"\nLoopback devices ({len(lb)}):")
        for d in lb:
            print(f"  idx={d['index']}  {d['name']}  [{d['channels']}ch {d['sr']}Hz]  "
                  f"API={d.get('host_api', '?')}")
        print(f"\nInput devices ({len(mic)}):")
        for d in mic:
            print(f"  idx={d['index']}  {d['name']}  [{d['channels']}ch {d['sr']}Hz]  "
                  f"API={d.get('host_api', '?')}")
        if IS_LINUX:
            monitors = _find_pulse_monitor_sources()
            if monitors:
                print(f"\nPulseAudio monitor sources ({len(monitors)}):")
                for m in monitors:
                    print(f"  {m['name']}")
            else:
                print("\nNo PulseAudio monitor sources found via pactl")
        return

    cfg = load_config(args.config)
    dry = args.dry_run or cfg.get("debug", {}).get("dry_run", False)
    cv = ChaosVisual(cfg, dry_run=dry)

    if args.status:
        chunks = cv.collect_all()
        meta = cv.mixer.mix(chunks)
        print("\n" + "=" * 40)
        if meta.get("success"):
            print(f"  Vial:       {meta['vial_id']}")
            print(f"  Sources:    {meta['sources_str']}")
            print(f"  Shannon MM: {meta['raw_shannon']:.4f} bits/byte")
            print(f"  Min-entropy:{meta['raw_min_entropy']:.4f} bits/byte")
            print(f"  NIST adj:   {meta['nist_adjusted_entropy_bits']:.2f} bits")
            print(f"  Health:     {meta['health_status'].upper()}")
            print("\n  Per source:")
            for sh in meta["source_health"]:
                print(f"    {sh['source']:20} "
                      f"shannon(MM)={sh['shannon_mm']:.3f}  "
                      f"min_ent={sh['min_entropy']:.3f}  "
                      f"{sh['status']}")
        else:
            print(f"  FAILED: {meta.get('error')}")
        print("=" * 40)
        return

    cv.run()


if __name__ == "__main__":
    main()

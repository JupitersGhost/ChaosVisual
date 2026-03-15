"""
chaosgui.py  v2.0.0
ChaosVisual — Cross-Platform Entropy Harvester GUI

Imports all entropy harvesting, NIST math, sources, mixing, and
networking from chaosmain.py. This file is the GUI only.

v1.5 changes (2026-03-08):
  - CROSS-PLATFORM: Full support for Windows 11, openSUSE, and Debian/Ubuntu.
    · Platform detection module at startup sets capability flags
    · Audio backend abstracted: WASAPI (Win) → PulseAudio/PipeWire (Linux)
    · Keyboard entropy: ctypes/GetAsyncKeyState (Win) → evdev (Linux) → pynput (fallback)
    · Screen capture: mss (cross-platform, already used) validated per-platform
    · DPI awareness and DWM title bar color gracefully skipped on Linux
    · Font stack: Consolas (Win) → monospace / DejaVu Sans Mono (Linux)
  - AUDIO BACKEND ABSTRACTION:
    · New: Linux uses PyAudio (standard) with PulseAudio/PipeWire ALSA backend
    · Loopback on Linux via PulseAudio monitor sources (auto-detected)
    · Device scanner detects monitor devices as loopback equivalents
    · openSUSE: tested with PipeWire-PulseAudio compatibility layer
  - KEYBOARD ENTROPY (Linux):
    · Primary: evdev raw input (reads /dev/input/eventN — needs input group)
    · Fallback: pynput X11/Wayland listener
    · Privacy-safe: same XOR-diff approach, no keylogging
    · Wayland note: evdev works; pynput may need XWayland
  - IMPROVEMENTS (all platforms):
    · Graceful degradation: missing optional deps don't crash, just disable features
    · Startup diagnostics now report platform, audio backend, display server
    · Config save/load uses platform-appropriate paths (~/.config/chaosvisual/ on Linux)
    · Better error messages with platform-specific hints
    · EntropyScope anti-aliasing on Linux (smoother rendering)
    · Log console auto-scrolling fixed for rapid message bursts
    · Added PLATFORM_INFO dict for programmatic platform queries
  - PACKAGING HELPERS:
    · requirements_linux.txt and install notes in docstring
    · openSUSE: zypper install python3-tk python3-PyAudio portaudio-devel
    · Debian:   apt install python3-tk python3-pyaudio portaudio19-dev

Linux audio setup (one-time):
  openSUSE Tumbleweed/Leap:
    sudo zypper install python3-tk portaudio-devel python3-devel gcc
    pip install pyaudio pynput evdev mss

  Debian/Ubuntu 22.04+:
    sudo apt install python3-tk portaudio19-dev python3-dev
    pip install pyaudio pynput evdev mss

  Keyboard entropy (evdev) — add your user to the input group:
    sudo usermod -aG input $USER
    # then log out/in for group to take effect

  PulseAudio loopback (for system audio capture):
    # PulseAudio monitor sources are auto-detected.
    # If none appear, load the module:
    pactl load-module module-loopback

  PipeWire (openSUSE Tumbleweed default):
    # PipeWire's PulseAudio compat layer works out of the box.
    # Monitor sources appear automatically.
"""

import json
import logging
import queue
import struct
import threading
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import ttk

# ── Import everything from chaosmain (the backend) ───────────────────────────
from chaosmain import (
    # Platform
    PLATFORM, IS_WINDOWS, IS_LINUX, IS_MACOS, PLATFORM_INFO,
    # Audio
    PYAUDIO_OK, _pyaudio, _PA_GLOBAL, AUDIO_BACKEND_NAME,
    scan_audio_devices,
    # Keyboard
    CTYPES_OK, EVDEV_OK, PYNPUT_OK,
    _user32, _GetAsyncKeyState, _pynput_kb,
    # Core engine
    ChaosVisual, load_config, _default_config_path,
)

# GUI always has the harvester available now — it's in chaosmain
HARVESTER_OK = True


# ─────────────────────────────────────────────────────────────────────────────
# COLOUR PALETTE & FONT
# ─────────────────────────────────────────────────────────────────────────────

BG     = "#080c12"
PANEL  = "#0e1420"
BORDER = "#1a2540"
ACCENT = "#00ff88"
CYAN   = "#00aaff"
VIOLET = "#bf7fff"
WARN   = "#ffaa00"
ERR    = "#ff3355"
DIM    = "#3a4a60"
FG     = "#c8d8e8"
FG2    = "#7a99b8"

# Platform-aware monospace font
if IS_WINDOWS:
    MONO = "Consolas"
elif IS_MACOS:
    MONO = "Menlo"
else:
    # Linux: try common monospace fonts in preference order
    MONO = "DejaVu Sans Mono"
    # Fallback detected at runtime in _setup_window if font not available

SRC_COLORS: Dict[str, str] = {
    "screen":        "#00ff88",
    "loopback":      "#00aaff",
    "mic":           "#bf7fff",
    "os_rng":        "#ffaa00",
    "timing_jitter": "#ff6688",
    "mouse":         "#88aaff",
    "keyboard":      "#ff9944",
}

HEALTH_COLORS = {
    "excellent":  ACCENT,
    "healthy":    ACCENT,
    "acceptable": WARN,
    "degraded":   "#ff8800",
    "poor":       ERR,
}


# ─────────────────────────────────────────────────────────────────────────────
# DEVICE HELPERS (delegate to chaosmain, add GUI-specific selection logic)
# ─────────────────────────────────────────────────────────────────────────────

def scan_devices():
    """Delegate to chaosmain.scan_audio_devices() which includes pactl fallback."""
    return scan_audio_devices()


def _best_loopback(devs: List[dict]) -> Optional[int]:
    """Pick best loopback device per platform."""
    if IS_WINDOWS:
        for d in devs:
            if "WASAPI" in d.get("host_api", ""):
                return d["index"]
        for d in devs:
            if "VoiceMeeter" in d["name"]:
                return d["index"]
    elif IS_LINUX:
        for d in devs:
            if "monitor" in d["name"].lower() and d.get("index") is not None:
                return d["index"]
    return devs[0]["index"] if devs else None


def _best_mic(devs: List[dict]) -> Optional[int]:
    """Pick best mic: prefer physical device."""
    for d in devs:
        name_lower = d["name"].lower()
        if ("voicemeeter" not in name_lower
            and "loopback" not in name_lower
            and "monitor" not in name_lower):
            return d["index"]
    return devs[0]["index"] if devs else None


def validate_audio_device(device_index: int, channels: int, sample_rate: int,
                          is_loopback: bool = False) -> Optional[str]:
    """Validate a device can be opened. Returns None on success, error string on failure."""
    if not PYAUDIO_OK or _PA_GLOBAL is None:
        return "audio backend not available"
    if device_index is None:
        return "device index is None"
    try:
        info = _PA_GLOBAL.get_device_info_by_index(device_index)
        if is_loopback and IS_WINDOWS:
            avail_ch = int(info.get("maxOutputChannels", 0))
            if avail_ch == 0:
                avail_ch = int(info.get("maxInputChannels", 0))
        else:
            avail_ch = int(info.get("maxInputChannels", 0))

        if avail_ch < channels:
            return (f"Device idx={device_index} has {avail_ch} channels, "
                    f"requested {channels}")

        stream = _PA_GLOBAL.open(
            format=_pyaudio.paInt16,
            channels=channels,
            rate=sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=1024,
            start=False,
        )
        stream.close()
        return None
    except Exception as exc:
        return f"Device idx={device_index} open failed: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# KEYBOARD POLLING (cross-platform)
# ─────────────────────────────────────────────────────────────────────────────

class KeyboardPoller:
    """
    Privacy-safe keyboard entropy harvester — cross-platform.

    Windows: GetAsyncKeyState VK polling (XOR diff).
    Linux:   evdev raw input events (XOR diff on key states).
    Fallback: pynput.keyboard.Listener (all platforms).

    Does NOT log keystrokes. Only harvests timing + state-change entropy.
    """

    VK_RANGE = range(0x08, 0xFF)
    POLL_INTERVAL = 0.015  # 15ms

    def __init__(self, entropy_queue: queue.Queue, log_queue: queue.Queue):
        self._eq = entropy_queue
        self._lq = log_queue
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._prev_state = bytearray(256)
        self._method = "none"

    def start(self):
        if self._running:
            return
        self._running = True

        if IS_WINDOWS and CTYPES_OK:
            self._method = "GetAsyncKeyState"
            self._thread = threading.Thread(
                target=self._poll_loop_ctypes, name="kb-poll", daemon=True)
        elif IS_LINUX and EVDEV_OK:
            self._method = "evdev"
            self._thread = threading.Thread(
                target=self._poll_loop_evdev, name="kb-evdev", daemon=True)
        elif PYNPUT_OK:
            self._method = "pynput"
            self._thread = threading.Thread(
                target=self._poll_loop_pynput, name="kb-pynput", daemon=True)
        else:
            self._lq.put_nowait(("log", "WARNING",
                "Keyboard: no backend available — "
                "install evdev (Linux) or pynput (any) for keyboard entropy"))
            self._running = False
            return

        self._lq.put_nowait(("log", "INFO",
            f"Keyboard entropy started (method: {self._method})"))
        self._thread.start()

    def stop(self):
        self._running = False

    # ── Windows: GetAsyncKeyState ────────────────────────────────────────────

    def _poll_loop_ctypes(self):
        """Poll all VK codes, XOR with previous state, yield diff bytes."""
        try:
            while self._running:
                current = bytearray(256)
                for vk in self.VK_RANGE:
                    state = _GetAsyncKeyState(vk)
                    current[vk] = 1 if (state & 0x8000) else 0

                diff = bytearray(a ^ b for a, b in zip(current, self._prev_state))
                self._prev_state = current

                if any(diff):
                    ts = time.perf_counter_ns()
                    ts_bytes = ts.to_bytes(8, "little")
                    entropy = bytes(b for b in diff if b) + ts_bytes
                    try:
                        self._eq.put_nowait(entropy)
                    except queue.Full:
                        pass

                time.sleep(self.POLL_INTERVAL)
        except Exception as exc:
            self._lq.put_nowait(("log", "ERROR",
                f"Keyboard poller crashed: {exc}"))
        finally:
            self._running = False

    # ── Linux: evdev ─────────────────────────────────────────────────────────

    def _poll_loop_evdev(self):
        """
        Read raw keyboard events from /dev/input/eventN via evdev.
        Harvests timing entropy from key press/release events.
        Requires user to be in the 'input' group.
        """
        try:
            import evdev
            from evdev import ecodes

            # Find keyboard devices
            devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
            keyboards = [
                d for d in devices
                if ecodes.EV_KEY in (d.capabilities().keys())
            ]

            if not keyboards:
                self._lq.put_nowait(("log", "WARNING",
                    "evdev: no keyboard devices found. "
                    "Ensure user is in 'input' group: sudo usermod -aG input $USER"))
                self._running = False
                return

            kbd = keyboards[0]  # Use first keyboard found
            self._lq.put_nowait(("log", "INFO",
                f"evdev: using {kbd.name} ({kbd.path})"))

            key_state = bytearray(256)
            prev_state = bytearray(256)

            for event in kbd.read_loop():
                if not self._running:
                    break
                if event.type != ecodes.EV_KEY:
                    continue

                # Update state (clamped to 0-255 range for keycode)
                code = event.code & 0xFF
                key_state[code] = 1 if event.value > 0 else 0  # 1=down, 2=repeat→1

                # XOR diff
                diff = bytearray(a ^ b for a, b in zip(key_state, prev_state))
                prev_state[:] = key_state

                if any(diff):
                    ts = time.perf_counter_ns()
                    ts_bytes = ts.to_bytes(8, "little")
                    # Include event timestamp for extra entropy
                    ev_ts = struct.pack("<II", event.sec, event.usec)
                    entropy = bytes(b for b in diff if b) + ts_bytes + ev_ts
                    try:
                        self._eq.put_nowait(entropy)
                    except queue.Full:
                        pass

        except PermissionError:
            self._lq.put_nowait(("log", "ERROR",
                "evdev: Permission denied reading /dev/input/event*. "
                "Run: sudo usermod -aG input $USER  (then log out/in)"))
        except Exception as exc:
            self._lq.put_nowait(("log", "ERROR",
                f"evdev keyboard poller crashed: {exc}"))
        finally:
            self._running = False

    # ── Fallback: pynput ─────────────────────────────────────────────────────

    def _poll_loop_pynput(self):
        """Fallback: pynput listener-based entropy (all platforms)."""
        try:
            pressed_times: list = []

            def on_press(key):
                if not self._running:
                    return False
                ts = time.perf_counter_ns()
                pressed_times.append(ts)
                if len(pressed_times) >= 4:
                    entropy = b""
                    for t in pressed_times:
                        entropy += t.to_bytes(8, "little")
                    pressed_times.clear()
                    try:
                        self._eq.put_nowait(entropy)
                    except queue.Full:
                        pass

            with _pynput_kb.Listener(on_press=on_press) as listener:
                while self._running:
                    time.sleep(0.1)
                listener.stop()
        except Exception as exc:
            self._lq.put_nowait(("log", "ERROR",
                f"pynput keyboard listener crashed: {exc}"))
            if IS_LINUX and "wayland" in PLATFORM_INFO.get("display_server", ""):
                self._lq.put_nowait(("log", "WARNING",
                    "HINT: pynput has limited Wayland support. "
                    "Try: pip install evdev  (and add user to input group)"))
        finally:
            self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# LOG BRIDGE
# ─────────────────────────────────────────────────────────────────────────────

class _GuiLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self._q = q
        self.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    def emit(self, record: logging.LogRecord):
        try:
            self._q.put_nowait(("log", record.levelname, self.format(record)))
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# STATS BRIDGE
# ─────────────────────────────────────────────────────────────────────────────

class _StatsBridge:
    """Replaces StatsTracker.record() so live data flows to GUI queue."""
    def __init__(self, q: queue.Queue):
        self._q = q

    def record(self, meta: dict, sent: bool):
        try:
            self._q.put_nowait(("stats", {
                "vial_id":    meta.get("vial_id", "?"),
                "shannon":    meta.get("raw_shannon", 0.0),
                "min_entropy":meta.get("raw_min_entropy", 0.0),
                "health":     meta.get("health_status", "?"),
                "sources":    meta.get("sources_list", []),
                "nist_adj":   meta.get("nist_adjusted_entropy_bits", 0.0),
                "sent":       sent,
                "src_health": meta.get("source_health", []),
            }))
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# HARVESTER THREAD
# ─────────────────────────────────────────────────────────────────────────────

class HarvesterThread:
    def __init__(self, cfg: dict, q: queue.Queue):
        self._cfg = cfg
        self._q   = q
        self._cv: Optional["ChaosVisual"] = None
        self._t:  Optional[threading.Thread] = None
        self._kb_poller: Optional[KeyboardPoller] = None
        self.running = False

    def start(self):
        if self.running:
            return
        # ChaosVisual engine is always available from chaosmain

        self._validate_audio_before_start()

        self.running = True
        self._t = threading.Thread(target=self._run, name="harvester", daemon=True)
        self._t.start()

        if self._cfg.get("sources", {}).get("keyboard", {}).get("enabled", False):
            kb_q = queue.Queue(maxsize=100)
            self._kb_poller = KeyboardPoller(kb_q, self._q)
            self._kb_poller.start()

    def _validate_audio_before_start(self):
        """Check audio devices and log warnings for problems."""
        sources = self._cfg.get("sources", {})

        lb = sources.get("loopback", {})
        if lb.get("enabled") and lb.get("device_index") is not None:
            err = validate_audio_device(
                lb["device_index"],
                lb.get("channels", 2),
                lb.get("sample_rate", 44100),
                is_loopback=True,
            )
            if err:
                self._q.put_nowait(("log", "WARNING",
                    f"Loopback validation: {err}"))
                try:
                    info = _PA_GLOBAL.get_device_info_by_index(lb["device_index"])
                    if IS_WINDOWS:
                        raw_ch = int(info.get("maxOutputChannels", 0)) or \
                                 int(info.get("maxInputChannels", 0))
                    else:
                        raw_ch = int(info.get("maxInputChannels", 0))
                    fixed_ch = max(1, min(raw_ch, 2))
                    self._cfg["sources"]["loopback"]["channels"] = fixed_ch
                    self._q.put_nowait(("log", "INFO",
                        f"Loopback: auto-fixed channels to {fixed_ch}"))
                except Exception:
                    pass
            else:
                self._q.put_nowait(("log", "INFO",
                    f"Loopback device idx={lb['device_index']} validated OK"))
        elif lb.get("enabled"):
            self._q.put_nowait(("log", "WARNING",
                "Loopback enabled but no device selected — will be skipped"))
            if IS_LINUX:
                self._q.put_nowait(("log", "INFO",
                    "HINT: On Linux, loopback = PulseAudio/PipeWire monitor source. "
                    "Run: pactl list short sources | grep monitor"))

        mic = sources.get("mic", {})
        if mic.get("enabled") and mic.get("device_index") is not None:
            err = validate_audio_device(
                mic["device_index"],
                mic.get("channels", 1),
                mic.get("sample_rate", 44100),
                is_loopback=False,
            )
            if err:
                self._q.put_nowait(("log", "WARNING",
                    f"Mic validation: {err}"))
            else:
                self._q.put_nowait(("log", "INFO",
                    f"Mic device idx={mic['device_index']} validated OK"))
        elif mic.get("enabled"):
            self._q.put_nowait(("log", "WARNING",
                "Mic enabled but no device selected — will be skipped"))

    def _run(self):
        try:
            self._cv = ChaosVisual(self._cfg, dry_run=False)
            self._cv.stats.record = _StatsBridge(self._q).record
            self._cv.run()
        except Exception as exc:
            tb = traceback.format_exc()
            self._q.put_nowait(("log", "ERROR",
                                f"Harvester crashed: {exc}\n{tb}"))
            err_str = str(exc).lower()
            if IS_WINDOWS:
                if "unanticipated host error" in err_str or "wasapi" in err_str:
                    self._q.put_nowait(("log", "WARNING",
                        "HINT: Audio error may be a Windows permission issue. "
                        "Try: Settings → Privacy → Microphone → Allow apps"))
                elif "invalid number of channels" in err_str:
                    self._q.put_nowait(("log", "WARNING",
                        "HINT: Channel mismatch. Try RESCAN DEVICES to refresh."))
            elif IS_LINUX:
                if "permission" in err_str or "errno 13" in err_str:
                    self._q.put_nowait(("log", "WARNING",
                        "HINT: Permission denied. Check: "
                        "1) user in 'audio' group  2) PulseAudio/PipeWire running"))
                elif "no such device" in err_str or "device unavailable" in err_str:
                    self._q.put_nowait(("log", "WARNING",
                        "HINT: Audio device not found. Try RESCAN or check: "
                        "pactl list short sources"))
                elif "connection refused" in err_str:
                    self._q.put_nowait(("log", "WARNING",
                        "HINT: PulseAudio/PipeWire not running? "
                        "Try: systemctl --user start pipewire pipewire-pulse"))
            if "device unavailable" in err_str:
                self._q.put_nowait(("log", "WARNING",
                    "HINT: Audio device may be in use by another app. "
                    "Close other audio apps and retry."))
        finally:
            self.running = False
            self._q.put_nowait(("status", "stopped"))

    def stop(self):
        if self._cv:
            self._cv.running = False
        if self._kb_poller:
            self._kb_poller.stop()
        self.running = False


# ─────────────────────────────────────────────────────────────────────────────
# ENTROPY OSCILLOSCOPE
# ─────────────────────────────────────────────────────────────────────────────

class EntropyScope(tk.Canvas):
    HISTORY = 60

    def __init__(self, parent, **kw):
        kw.setdefault("bg", BG)
        kw.setdefault("highlightthickness", 0)
        super().__init__(parent, **kw)
        self._data: List[tuple] = []
        self.bind("<Configure>", lambda _e: self._draw())

    def push(self, shannon: float, health: str):
        self._data.append((shannon, health))
        if len(self._data) > self.HISTORY:
            self._data.pop(0)
        self._draw()

    def _hcolor(self, h: str) -> str:
        return HEALTH_COLORS.get(h, DIM)

    def _draw(self):
        self.delete("all")
        W = self.winfo_width()
        H = self.winfo_height()
        if W < 10 or H < 10:
            return
        # Reference lines
        for val, label in [(6.0, "6.0"), (7.5, "7.5"), (8.0, "8.0")]:
            y = H - int((val / 8.0) * (H - 14)) - 4
            self.create_line(0, y, W, y, fill=BORDER, dash=(3, 6))
            self.create_text(W - 3, y - 2, text=label, fill=DIM,
                             font=(MONO, 7), anchor="ne")
        if not self._data:
            self.create_text(W // 2, H // 2, text="awaiting samples…",
                             fill=DIM, font=(MONO, 9))
            return
        bw = max(2, W // self.HISTORY)
        for i, (sh, hl) in enumerate(self._data):
            bh = max(1, int((sh / 8.0) * (H - 14)))
            x1, x2 = i * bw + 1, i * bw + bw - 1
            self.create_rectangle(x1, H - bh - 4, x2, H - 4,
                                  fill=self._hcolor(hl), outline="")
        last_sh, last_hl = self._data[-1]
        self.create_text(5, 5, text=f"{last_sh:.3f} b/B",
                         fill=self._hcolor(last_hl),
                         font=(MONO, 9, "bold"), anchor="nw")


# ─────────────────────────────────────────────────────────────────────────────
# WIDGET HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _panel(parent, title: str = ""):
    """Bordered panel. Returns (outer, inner). Pack outer; put widgets in inner."""
    outer = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
    inner = tk.Frame(outer, bg=PANEL, padx=8, pady=6)
    inner.pack(fill="both", expand=True)
    if title:
        tk.Label(inner, text=title, bg=PANEL, fg=ACCENT,
                 font=(MONO, 8, "bold")).pack(anchor="w", pady=(0, 4))
    return outer, inner


def _lbl(parent, text: str, fg=FG2, size=8, bold=False):
    return tk.Label(parent, text=text, bg=PANEL, fg=fg,
                    font=(MONO, size, "bold") if bold else (MONO, size))


def _stat_row(parent, key: str, val_fg=FG):
    row = tk.Frame(parent, bg=PANEL)
    row.pack(anchor="w", pady=1)
    tk.Label(row, text=key, bg=PANEL, fg=FG2,
             font=(MONO, 8), width=16, anchor="w").pack(side="left")
    val = tk.Label(row, text="—", bg=PANEL, fg=val_fg,
                   font=(MONO, 9, "bold"), anchor="w")
    val.pack(side="left")
    return val


def _checkbox(parent, text: str, var: tk.BooleanVar, color: str = FG):
    return tk.Checkbutton(
        parent, text=text, variable=var,
        bg=PANEL, fg=color,
        activebackground=PANEL, activeforeground=ACCENT,
        selectcolor=BORDER, font=(MONO, 9), cursor="hand2",
    )


def _button(parent, text: str, cmd, color: str = ACCENT, **kw):
    b = tk.Button(
        parent, text=text, command=cmd,
        bg=PANEL, fg=color, activebackground=BORDER,
        activeforeground=color, relief="flat", bd=0,
        font=(MONO, 9, "bold"), cursor="hand2",
        highlightthickness=1, highlightbackground=color,
        padx=10, pady=4, **kw,
    )
    b.bind("<Enter>", lambda _e: b.config(bg=color, fg=BG))
    b.bind("<Leave>", lambda _e: b.config(bg=PANEL, fg=color))
    return b


# ─────────────────────────────────────────────────────────────────────────────
# AUDIO DEVICE SUB-PANEL  (reusable for loopback + mic)
# ─────────────────────────────────────────────────────────────────────────────

class AudioDevicePanel:
    """
    A self-contained panel for one audio device (loopback or mic).
    Contains: enable checkbox, device dropdown, info label, LSB spinbox.

    After construction, read:
      .enabled_var   — BooleanVar
      .device_idx    — int or None (currently selected device index)
      .device_ch     — int         (channel count of selected device)
      .lsb_var       — StringVar   (LSB bits)
    """

    def __init__(self, parent, title: str, icon: str, color: str,
                 devices: List[dict], best_idx: Optional[int]):
        self.devices    = devices
        self.device_idx: Optional[int] = None
        self.device_ch:  int = 1

        outer, inner = _panel(parent, "")
        self.frame = outer

        # Header row
        hdr = tk.Frame(inner, bg=PANEL)
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text=f"{icon}  {title}",
                 bg=PANEL, fg=color,
                 font=(MONO, 8, "bold")).pack(side="left")
        self.enabled_var = tk.BooleanVar(value=True)
        _checkbox(hdr, "enable", self.enabled_var, color).pack(side="right", padx=4)
        self.dot = tk.Label(hdr, text="●", bg=PANEL, fg=DIM, font=(MONO, 11))
        self.dot.pack(side="right", padx=2)

        if not PYAUDIO_OK:
            hint = "pip install pyaudio"
            if IS_LINUX:
                if "suse" in PLATFORM_INFO.get("distro", "").lower():
                    hint = "sudo zypper install python3-PyAudio portaudio-devel"
                else:
                    hint = "sudo apt install python3-pyaudio portaudio19-dev"
            elif IS_WINDOWS:
                hint = "pip install pyaudiowpatch"

            tk.Label(inner,
                     text=f"Audio backend not installed\n{hint}",
                     bg=PANEL, fg=ERR, font=(MONO, 8)).pack(anchor="w")
            self.combo    = None
            self.info_lbl = None
            self.lsb_var  = tk.StringVar(value="4")
            return

        # Device combo
        self.combo = ttk.Combobox(inner, style="CV.TCombobox",
                                  state="readonly", font=(MONO, 8))
        self.combo.pack(fill="x", pady=(0, 3))
        self.combo.bind("<<ComboboxSelected>>", self._on_select)

        # Info line
        self.info_lbl = tk.Label(inner, text="no device selected",
                                 bg=PANEL, fg=FG2, font=(MONO, 7),
                                 wraplength=300, justify="left")
        self.info_lbl.pack(anchor="w")

        # LSB spinbox
        lsb_row = tk.Frame(inner, bg=PANEL)
        lsb_row.pack(fill="x", pady=(4, 0))
        _lbl(lsb_row, "LSB bits").pack(side="left")
        self.lsb_var = tk.StringVar(value="4")
        tk.Spinbox(lsb_row, from_=1, to=8,
                   textvariable=self.lsb_var,
                   bg=BORDER, fg=FG, font=(MONO, 9),
                   relief="flat", buttonbackground=BORDER,
                   width=4).pack(side="right")

        self.set_devices(devices, best_idx)

    def set_devices(self, devices: List[dict], best_idx: Optional[int]):
        """Replace device list and auto-select best_idx (or first)."""
        self.devices = devices
        if self.combo is None:
            return
        labels = [d["label"] for d in devices]
        self.combo["values"] = labels

        if not devices:
            self.combo.set("")
            self.device_idx = None
            self.device_ch = 1
            if self.info_lbl:
                self.info_lbl.config(text="no devices found", fg=ERR)
            return

        target = best_idx
        found = False
        for i, d in enumerate(devices):
            if d["index"] == target:
                self.combo.current(i)
                found = True
                break
        if not found and devices:
            self.combo.current(0)
        self._on_select()

    def _on_select(self, _event=None):
        if self.combo is None:
            return
        sel = self.combo.current()
        if 0 <= sel < len(self.devices):
            d = self.devices[sel]
            self.device_idx = d["index"]
            self.device_ch  = d["channels"]
            if self.info_lbl:
                api_str = f"  API={d['host_api']}" if d.get("host_api") else ""
                self.info_lbl.config(
                    text=f"idx={d['index']}  ch={d['channels']}  {d['sr']}Hz  "
                         f"maxOut={d['max_out']}  maxIn={d['max_in']}{api_str}",
                    fg=FG2,
                )
        else:
            self.device_idx = None
            self.device_ch = 1

    def restore_saved(self, saved_idx: Optional[int]):
        """Try to restore a previously saved device_index."""
        if saved_idx is not None:
            self.set_devices(self.devices, saved_idx)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────────────────────────────────────

class ChaosVisualGUI:
    CONFIG_PATH = _default_config_path()

    def __init__(self, root: tk.Tk):
        self.root = root
        self._q: queue.Queue = queue.Queue()
        self._harvester: Optional[HarvesterThread] = None
        self._running = False
        self._total_samples = 0
        self._total_sent    = 0
        self._saved_lb_idx:  Optional[int] = None
        self._saved_mic_idx: Optional[int] = None

        self._setup_window()
        self._setup_ttk_style()

        self._build_ui()

        cfg = load_config(self.CONFIG_PATH)
        self._load_cfg(cfg)

        self._do_rescan(silent=True)

        logging.getLogger().addHandler(_GuiLogHandler(self._q))
        logging.getLogger().setLevel(logging.INFO)

        self._log_startup_diagnostics()
        self._poll()

    def _log_startup_diagnostics(self):
        """Log system capability info at startup for debugging."""
        platform_label = PLATFORM_INFO["distro"]
        if IS_LINUX:
            platform_label += f" ({PLATFORM_INFO['display_server']})"

        self._log(f"ChaosVisual v2.0.0 — {platform_label}")
        self._log(f"Audio backend: {AUDIO_BACKEND_NAME} "
                  f"({'OK' if PYAUDIO_OK else 'NOT FOUND'})")

        # Keyboard method
        if IS_WINDOWS and CTYPES_OK:
            self._log("Keyboard method: GetAsyncKeyState (VK polling)")
        elif IS_LINUX and EVDEV_OK:
            self._log("Keyboard method: evdev (raw input)")
        elif PYNPUT_OK:
            self._log("Keyboard method: pynput (listener)")
        else:
            self._log("Keyboard method: NONE — install evdev or pynput", "WARNING")

        self._log(f"Harvester backend: chaosmain.py (integrated)")
        self._log(f"Config path: {self.CONFIG_PATH}")

        if IS_LINUX:
            self._log(f"Audio server: {PLATFORM_INFO['audio_backend']}")

        if PYAUDIO_OK:
            lb_devs, mic_devs = scan_devices()
            self._log(f"Audio devices: {len(lb_devs)} loopback, "
                      f"{len(mic_devs)} input")
            for d in lb_devs:
                self._log(f"  LB: {d['name']} [{d['channels']}ch] "
                          f"API={d.get('host_api', '?')}", "DEBUG")
            for d in mic_devs:
                self._log(f"  MIC: {d['name']} [{d['channels']}ch] "
                          f"API={d.get('host_api', '?')}", "DEBUG")

    # ── WINDOW SETUP ────────────────────────────────────────────────────────

    def _setup_window(self):
        self.root.title("ChaosVisual  ·  Entropy Harvester")
        self.root.configure(bg=BG)
        self.root.geometry("1100x820")
        self.root.minsize(900, 660)

        # Windows-specific: DPI awareness + DWM dark titlebar
        if IS_WINDOWS:
            try:
                import ctypes as _ct
                _ct.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
            try:
                import ctypes as _ct
                self.root.update()
                hwnd = _ct.windll.user32.GetParent(self.root.winfo_id())
                _ct.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 35, _ct.byref(_ct.c_int(0x00120c08)), 4)
            except Exception:
                pass

        # Linux: validate font availability
        if IS_LINUX:
            global MONO
            try:
                import tkinter.font as tkfont
                available = tkfont.families(self.root)
                preferred = ["DejaVu Sans Mono", "Liberation Mono",
                             "Noto Sans Mono", "Ubuntu Mono", "monospace"]
                for font_name in preferred:
                    if font_name in available:
                        MONO = font_name
                        break
                else:
                    MONO = "TkFixedFont"
            except Exception:
                MONO = "TkFixedFont"

    def _setup_ttk_style(self):
        """Configure ttk combobox appearance ONCE."""
        style = ttk.Style(self.root)
        # Use 'clam' theme — available on all platforms with ttk
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass  # If clam not available, use default
        style.configure("CV.TCombobox",
                        fieldbackground=BORDER, background=BORDER,
                        foreground=FG, selectbackground=BORDER,
                        selectforeground=ACCENT, arrowcolor=ACCENT,
                        borderwidth=0, relief="flat")
        style.map("CV.TCombobox",
                  fieldbackground=[("readonly", BORDER)],
                  foreground=[("readonly", FG)],
                  selectbackground=[("readonly", BORDER)])

    # ── BUILD UI ────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        top = tk.Frame(self.root, bg=PANEL, height=46)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="◈ CHAOSVISUAL", bg=PANEL, fg=ACCENT,
                 font=(MONO, 14, "bold")).pack(side="left", padx=12, pady=8)

        platform_tag = "Windows 11" if IS_WINDOWS else PLATFORM_INFO["distro"]
        tk.Label(top, text=f"v2.0  ·  entropy harvester  ·  {platform_tag}",
                 bg=PANEL, fg=DIM, font=(MONO, 8)).pack(side="left", padx=4)
        self._dot    = tk.Label(top, text="●", bg=PANEL, fg=DIM, font=(MONO, 14))
        self._dot.pack(side="right", padx=8)
        self._st_lbl = tk.Label(top, text="OFFLINE", bg=PANEL, fg=DIM,
                                font=(MONO, 9, "bold"))
        self._st_lbl.pack(side="right", padx=2)
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        # Body
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=8, pady=6)

        left = tk.Frame(body, bg=BG, width=350)
        left.pack(side="left", fill="y", padx=(0, 6))
        left.pack_propagate(False)

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._build_left(left)
        self._build_right(right)

        # Bottom bar
        bot = tk.Frame(self.root, bg=PANEL, height=44)
        bot.pack(fill="x", pady=(4, 0))
        bot.pack_propagate(False)

        self._btn_start = _button(bot, "▶  START", self._on_start, ACCENT)
        self._btn_start.pack(side="left", padx=8, pady=7)

        self._btn_stop = _button(bot, "■  STOP", self._on_stop, ERR)
        self._btn_stop.pack(side="left", padx=4, pady=7)
        self._btn_stop.config(state="disabled")

        _button(bot, "💾  SAVE CONFIG",     self._save_config, CYAN).pack(
            side="left", padx=10, pady=7)
        _button(bot, "⟳  RESCAN DEVICES",
                lambda: self._do_rescan(silent=False), DIM).pack(
            side="left", padx=4, pady=7)

        self._pkt_lbl = tk.Label(bot, text="packets: 0  |  sent: 0",
                                 bg=PANEL, fg=DIM, font=(MONO, 8))
        self._pkt_lbl.pack(side="right", padx=12)

    # ── LEFT COLUMN ─────────────────────────────────────────────────────────

    def _build_left(self, parent):
        # --- Remote server connection ---
        o, f = _panel(parent, "SERVER CONNECTION")
        o.pack(fill="x", pady=(0, 5))
        _lbl(f, "Host (IP / hostname)").pack(anchor="w")
        self._v_host = tk.StringVar(value="127.0.0.1")
        tk.Entry(f, textvariable=self._v_host, bg=BORDER, fg=ACCENT,
                 insertbackground=ACCENT, font=(MONO, 10),
                 relief="flat", bd=4).pack(fill="x", pady=(2, 4))
        row = tk.Frame(f, bg=PANEL); row.pack(fill="x")
        _lbl(row, "Port").pack(side="left")
        self._v_port = tk.StringVar(value="8213")
        tk.Entry(row, textvariable=self._v_port, bg=BORDER, fg=FG,
                 insertbackground=FG, font=(MONO, 9), relief="flat",
                 bd=4, width=7).pack(side="left", padx=4)
        _lbl(row, "Interval (s)").pack(side="left", padx=(10, 0))
        self._v_interval = tk.StringVar(value="2.0")
        tk.Entry(row, textvariable=self._v_interval, bg=BORDER, fg=FG,
                 insertbackground=FG, font=(MONO, 9), relief="flat",
                 bd=4, width=6).pack(side="left", padx=4)

        # --- General entropy sources ---
        o, f = _panel(parent, "ENTROPY SOURCES")
        o.pack(fill="x", pady=(0, 5))

        self._v_en_screen   = tk.BooleanVar(value=True)
        self._v_en_os_rng   = tk.BooleanVar(value=True)
        self._v_en_timing   = tk.BooleanVar(value=True)
        self._v_en_mouse    = tk.BooleanVar(value=True)
        self._v_en_keyboard = tk.BooleanVar(value=True)

        self._src_dots: Dict[str, tk.Label] = {}

        for txt, var, key in [
            ("🖥  Screen capture",  self._v_en_screen,   "screen"),
            ("🎲  OS RNG",          self._v_en_os_rng,   "os_rng"),
            ("⏱  Timing jitter",   self._v_en_timing,   "timing_jitter"),
            ("🖱  Mouse position",  self._v_en_mouse,    "mouse"),
            ("⌨  Keyboard state",  self._v_en_keyboard, "keyboard"),
        ]:
            row = tk.Frame(f, bg=PANEL); row.pack(fill="x", pady=1)
            _checkbox(row, txt, var, SRC_COLORS.get(key, FG)).pack(side="left")
            dot = tk.Label(row, text="●", bg=PANEL, fg=DIM, font=(MONO, 11))
            dot.pack(side="right", padx=2)
            self._src_dots[key] = dot

        # Screen FPS
        fps_row = tk.Frame(f, bg=PANEL); fps_row.pack(fill="x", pady=(5, 0))
        _lbl(fps_row, "Screen FPS").pack(side="left")
        self._v_fps = tk.StringVar(value="4")
        tk.Spinbox(fps_row, from_=1, to=30, textvariable=self._v_fps,
                   bg=BORDER, fg=FG, font=(MONO, 9), relief="flat",
                   buttonbackground=BORDER, width=4).pack(side="right")

        # --- Loopback audio ---
        lb_title = "SYSTEM AUDIO  (loopback)"
        if IS_LINUX:
            lb_title = "SYSTEM AUDIO  (monitor source)"
        self._lb_panel = AudioDevicePanel(
            parent, title=lb_title,
            icon="🔁", color=CYAN,
            devices=[], best_idx=None,
        )
        self._lb_panel.frame.pack(fill="x", pady=(0, 5))
        self._src_dots["loopback"] = self._lb_panel.dot

        # --- Microphone ---
        self._mic_panel = AudioDevicePanel(
            parent, title="MICROPHONE  (input)",
            icon="🎤", color=VIOLET,
            devices=[], best_idx=None,
        )
        self._mic_panel.frame.pack(fill="x", pady=(0, 5))
        self._src_dots["mic"] = self._mic_panel.dot

    # ── RIGHT COLUMN ─────────────────────────────────────────────────────────

    def _build_right(self, parent):
        # --- Live entropy stats ---
        o, f = _panel(parent, "LIVE ENTROPY")
        o.pack(fill="x", pady=(0, 5))

        cols = [tk.Frame(f, bg=PANEL) for _ in range(3)]
        for c in cols:
            c.pack(side="left", fill="y", padx=(0, 18))

        self._l_vial = _stat_row(cols[0], "Vial ID")
        self._l_sh   = _stat_row(cols[0], "Shannon (MM)", val_fg=ACCENT)
        self._l_me   = _stat_row(cols[0], "Min-entropy")
        tk.Label(cols[0], text="MM = Miller-Madow bias correction",
                 bg=PANEL, fg=DIM, font=(MONO, 7)).pack(anchor="w")

        self._l_nist  = _stat_row(cols[1], "NIST adjusted")
        self._l_hlth  = _stat_row(cols[1], "Health", val_fg=ACCENT)
        self._l_srcs  = _stat_row(cols[1], "Sources", val_fg=FG2)

        self._l_server = _stat_row(cols[2], "Server host")
        self._l_sent   = _stat_row(cols[2], "Pkts sent")
        self._l_fb     = _stat_row(cols[2], "Fallback")

        # --- Oscilloscope ---
        o, f = _panel(parent, "SHANNON HISTORY  (Miller-Madow corrected, last 60 samples)")
        o.pack(fill="x", pady=(0, 5))
        self._scope = EntropyScope(f, height=90)
        self._scope.pack(fill="x")

        # --- Per-source activity bars ---
        o, f = _panel(parent, "SOURCE ACTIVITY")
        o.pack(fill="x", pady=(0, 5))
        self._src_bars: Dict[str, tuple] = {}
        bar_frame = tk.Frame(f, bg=PANEL)
        bar_frame.pack(fill="x")
        for key, color in SRC_COLORS.items():
            col = tk.Frame(bar_frame, bg=PANEL)
            col.pack(side="left", fill="y", padx=(0, 3))
            short = (key.replace("timing_jitter", "timing")
                       .replace("keyboard", "kbd")
                       .replace("_", "\n"))
            tk.Label(col, text=short, bg=PANEL, fg=color,
                     font=(MONO, 7), anchor="center", width=8).pack()
            c = tk.Canvas(col, bg=BG, width=48, height=36, highlightthickness=0)
            c.pack()
            self._src_bars[key] = (c, color)

        # --- Console ---
        o, f = _panel(parent, "CONSOLE")
        o.pack(fill="both", expand=True)
        txt_wrap = tk.Frame(f, bg=BG)
        txt_wrap.pack(fill="both", expand=True)
        self._log_w = tk.Text(
            txt_wrap, bg=BG, fg=FG2, font=(MONO, 8),
            relief="flat", bd=0, wrap="word",
            state="disabled", height=8,
        )
        self._log_w.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(txt_wrap, command=self._log_w.yview,
                          bg=BORDER, troughcolor=BG, relief="flat")
        sb.pack(side="right", fill="y")
        self._log_w.config(yscrollcommand=sb.set)
        for tag, color in [
            ("INFO", FG2), ("WARNING", WARN), ("ERROR", ERR),
            ("DEBUG", DIM), ("SYSTEM", ACCENT),
        ]:
            self._log_w.tag_config(tag, foreground=color)

    # ── DEVICE RESCAN ────────────────────────────────────────────────────────

    def _do_rescan(self, silent: bool = False):
        lb_devs, mic_devs = scan_devices()
        self._lb_panel.set_devices(lb_devs, _best_loopback(lb_devs))
        self._mic_panel.set_devices(mic_devs, _best_mic(mic_devs))

        if self._saved_lb_idx is not None:
            self._lb_panel.restore_saved(self._saved_lb_idx)
        if self._saved_mic_idx is not None:
            self._mic_panel.restore_saved(self._saved_mic_idx)

        if not silent:
            self._log(f"Rescan: {len(lb_devs)} loopback, {len(mic_devs)} input devices")
            if self._lb_panel.device_idx is not None:
                self._log(f"  Loopback selected: idx={self._lb_panel.device_idx} "
                          f"ch={self._lb_panel.device_ch}")
            if self._mic_panel.device_idx is not None:
                self._log(f"  Mic selected: idx={self._mic_panel.device_idx} "
                          f"ch={self._mic_panel.device_ch}")

    # ── CONFIG ───────────────────────────────────────────────────────────────

    def _load_cfg(self, cfg: dict):
        # "akari" fallback supports legacy config files
        a = cfg.get("server", cfg.get("akari", {}))
        self._v_host.set(a.get("host", "127.0.0.1"))
        self._v_port.set(str(a.get("port", 8213)))
        self._v_interval.set(str(a.get("send_interval_sec", 2.0)))

        s = cfg.get("sources", {})
        self._v_en_screen.set(  s.get("screen",       {}).get("enabled", True))
        self._v_en_os_rng.set(  s.get("os_rng",        {}).get("enabled", True))
        self._v_en_timing.set(  s.get("timing_jitter", {}).get("enabled", True))
        self._v_en_mouse.set(   s.get("mouse",         {}).get("enabled", True))
        self._v_en_keyboard.set(s.get("keyboard",      {}).get("enabled", True))
        self._v_fps.set(str(s.get("screen", {}).get("fps", 4)))

        self._lb_panel.enabled_var.set(s.get("loopback", {}).get("enabled", True))
        self._mic_panel.enabled_var.set(s.get("mic",     {}).get("enabled", True))

        if PYAUDIO_OK:
            lb_bits  = str(s.get("loopback", {}).get("lsb_bits", 4))
            mic_bits = str(s.get("mic",      {}).get("lsb_bits", 4))
            self._lb_panel.lsb_var.set(lb_bits)
            self._mic_panel.lsb_var.set(mic_bits)

        self._saved_lb_idx  = s.get("loopback", {}).get("device_index")
        self._saved_mic_idx = s.get("mic",      {}).get("device_index")

        self._l_server.config(text=a.get("host", "?"))

    def _build_cfg(self) -> dict:
        """Read all widget values and return complete config dict."""
        lb  = self._lb_panel
        mic = self._mic_panel

        def _lsb(panel):
            try:
                return int(panel.lsb_var.get())
            except Exception:
                return 4

        return {
            "server": {
                "host":              self._v_host.get().strip(),
                "port":              int(self._v_port.get().strip() or 8213),
                "send_interval_sec": float(self._v_interval.get() or 2.0),
                "retry_delay_sec":   15.0,
            },
            "sources": {
                "screen": {
                    "enabled":       self._v_en_screen.get(),
                    "fps":           int(self._v_fps.get() or 4),
                    "monitor":       1,
                    "resize_width":  320,
                    "resize_height": 180,
                },
                "loopback": {
                    "enabled":           lb.enabled_var.get(),
                    "device_index":      lb.device_idx,
                    "channels":          lb.device_ch if lb.device_idx is not None else None,
                    "sample_rate":       44100,
                    "chunk_duration_ms": 100,
                    "lsb_bits":          _lsb(lb),
                    "exclusive_mode":    False if IS_LINUX else False,
                },
                "mic": {
                    "enabled":           mic.enabled_var.get(),
                    "device_index":      mic.device_idx,
                    "channels":          mic.device_ch if mic.device_idx is not None else None,
                    "sample_rate":       44100,
                    "chunk_duration_ms": 100,
                    "lsb_bits":          _lsb(mic),
                },
                "os_rng": {
                    "enabled":          self._v_en_os_rng.get(),
                    "bytes_per_sample": 256,
                },
                "timing_jitter": {
                    "enabled":    self._v_en_timing.get(),
                    "iterations": 200,
                },
                "mouse": {
                    "enabled": self._v_en_mouse.get(),
                },
                "keyboard": {
                    "enabled": self._v_en_keyboard.get(),
                },
            },
            "processing": {
                "output_bytes":          64,
                "min_shannon_threshold": 6.0,
                "stats_interval_sec":    30.0,
            },
            "debug": {
                "dry_run":             False,
                "verbose":             False,
                "save_local_fallback": True,
                "fallback_path":       "entropy_fallback.bin",
            },
            "platform": {
                "os":            PLATFORM,
                "distro":        PLATFORM_INFO.get("distro", "unknown"),
                "audio_backend": AUDIO_BACKEND_NAME,
                "keyboard_method": (
                    "ctypes" if IS_WINDOWS and CTYPES_OK
                    else "evdev" if IS_LINUX and EVDEV_OK
                    else "pynput" if PYNPUT_OK
                    else "none"
                ),
            },
        }

    def _save_config(self):
        cfg = self._build_cfg()
        try:
            # Ensure directory exists (for Linux XDG path)
            config_path = Path(self.CONFIG_PATH)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.CONFIG_PATH, "w") as fh:
                json.dump(cfg, fh, indent=2)
            self._log(f"Config saved → {config_path.absolute()}")
        except Exception as exc:
            self._log(f"Save failed: {exc}", "ERROR")

    # ── START / STOP ─────────────────────────────────────────────────────────

    def _on_start(self):
        if self._running:
            return
        cfg = self._build_cfg()

        problems = []
        lb = cfg["sources"]["loopback"]
        if lb["enabled"] and lb["device_index"] is None:
            problems.append("Loopback enabled but no device selected!")
        mic = cfg["sources"]["mic"]
        if mic["enabled"] and mic["device_index"] is None:
            problems.append("Mic enabled but no device selected!")

        if problems:
            for p in problems:
                self._log(p, "WARNING")
            self._log("Disabling audio sources with no device. "
                      "Select a device or disable the source.", "WARNING")
            if lb["enabled"] and lb["device_index"] is None:
                cfg["sources"]["loopback"]["enabled"] = False
            if mic["enabled"] and mic["device_index"] is None:
                cfg["sources"]["mic"]["enabled"] = False

        # "akari" fallback supports legacy config files
        server_cfg = cfg.get("server", cfg.get("akari", {}))
        self._l_server.config(text=server_cfg["host"])
        self._log(f"Starting → {server_cfg['host']}:{server_cfg['port']}")

        lb  = cfg["sources"]["loopback"]
        mic = cfg["sources"]["mic"]
        kb_method = cfg.get("platform", {}).get("keyboard_method", "?")
        self._log(f"Loopback: enabled={lb['enabled']}  idx={lb['device_index']}  "
                  f"ch={lb['channels']}  lsb={lb['lsb_bits']}")
        self._log(f"Mic:      enabled={mic['enabled']}  idx={mic['device_index']}  "
                  f"ch={mic['channels']}  lsb={mic['lsb_bits']}")
        self._log(f"Mouse: {'ON' if cfg['sources']['mouse']['enabled'] else 'OFF'}  "
                  f"Keyboard: {'ON' if cfg['sources']['keyboard']['enabled'] else 'OFF'}  "
                  f"(method: {kb_method})")

        self._harvester = HarvesterThread(cfg, self._q)
        self._harvester.start()
        self._running = True
        self._set_status("RUNNING", ACCENT)
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")

    def _on_stop(self):
        if self._harvester:
            self._harvester.stop()
        self._set_status("STOPPING…", WARN)
        self._btn_stop.config(state="disabled")

    def _set_status(self, text: str, color: str):
        self._dot.config(fg=color)
        self._st_lbl.config(text=text, fg=color)

    # ── QUEUE POLL ───────────────────────────────────────────────────────────

    def _poll(self):
        try:
            for _ in range(50):  # Process up to 50 items per tick (prevents GUI freeze)
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[2], item[1])
                elif kind == "stats":
                    self._update_stats(item[1])
                elif kind == "status" and item[1] == "stopped":
                    self._running = False
                    self._set_status("OFFLINE", DIM)
                    self._btn_start.config(state="normal")
                    self._btn_stop.config(state="disabled")
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._poll)

    # ── LIVE STATS ───────────────────────────────────────────────────────────

    def _update_stats(self, m: dict):
        self._total_samples += 1
        if m["sent"]:
            self._total_sent += 1

        def _sf(val, default=0.0):
            try:
                return float(val)
            except (TypeError, ValueError):
                return default

        sh   = _sf(m["shannon"])
        me   = _sf(m["min_entropy"])
        hlth = m["health"]
        srcs = m["sources"]
        hc   = HEALTH_COLORS.get(hlth, FG)

        self._l_vial.config(text=m["vial_id"])
        self._l_sh.config(  text=f"{sh:.4f} b/B", fg=hc)
        self._l_me.config(  text=f"{me:.4f} b/B")
        self._l_nist.config(text=f"{_sf(m['nist_adj']):.2f} bits")
        self._l_hlth.config(text=hlth.upper(), fg=hc)
        self._l_srcs.config(text=", ".join(srcs) if srcs else "—")
        self._l_sent.config(text=str(self._total_sent))
        fb = self._total_samples - self._total_sent
        self._l_fb.config(text=str(fb), fg=WARN if fb else FG)
        self._pkt_lbl.config(
            text=f"packets: {self._total_samples}  |  sent: {self._total_sent}")

        self._scope.push(sh, hlth)

        # Source dots
        active = set(srcs)
        for key, dot in self._src_dots.items():
            dot.config(fg=SRC_COLORS.get(key, ACCENT) if key in active else DIM)

        # Source spark bars
        sh_map = {s["source"]: s for s in m.get("src_health", [])}
        for key, (canvas, color) in self._src_bars.items():
            canvas.delete("all")
            W = canvas.winfo_width()  or 48
            H = canvas.winfo_height() or 36
            if key in active:
                sh_src = _sf(sh_map.get(key, {}).get("shannon_mm", sh))
                bh = max(2, int((sh_src / 8.0) * (H - 2)))
                canvas.create_rectangle(2, H - bh, W - 2, H - 1,
                                        fill=color, outline="")
                canvas.create_text(W // 2, 2, text=f"{sh_src:.1f}",
                                   fill=color, font=(MONO, 6), anchor="n")
            else:
                canvas.create_rectangle(2, H - 2, W - 2, H - 1,
                                        fill=BORDER, outline="")
                canvas.create_text(W // 2, H // 2, text="OFF",
                                   fill=DIM, font=(MONO, 7))

    # ── CONSOLE LOG ──────────────────────────────────────────────────────────

    def _log(self, msg: str, level: str = "INFO"):
        ts  = time.strftime("%H:%M:%S")
        tag = level if level in ("INFO", "WARNING", "ERROR", "DEBUG") else "INFO"
        if any(x in msg for x in
               ("Starting", "stream", "Loopback:", "Mic:", "Keyboard:", "Connected",
                "validated OK", "WASAPI", "method:", "evdev:", "monitor")):
            tag = "SYSTEM"
        w = self._log_w
        w.config(state="normal")
        w.insert("end", f"[{ts}] {msg}\n", tag)
        lines = int(w.index("end-1c").split(".")[0])
        if lines > 500:
            w.delete("1.0", "50.0")
        w.see("end")
        w.config(state="disabled")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    app  = ChaosVisualGUI(root)

    def _on_close():
        if app._harvester:
            app._harvester.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

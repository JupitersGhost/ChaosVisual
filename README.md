<p align="center">
  <img src="cover.png" alt="ChaosVisual" width="600" />
</p>

# ChaosVisual

**Cross-platform multi-source entropy harvester with NIST SP 800-90B pipeline and real-time GUI.**

ChaosVisual collects environmental randomness from seven simultaneous sources on your machine, conditions it through a cryptographic pipeline, and forwards the result to a remote server over TCP. It includes a full Tkinter GUI with live Shannon entropy monitoring, per-source health tracking, and an oscilloscope-style history display.

> **WARNING: This is a prototype and a work-in-progress.** ChaosVisual is an experimental research tool. Do not use it in production security systems, key generation for real-world cryptographic applications, or any context where the quality of randomness is safety-critical. Performance, entropy quality, and device compatibility will vary significantly from machine to machine.

---

## Entropy Sources

ChaosVisual harvests from all seven sources in parallel and mixes them into a single conditioned output:

1. **Screen capture** -- frame diffs, LSB extraction, edge noise via `mss`
2. **System audio loopback** -- WASAPI (Windows) / PulseAudio monitor (Linux)
3. **Microphone input** -- ADC quantization noise
4. **OS RNG** -- `CryptGenRandom` (Windows) / `getrandom` (Linux)
5. **CPU timing jitter** -- nanosecond instruction timing noise
6. **Mouse movement** -- human-driven positional deltas
7. **Keyboard state** -- privacy-safe XOR-diff polling (no keylogging)

## NIST SP 800-90B Pipeline

Each harvesting cycle runs the collected bytes through:

```
raw --> LSB extract --> Von Neumann debias --> XOR fold --> SHA3-256 conditioning --> health check --> TCP send
```

Entropy quality is assessed using Miller-Madow bias-corrected Shannon entropy and NIST min-entropy, with a conservative 0.85x adjustment factor applied to the final estimate.

## Platform Support

| Platform | Audio Backend | Keyboard Method | Status |
|---|---|---|---|
| Windows 11 | pyaudiowpatch (WASAPI) | ctypes / GetAsyncKeyState | Tested |
| Linux Mint / Debian / Ubuntu | pyaudio (PulseAudio/PipeWire) | evdev (raw input) | Tested |
| openSUSE Tumbleweed | pyaudio (PipeWire compat) | evdev (raw input) | Tested |
| macOS | pyaudio (CoreAudio) | pynput (listener) | Experimental |

---

## Requirements

**Python 3.10+** and **Tkinter** (usually bundled with Python on Windows, may need separate install on Linux).

### Core dependencies

```
numpy
mss
```

### Audio (pick one based on platform)

```
pyaudiowpatch    # Windows (WASAPI loopback support)
pyaudio          # Linux / macOS
```

### Keyboard entropy (Linux, pick one)

```
evdev            # Preferred -- raw input, needs 'input' group membership
pynput           # Fallback -- works on X11, limited Wayland support
```

### Optional (Linux, for mouse entropy)

```
python-xlib      # Fast mouse position via Xlib
```

---

## Installation

### Windows

```bash
pip install numpy mss pyaudiowpatch pynput
```

### Debian / Ubuntu

```bash
sudo apt install python3-tk portaudio19-dev python3-dev
pip install numpy mss pyaudio evdev pynput
sudo usermod -aG input $USER   # for keyboard entropy via evdev, then log out/in
```

### openSUSE

```bash
sudo zypper install python3-tk portaudio-devel python3-devel gcc
pip install numpy mss pyaudio evdev pynput
sudo usermod -aG input $USER
```

### PulseAudio loopback (Linux)

PulseAudio monitor sources are auto-detected. If no loopback devices appear in the GUI, try:

```bash
pactl load-module module-loopback
```

PipeWire users (default on openSUSE Tumbleweed and newer Fedora) should have monitor sources available automatically through the PulseAudio compatibility layer.

---

## Usage

### GUI mode (recommended)

```bash
python chaosgui.py
```

This opens the full GUI where you can:

- Configure the target server host, port, and send interval
- Enable/disable individual entropy sources
- Select audio devices for loopback and microphone capture
- Monitor live Shannon entropy, min-entropy, and NIST-adjusted estimates
- Watch per-source health indicators and the entropy oscilloscope
- Save and load configuration

### CLI mode

```bash
# Run with default config
python chaosmain.py

# Dry run (collect and measure, don't send)
python chaosmain.py --dry-run

# One-shot status check
python chaosmain.py --status

# List detected audio devices
python chaosmain.py --list-devices

# Verbose logging
python chaosmain.py --verbose

# Custom config file
python chaosmain.py --config /path/to/config.json
```

---

## Configuration

On first run, a default `config.json` is created:

- **Windows:** `./config.json` (working directory)
- **Linux/macOS:** `~/.config/chaosvisual/config.json`

The config controls server connection, source toggles, audio device indices, processing thresholds, and debug options. The GUI provides a visual editor for all of these settings and a "Save Config" button to persist them.

### Server connection

ChaosVisual sends conditioned entropy packets over TCP using a simple framed protocol with a `CHVS` magic header. Configure the target server's IP and port in the GUI or config file. The default is `127.0.0.1:8213`.

If the server is unreachable, packets are saved to a local fallback file (`entropy_fallback.bin`) so no harvested entropy is lost.

---

## File Structure

```
ChaosVisual/
  chaosmain.py    # Backend: sources, NIST math, mixer, network sender, CLI
  chaosgui.py     # Frontend: Tkinter GUI, device panels, oscilloscope
  config.json     # Auto-generated on first run
  cover.png       # Project logo
  README.md
```

`chaosmain.py` is fully self-contained and can be used as a library or standalone CLI tool. `chaosgui.py` imports from it and adds the graphical interface.

---

## Health Ratings

The GUI displays a health rating for each sample based on combined Shannon and min-entropy:

| Rating | Shannon (MM) | Min-Entropy | Meaning |
|---|---|---|---|
| Excellent | >= 7.9 | >= 7.0 | Near-ideal randomness |
| Healthy | >= 7.5 | >= 6.5 | Good quality |
| Acceptable | >= 6.0 | -- | Usable but not ideal |
| Degraded | >= 4.0 | -- | Low quality, investigate sources |
| Poor | < 4.0 | -- | Insufficient entropy |

Shannon values use Miller-Madow bias correction (Miller 1955) to reduce underestimation in small samples.

---

## Troubleshooting

**No audio devices found (Linux):**
Check that PulseAudio or PipeWire is running. Try `pactl list short sources` to see available sources. Rebuild pyaudio against portaudio19 if monitor sources are visible to pactl but not to PyAudio.

**Keyboard entropy not working (Linux):**
Make sure your user is in the `input` group (`sudo usermod -aG input $USER`) and log out/in. On Wayland, `evdev` is preferred over `pynput`.

**WASAPI errors (Windows):**
Check Settings > Privacy > Microphone > Allow apps. If channel mismatch errors appear, use the Rescan Devices button in the GUI.

**Low entropy readings:**
Enable more sources. Screen capture and audio loopback tend to contribute the most raw entropy. Mouse and keyboard contribute less frequently but add valuable human-driven randomness.

---

## License

MIT

---

## Disclaimer

ChaosVisual is provided as-is for research and educational purposes. It is a prototype under active development. Entropy quality depends heavily on hardware, drivers, OS configuration, and environmental factors. The authors make no guarantees about the cryptographic strength of the output. Do not rely on this tool for security-critical applications.

# 🌟 Dio Design

🏆 1st Place — Qualcomm Multi-Device Track @ UCSD DiamondHacks

> *Speak your design into existence.*

Dio Design is a voice-controlled mixed-reality 3D design workspace powered by four Qualcomm platforms. Wear a headset, hold a controller, and talk to Dio — your AI design companion — to create, modify, and manage 3D scenes in augmented reality.

---

## Demo

```
You:    "Create a modern walnut desk with tapered legs"
Dio:    *thinks for 2 seconds*
        *a detailed desk materializes in AR space in front of you*
Dio:    "Here's your walnut desk!"

You:    "Make it marble instead"
Dio:    *desk material transforms to white marble with veining*
Dio:    "Switched to marble — looking elegant!"

You:    *pushes joystick forward*
        *desk scales up smoothly*

You:    *presses undo button*
        *desk reverts to walnut*
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        DIO DESIGN SYSTEM                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   👤 Designer wearing VR Headset + holding Controller           │
│       │                              │                          │
│       ▼                              ▼                          │
│   ┌────────────┐              ┌──────────────┐                  │
│   │ Samsung S25│              │ Arduino UNO Q│                  │
│   │   Ultra    │              │ (Dragonwing  │                  │
│   │ (Snap 8    │              │  QRB2210)    │                  │
│   │  Elite)    │              │              │                  │
│   │            │              │ Joystick     │                  │
│   │ WebXR AR   │              │ 3x Buttons   │                  │
│   │ Voice In   │              │ IMU          │                  │
│   │ Dio Avatar │              │              │                  │
│   │ 3D Render  │              └──────┬───────┘                  │
│   └─────┬──────┘                     │ UDP                      │
│         │ WebSocket                  │                          │
│         ▼                            ▼                          │
│   ┌──────────────────────────────────────────┐                  │
│   │         AI PC — Snapdragon X Elite       │                  │
│   │         (Copilot+ PC)                    │                  │
│   │                                          │                  │
│   │   FastAPI Hub Server                     │                  │
│   │   ├── WebSocket ↔ AR Viewer              │                  │
│   │   ├── WebSocket ↔ Dashboard              │                  │
│   │   ├── UDP ← Controller                   │                  │
│   │   ├── REST → Qualcomm Cloud AI           │                  │
│   │   ├── REST → ElevenLabs TTS             │                  │
│   │   └── Version History + Storage          │                  │
│   │                                          │                  │
│   │   Designer Dashboard (browser)           │                  │
│   │   ├── Version Timeline                   │                  │
│   │   ├── 3D Preview + Orbit Controls        │                  │
│   │   └── Export GLB                         │                  │
│   └──────────────────┬───────────────────────┘                  │
│                      │ REST API                                 │
│                      ▼                                          │
│   ┌──────────────────────────────────────────┐                  │
│   │    Qualcomm Cloud AI 100 Ultra           │                  │
│   │    (Cirrascale Inference Cloud)          │                  │
│   │                                          │                  │
│   │    Llama-3.3-70B  → 3D code generation   │                  │
│   └──────────────────────────────────────────┘                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Four Qualcomm Platforms

| Platform | Chip | Role |
|----------|------|------|
| **Qualcomm Cloud AI 100 Ultra** | Cloud AI 100 Ultra | LLM inference (Llama-3.3-70B) |
| **AI PC / Copilot+** | Snapdragon X Elite | Hub server, version control, designer dashboard, device orchestration |
| **Samsung Galaxy S25 Ultra** | Snapdragon 8 Elite | AR/VR viewport, voice capture, 3D rendering, Dio avatar |
| **Arduino UNO Q** | Dragonwing QRB2210 + STM32U585 | Wireless controller — joystick, buttons, IMU over WiFi UDP |

---

## Features

**Voice-Driven 3D Design**
Speak naturally to create and manipulate 3D objects in augmented reality. Powered by Llama-3.3-70B running on Qualcomm Cloud AI 100 Ultra, which generates three.js code executed live in the AR scene.

**Dio — AI Design Companion**
A procedural animated star character with emotional states, blinking, eye tracking, squash-and-stretch physics, personality quirks, voice-reactive behavior, and a completion celebration orbit. Dio makes the design process feel collaborative.

**Physical Wireless Controller**
Custom-built with Arduino UNO Q: push-to-talk for voice commands, joystick for scaling, buttons for pick/select and undo. Communicates over WiFi UDP. Housed in a 3D-printed enclosure ("Diora's Box").

**Designer Dashboard**
Real-time version timeline, live 3D preview with orbit controls, export to GLB, session save/load. Every voice command and scene state is logged and recoverable.

**Custom 3D-Printed Hardware**
VR headset (modified Secondsight design, re-parameterized for S25 Ultra + 34mm/45mm lenses) and controller enclosure, both designed in OpenSCAD and printed during the hackathon.

**Dual Display Modes**
Handheld AR mode with surface detection and tap-to-place, or stereo VR mode with camera passthrough for the 3D-printed headset.

---

## Project Structure

```
Dio-Design/
├── server.py              # FastAPI hub server — orchestrates everything
├── index.html             # WebXR AR viewer — 3D scene, Dio avatar, voice input
├── dashboard.html         # Designer dashboard — version history, 3D preview
├── controller/
│   ├── firmware.ino       # Arduino UNO Q MCU firmware
│   └── udp_sender.py      # UNO Q Linux-side UDP forwarder
├── start.bat              # Windows startup script
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── ARCHITECTURE.md        # Detailed system architecture
└── SETUP.md               # Setup and deployment guide
```

---

## Quick Start

### Prerequisites

- Python 3.10+
- Samsung Galaxy S25 Ultra (or any WebXR-capable Android phone)
- Chrome browser with WebXR flags enabled
- Qualcomm Cloud AI API key from [Cirrascale](https://aisuite.cirrascale.com/account/api-keys)

### 1. Clone and Install

```bash
git clone https://github.com/Sanjith-Shan/Dio-Design.git
cd Dio-Design
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your Qualcomm Cloud AI key and (optionally) ElevenLabs key
```

The system works without API keys — it falls back to the built-in command parser and skips voice output.

### 3. Run

```bash
python server.py
```

### 4. Connect

- **AR Viewer:** Open `http://YOUR_PC_IP:8080` on the S25 Ultra in Chrome
- **Dashboard:** Open `http://localhost:8080/dashboard` on the AI PC
- **Health Check:** `http://localhost:8080/health`

### 5. Chrome Flags (S25 Ultra)

Navigate to `chrome://flags` and enable:
- `#unsafely-treat-insecure-origin-as-secure` → add `http://YOUR_PC_IP:8080`
- `#webxr-cardboard` → Enabled (for stereo headset mode)

---

## Hardware Build

### VR Headset

Modified [Secondsight](https://github.com/secondsight/secondsight-hardware) open-source design, re-parameterized in OpenSCAD for:
- Samsung Galaxy S25 Ultra (162.8 × 77.6 × 8.2 mm)
- 34mm diameter / 45mm focal length biconvex PMMA lenses
- 120mm temple distance

**Print 3 parts:** body (`plate="body"`), lens holders (`plate="lens_holders"`), optics support (`plate="optics_support"`)

**Assembly:** Press lenses into holders → mount holders in support plates → slide plates into body ledges → thread elastic strap → attach foam padding

### Controller ("Diora's Box")

18cm × 6cm × 7cm two-piece OpenSCAD enclosure with precision-cut holes:
- 27mm hole for KY-023 joystick
- 4× 12mm holes for push buttons (Push-to-Talk, Pick, Undo, Redo)

**Wiring:**

| Component | Pin |
|-----------|-----|
| Joystick VRx | A0 |
| Joystick VRy | A1 |
| Joystick SW | D2 |
| MPU-6500 SDA | A4 |
| MPU-6500 SCL | A5 |
| Push-to-Talk | D4 → GND |
| Pick/Select | D5 → GND |
| Undo | D6 → GND |
| MPU-6500 VCC | 3.3V |
| Joystick +5V | 5V |

All buttons use INPUT_PULLUP — no external resistors needed.

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | AR viewer (index.html) |
| `/dashboard` | GET | Designer dashboard |
| `/health` | GET | System status JSON |
| `/versions` | GET | List all saved scene versions |
| `/ws/ar` | WebSocket | AR viewer connection |
| `/ws/dashboard` | WebSocket | Dashboard connection |
| UDP `:9877` | UDP | Controller input |

---

## Qualcomm Cloud AI API

The Cirrascale Inference Cloud provides OpenAI-compatible endpoints running on Qualcomm Cloud AI 100 Ultra:

```
Base URL: https://aisuite.cirrascale.com/apis/v2
Auth:     Authorization: Bearer <API_KEY>
```

**Available Models:**
- `Llama-3.3-70B` — primary LLM for voice command → three.js code generation
- `DeepSeek-R1-Distill-Llama-70B` — reasoning model
- `Llama-3.1-8B` — lightweight fallback

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Hub Server | Python, FastAPI, httpx, uvicorn |
| AR Viewer | three.js r162, WebXR, Web Speech API, WebSocket |
| Dashboard | three.js, OrbitControls, vanilla JS |
| Firmware | Arduino/Zephyr, C++, Wire.h (I2C) |
| Controller Comm | Python, pyserial, UDP sockets |
| LLM Inference | Qualcomm Cloud AI 100 Ultra via REST |
| TTS | ElevenLabs API |
| 3D Printing | OpenSCAD, PLA filament |
| Hardware Design | OpenSCAD (parametric) |

---

## The Pipeline

```
1. User speaks          →  "Create a red sports car"
2. Web Speech API       →  Transcribes to text
3. WebSocket            →  Sends to hub server
4. Hub Server           →  Forwards to Qualcomm Cloud AI 100
5. Llama-3.3-70B        →  Generates three.js code
6. Hub Server           →  Extracts code, sends to AR viewer
7. AR Viewer            →  Executes code via Function constructor
8. three.js             →  Red sports car appears in AR
9. Scene State          →  Serialized, versioned, sent to dashboard
10. ElevenLabs          →  "Here's your red sports car!" (audio)
11. Dio Avatar          →  Celebration orbit animation
```

**Latency:** ~2-4 seconds end-to-end (voice → visible object)

---

## Challenges & Solutions

| Challenge | Solution |
|-----------|----------|
| Web Speech API fails inside WebXR | Start recognition before `requestSession()` in same user gesture; implemented push-to-talk via hardware controller |
| `aiohttp` won't install on Windows ARM | Replaced with `httpx` (pure Python) |
| No sudo on AI PC, can't install Blender | Eliminated Blender entirely — LLM generates three.js code executed directly in the AR viewer |
| Llama-3.1-8B produces poor 3D code | Switched to Llama-3.3-70B (same API, same infrastructure, 9x larger model) |
| ElevenLabs 402 errors | Graceful fallback to text bubbles via Dio avatar |
| UNO Q serial port discovery | MCU-to-Linux UART on non-standard device path, required probing via ADB |

---

## License

MIT

---

## Acknowledgments

- [Qualcomm Technologies](https://www.qualcomm.com/) — Cloud AI 100 Ultra infrastructure and hackathon sponsorship
- [Cirrascale Cloud Services](https://www.cirrascale.com/) — Inference Cloud platform
- [Secondsight](https://github.com/secondsight/secondsight-hardware) — Open-source VR headset design (MPL-2.0)
- [three.js](https://threejs.org/) — 3D rendering engine
- [ElevenLabs](https://elevenlabs.io/) — Text-to-speech API
- [Arduino](https://www.arduino.cc/) — UNO Q platform and IDE

---

<p align="center">
  <em>Built in 48 hours at the Qualcomm Multiverse Hackathon</em><br>
  <strong>🌟 Speak your design into existence 🌟</strong>
</p>

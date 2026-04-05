# Dio Design — System Architecture

## Overview

Dio Design is a voice-controlled mixed-reality CAD workspace. LLM-generated three.js code executes directly in the live AR scene — no intermediate 3D software, no export pipeline.

## Platforms

- **Qualcomm Cloud AI 100** — LLM inference (Llama-3.1-8B) generates three.js from natural language
- **AI PC / Copilot+ PC (Snapdragon X Elite)** — Hub server, Dashboard, version control
- **Samsung S25 Ultra (Snapdragon 8 Elite)** — WebXR AR viewer, voice input, Dio avatar, scene execution
- **Arduino UNO Q (Dragonwing QRB2210)** — Wireless spatial controller

## Device Communication

```
                    ┌────────────────────────────┐
                    │  Qualcomm Cloud AI 100     │
                    │  Llama-3.1-8B              │
                    │  REST API — three.js gen   │
                    └─────────────┬──────────────┘
                                  │ HTTPS
┌─────────────────────────────────┼───────────────────────────┐
│               AI PC (Copilot+ / Snapdragon X Elite)        │
│                                 │                           │
│  ┌──────────────────────────────▼────────────────────────┐  │
│  │               Hub Server (FastAPI)                    │  │
│  │               http://0.0.0.0:8080                     │  │
│  │                                                       │  │
│  │  GET /        → AR viewer (index.html)                │  │
│  │  GET /dashboard → Dashboard (dashboard.html)          │  │
│  │  WS /ws/ar    → S25 AR viewer                         │  │
│  │  WS /ws/dashboard → Designer Dashboard               │  │
│  │  UDP :9877    → UNO Q controller                      │  │
│  │  HTTPS → Qualcomm Cloud AI (LLM)                      │  │
│  │  HTTPS → ElevenLabs (TTS)                             │  │
│  └──────┬─────────────────────────────────────────────────┘  │
│         │                                                   │
│  ┌──────▼──────────────────┐  ┌─────────────────────────┐   │
│  │ Dashboard (browser)     │  │ versions/ directory     │   │
│  │ dashboard.html          │  │ scene JSON history      │   │
│  └─────────────────────────┘  └─────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
        │ WebSocket                         │ UDP
        │ (WiFi)                            │ (WiFi)
┌───────▼──────────────────┐    ┌───────────▼────────────┐
│ Samsung S25 Ultra        │    │   Arduino UNO Q        │
│ Snapdragon 8 Elite       │    │ Dragonwing QRB2210     │
│                          │    │                        │
│ Chrome Browser           │    │ MCU: IMU+Joystick      │
│ ├─ WebXR AR View         │    │      +Buttons          │
│ ├─ three.js scene        │    │ Linux: UDP sender      │
│ ├─ LLM code execution    │    └────────────────────────┘
│ ├─ Voice Input           │
│ ├─ Dio Avatar            │
│ └─ Audio Playback        │
└──────────────────────────┘
```

## Data Flow — Voice Command

```
1. User speaks "Add a red sphere"
2. S25 Web Speech API transcribes → sends {type:"voice", text:"...", final:true}
3. Hub → sends {type:"avatar", state:"thinking"} to S25
4. Hub → sends prompt to Qualcomm Cloud AI 100
5. LLM returns three.js JS code in ```javascript``` block + spoken text
6. Hub extracts code → sends {type:"execute", code:"..."} to S25
7. S25 runs: new Function('THREE','scene',..., code)(THREE, scene, ...)
8. Red sphere appears in AR instantly
9. S25 serializes scene → sends {type:"scene_state", data:{...}} to hub
10. Hub saves new version, notifies dashboard
11. Hub sends spoken text to ElevenLabs → gets mp3
12. Hub sends {type:"audio", format:"mp3", size:N} + mp3 binary to S25
13. S25 plays audio (Dio speaks)
14. Hub sends {type:"avatar", state:"done"} to S25
```

## Message Protocol

### S25 → Hub (WebSocket text)
```json
{"type": "voice", "text": "add a red sphere", "final": true}
{"type": "scene_state", "data": {"objects": [...], "timestamp": 1234567890}}
{"type": "ping"}
{"type": "request_state"}
```

### Hub → S25 (WebSocket text + binary)
```json
{"type": "execute", "code": "const geo=new THREE.SphereGeometry(0.5)..."}
{"type": "load_state", "data": {"objects": [...]}}
{"type": "trigger_export", "version_id": "uuid"}
{"type": "avatar", "state": "idle|thinking|done|error", "text": "..."}
{"type": "audio", "format": "mp3", "size": 8192}
[binary frame: mp3 audio]
{"type": "status", "text": "Connected to Dio Hub"}
{"type": "pong"}
{"type": "pick_mode", "active": true}
```

### Dashboard → Hub (WebSocket text)
```json
{"type": "load_version", "version_id": "uuid"}
{"type": "trigger_export", "version_id": "uuid"}
{"type": "save_session"}
{"type": "load_session", "data": [...]}
{"type": "request_state"}
```

### Hub → Dashboard (WebSocket text)
```json
{"type": "versions", "data": [{"id":"uuid","version":1,"timestamp":"...","command":"..."}]}
{"type": "command_log", "command": "...", "response": "...", "timestamp": "..."}
{"type": "connection_status", "ar_connected": true, "controller_connected": false}
```

### UNO Q → Hub (UDP)
```json
{"type": "controller", "joy_x": 0.0, "joy_y": 0.45,
 "imu": [ax, ay, az, gx, gy, gz],
 "buttons": {"pick": false, "undo": false, "redo": false, "joy_btn": false}}
```

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8080 | HTTP/WS  | Hub server (AR viewer, dashboard, WebSockets) |
| 9877 | UDP      | UNO Q controller input |

## Startup Order

1. Start Hub server: `python server.py`
2. On S25: open http://AI_PC_IP:8080 in Chrome
3. On AI PC browser: open http://localhost:8080/dashboard
4. Power on UNO Q (connects automatically via WiFi)

## Qualcomm Narrative

"Every layer of Dio Design runs on Qualcomm:
- **Cloud AI 100** powers the intelligence — LLM inference generating live three.js from voice
- **Snapdragon X Elite** orchestrates the system — hub, dashboard, version control
- **Snapdragon 8 Elite** renders the spatial experience — AR visualization and real-time code execution
- **Dragonwing QRB2210** provides physical precision — wireless controller with 6-DOF input"

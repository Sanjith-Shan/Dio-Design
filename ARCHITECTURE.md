# Dio Design — System Architecture

## Overview

Dio Design is a voice-controlled mixed-reality CAD workspace distributed across four Qualcomm-powered platforms:

- **Qualcomm Cloud AI 100** — LLM inference (generates Blender commands from natural language)
- **AI PC / Copilot+ PC (Snapdragon X Elite)** — Hub server, Blender, OpenClaw orchestration
- **Samsung S25 Ultra (Snapdragon 8 Elite)** — WebXR AR viewer, voice input, Dio avatar
- **Arduino UNO Q (Dragonwing QRB2210)** — Wireless spatial controller

Every layer runs on Qualcomm silicon: inference, orchestration, visualization, and physical input.

## External Services

- **Qualcomm AI Inference Suite** — LLM API endpoint (hosted on Cloud AI 100 Ultra hardware)
- **ElevenLabs** — Text-to-speech for Dio avatar voice responses (streaming audio)

## Device Communication

```
                    ┌────────────────────────────┐
                    │  Qualcomm Cloud AI 100     │
                    │  (AI Inference Suite)       │
                    │  REST API — LLM inference   │
                    └─────────────┬──────────────┘
                                  │ HTTPS
┌─────────────────────────────────┼───────────────────────────┐
│               AI PC (Copilot+ / Snapdragon X Elite)        │
│                                 │                           │
│  ┌──────────────────────────────▼────────────────────────┐  │
│  │               Hub Server (FastAPI)                    │  │
│  │               http://0.0.0.0:8080                     │  │
│  │                                                       │  │
│  │  HTTP  → Serves AR viewer to S25                      │  │
│  │  WS    → /ws/ar (S25 connects here)                   │  │
│  │  UDP   → :9877 (UNO Q sends here)                     │  │
│  │  TCP   → localhost:9876 (Blender addon)               │  │
│  │  HTTPS → Qualcomm Cloud AI (LLM inference)            │  │
│  │  HTTPS → ElevenLabs (TTS for avatar voice)            │  │
│  └──────┬─────────────┬──────────────────────────────────┘  │
│         │             │                                     │
│    ┌────▼─────┐  ┌────▼────────┐                            │
│    │ Blender  │  │ File System │                            │
│    │ (addon)  │  │ /tmp/*.glb  │                            │
│    │ :9876    │  │ /tmp/*.mp3  │                            │
│    └──────────┘  └─────────────┘                            │
└─────────────────────────────────────────────────────────────┘
        │ WebSocket                         │ UDP
        │ (WiFi)                            │ (WiFi)
┌───────▼──────────┐              ┌─────────▼──────────┐
│ Samsung S25 Ultra │              │   Arduino UNO Q    │
│ Snapdragon 8 Elite│              │ Dragonwing QRB2210 │
│                   │              │                    │
│ Chrome Browser    │              │ MCU: IMU+Joystick  │
│ ├─ WebXR AR View  │              │      +Buttons      │
│ ├─ Voice Input    │              │ Linux: UDP sender  │
│ ├─ Dio Avatar     │              │                    │
│ ├─ Audio Playback │              │ Battery powered    │
│ └─ Touch Controls │              └────────────────────┘
└───────────────────┘

```

## Data Flow — Voice Command (Full Pipeline)

```
1. User speaks "Scale the cube by 200%"
2. S25 Web Speech API transcribes → sends {type:"voice", text:"...", final:true}
3. Hub receives → sends {type:"avatar", state:"thinking"} to S25
4. Hub sends text to Qualcomm Cloud AI 100 via REST API
5. Cloud AI returns bpy code (Blender Python commands)
6. Hub sends bpy code to Blender addon via TCP (localhost:9876)
7. Blender executes the code
8. Hub triggers glTF export in Blender
9. Hub reads /tmp/dio_scene.glb
10. Hub sends {type:"model"} + binary glTF to S25
11. Hub sends LLM response text to ElevenLabs TTS API
12. ElevenLabs returns audio stream
13. Hub sends audio chunks to S25
14. S25 plays audio (Dio speaks), avatar reacts to audio
15. Hub sends {type:"avatar", state:"done"} to S25
16. S25 hot-swaps 3D model in AR, avatar does completion orbit
```

## Message Protocol

### S25 → Hub (WebSocket text)

```json
{"type": "voice", "text": "make the cube bigger", "final": true}
{"type": "voice", "text": "make the cu", "final": false}
{"type": "place_model", "position": [0, 0, -1.5]}
{"type": "request_model"}
{"type": "ping"}
```

### Hub → S25 (WebSocket text + binary)

```json
{"type": "avatar", "state": "idle|listening|thinking|done|error", "text": "..."}
{"type": "model", "format": "glb", "size": 12345}
[binary frame: glTF data]
{"type": "audio", "format": "mp3", "size": 8192}
[binary frame: audio data]
{"type": "status", "text": "Connected to Blender"}
{"type": "command_result", "success": true, "description": "Scaled cube by 200%"}
{"type": "pong"}
```

### UNO Q → Hub (UDP)

```json
{"type": "controller",
 "joy_x": 0.0, "joy_y": 0.45,
 "imu": [0.1, -0.3, 0.95, 0.0, 0.02, -0.01],
 "buttons": {"pick": false, "undo": false, "redo": false, "joy_btn": false}}
```

## Environment Variables

```env
# Qualcomm Cloud AI
QUALCOMM_AI_API_URL=https://...  # Provided at hackathon
QUALCOMM_AI_API_KEY=...          # Provided at hackathon
QUALCOMM_AI_MODEL=...            # e.g. llama-3.1-70b or whatever they host

# ElevenLabs
ELEVENLABS_API_KEY=...           # Your ElevenLabs API key
ELEVENLABS_VOICE_ID=...          # Voice ID for Dio (pick a friendly one)

# Blender
BLENDER_HOST=127.0.0.1           # localhost if hub + Blender on same machine
BLENDER_PORT=9876

# Hub
HUB_HOST=0.0.0.0
HUB_PORT=8080

# Controller
CONTROLLER_UDP_PORT=9877

# Export
GLB_EXPORT_PATH=C:/tmp/dio_scene.glb
```

## Ports

| Port  | Protocol | Purpose                          |
|-------|----------|----------------------------------|
| 8080  | HTTP/WS  | Hub server (AR viewer + WebSocket) |
| 9876  | TCP      | Blender addon socket (localhost)  |
| 9877  | UDP      | UNO Q controller input           |

## Startup Order

1. Start Blender → enable MCP addon → click Connect (port 9876)
2. Start Hub server → `python hub/server.py`
3. On S25 → open http://AI_PC_IP:8080 in Chrome
4. Power on UNO Q (connects automatically via WiFi)

## Latency Budget

| Segment                          | Target   |
|----------------------------------|----------|
| Voice transcription (Web Speech) | ~300ms   |
| WebSocket S25 → Hub             | ~5ms     |
| Qualcomm Cloud AI inference      | ~1-3s    |
| Blender command execution        | ~50ms    |
| glTF export                      | ~200ms   |
| glTF transfer (WebSocket)        | ~100ms   |
| ElevenLabs TTS                   | ~500ms   |
| AR model swap                    | ~50ms    |
| **Total voice → visual + audio** | **~2-5s** |
| Controller input → visual        | ~400ms   |

## Qualcomm Narrative (for judges)

"Every layer of Dio Design runs on Qualcomm:
- **Cloud AI 100** powers the intelligence — LLM inference that understands voice commands
- **Snapdragon X Elite** orchestrates the system — running the hub, Blender, and agent framework
- **Snapdragon 8 Elite** renders the spatial experience — AR visualization and voice capture
- **Dragonwing QRB2210** provides physical precision — wireless controller with 6-DOF input

From cloud to edge to hand, it's Qualcomm silicon end to end."

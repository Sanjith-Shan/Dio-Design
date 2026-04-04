# CLAUDE.md — Dio Design Project Context

## What is Dio Design?

Dio Design is a voice-controlled mixed-reality CAD workspace for a Qualcomm hackathon. A user speaks natural language commands to manipulate 3D models in Blender, sees the results live in augmented reality on a Samsung Galaxy S25 Ultra, and uses a wireless Arduino UNO Q controller for fine-grained input like scaling and undo/redo.

## Architecture (4 Qualcomm platforms)

1. **Qualcomm Cloud AI 100** — LLM inference via REST API (generates Blender Python code from voice)
2. **AI PC / Copilot+ (Snapdragon X Elite)** — Hub server (FastAPI), Blender, orchestration
3. **Samsung S25 Ultra (Snapdragon 8 Elite)** — WebXR AR viewer in Chrome, voice input, Dio avatar
4. **Arduino UNO Q (Dragonwing QRB2210)** — Wireless 6-DOF controller (IMU + joystick + buttons over WiFi UDP)

## Project Structure

```
dio-design/
├── ARCHITECTURE.md          # Full system design, message protocol, data flows
├── SETUP.md                 # Hackathon day quickstart
├── .env.example             # Environment variables template
├── start.bat                # Windows startup script
├── hub/
│   ├── server.py            # Central FastAPI hub (the main orchestrator)
│   └── requirements.txt     # Python deps
├── ar/
│   └── index.html           # WebXR AR viewer + Dio avatar (served by hub)
└── controller/
    ├── firmware.ino          # Arduino sketch for UNO Q MCU
    └── udp_sender.py        # Python UDP forwarder for UNO Q Linux side
```

## Key Technical Details

- **Hub server** (`hub/server.py`): FastAPI app. Connects to Blender addon via TCP socket on port 9876. Accepts WebSocket from S25 on /ws/ar. Listens for UDP from UNO Q on port 9877. Calls Qualcomm Cloud AI for LLM inference. Calls ElevenLabs for TTS. Has a fallback command parser for testing without cloud services.

- **AR viewer** (`ar/index.html`): Single-file WebXR app using three.js r162. Uses `immersive-ar` with `hit-test` for surface placement. Loads glTF models pushed from hub. Contains the Dio avatar — a procedural animated star character with eyes, mouth, blinking, personality quirks, and continuous emotion blending. Uses Web Speech API for voice input and Web Audio API AnalyserNode for voice amplitude detection.

- **Avatar animation system**: NOT a rigid state machine. Uses continuous values (energy, attention, happiness) that lerp toward targets. Features: organic floating motion (layered sine waves), squash-and-stretch spring physics on transitions, random personality quirks (tilt, shiver, bounce, look-around), blinking at random intervals, eye gaze wandering, voice attention (leans forward and glows when user speaks), completion orbit (arcs around model when command finishes).

- **Communication protocol**: All messages are JSON over WebSocket (S25↔Hub) or UDP (UNO Q→Hub). Model data is sent as binary WebSocket frames (glTF GLB). Audio from ElevenLabs is sent as binary frames (mp3).

- **LLM system prompt**: The hub sends a system prompt that instructs the LLM to output valid bpy code in ```python``` blocks along with a brief friendly spoken response. The hub extracts the code (executes in Blender) and the text (sends to ElevenLabs for TTS).

## Environment Variables

See `.env.example`. The system gracefully degrades — works without Qualcomm AI (uses fallback parser) and without ElevenLabs (text-only responses).

## Things That Still Need Work

- OpenClaw integration (optional — hub currently handles LLM calls directly)
- IMU-based model rotation from controller (firmware sends data, hub doesn't process it yet)
- Pick-up gesture (hold button + IMU to move model in 3D)
- HTTPS for production (currently uses Chrome flag workaround for WebXR over HTTP)
- Replacing procedural star avatar with Blender-modeled version (optional)
- Audio playback handler in AR viewer (hub sends audio, viewer needs to decode and play mp3 binary)

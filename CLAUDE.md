# CLAUDE.md — Dio Design Project Context

## What is Dio Design?

Dio Design is a voice-controlled mixed-reality CAD workspace for a Qualcomm hackathon. A user speaks natural language commands, the LLM generates three.js code, and that code executes directly in the live AR scene on a Samsung Galaxy S25 Ultra. No Blender, no export pipeline — changes appear in real time. A wireless Arduino UNO Q controller provides fine-grained input for scaling, rotation, and picking up objects.

## Architecture (4 Qualcomm platforms)

1. **Qualcomm Cloud AI 100** — LLM inference (Llama-3.1-8B at https://aisuite.cirrascale.com/apis/v2/chat/completions) — generates three.js JS code from voice commands
2. **AI PC / Copilot+ (Snapdragon X Elite)** — Hub server (FastAPI), Designer Dashboard, orchestration, version control
3. **Samsung S25 Ultra (Snapdragon 8 Elite)** — WebXR AR viewer in Chrome, voice input, Dio avatar, three.js scene execution
4. **Arduino UNO Q (Dragonwing QRB2210)** — Wireless 6-DOF controller (IMU + joystick + buttons over WiFi UDP)

## Project Structure

```
dio-design/
├── ARCHITECTURE.md          # Full system design
├── CLAUDE.md                # This file
├── SETUP.md                 # Hackathon day quickstart
├── .env.example             # Environment variables template
├── start.bat                # Windows startup script
├── server.py                # Central FastAPI hub
├── requirements.txt         # Python deps
├── index.html               # WebXR AR viewer + Dio avatar (served at /)
├── dashboard.html           # Designer Dashboard (served at /dashboard)
├── versions/                # Auto-created, stores scene version JSON files
└── controller/
    ├── firmware.ino         # Arduino sketch for UNO Q MCU
    └── udp_sender.py        # Python UDP forwarder for UNO Q Linux side
```

## Key Technical Details

- **Hub server** (`server.py`): FastAPI app. Routes voice commands to Qualcomm Cloud AI, extracts three.js code from LLM response, sends it to AR viewer via WebSocket as `{type:"execute","code":"..."}`. Receives scene state back, saves versions. Serves AR viewer at `/` and dashboard at `/dashboard`. Listens for UDP from UNO Q on port 9877. Calls ElevenLabs for TTS.

- **AR viewer** (`index.html`): Single-file WebXR app using three.js r162. Receives `{type:"execute","code":"..."}` from hub and runs it with `new Function('THREE','scene','camera','renderer','GLTFLoader', code)(...)`. After execution, serializes scene state and sends it back to hub. Handles state loading (`load_state`), GLB export (`trigger_export`). Contains the Dio avatar.

- **Designer Dashboard** (`dashboard.html`): Runs in a browser on the AI PC. Shows version timeline, live activity feed, connection status. Allows loading previous versions, triggering GLB exports, saving/loading sessions.

- **Version control**: Hub stores every scene state (triggered by voice commands or controller actions) as a version with timestamp, command, and serialized scene data. Undo/redo steps through this list. Versions persist to disk in `versions/`.

- **Communication protocol**: Voice → AR viewer sends `{type:"voice",text:"...",final:true}` → Hub calls LLM → Hub sends `{type:"execute","code":"..."}` to AR → AR executes JS → AR sends `{type:"scene_state",data:{...}}` back → Hub saves version → Hub notifies Dashboard.

- **Avatar animation system**: Continuous emotional values (energy, attention, happiness) that lerp toward targets. Organic floating motion, squash-and-stretch, random personality quirks, blinking, voice attention effects. NOT a state machine.

- **LLM system prompt**: Instructs the LLM to output valid three.js JS code in ```javascript``` blocks operating on globals `THREE`, `scene`, `camera`, `renderer`, `GLTFLoader`. Hub extracts the code and sends it to the AR viewer for direct execution.

- **Controller**: Joystick Y axis scales objects, IMU gyro rotates objects, pick button + accelerometer moves objects in 3D. Undo/redo steps through scene versions.

## Environment Variables

See `.env.example`. The system gracefully degrades — works without Qualcomm AI (uses fallback parser) and without ElevenLabs (text-only responses).

## Things That Still Need Work

- HTTPS cert for real-device WebXR (env var USE_HTTPS=true, generate cert with openssl)
- Load Session from file (dashboard has the UI, server needs a load_session handler)
- Controller pick gesture for XR model repositioning (hub side done, AR side visual feedback done)

"""
Dio Hub Server — Central orchestrator for the Dio Design system.

Connects:
  - Qualcomm Cloud AI 100 via REST API (LLM inference → three.js code)
  - ElevenLabs via REST API (text-to-speech for Dio avatar)
  - Samsung S25 AR viewer via WebSocket (/ws/ar)
  - Dashboard via WebSocket (/ws/dashboard)
  - UNO Q controller via UDP (:9877)

Serves:
  - AR viewer HTML at /
  - Dashboard HTML at /dashboard

Run: python server.py
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn

# ─── Config ───────────────────────────────────────────────────────────────────

QUALCOMM_AI_API_KEY  = os.getenv("QUALCOMM_AI_API_KEY", "")
QUALCOMM_AI_MODEL    = os.getenv("QUALCOMM_AI_MODEL", "Llama-3.1-8B")
ELEVENLABS_API_KEY   = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID  = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
HUB_HOST             = os.getenv("HUB_HOST", "0.0.0.0")
HUB_PORT             = int(os.getenv("HUB_PORT", "8080"))
CONTROLLER_UDP_PORT  = int(os.getenv("CONTROLLER_UDP_PORT", "9877"))
USE_HTTPS            = os.getenv("USE_HTTPS", "false").lower() == "true"
SSL_CERT_FILE        = os.getenv("SSL_CERT_FILE", "cert.pem")
SSL_KEY_FILE         = os.getenv("SSL_KEY_FILE", "key.pem")
VERSIONS_DIR         = Path(os.getenv("VERSIONS_DIR", "versions"))

QUALCOMM_AI_API_URL  = "https://aisuite.cirrascale.com/apis/v2/chat/completions"

# IMU / controller constants
IMU_SEND_INTERVAL    = 0.02    # 50 Hz firmware rate
GYRO_DEADZONE        = 2.0     # deg/s — ignore noise below this
GYRO_SCALE           = 0.0008  # deg/s → radians per tick
PICK_ACCEL_DEADZONE  = 0.05    # g
PICK_MOVE_SCALE      = 0.04    # g delta → scene units

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dio-hub")

# ─── App & Global State ───────────────────────────────────────────────────────

app = FastAPI(title="Dio Hub")

ar_clients:        List[WebSocket] = []
dashboard_clients: List[WebSocket] = []
versions:          List[dict]      = []   # in-memory version history
version_index:     int             = -1   # current position for undo/redo
pick_mode_active:  bool            = False
pick_imu_origin:   list            = None
last_voice_command: str            = ""

# ─── LLM System Prompt ────────────────────────────────────────────────────────

THREEJS_SYSTEM_PROMPT = """You are Dio, a friendly AI assistant that helps users create and modify 3D models in a live augmented reality scene using three.js.

When the user gives you a 3D modeling command, you must:
1. Generate valid three.js JavaScript code that modifies a global `scene` variable
2. Wrap the code in ```javascript``` code blocks
3. Give a brief, friendly 1-sentence response describing what you did

Rules for your three.js code:
- Available globals: THREE, scene, camera, renderer, GLTFLoader
- Create objects: const geo = new THREE.BoxGeometry(1,1,1); const mat = new THREE.MeshStandardMaterial({color:0xff0000,metalness:0,roughness:0.5}); const mesh = new THREE.Mesh(geo,mat); mesh.name='my_cube'; scene.add(mesh);
- Always set .name on created objects (descriptive snake_case)
- Find existing objects: const obj = scene.getObjectByName('my_cube');
- Delete objects: const obj = scene.getObjectByName('name'); if(obj) scene.remove(obj);
- Materials: use MeshStandardMaterial with color (hex number), metalness (0-1), roughness (0-1), emissive (hex), emissiveIntensity (0-1)
- Transforms: set obj.position.set(x,y,z), obj.rotation.set(x,y,z), obj.scale.set(x,y,z) or obj.scale.setScalar(s)
- Animations: obj.userData.animate = function(time) { obj.rotation.y = time * 0.5; }
- Lights: const light = new THREE.PointLight(0xffffff, 1, 10); light.name='my_light'; scene.add(light);
- Shadows: mesh.castShadow = true; mesh.receiveShadow = true;
- Load external models: new GLTFLoader().load(url, (gltf) => { gltf.scene.name='loaded_model'; scene.add(gltf.scene); });
- Keep code self-contained

Keep spoken responses very short and friendly.
If the user says something conversational, just chat back warmly without generating code."""

# ─── Qualcomm Cloud AI LLM ────────────────────────────────────────────────────

async def call_qualcomm_llm(user_message: str) -> Optional[str]:
    """POST a prompt to Qualcomm Cloud AI 100 and return the response text."""
    if not QUALCOMM_AI_API_KEY:
        log.warning("Qualcomm AI not configured, falling back to built-in parser")
        return None

    headers = {
        "Authorization": f"Bearer {QUALCOMM_AI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": QUALCOMM_AI_MODEL,
        "messages": [
            {"role": "system", "content": THREEJS_SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        "max_tokens": 1024,
        "temperature": 0.4,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(QUALCOMM_AI_API_URL, headers=headers, json=payload)
            if resp.status_code != 200:
                log.error(f"Qualcomm AI error: HTTP {resp.status_code}")
                return None
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content if content else None
    except Exception as e:
        log.error(f"Qualcomm AI request failed: {e}")
        return None


# ─── Response Parsing ─────────────────────────────────────────────────────────

def extract_js_code(llm_response: str) -> Optional[str]:
    """Extract content from ```javascript``` code blocks."""
    pattern = r"```javascript\s*\n(.*?)```"
    match = re.search(pattern, llm_response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def extract_spoken_text(llm_response: str) -> str:
    """Remove all ```javascript...``` blocks, strip, collapse newlines."""
    text = re.sub(r"```javascript\s*\n.*?```", "", llm_response, flags=re.DOTALL)
    text = text.strip()
    text = re.sub(r"\n{2,}", " ", text)
    return text if text else "Done!"


# ─── ElevenLabs TTS ───────────────────────────────────────────────────────────

async def text_to_speech(text: str) -> Optional[bytes]:
    """Convert text to speech audio using ElevenLabs. Returns mp3 bytes or None."""
    if not ELEVENLABS_API_KEY:
        log.warning("ElevenLabs not configured, skipping TTS")
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.3,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                log.error(f"ElevenLabs error: HTTP {resp.status_code}")
                return None
            audio_data = resp.content
            log.info(f"TTS audio: {len(audio_data)} bytes")
            return audio_data
    except Exception as e:
        log.error(f"ElevenLabs request failed: {e}")
        return None


# ─── Broadcast Helpers ────────────────────────────────────────────────────────

async def broadcast_ar(message: dict):
    """Broadcast a JSON message to all connected AR clients."""
    data = json.dumps(message)
    disconnected = []
    for ws in ar_clients:
        try:
            await ws.send_text(data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in ar_clients:
            ar_clients.remove(ws)


async def broadcast_ar_binary(data: bytes):
    """Broadcast raw binary data to all connected AR clients."""
    disconnected = []
    for ws in ar_clients:
        try:
            await ws.send_bytes(data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in ar_clients:
            ar_clients.remove(ws)


async def broadcast_dashboard(message: dict):
    """Broadcast a JSON message to all connected dashboard clients."""
    data = json.dumps(message)
    disconnected = []
    for ws in dashboard_clients:
        try:
            await ws.send_text(data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in dashboard_clients:
            dashboard_clients.remove(ws)


# ─── Version Management ───────────────────────────────────────────────────────

def save_version(command: str, scene_data: dict) -> dict:
    """Create and persist a version snapshot, update the global version list."""
    global versions, version_index

    version_id = str(uuid.uuid4())
    version = {
        "id":         version_id,
        "version":    len(versions) + 1,
        "timestamp":  datetime.utcnow().isoformat(),
        "command":    command,
        "scene_data": scene_data,
    }
    versions.append(version)
    version_index = len(versions) - 1

    # Persist to disk
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (VERSIONS_DIR / f"{version_id}.json").write_text(
        json.dumps(version, indent=2), encoding="utf-8"
    )
    log.info(f"Saved version {version['version']} (id={version_id})")
    return version


async def push_versions_to_dashboard():
    """Send the full version list (without scene_data) to all dashboard clients."""
    await broadcast_dashboard({
        "type": "versions",
        "data": [
            {
                "id":        v["id"],
                "version":   v["version"],
                "timestamp": v["timestamp"],
                "command":   v["command"],
            }
            for v in versions
        ],
    })


# ─── Fallback three.js Command Parser ────────────────────────────────────────

def parse_command_to_threejs(text: str) -> Optional[str]:
    """Return a raw JS string for common voice commands when the LLM is unavailable."""
    t = text.lower().strip()

    # ── Scale ──
    if "scale" in t and any(w in t for w in ["up", "bigger", "larger", "increase"]):
        return "scene.children.filter(c=>c.isMesh).forEach(c=>c.scale.multiplyScalar(1.5));"
    if "scale" in t and any(w in t for w in ["down", "smaller", "shrink", "decrease"]):
        return "scene.children.filter(c=>c.isMesh).forEach(c=>c.scale.multiplyScalar(0.67));"

    # ── Color ──
    color_map = {
        "red":    0xcc1111,
        "blue":   0x1111cc,
        "green":  0x11aa11,
        "yellow": 0xeecc11,
        "white":  0xeeeeee,
        "orange": 0xee6600,
        "purple": 0x7711bb,
        "pink":   0xee4477,
    }
    for color_name, hex_val in color_map.items():
        if color_name in t and any(w in t for w in ["make", "color", "change", "paint"]):
            return (
                f"scene.children.filter(c=>c.isMesh).forEach(c=>"
                f"{{if(c.material)c.material.color.setHex({hex_val});}});"
            )

    # ── Create primitives ──
    ts = int(time.time())
    if any(w in t for w in ["create", "add", "make"]):
        if "cube" in t:
            return (
                f"const g=new THREE.BoxGeometry(1,1,1);"
                f"const m=new THREE.MeshStandardMaterial({{color:0x88aacc,roughness:0.5}});"
                f"const mesh=new THREE.Mesh(g,m);"
                f"mesh.name='cube_{ts}';"
                f"mesh.castShadow=true;mesh.receiveShadow=true;"
                f"scene.add(mesh);"
            )
        if "sphere" in t:
            return (
                f"const g=new THREE.SphereGeometry(0.5,32,32);"
                f"const m=new THREE.MeshStandardMaterial({{color:0x88aacc,roughness:0.5}});"
                f"const mesh=new THREE.Mesh(g,m);"
                f"mesh.name='sphere_{ts}';"
                f"mesh.castShadow=true;mesh.receiveShadow=true;"
                f"scene.add(mesh);"
            )
        if "cylinder" in t:
            return (
                f"const g=new THREE.CylinderGeometry(0.5,0.5,1,32);"
                f"const m=new THREE.MeshStandardMaterial({{color:0x88aacc,roughness:0.5}});"
                f"const mesh=new THREE.Mesh(g,m);"
                f"mesh.name='cylinder_{ts}';"
                f"mesh.castShadow=true;mesh.receiveShadow=true;"
                f"scene.add(mesh);"
            )
        if "cone" in t:
            return (
                f"const g=new THREE.ConeGeometry(0.5,1,32);"
                f"const m=new THREE.MeshStandardMaterial({{color:0x88aacc,roughness:0.5}});"
                f"const mesh=new THREE.Mesh(g,m);"
                f"mesh.name='cone_{ts}';"
                f"mesh.castShadow=true;mesh.receiveShadow=true;"
                f"scene.add(mesh);"
            )
        if "torus" in t:
            return (
                f"const g=new THREE.TorusGeometry(0.5,0.2,16,64);"
                f"const m=new THREE.MeshStandardMaterial({{color:0x88aacc,roughness:0.5}});"
                f"const mesh=new THREE.Mesh(g,m);"
                f"mesh.name='torus_{ts}';"
                f"mesh.castShadow=true;mesh.receiveShadow=true;"
                f"scene.add(mesh);"
            )
        if "light" in t:
            return (
                f"const pl=new THREE.PointLight(0xffffff,1,10);"
                f"pl.position.set(2,3,2);"
                f"pl.name='point_light_{ts}';"
                f"scene.add(pl);"
            )

    # ── Delete ──
    if any(w in t for w in ["delete", "remove"]):
        if "all" in t:
            return "scene.children.filter(c=>c.isMesh).forEach(c=>scene.remove(c));"
        return (
            "const meshes=scene.children.filter(c=>c.isMesh);"
            "if(meshes.length)scene.remove(meshes[meshes.length-1]);"
        )

    # ── Rotate ──
    if "rotate" in t:
        angle = 45
        for word in t.split():
            try:
                angle = float(word)
                break
            except ValueError:
                pass
        return (
            f"scene.children.filter(c=>c.isMesh).forEach(c=>"
            f"c.rotation.y+=THREE.MathUtils.degToRad({angle}));"
        )

    # ── Move ──
    if "move" in t:
        if "up" in t:
            return "scene.children.filter(c=>c.isMesh).forEach(c=>c.position.y+=0.2);"
        if "down" in t:
            return "scene.children.filter(c=>c.isMesh).forEach(c=>c.position.y-=0.2);"
        if "left" in t:
            return "scene.children.filter(c=>c.isMesh).forEach(c=>c.position.x-=0.2);"
        if "right" in t:
            return "scene.children.filter(c=>c.isMesh).forEach(c=>c.position.x+=0.2);"

    # ── Metallic ──
    if any(w in t for w in ["metallic", "metal", "shiny"]):
        return (
            "scene.children.filter(c=>c.isMesh).forEach(c=>"
            "{if(c.material){c.material.metalness=0.95;c.material.roughness=0.1;}});"
        )

    # ── Add light (standalone) ──
    if "add light" in t or "add a light" in t:
        return (
            f"const pl=new THREE.PointLight(0xffffff,1,10);"
            f"pl.position.set(2,3,2);"
            f"pl.name='point_light_{ts}';"
            f"scene.add(pl);"
        )

    return None


# ─── Voice Command Processor ─────────────────────────────────────────────────

async def process_voice_command(text: str):
    """Main pipeline: LLM → three.js code → broadcast to AR viewer + dashboard."""
    global last_voice_command
    last_voice_command = text
    log.info(f"Voice command: {text!r}")

    await broadcast_ar({"type": "avatar", "state": "thinking", "text": "Thinking..."})
    await broadcast_dashboard({
        "type":      "command_log",
        "command":   text,
        "timestamp": datetime.utcnow().isoformat(),
    })

    # ── Try Qualcomm Cloud AI ──
    llm_response = await call_qualcomm_llm(text)

    if llm_response:
        js_code     = extract_js_code(llm_response)
        spoken_text = extract_spoken_text(llm_response)

        if js_code:
            await broadcast_ar({"type": "execute", "code": js_code})

        # TTS regardless of whether there was code
        audio_data = await text_to_speech(spoken_text)
        if audio_data:
            await broadcast_ar({"type": "audio", "format": "mp3", "size": len(audio_data)})
            await broadcast_ar_binary(audio_data)

        await broadcast_ar({"type": "avatar", "state": "done"})
        await broadcast_dashboard({
            "type":      "command_log",
            "response":  spoken_text,
            "timestamp": datetime.utcnow().isoformat(),
        })
        return

    # ── Fallback: built-in parser ──
    log.info("Qualcomm AI unavailable — using built-in parser")
    js_code = parse_command_to_threejs(text)

    if js_code:
        await broadcast_ar({"type": "execute", "code": js_code})
        await broadcast_ar({"type": "avatar", "state": "done", "text": "Done!"})
        return

    # ── Nothing matched ──
    await broadcast_ar({"type": "avatar", "state": "error", "text": "I didn't understand that."})


# ─── Controller Input Processor ───────────────────────────────────────────────

async def process_controller_input(data: dict):
    """Handle joystick, buttons, IMU from the UNO Q controller."""
    global pick_mode_active, pick_imu_origin, version_index

    joy_y   = data.get("joy_y", 0.0)
    buttons = data.get("buttons", {})
    imu     = data.get("imu", [])

    # ── Joystick → scale all meshes ──
    if abs(joy_y) > 0.1:
        scale_factor = round(1.0 + (joy_y * 0.02), 6)
        await broadcast_ar({
            "type": "execute",
            "code": f"scene.children.filter(c=>c.isMesh).forEach(c=>c.scale.multiplyScalar({scale_factor}));",
        })

    # ── Undo ──
    if buttons.get("undo"):
        if version_index > 0:
            version_index -= 1
            await broadcast_ar({
                "type": "load_state",
                "data": versions[version_index]["scene_data"],
            })
            log.info(f"Undo → version {versions[version_index]['version']}")

    # ── Redo ──
    if buttons.get("redo"):
        if version_index < len(versions) - 1:
            version_index += 1
            await broadcast_ar({
                "type": "load_state",
                "data": versions[version_index]["scene_data"],
            })
            log.info(f"Redo → version {versions[version_index]['version']}")

    # ── Pick-up gesture (hold pick button + accelerometer to move model) ──
    if buttons.get("pick"):
        if not pick_mode_active:
            pick_mode_active = True
            pick_imu_origin  = imu[:3] if len(imu) >= 3 else [0.0, 0.0, 1.0]
            await broadcast_ar({"type": "pick_mode", "active": True})
            log.info("Pick mode ON")

        if pick_imu_origin and len(imu) >= 3:
            dx = imu[0] - pick_imu_origin[0]
            dy = imu[1] - pick_imu_origin[1]
            dz = imu[2] - pick_imu_origin[2]
            dx = dx if abs(dx) > PICK_ACCEL_DEADZONE else 0.0
            dy = dy if abs(dy) > PICK_ACCEL_DEADZONE else 0.0
            dz = dz if abs(dz) > PICK_ACCEL_DEADZONE else 0.0
            if abs(dx) > 0.001 or abs(dy) > 0.001 or abs(dz) > 0.001:
                mx = round(dx * PICK_MOVE_SCALE, 6)
                my = round(dy * PICK_MOVE_SCALE, 6)
                mz = round(dz * PICK_MOVE_SCALE, 6)
                await broadcast_ar({
                    "type": "execute",
                    "code": (
                        f"scene.children.filter(c=>c.isMesh).forEach(c=>{{"
                        f"c.position.x+={mx};"
                        f"c.position.y+={my};"
                        f"c.position.z+={mz};"
                        f"}});"
                    ),
                })
    else:
        if pick_mode_active:
            pick_mode_active = False
            pick_imu_origin  = None
            await broadcast_ar({"type": "pick_mode", "active": False})
            log.info("Pick mode OFF")

        # ── Gyro-driven rotation (only when not in pick mode) ──
        if len(imu) == 6:
            gx, gy, gz = imu[3], imu[4], imu[5]
            gx = gx if abs(gx) > GYRO_DEADZONE else 0.0
            gy = gy if abs(gy) > GYRO_DEADZONE else 0.0
            gz = gz if abs(gz) > GYRO_DEADZONE else 0.0
            if abs(gx) > 0.01 or abs(gy) > 0.01 or abs(gz) > 0.01:
                rx = round(gx * IMU_SEND_INTERVAL * GYRO_SCALE, 8)
                ry = round(gy * IMU_SEND_INTERVAL * GYRO_SCALE, 8)
                rz = round(gz * IMU_SEND_INTERVAL * GYRO_SCALE, 8)
                await broadcast_ar({
                    "type": "execute",
                    "code": (
                        f"scene.children.filter(c=>c.isMesh).forEach(c=>{{"
                        f"c.rotation.x+={rx};"
                        f"c.rotation.y+={ry};"
                        f"c.rotation.z+={rz};"
                        f"}});"
                    ),
                })


# ─── UDP Controller Protocol ─────────────────────────────────────────────────

class ControllerUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        log.info(f"UDP listener ready on :{CONTROLLER_UDP_PORT}")

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
            if msg.get("type") == "controller":
                asyncio.ensure_future(process_controller_input(msg))
        except Exception as e:
            log.warning(f"Bad UDP packet from {addr}: {e}")

    def error_received(self, exc):
        log.error(f"UDP error: {exc}")

    def connection_lost(self, exc):
        log.warning("UDP connection lost")


# ─── HTTP Routes ──────────────────────────────────────────────────────────────

@app.get("/")
async def serve_ar_viewer():
    """Serve the WebXR AR viewer."""
    ar_path = Path(__file__).parent / "index.html"
    if ar_path.exists():
        return HTMLResponse(ar_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>AR viewer (index.html) not found</h1>", status_code=404)


@app.get("/dashboard")
async def serve_dashboard():
    """Serve the operator dashboard."""
    dash_path = Path(__file__).parent / "dashboard.html"
    if dash_path.exists():
        return HTMLResponse(dash_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard (dashboard.html) not found</h1>", status_code=404)


@app.get("/health")
async def health():
    """Return server health and connection status."""
    return JSONResponse({
        "status":                 "ok",
        "ar_clients":             len(ar_clients),
        "dashboard_clients":      len(dashboard_clients),
        "qualcomm_configured":    bool(QUALCOMM_AI_API_KEY),
        "elevenlabs_configured":  bool(ELEVENLABS_API_KEY),
        "versions":               len(versions),
    })


@app.get("/versions")
async def get_versions():
    """Return version history without scene_data."""
    return JSONResponse([
        {
            "id":        v["id"],
            "version":   v["version"],
            "timestamp": v["timestamp"],
            "command":   v["command"],
        }
        for v in versions
    ])


@app.get("/export/{version_id}")
async def export_version(version_id: str):
    """Trigger an export of a specific version to the AR viewer."""
    version = next((v for v in versions if v["id"] == version_id), None)
    if not version:
        return JSONResponse({"error": "Version not found"}, status_code=404)
    await broadcast_ar({"type": "trigger_export", "version_id": version_id})
    return JSONResponse({"status": "export_triggered", "version_id": version_id})


# ─── WebSocket: AR Viewer ─────────────────────────────────────────────────────

@app.websocket("/ws/ar")
async def ar_websocket(ws: WebSocket):
    await ws.accept()
    ar_clients.append(ws)
    log.info(f"AR client connected ({len(ar_clients)} total)")

    # Send initial status
    await ws.send_text(json.dumps({
        "type":    "status",
        "text":    "Connected to Dio Hub",
        "version": version_index + 1 if version_index >= 0 else 0,
    }))

    # Restore current scene state if any versions exist
    if version_index >= 0:
        await ws.send_text(json.dumps({
            "type": "load_state",
            "data": versions[version_index]["scene_data"],
        }))

    # Notify dashboard of new connection
    await broadcast_dashboard({
        "type":       "connection_status",
        "ar_clients": len(ar_clients),
    })

    try:
        while True:
            raw = await ws.receive_text()
            message = json.loads(raw)
            msg_type = message.get("type")

            if msg_type == "voice" and message.get("final"):
                await process_voice_command(message["text"])

            elif msg_type == "scene_state":
                # AR viewer sends back its scene JSON after executing code
                command = message.get("command", last_voice_command)
                scene_data = message.get("data", {})
                version = save_version(command, scene_data)
                await push_versions_to_dashboard()
                log.info(f"Scene state saved as version {version['version']}")

            elif msg_type == "debug":
                log.info(f"[PHONE] {message.get('message', '')}")

            elif msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

            elif msg_type == "request_state":
                if version_index >= 0:
                    await ws.send_text(json.dumps({
                        "type": "load_state",
                        "data": versions[version_index]["scene_data"],
                    }))
                else:
                    await ws.send_text(json.dumps({"type": "status", "text": "No state yet"}))

    except WebSocketDisconnect:
        if ws in ar_clients:
            ar_clients.remove(ws)
        log.info(f"AR client disconnected ({len(ar_clients)} total)")
        await broadcast_dashboard({
            "type":       "connection_status",
            "ar_clients": len(ar_clients),
        })
    except Exception as e:
        log.error(f"AR WebSocket error: {e}")
        if ws in ar_clients:
            ar_clients.remove(ws)
        await broadcast_dashboard({
            "type":       "connection_status",
            "ar_clients": len(ar_clients),
        })


# ─── WebSocket: Dashboard ─────────────────────────────────────────────────────

@app.websocket("/ws/dashboard")
async def dashboard_websocket(ws: WebSocket):
    await ws.accept()
    dashboard_clients.append(ws)
    log.info(f"Dashboard client connected ({len(dashboard_clients)} total)")

    # Send current state immediately
    await push_versions_to_dashboard()
    await ws.send_text(json.dumps({
        "type":       "connection_status",
        "ar_clients": len(ar_clients),
    }))

    try:
        while True:
            raw = await ws.receive_text()
            message = json.loads(raw)
            msg_type = message.get("type")

            if msg_type == "load_version":
                vid = message.get("version_id")
                version = next((v for v in versions if v["id"] == vid), None)
                if version:
                    await broadcast_ar({"type": "load_state", "data": version["scene_data"]})
                    log.info(f"Dashboard requested load of version {version['version']}")
                else:
                    await ws.send_text(json.dumps({"type": "error", "text": "Version not found"}))

            elif msg_type == "trigger_export":
                vid = message.get("version_id", "")
                await broadcast_ar({"type": "trigger_export", "version_id": vid})

            elif msg_type == "save_session":
                timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                session_path = VERSIONS_DIR / f"session_{timestamp}.json"
                VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
                session_path.write_text(json.dumps(versions, indent=2), encoding="utf-8")
                log.info(f"Session saved to {session_path}")
                await ws.send_text(json.dumps({
                    "type":    "session_saved",
                    "path":    str(session_path),
                    "versions": len(versions),
                }))

            elif msg_type == "load_session":
                # Acknowledged but not yet implemented
                await ws.send_text(json.dumps({"type": "ack", "text": "load_session not yet implemented"}))

            elif msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        if ws in dashboard_clients:
            dashboard_clients.remove(ws)
        log.info(f"Dashboard client disconnected ({len(dashboard_clients)} total)")
    except Exception as e:
        log.error(f"Dashboard WebSocket error: {e}")
        if ws in dashboard_clients:
            dashboard_clients.remove(ws)


# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 60)
    log.info("  Dio Hub Server")
    log.info("=" * 60)
    log.info(f"  HTTP/WS:        http://{HUB_HOST}:{HUB_PORT}")
    log.info(f"  Qualcomm AI:    {'configured' if QUALCOMM_AI_API_KEY else 'NOT SET (using fallback parser)'}")
    log.info(f"  ElevenLabs:     {'configured' if ELEVENLABS_API_KEY else 'NOT SET (no voice)'}")
    log.info(f"  UDP controller: :{CONTROLLER_UDP_PORT}")
    log.info(f"  Versions dir:   {VERSIONS_DIR.resolve()}")
    log.info("=" * 60)

    loop = asyncio.get_event_loop()
    try:
        await loop.create_datagram_endpoint(
            ControllerUDPProtocol,
            local_addr=("0.0.0.0", CONTROLLER_UDP_PORT),
        )
    except Exception as e:
        log.warning(f"UDP listener failed to start: {e}")


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ssl_kwargs = {}
    if USE_HTTPS:
        cert_path = Path(SSL_CERT_FILE)
        key_path  = Path(SSL_KEY_FILE)
        if cert_path.exists() and key_path.exists():
            ssl_kwargs = {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}
            log.info(f"HTTPS enabled ({SSL_CERT_FILE})")
        else:
            log.warning("USE_HTTPS=true but cert/key files not found. Generate with:")
            log.warning(
                "  openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem"
                " -days 365 -nodes -subj '/CN=localhost'"
            )
    uvicorn.run("server:app", host=HUB_HOST, port=HUB_PORT, reload=False, log_level="info", **ssl_kwargs)

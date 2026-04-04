"""
Dio Hub Server — Central orchestrator for the Dio system.

Connects:
  - Qualcomm Cloud AI 100 via REST API (LLM inference)
  - ElevenLabs via REST API (text-to-speech for avatar)
  - Samsung S25 AR viewer via WebSocket (/ws/ar)
  - Blender addon via TCP socket (localhost:9876)
  - UNO Q controller via UDP (:9877)

Serves:
  - AR viewer HTML at /

Run: python server.py
"""

import asyncio
import json
import logging
import os
import re
import socket
import time
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

# ─── Config ───────────────────────────────────────────────────────────

# Qualcomm Cloud AI
QUALCOMM_AI_API_URL = os.getenv("QUALCOMM_AI_API_URL", "")
QUALCOMM_AI_API_KEY = os.getenv("QUALCOMM_AI_API_KEY", "")
QUALCOMM_AI_MODEL = os.getenv("QUALCOMM_AI_MODEL", "")

# ElevenLabs
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")  # "Adam" default

# Blender
BLENDER_HOST = os.getenv("BLENDER_HOST", "127.0.0.1")
BLENDER_PORT = int(os.getenv("BLENDER_PORT", "9876"))

# Hub
HUB_HOST = os.getenv("HUB_HOST", "0.0.0.0")
HUB_PORT = int(os.getenv("HUB_PORT", "8080"))

# Controller
CONTROLLER_UDP_PORT = int(os.getenv("CONTROLLER_UDP_PORT", "9877"))

# Export
GLB_EXPORT_PATH = os.getenv("GLB_EXPORT_PATH", "C:/tmp/dio_scene.glb")

EXPORT_DEBOUNCE_SEC = 0.3

# HTTPS
USE_HTTPS = os.getenv("USE_HTTPS", "false").lower() == "true"
SSL_CERT_FILE = os.getenv("SSL_CERT_FILE", "cert.pem")
SSL_KEY_FILE = os.getenv("SSL_KEY_FILE", "key.pem")

# IMU / controller
IMU_SEND_INTERVAL = 0.02   # 50 Hz firmware rate
GYRO_DEADZONE = 2.0        # deg/s — ignore noise below this
GYRO_SCALE = 0.0008        # deg/s → Blender radians per tick

# ─── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dio-hub")

# ─── App ──────────────────────────────────────────────────────────────

app = FastAPI(title="Dio Hub")
ar_clients: list[WebSocket] = []
last_export_time: float = 0
pick_mode_active: bool = False
pick_imu_origin: list = None
PICK_ACCEL_DEADZONE = 0.05   # g
PICK_MOVE_SCALE = 0.04       # g delta → Blender units

# ─── System Prompt for the LLM ───────────────────────────────────────

BLENDER_SYSTEM_PROMPT = """You are Dio, a friendly AI assistant that helps users create and modify 3D models in Blender.

When the user gives you a command about a 3D model, you must:
1. Generate valid Blender Python (bpy) code that executes the command
2. Wrap the code in ```python``` code blocks
3. Give a brief, friendly 1-sentence response describing what you did

Rules for your bpy code:
- Always start with `import bpy`
- Use `bpy.data.objects` to reference objects by name
- Use `bpy.ops.mesh.primitive_*_add()` for creating objects
- Use `bpy.context.active_object` right after creating to reference new objects
- Set materials via Principled BSDF nodes
- For transforms: set .location, .rotation_euler, .scale directly
- Always use radians for rotation (import math)
- If no specific object is named, operate on the first mesh object found
- Keep code self-contained and idempotent where possible

Keep your spoken responses very short and friendly — you're a companion, not a lecturer.
If the user says something conversational (not a Blender command), just chat back warmly without generating code."""


# ─── Blender Connection ──────────────────────────────────────────────

class BlenderBridge:
    def __init__(self, host=BLENDER_HOST, port=BLENDER_PORT):
        self.host = host
        self.port = port

    def send_command(self, cmd_type, params=None, timeout=10.0):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((self.host, self.port))
            command = {"type": cmd_type}
            if params:
                command["params"] = params
            sock.sendall(json.dumps(command).encode("utf-8"))
            response = b""
            while True:
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    response += chunk
                except socket.timeout:
                    break
            sock.close()
            return json.loads(response.decode("utf-8")) if response else {"status": "error", "message": "Empty response"}
        except ConnectionRefusedError:
            return {"status": "error", "message": "Blender not connected"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def execute_code(self, code):
        return self.send_command("execute_code", {"code": code})

    def get_scene_info(self):
        return self.send_command("get_scene_info")

    def export_glb(self, filepath=GLB_EXPORT_PATH):
        code = f"""
import bpy, os
filepath = r"{filepath}"
os.makedirs(os.path.dirname(filepath), exist_ok=True)
bpy.ops.export_scene.gltf(
    filepath=filepath, export_format='GLB',
    use_selection=False, export_apply=True,
    export_materials='EXPORT', export_colors=True,
    export_cameras=False, export_lights=False, export_yup=True,
)
"""
        result = self.execute_code(code)
        return result.get("status") != "error"


blender = BlenderBridge()


# ─── Qualcomm Cloud AI LLM ───────────────────────────────────────────

async def call_qualcomm_llm(user_message: str) -> str:
    """Send a prompt to Qualcomm Cloud AI 100 and return the response."""
    if not QUALCOMM_AI_API_URL or not QUALCOMM_AI_API_KEY:
        log.warning("Qualcomm AI not configured, falling back to built-in parser")
        return None

    headers = {
        "Authorization": f"Bearer {QUALCOMM_AI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": QUALCOMM_AI_MODEL,
        "messages": [
            {"role": "system", "content": BLENDER_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
        "stream": False,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(QUALCOMM_AI_API_URL, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.error(f"Qualcomm AI error: {resp.status}")
                    return None
                data = await resp.json()
                # Standard OpenAI-compatible response format
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return content
    except Exception as e:
        log.error(f"Qualcomm AI request failed: {e}")
        return None


def extract_bpy_code(llm_response: str) -> Optional[str]:
    """Extract Python code block from LLM response."""
    # Look for ```python ... ``` blocks
    pattern = r"```python\s*\n(.*?)```"
    match = re.search(pattern, llm_response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def extract_spoken_text(llm_response: str) -> str:
    """Extract the conversational text (non-code) from LLM response."""
    # Remove code blocks
    text = re.sub(r"```python\s*\n.*?```", "", llm_response, flags=re.DOTALL)
    text = text.strip()
    # Clean up
    text = re.sub(r"\n{2,}", " ", text)
    return text if text else "Done!"


# ─── ElevenLabs TTS ──────────────────────────────────────────────────

async def text_to_speech(text: str) -> Optional[bytes]:
    """Convert text to speech audio using ElevenLabs."""
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
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.error(f"ElevenLabs error: {resp.status}")
                    return None
                audio_data = await resp.read()
                log.info(f"TTS audio: {len(audio_data)} bytes")
                return audio_data
    except Exception as e:
        log.error(f"ElevenLabs request failed: {e}")
        return None


# ─── Broadcast ────────────────────────────────────────────────────────

async def broadcast_json(message: dict):
    data = json.dumps(message)
    disconnected = []
    for ws in ar_clients:
        try:
            await ws.send_text(data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        ar_clients.remove(ws)


async def broadcast_binary(data: bytes):
    disconnected = []
    for ws in ar_clients:
        try:
            await ws.send_bytes(data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        ar_clients.remove(ws)


# ─── Export & Push ────────────────────────────────────────────────────

async def export_and_push_model():
    global last_export_time
    now = time.time()
    if now - last_export_time < EXPORT_DEBOUNCE_SEC:
        return
    last_export_time = now

    log.info("Exporting glTF from Blender...")
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, blender.export_glb)

    if not success:
        log.error("glTF export failed")
        return

    await asyncio.sleep(0.1)
    filepath = Path(GLB_EXPORT_PATH)
    if not filepath.exists():
        return

    glb_data = filepath.read_bytes()
    log.info(f"Pushing glTF ({len(glb_data)} bytes) to {len(ar_clients)} client(s)")
    await broadcast_json({"type": "model", "format": "glb", "size": len(glb_data)})
    await broadcast_binary(glb_data)


# ─── Process Voice Command ───────────────────────────────────────────

async def process_voice_command(text: str):
    log.info(f"Voice command: {text}")
    await broadcast_json({"type": "avatar", "state": "thinking", "text": f"Thinking..."})

    # ─── Try Qualcomm Cloud AI first ───
    llm_response = await call_qualcomm_llm(text)

    if llm_response:
        bpy_code = extract_bpy_code(llm_response)
        spoken_text = extract_spoken_text(llm_response)

        if bpy_code:
            # Execute in Blender
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, blender.execute_code, bpy_code)
            log.info(f"Blender result: {result}")

            if result.get("status") != "error":
                await broadcast_json({"type": "command_result", "success": True, "description": spoken_text})
                await export_and_push_model()
            else:
                spoken_text = "Hmm, that didn't work. Let me try again."
                await broadcast_json({"type": "avatar", "state": "error", "text": spoken_text})
        else:
            # Conversational response (no code)
            await broadcast_json({"type": "command_result", "success": True, "description": spoken_text})

        # ─── Generate voice with ElevenLabs ───
        audio_data = await text_to_speech(spoken_text)
        if audio_data:
            await broadcast_json({"type": "audio", "format": "mp3", "size": len(audio_data)})
            await broadcast_binary(audio_data)

        await broadcast_json({"type": "avatar", "state": "done"})
        return

    # ─── Fallback: built-in command parser (no cloud needed) ───
    log.info("Using built-in parser (Qualcomm AI not available)")
    bpy_code = parse_command_to_bpy(text)

    if bpy_code:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, blender.execute_code, bpy_code)
        if result.get("status") != "error":
            await broadcast_json({"type": "command_result", "success": True, "description": f"Done: {text}"})
            await export_and_push_model()
            await broadcast_json({"type": "avatar", "state": "done", "text": "Done!"})
        else:
            await broadcast_json({"type": "avatar", "state": "error", "text": "Something went wrong."})
    else:
        await broadcast_json({"type": "avatar", "state": "error", "text": "I didn't understand that."})


def parse_command_to_bpy(text: str) -> Optional[str]:
    """Fallback command parser when Qualcomm Cloud AI is not available."""
    text = text.lower().strip()

    if "scale" in text and any(w in text for w in ["up", "bigger", "larger", "increase"]):
        return "import bpy\nfor o in bpy.data.objects:\n    if o.type=='MESH': o.scale*=1.5; break"
    if "scale" in text and any(w in text for w in ["down", "smaller", "shrink", "decrease"]):
        return "import bpy\nfor o in bpy.data.objects:\n    if o.type=='MESH': o.scale*=0.67; break"

    colors = {"red":"(0.8,0.1,0.1,1)","blue":"(0.1,0.1,0.8,1)","green":"(0.1,0.6,0.1,1)",
              "yellow":"(0.9,0.8,0.1,1)","white":"(0.9,0.9,0.9,1)","orange":"(0.9,0.4,0.05,1)",
              "purple":"(0.5,0.1,0.7,1)","pink":"(0.9,0.3,0.5,1)"}
    for cn, rgba in colors.items():
        if cn in text and any(w in text for w in ["make","color","change","paint"]):
            return f"""import bpy
for o in bpy.data.objects:
    if o.type=='MESH':
        m=bpy.data.materials.new('{cn}')
        m.use_nodes=True
        m.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value={rgba}
        if o.data.materials: o.data.materials[0]=m
        else: o.data.materials.append(m)
        break"""

    prims = {"cube":"cube_add","sphere":"uv_sphere_add","cylinder":"cylinder_add",
             "cone":"cone_add","torus":"torus_add","monkey":"monkey_add"}
    if any(w in text for w in ["create","add","make"]):
        for pn, fn in prims.items():
            if pn in text:
                return f"import bpy\nbpy.ops.mesh.primitive_{fn}(location=(0,0,0))\nbpy.context.active_object.name='Dio_{pn}'"

    if any(w in text for w in ["delete","remove"]):
        if "all" in text:
            return "import bpy\nbpy.ops.object.select_all(action='SELECT')\nbpy.ops.object.delete()"
        return "import bpy\nif bpy.context.active_object: bpy.data.objects.remove(bpy.context.active_object,do_unlink=True)"

    if "undo" in text: return "import bpy\nbpy.ops.ed.undo()"
    if "redo" in text: return "import bpy\nbpy.ops.ed.redo()"

    if "rotate" in text:
        angle = 45
        for w in text.split():
            try: angle = float(w)
            except: pass
        return f"import bpy,math\nfor o in bpy.data.objects:\n    if o.type=='MESH': o.rotation_euler.z+=math.radians({angle}); break"

    if "move" in text:
        axis = "z" if "up" in text else "-z" if "down" in text else "-x" if "left" in text else "x" if "right" in text else None
        if axis:
            sign = -1 if axis.startswith("-") else 1
            ax = axis[-1]
            return f"import bpy\nfor o in bpy.data.objects:\n    if o.type=='MESH': o.location.{ax}+={sign}; break"

    if any(w in text for w in ["metallic","metal","shiny"]):
        return """import bpy
for o in bpy.data.objects:
    if o.type=='MESH' and o.data.materials:
        b=o.data.materials[0].node_tree.nodes.get('Principled BSDF')
        if b: b.inputs['Metallic'].default_value=0.95; b.inputs['Roughness'].default_value=0.1
        break"""

    if "smooth" in text:
        return "import bpy\nfor o in bpy.data.objects:\n    if o.type=='MESH': bpy.context.view_layer.objects.active=o; bpy.ops.object.shade_smooth(); break"

    if any(w in text for w in ["subdivide","subdivision","more detail"]):
        return "import bpy\nfor o in bpy.data.objects:\n    if o.type=='MESH': o.modifiers.new('Subsurf','SUBSURF').levels=2; break"

    return None


# ─── Controller Input ─────────────────────────────────────────────────

async def process_controller_input(data: dict):
    global pick_mode_active, pick_imu_origin

    joy_y = data.get("joy_y", 0.0)
    buttons = data.get("buttons", {})
    imu = data.get("imu", [])

    if abs(joy_y) > 0.1:
        scale_factor = 1.0 + (joy_y * 0.02)
        code = f"import bpy\nfor o in bpy.data.objects:\n    if o.type=='MESH': o.scale*={scale_factor}; break"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, blender.execute_code, code)
        await export_and_push_model()

    if buttons.get("undo"):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, blender.execute_code, "import bpy\nbpy.ops.ed.undo()")
        await export_and_push_model()

    if buttons.get("redo"):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, blender.execute_code, "import bpy\nbpy.ops.ed.redo()")
        await export_and_push_model()

    # ─── Pick-up gesture (hold pick button + tilt to move model) ──────
    if buttons.get("pick"):
        if not pick_mode_active:
            pick_mode_active = True
            pick_imu_origin = imu[:3] if len(imu) >= 3 else [0.0, 0.0, 1.0]
            await broadcast_json({"type": "pick_mode", "active": True})
            log.info("Pick mode ON")
        # Move model based on accelerometer delta from origin
        if pick_imu_origin and len(imu) >= 3:
            dx = imu[0] - pick_imu_origin[0]
            dy = imu[1] - pick_imu_origin[1]
            dz = imu[2] - pick_imu_origin[2]
            dx = dx if abs(dx) > PICK_ACCEL_DEADZONE else 0.0
            dy = dy if abs(dy) > PICK_ACCEL_DEADZONE else 0.0
            dz = dz if abs(dz) > PICK_ACCEL_DEADZONE else 0.0
            if abs(dx) > 0.001 or abs(dy) > 0.001 or abs(dz) > 0.001:
                pick_code = (
                    f"import bpy\n"
                    f"for o in bpy.data.objects:\n"
                    f"    if o.type=='MESH':\n"
                    f"        o.location.x += {dx * PICK_MOVE_SCALE}\n"
                    f"        o.location.y += {dz * PICK_MOVE_SCALE}\n"
                    f"        o.location.z += {dy * PICK_MOVE_SCALE}\n"
                    f"        break"
                )
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, blender.execute_code, pick_code)
                await export_and_push_model()
    else:
        if pick_mode_active:
            pick_mode_active = False
            pick_imu_origin = None
            await broadcast_json({"type": "pick_mode", "active": False})
            log.info("Pick mode OFF")

        # ─── Gyro-driven rotation (only when not in pick mode) ────────
        if len(imu) == 6:
            gx, gy, gz = imu[3], imu[4], imu[5]
            gx = gx if abs(gx) > GYRO_DEADZONE else 0.0
            gy = gy if abs(gy) > GYRO_DEADZONE else 0.0
            gz = gz if abs(gz) > GYRO_DEADZONE else 0.0
            if abs(gx) > 0.01 or abs(gy) > 0.01 or abs(gz) > 0.01:
                rx = gx * IMU_SEND_INTERVAL * GYRO_SCALE
                ry = gy * IMU_SEND_INTERVAL * GYRO_SCALE
                rz = gz * IMU_SEND_INTERVAL * GYRO_SCALE
                imu_code = (
                    f"import bpy\n"
                    f"for o in bpy.data.objects:\n"
                    f"    if o.type=='MESH':\n"
                    f"        o.rotation_euler.x += {rx}\n"
                    f"        o.rotation_euler.y += {ry}\n"
                    f"        o.rotation_euler.z += {rz}\n"
                    f"        break"
                )
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, blender.execute_code, imu_code)
                await export_and_push_model()


class ControllerUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self): self.transport = None
    def connection_made(self, transport): self.transport = transport; log.info(f"UDP ready on :{CONTROLLER_UDP_PORT}")
    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
            if msg.get("type") == "controller":
                asyncio.ensure_future(process_controller_input(msg))
        except Exception as e:
            log.warning(f"Bad UDP from {addr}: {e}")


# ─── HTTP Routes ──────────────────────────────────────────────────────

@app.get("/")
async def serve_ar_viewer():
    ar_path = Path(__file__).parent.parent / "ar" / "index.html"
    if ar_path.exists():
        return HTMLResponse(ar_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>AR viewer not found</h1>")


@app.get("/health")
async def health():
    loop = asyncio.get_event_loop()
    blender_status = await loop.run_in_executor(None, blender.get_scene_info)
    return {
        "status": "ok",
        "ar_clients": len(ar_clients),
        "blender_connected": blender_status.get("status") != "error",
        "qualcomm_ai_configured": bool(QUALCOMM_AI_API_URL),
        "elevenlabs_configured": bool(ELEVENLABS_API_KEY),
    }


@app.get("/model")
async def get_current_model():
    filepath = Path(GLB_EXPORT_PATH)
    if filepath.exists():
        return FileResponse(filepath, media_type="model/gltf-binary", filename="scene.glb")
    return {"error": "No model exported yet"}


# ─── WebSocket ────────────────────────────────────────────────────────

@app.websocket("/ws/ar")
async def ar_websocket(ws: WebSocket):
    await ws.accept()
    ar_clients.append(ws)
    log.info(f"AR client connected ({len(ar_clients)} total)")
    await ws.send_text(json.dumps({"type": "status", "text": "Connected to Dio Hub"}))

    filepath = Path(GLB_EXPORT_PATH)
    if filepath.exists():
        glb_data = filepath.read_bytes()
        await ws.send_text(json.dumps({"type": "model", "format": "glb", "size": len(glb_data)}))
        await ws.send_bytes(glb_data)

    try:
        while True:
            raw = await ws.receive_text()
            message = json.loads(raw)
            if message["type"] == "voice" and message.get("final"):
                await process_voice_command(message["text"])
            elif message["type"] == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif message["type"] == "request_model":
                await export_and_push_model()
    except WebSocketDisconnect:
        ar_clients.remove(ws)
        log.info(f"AR client disconnected ({len(ar_clients)} total)")
    except Exception as e:
        log.error(f"WebSocket error: {e}")
        if ws in ar_clients:
            ar_clients.remove(ws)


# ─── Startup ──────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    log.info("=" * 60)
    log.info("  Dio Hub Server")
    log.info("=" * 60)
    log.info(f"  HTTP/WS:       http://{HUB_HOST}:{HUB_PORT}")
    log.info(f"  Blender:       {BLENDER_HOST}:{BLENDER_PORT}")
    log.info(f"  Qualcomm AI:   {'configured' if QUALCOMM_AI_API_URL else 'NOT SET (using fallback parser)'}")
    log.info(f"  ElevenLabs:    {'configured' if ELEVENLABS_API_KEY else 'NOT SET (no voice)'}")
    log.info(f"  UDP ctrl:      :{CONTROLLER_UDP_PORT}")
    log.info(f"  GLB path:      {GLB_EXPORT_PATH}")
    log.info("=" * 60)

    os.makedirs(os.path.dirname(GLB_EXPORT_PATH) or ".", exist_ok=True)

    loop = asyncio.get_event_loop()
    try:
        await loop.create_datagram_endpoint(ControllerUDPProtocol, local_addr=("0.0.0.0", CONTROLLER_UDP_PORT))
    except Exception as e:
        log.warning(f"UDP listener failed: {e}")


if __name__ == "__main__":
    ssl_kwargs = {}
    if USE_HTTPS:
        cert_path = Path(SSL_CERT_FILE)
        key_path = Path(SSL_KEY_FILE)
        if cert_path.exists() and key_path.exists():
            ssl_kwargs = {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}
            log.info(f"HTTPS enabled ({SSL_CERT_FILE})")
        else:
            log.warning("USE_HTTPS=true but cert/key not found. Generate with:")
            log.warning("  openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'")
    uvicorn.run("server:app", host=HUB_HOST, port=HUB_PORT, reload=False, log_level="info", **ssl_kwargs)

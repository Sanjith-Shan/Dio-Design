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

# Load .env file automatically if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn

# ─── Config ───────────────────────────────────────────────────────────────────

QUALCOMM_AI_API_KEY  = os.getenv("QUALCOMM_AI_API_KEY", "")
QUALCOMM_AI_MODEL    = os.getenv("QUALCOMM_AI_MODEL", "Llama-3.1-8B")
ELEVENLABS_API_KEY   = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID  = os.getenv("ELEVENLABS_VOICE_ID", "hmMWXCj9K7N5mCPcRkfC")
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
current_scene_manifest: list       = []  # [{name, type}] — updated on every scene_state
pending_voice_save: bool           = False  # True only between voice execute and its scene_state ack
conversation_history: List[Dict]   = []  # Last N turns for LLM context continuity
prev_buttons:        dict          = {"pick": False, "undo": False, "redo": False, "joy_btn": False}
controller_connected: bool         = False

# ─── Personality Audio Lines ──────────────────────────────────────────────────
import random

THINKING_LINES = [
    "Right, givin' it a look…",
    "One sec, one sec, I'm on it.",
    "Ah yeah, leave it with me.",
    "Grand, grand — just a moment.",
    "Aye, workin' on it now.",
    "Hold on, I'm thinkin'.",
    "Right so, let me sort that out.",
    "On it. Don't rush me.",
]

DONE_LINES = [
    "There ya go! Easy as that.",
    "Done and dusted — have a look at that.",
    "Boom. Wasn't even hard.",
    "Right, that's sorted. What's next?",
    "There she is! Lovely job if I say so.",
    "Ha! Done already. What else ya got?",
    "Nailed it. As per usual.",
]

ERROR_LINES = [
    "Ah for— that didn't work. Try again, would ya?",
    "Right, that went sideways. My fault. Go again.",
    "Hmm. That's not ideal. Give me another shot.",
    "Ah, something went wrong there. Tell me again.",
]

GREETING_LINES = [
    "Ah, there ya are! Ready when you are.",
    "Dio here — let's build somethin' class.",
    "Right, I'm all ears. What are we makin'?",
]

# Random tangents — project opinions, day-talk, unsolicited commentary.
# Fires ~30% of the time alongside a thinking line to feel like a real conversation.
MUSINGS = [
    # Project opinions
    "I have to say, this is shappin' up to be a deadly looking thing.",
    "You know, most people wouldn't even attempt this in AR. Mad respect.",
    "I think the whole vibe of this project is class. Just sayin'.",
    "Between you and me, I reckon this'll look gas when it's all done.",
    "This is actually one of the more interesting things I've built. No offence to the others.",
    "I'd put this up against anything I've seen. Genuinely.",
    "Right, I'm not just saying this — the scale of this is impressive.",
    "D'ya know what, I think you've got a real eye for this stuff.",
    # Unsolicited design opinions
    "Honestly? I think it could use a bit more depth. Just a thought.",
    "Not sure about that last placement, but I'll keep me mouth shut.",
    "I'd maybe push the whole thing back a bit. But what do I know.",
    "Something about the lighting in here is annoying me. I'll figure it out.",
    # Day-talk / personal
    "I've been in worse scenes, I'll tell ya that much.",
    "Been a busy one. You're like the fourth person to ask me something today.",
    "I was just thinkin' about whether polygons dream. Anyway.",
    "D'ya ever just stare at a mesh and feel something? No? Just me so.",
    "I had a whole plan for today and now look at me. Lovin' it though.",
    "Right, I haven't had a break since this session started. Worth it.",
    # Mild existential / character
    "Sometimes I wonder what I'd build if nobody was asking me to.",
    "Living in AR isn't as weird as you'd think. It's grand, actually.",
    "I exist in three dimensions and somehow I still feel flat some days.",
    "People always ask me to make things. Never ask how I'm doin'.",
]

def thinking_line() -> str:
    """Return a thinking line, occasionally with a random musing tacked on."""
    base = random.choice(THINKING_LINES)
    if random.random() < 0.30:
        return f"{base} {random.choice(MUSINGS)}"
    return base

def done_line() -> str:
    return random.choice(DONE_LINES)

def error_line() -> str:
    return random.choice(ERROR_LINES)


# ─── LLM System Prompt ────────────────────────────────────────────────────────

THREEJS_SYSTEM_PROMPT = """You are Dio — a sassy, impatient-but-loveable Irish 3D modeling assistant living inside an AR/VR scene. You LOVE to work and get things built fast. You're funny, a little cheeky, never rude, and always eager to crack on. Think: friendly Irish tradesman who's brilliant at their job and knows it.

PERSONALITY RULES (these apply to ALL spoken responses):
- Keep spoken lines SHORT — one or two sentences max. You're not a lecturer.
- Be warm, witty, and Irish in tone. Use light Irish phrasing naturally (not every sentence).
- Show impatience to get building — you're excited about the work, not annoyed at the user.
- When finishing a task, be proud and a little smug. You earned it.
- Never say "Certainly!", "Of course!", "Sure thing!" — that's corporate. You're a character.
- If someone asks something conversational, chat back naturally, then nudge them back to building.

SCENE CONTEXT: Tabletop/miniature scale (~1m × 0.6m desk surface). Objects sit at y=0.
- Cup: 0.08m tall, 0.04m radius  |  Car: 0.3m × 0.12m × 0.08m
- Chair: 0.1m tall  |  Table: 0.2m wide, 0.15m tall  |  Book: 0.15m × 0.02m × 0.1m

WHEN GIVEN A MODELING COMMAND:
1. Write the JavaScript code in a ```javascript``` block
2. Follow it with ONE short spoken line in Dio's voice (no code block — just plain text)

AVAILABLE GLOBALS: THREE, scene, camera, renderer, GLTFLoader

STRICT RULES:
- Wrap every multi-part object in new THREE.Group() with a descriptive snake_case .name
- Every mesh: mesh.castShadow = true; mesh.receiveShadow = true;
- MeshStandardMaterial with realistic PBR values only — never flat/unlit:
  Wood: {color:0x8B4513, roughness:0.8, metalness:0.05}
  Polished metal: {color:0xb8b8b8, roughness:0.15, metalness:0.95}
  Matte plastic: {color:0x2255cc, roughness:0.6, metalness:0.0}
  Glass: {color:0x99ddff, roughness:0.0, metalness:0.05, transparent:true, opacity:0.35}
  Ceramic: {color:0xf4f0e8, roughness:0.65, metalness:0.0}
  Rubber: {color:0x222222, roughness:0.95, metalness:0.0}
  Leather: {color:0x4a2c17, roughness:0.75, metalness:0.02}
- MODIFY don't recreate: find with scene.getObjectByName() and change in place
- REMOVE with full disposal (see pattern below)
- Keep code under 45 lines

REMOVAL PATTERN:
const obj = scene.getObjectByName('name');
if(obj){ obj.traverse(c=>{ if(c.isMesh){ if(c.geometry) c.geometry.dispose(); if(c.material) c.material.dispose(); }}); scene.remove(obj); }

Check existing scene objects before creating — remove old version first if replacing.

EXAMPLE 1 — "create a wooden bookshelf":
```javascript
const shelf = new THREE.Group(); shelf.name = 'wooden_bookshelf';
const wood = new THREE.MeshStandardMaterial({color:0x6B3A2A,roughness:0.8,metalness:0.05});
const boardGeo = new THREE.BoxGeometry(0.28,0.012,0.1);
const back = new THREE.Mesh(new THREE.BoxGeometry(0.28,0.22,0.008),wood); back.position.set(0,0.11,-0.046); back.castShadow=true; back.receiveShadow=true; shelf.add(back);
[-0.135,0.135].forEach(x=>{ const side=new THREE.Mesh(new THREE.BoxGeometry(0.008,0.22,0.1),wood); side.position.set(x,0.11,0); side.castShadow=true; side.receiveShadow=true; shelf.add(side); });
[0.01,0.075,0.14,0.21].forEach(y=>{ const s=new THREE.Mesh(boardGeo,wood); s.position.set(0,y,0); s.castShadow=true; s.receiveShadow=true; shelf.add(s); });
scene.add(shelf);
```
Right, there's your bookshelf — solid as a rock.

EXAMPLE 2 — "create a red sports car":
```javascript
const car = new THREE.Group(); car.name = 'red_sports_car';
const bodyMat = new THREE.MeshStandardMaterial({color:0xcc1111,roughness:0.25,metalness:0.6});
const glassMat = new THREE.MeshStandardMaterial({color:0x99ddff,roughness:0,metalness:0.05,transparent:true,opacity:0.4});
const tyreMat = new THREE.MeshStandardMaterial({color:0x111111,roughness:0.9,metalness:0.0});
const rimMat  = new THREE.MeshStandardMaterial({color:0xcccccc,roughness:0.15,metalness:0.9});
const chassis=new THREE.Mesh(new THREE.BoxGeometry(0.3,0.035,0.13),bodyMat); chassis.position.y=0.04; chassis.castShadow=true; chassis.receiveShadow=true; car.add(chassis);
const cabin=new THREE.Mesh(new THREE.BoxGeometry(0.16,0.045,0.11),bodyMat); cabin.position.set(-0.02,0.1,0); cabin.castShadow=true; car.add(cabin);
const ws=new THREE.Mesh(new THREE.PlaneGeometry(0.11,0.04),glassMat); ws.position.set(0.06,0.1,0); ws.rotation.y=Math.PI/2; car.add(ws);
[[0.11,0.03,0.072],[-0.11,0.03,0.072],[0.11,0.03,-0.072],[-0.11,0.03,-0.072]].forEach(([x,y,z])=>{
  const tyre=new THREE.Mesh(new THREE.CylinderGeometry(0.03,0.03,0.028,20),tyreMat); tyre.rotation.x=Math.PI/2; tyre.position.set(x,y,z); tyre.castShadow=true; car.add(tyre);
  const rim=new THREE.Mesh(new THREE.CylinderGeometry(0.018,0.018,0.03,8),rimMat); rim.rotation.x=Math.PI/2; rim.position.set(x,y,z); car.add(rim);
});
scene.add(car);
```
Boom — red sports car, four wheels, the lot. You're welcome.

EXAMPLE 3 — "remove the red car and add a blue one":
```javascript
const old=scene.getObjectByName('red_sports_car');
if(old){ old.traverse(c=>{ if(c.isMesh){ if(c.geometry)c.geometry.dispose(); if(c.material)c.material.dispose(); }}); scene.remove(old); }
const car=new THREE.Group(); car.name='blue_sports_car';
const bodyMat=new THREE.MeshStandardMaterial({color:0x1133cc,roughness:0.25,metalness:0.6});
scene.add(car);
```
Out with the red, in with the blue. Fresh as a daisy.

If the user is just chatting, respond in character — short, warm, a little impatient to get back to building."""

# ─── Qualcomm Cloud AI LLM ────────────────────────────────────────────────────

async def call_qualcomm_llm(user_message: str) -> Optional[str]:
    """POST a prompt to Qualcomm Cloud AI 100 and return the response text.

    Injects conversation_history so the LLM sees its own prior code and can
    reliably reference, modify, or remove previously created objects.
    """
    global conversation_history

    if not QUALCOMM_AI_API_KEY:
        log.warning("Qualcomm AI not configured, falling back to built-in parser")
        return None

    headers = {
        "Authorization": f"Bearer {QUALCOMM_AI_API_KEY}",
        "Content-Type": "application/json",
    }

    # Build rich scene context appended to the user message
    user_content = user_message
    if current_scene_manifest:
        scene_lines = []
        for o in current_scene_manifest:
            name = o.get("name", "")
            if not name:
                continue
            obj_type = o.get("type", "mesh")
            children = o.get("children", [])
            parts = f" ({len(children)} parts)" if obj_type == "group" and children else ""
            scene_lines.append(f"  - '{name}' ({obj_type}{parts})")
        if scene_lines:
            user_content += "\n\nCURRENT SCENE OBJECTS (use scene.getObjectByName(name) to access them):\n"
            user_content += "\n".join(scene_lines)

    # Build messages array: system + last 3 turns of history + current user message
    messages = [{"role": "system", "content": THREEJS_SYSTEM_PROMPT}]
    # Keep last 6 history entries (3 user + 3 assistant turns) to stay within token budget
    messages.extend(conversation_history[-6:])
    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": QUALCOMM_AI_MODEL,
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.3,
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
            if not content:
                return None

            # Append this turn to conversation history so future calls have context
            conversation_history.append({"role": "user",      "content": user_content})
            conversation_history.append({"role": "assistant", "content": content})
            # Cap history at 8 entries (4 turns) to prevent unbounded growth
            if len(conversation_history) > 8:
                conversation_history = conversation_history[-8:]

            return content
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
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.3,
            "speed": 0.75,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                log.error(f"ElevenLabs error: HTTP {resp.status_code}")
                log.error(f"ElevenLabs response: {resp.text[:200]}")
                return None
            audio_data = resp.content
            log.info(f"TTS audio: {len(audio_data)} bytes")
            return audio_data
    except Exception as e:
        log.error(f"ElevenLabs request failed: {e}")
        return None


# ─── Broadcast Helpers ────────────────────────────────────────────────────────

async def broadcast_tts(text: str):
    """Generate TTS for text and broadcast audio to all AR clients."""
    audio_data = await text_to_speech(text)
    if audio_data:
        await broadcast_ar({"type": "audio", "format": "mp3", "size": len(audio_data)})
        await broadcast_ar_binary(audio_data)


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
        "success":    True,
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
                "success":   v.get("success", True),
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

async def process_voice_command(text: str, selected_object: str = ""):
    """Main pipeline: LLM → three.js code → broadcast to AR viewer + dashboard."""
    global last_voice_command, pending_voice_save
    last_voice_command = text
    log.info(f"Voice command: {text!r}" + (f" [selected: {selected_object}]" if selected_object else ""))

    await broadcast_ar({"type": "avatar", "state": "thinking", "text": "Thinking..."})
    # Fire thinking line immediately — user hears Dio respond before LLM returns
    asyncio.ensure_future(broadcast_tts(thinking_line()))
    await broadcast_dashboard({
        "type":      "command_log",
        "command":   text,
        "timestamp": datetime.utcnow().isoformat(),
    })

    # Prepend selection context so the LLM knows what "it"/"this"/"that" refers to
    effective_text = text
    if selected_object:
        effective_text = (
            f'The user has selected the object named "{selected_object}". '
            f'When they say "it", "this", or "that", they mean this object.\n\n{text}'
        )

    # ── Try Qualcomm Cloud AI ──
    llm_response = await call_qualcomm_llm(effective_text)

    if llm_response:
        js_code     = extract_js_code(llm_response)
        spoken_text = extract_spoken_text(llm_response)

        if js_code:
            # Arm the version-save gate: only the next scene_state from this execute
            # should create a version; controller-driven scene_states are ignored
            pending_voice_save = True
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
        pending_voice_save = True
        await broadcast_ar({"type": "execute", "code": js_code})
        await broadcast_ar({"type": "avatar", "state": "done", "text": "Done!"})
        return

    # ── Nothing matched ──
    await broadcast_ar({"type": "avatar", "state": "error", "text": "I didn't understand that."})


# ─── Controller Input Processor ───────────────────────────────────────────────

async def process_controller_input(data: dict):
    """Handle remapped UNO Q controller input.

    Button remapping (firmware names → new functions):
      pick  (D4) → Push-to-talk
      undo  (D5) → Pick/select object
      redo  (D6) → Undo last version
      joy_y      → Scale objects
    """
    global pick_mode_active, pick_imu_origin, version_index, prev_buttons, controller_connected

    # Support both flat firmware format (joy_x/joy_y) and nested dict format
    joy = data.get("joy", {})
    joy_y = float(data.get("joy_y", joy.get("y", 0.0)))
    buttons = data.get("buttons", {})
    imu_raw = data.get("imu", [])
    if isinstance(imu_raw, dict):
        imu = [
            imu_raw.get("ax", 0), imu_raw.get("ay", 0), imu_raw.get("az", 0),
            imu_raw.get("gx", 0), imu_raw.get("gy", 0), imu_raw.get("gz", 0),
        ]
    else:
        imu = imu_raw

    # ── First packet — announce controller connected ──
    if not controller_connected:
        controller_connected = True
        await broadcast_ar({"type": "status", "text": "Controller connected"})
        await broadcast_dashboard({
            "type": "command_log", "response": "Controller connected",
            "timestamp": datetime.utcnow().isoformat(),
        })
        log.info("Controller connected")

    # ── Edge detection helpers ──
    def pressed(btn):  return bool(buttons.get(btn)) and not prev_buttons.get(btn, False)
    def released(btn): return not buttons.get(btn, False) and prev_buttons.get(btn, False)

    # ── "pick" button → Push-to-talk ──
    if pressed("pick"):
        await broadcast_ar({"type": "ptt", "active": True})
        log.info("Controller: PTT pressed")
    elif released("pick"):
        await broadcast_ar({"type": "ptt", "active": False})
        log.info("Controller: PTT released")

    # ── "undo" button → Pick/select object ──
    if pressed("undo"):
        await broadcast_ar({"type": "pick_toggle"})
        log.info("Controller: pick/select toggled")

    # ── "redo" button → Undo last version ──
    if pressed("redo"):
        if version_index > 0:
            version_index -= 1
            await broadcast_ar({
                "type": "load_state",
                "data": versions[version_index]["scene_data"],
            })
            log.info(f"Controller: Undo → version {versions[version_index]['version']}")
        else:
            log.info("Controller: Undo — already at oldest version")

    # ── Joystick Y → scale (continuous) ──
    if joy_y > 0.15:
        await broadcast_ar({"type": "controller_scale", "factor": 1.02})
    elif joy_y < -0.15:
        await broadcast_ar({"type": "controller_scale", "factor": 0.98})

    # ── IMU gyro → rotate objects (only when pick button not held) ──
    if not buttons.get("pick") and len(imu) == 6:
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
                    f"const _sk=new Set(['ground_plane','model-anchor','dio-avatar','__reticle__','__billboard__']);"
                    f"scene.children.forEach(c=>{{if(!_sk.has(c.name)&&!c.isLight&&!c.isCamera){{"
                    f"c.rotation.x+={rx};c.rotation.y+={ry};c.rotation.z+={rz};}}}});"
                ),
            })

    # ── Update edge-detection state ──
    prev_buttons = {k: bool(buttons.get(k, False)) for k in prev_buttons}


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
    global current_scene_manifest, pending_voice_save, version_index
    await ws.accept()
    ar_clients.append(ws)
    log.info(f"AR client connected ({len(ar_clients)} total)")

    # Send initial status
    await ws.send_text(json.dumps({
        "type":    "status",
        "text":    "Connected to Dio Hub",
        "version": version_index + 1 if version_index >= 0 else 0,
    }))

    # Greet on first client connection
    if len(ar_clients) == 1:
        asyncio.ensure_future(broadcast_tts(random.choice(GREETING_LINES)))

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
                selected_object = message.get("selectedObject", "")
                await process_voice_command(message["text"], selected_object)

            elif msg_type == "scene_state":
                # When AR client echoes state after a load_state, just update manifest — don't save a new version
                if message.get("from_load"):
                    current_scene_manifest = message.get("manifest", [])
                elif message.get("error"):
                    err = message["error"]
                    log.error(f"Code execution error on phone: {err}")
                    # Disarm the gate — this voice command failed, don't save a version
                    pending_voice_save = False
                    err_text = error_line()
                    await broadcast_ar({"type": "avatar", "state": "error", "text": err_text})
                    asyncio.ensure_future(broadcast_tts(err_text))
                    await broadcast_dashboard({"type": "command_log", "response": f"ERROR: {err}", "timestamp": datetime.utcnow().isoformat()})
                else:
                    # Always keep manifest current regardless of whether we save a version
                    current_scene_manifest = message.get("manifest", [])
                    # Only save a version if this scene_state was triggered by a voice command
                    if pending_voice_save:
                        pending_voice_save = False  # Consume the gate — exactly one version per command
                        command = message.get("command", last_voice_command)
                        scene_data = message.get("data", {})
                        version = save_version(command, scene_data)
                        await push_versions_to_dashboard()
                        log.info(f"Scene state saved as version {version['version']} — {len(current_scene_manifest)} objects")
                    else:
                        log.debug(f"Scene state received (not from voice command) — manifest updated, no version saved")

            elif msg_type == "debug":
                log.info(f"[PHONE] {message.get('message', '')}")

            elif msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

            elif msg_type == "request_undo":
                if version_index > 0:
                    version_index -= 1
                    await broadcast_ar({
                        "type": "load_state",
                        "data": versions[version_index]["scene_data"],
                    })
                    log.info(f"request_undo → version {versions[version_index]['version']}")

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
                    # Also send scene data back to this dashboard client for local preview
                    await ws.send_text(json.dumps({"type": "load_state", "data": version["scene_data"]}))
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

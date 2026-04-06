# Dio Design — Quick Setup Guide

## 1. Environment Setup

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

You need:
- **Qualcomm Cloud AI credentials** — get the API URL, key, and model name at the hackathon
- **ElevenLabs API key** — sign up at elevenlabs.io (free tier gives 10k characters/month, plenty for a demo). Pick a voice ID from their voice library — "Adam" (pNInz6obpgDQGcFmaJgB) is a good default

The system works WITHOUT these configured — it falls back to the built-in command parser (no LLM) and skips voice output (text-only). So you can test the full pipeline locally first, then add the cloud services.

## 2. AI PC (Copilot+ / Snapdragon X Elite)

```powershell
# Get your PC's WiFi IP address
ipconfig

# Install hub dependencies
pip install -r requirements.txt

# Start hub server
python server.py

# Open the Designer Dashboard in a browser
# http://localhost:8080/dashboard
```

The hub starts on http://0.0.0.0:8080. Note your IP for the S25.

## 3. Samsung S25 Ultra (Chrome)

### One-time Chrome flag setup

1. Open Chrome → `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
2. Enter: `http://YOUR_PC_IP:8080`
3. Set to **Enabled** → Relaunch Chrome

### VR/Cardboard headset (optional)
If using a Cardboard-style VR headset with the S25:
1. Open Chrome → `chrome://flags/#webxr-incubations`
2. Set to **Enabled** → Relaunch Chrome
3. Also enable `chrome://flags/#cardboard-headset` if available

The AR viewer will automatically fall back to immersive-vr mode with camera passthrough if immersive-ar is not available.

### Launch AR

1. Open `http://YOUR_PC_IP:8080` in Chrome
2. Tap **Enter AR**
3. Grant camera + microphone permissions
4. Point at a flat surface, tap to place model
5. Tap mic button, start talking to Dio

## 4. Arduino UNO Q

### Flash firmware
Open `controller/firmware.ino` in Arduino IDE → upload to UNO Q MCU subsystem

### Start UDP sender
```bash
# On UNO Q Linux side
nmcli device wifi connect "WIFI_SSID" password "WIFI_PASSWORD"
pip3 install pyserial
python3 controller/udp_sender.py YOUR_PC_IP 9877
```

## 5. Test Commands

**With Qualcomm Cloud AI configured** — speak naturally:
- "Hey Dio, make the cube red and metallic"
- "Create a sphere next to it"
- "Scale everything up by 50 percent"
- "Can you add a torus on top?"

**Without Cloud AI (fallback parser):**
- "Make it red" / "Make it blue"
- "Scale it up" / "Make it bigger"
- "Create a sphere" / "Add a cube"
- "Delete it" / "Undo" / "Redo"
- "Rotate it 90 degrees"
- "Make it metallic"

## 6. Troubleshooting

**Check hub health:** `http://YOUR_PC_IP:8080/health`
— Shows Qualcomm AI config and ElevenLabs config

**Dashboard not loading:** Open http://YOUR_PC_IP:8080/dashboard in a browser on the AI PC.

**3D preview empty:** Speak a command in AR first to create a version.

**AR not working:** Use Chrome, not Samsung Internet. Set the Chrome flag. Same WiFi.

**No voice from Dio:** Check ELEVENLABS_API_KEY is set. Check browser allows autoplay audio.

**LLM not generating code:** Check QUALCOMM_AI_API_URL and key. Hub auto-falls back to built-in parser.

**Controller not working:** Check UNO Q WiFi. Verify IP in udp_sender.py. Check port 9877 isn't firewalled.

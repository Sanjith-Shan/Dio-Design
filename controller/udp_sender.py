"""
Dio Controller UDP Sender — Runs on UNO Q Linux side (Debian)

Reads JSON sensor data from the MCU via internal serial,
and forwards it to the Hub server via UDP over WiFi.

Setup on UNO Q:
  1. Connect UNO Q to WiFi (via App Lab or nmcli)
  2. Set HUB_IP to the AI PC's IP address
  3. Run: python3 udp_sender.py

The MCU sketch sends JSON lines over serial at 50Hz.
This script reads them and fires UDP packets to the Hub.
"""

import json
import socket
import sys
import time
import serial

# ─── Config ───────────────────────────────────────────────────────────

HUB_IP = "192.168.1.100"    # ← Change to your AI PC's IP
HUB_PORT = 9877
SERIAL_PORT = "/dev/ttyACM0"  # UNO Q internal MCU serial
SERIAL_BAUD = 115200

# ─── Main ─────────────────────────────────────────────────────────────

def main():
    print(f"Dio Controller UDP Sender")
    print(f"  Hub: {HUB_IP}:{HUB_PORT}")
    print(f"  Serial: {SERIAL_PORT} @ {SERIAL_BAUD}")
    print()

    # Open UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Open serial to MCU
    try:
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.1)
        print(f"Serial connected: {SERIAL_PORT}")
    except Exception as e:
        print(f"Error opening serial: {e}")
        print(f"Available ports: check /dev/ttyACM* or /dev/ttyUSB*")
        sys.exit(1)

    packets_sent = 0
    errors = 0

    try:
        while True:
            line = ser.readline()
            if not line:
                continue

            try:
                line = line.decode("utf-8").strip()
                if not line or not line.startswith("{"):
                    continue

                # Validate JSON
                data = json.loads(line)

                if data.get("type") == "controller":
                    # Forward to hub
                    sock.sendto(line.encode("utf-8"), (HUB_IP, HUB_PORT))
                    packets_sent += 1

                    if packets_sent % 250 == 0:  # Log every 5 seconds at 50Hz
                        print(f"  Sent {packets_sent} packets ({errors} errors)")

                elif data.get("type") == "boot":
                    print(f"  MCU booted: {data}")

            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                errors += 1
                continue

    except KeyboardInterrupt:
        print(f"\nStopped. Sent {packets_sent} packets total.")
    finally:
        ser.close()
        sock.close()


if __name__ == "__main__":
    # Allow overriding HUB_IP from command line
    if len(sys.argv) > 1:
        HUB_IP = sys.argv[1]
    if len(sys.argv) > 2:
        HUB_PORT = int(sys.argv[2])

    main()

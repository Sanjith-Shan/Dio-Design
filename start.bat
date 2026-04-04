@echo off
echo ============================================================
echo   Dio Hub Server — Starting...
echo ============================================================
echo.
echo   Make sure Blender is running with the MCP addon connected!
echo   Make sure OpenClaw gateway is running!
echo.
echo   AR Viewer URL: http://YOUR_IP:8080
echo   (Open this on the Samsung S25 in Chrome)
echo.
echo ============================================================
echo.

REM Create temp export directory
if not exist C:\tmp mkdir C:\tmp

REM Navigate to hub directory
cd /d "%~dp0hub"

REM Install dependencies if needed
pip install -r requirements.txt --quiet 2>nul

REM Start the server
python server.py --host 0.0.0.0 --port 8080

pause

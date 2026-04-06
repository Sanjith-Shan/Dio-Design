@echo off
echo ============================================================
echo   Dio Hub Server -- Starting...
echo ============================================================
echo.
echo   AR Viewer URL: http://YOUR_IP:8080
echo   (Open this on the Samsung S25 in Chrome)
echo.
echo   Dashboard:     http://localhost:8080/dashboard
echo.
echo ============================================================
echo.

REM Navigate to project root
cd /d "%~dp0"

REM Install dependencies if needed
pip install -r requirements.txt --quiet 2>nul

REM Start the server
python server.py

pause

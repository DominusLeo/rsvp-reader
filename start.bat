@echo off
rem RSVP reader: local server on 127.0.0.1:8765 + browser. Close this window to stop.
cd /d "%~dp0"
"venv\Scripts\python.exe" server.py --open

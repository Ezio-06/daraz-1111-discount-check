@echo off
rem daraz tracker control panel: status, start/stop collector, last sweep, re-read raw data.
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" panel.py
if errorlevel 1 pause

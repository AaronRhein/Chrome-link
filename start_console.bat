@echo off
cd /d "%~dp0"
set PY=python-embed\python.exe
if not exist "%PY%" set PY=python
echo === Chrome Window Bridge (debug console - close window or Ctrl+C to quit) ===
"%PY%" bridge.py
pause

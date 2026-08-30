@echo off
setlocal
cd /d "%~dp0.."

rem Download the bundled portable Python runtime (python-embed).
rem Mirror first (fast in CN), python.org as fallback.

set PYVER=3.12.10
set ZIP=python-embed.zip

if exist "python-embed\python.exe" (
    echo [OK] python-embed already exists. Nothing to do.
    exit /b 0
)

where curl >nul 2>nul
if errorlevel 1 (
    echo [ERROR] curl.exe not found. Please install Python 3.8+ manually instead.
    exit /b 1
)

echo Downloading embedded Python %PYVER% ...
curl -L --retry 2 -sS -o "%ZIP%" "https://mirrors.huaweicloud.com/python/%PYVER%/python-%PYVER%-embed-amd64.zip"
if errorlevel 1 curl -L --retry 2 -sS -o "%ZIP%" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip"
if errorlevel 1 (
    echo [ERROR] Download failed. Please download manually and extract to python-embed\.
    exit /b 1
)

echo Extracting ...
powershell -NoProfile -Command "Expand-Archive -Path '%ZIP%' -DestinationPath 'python-embed' -Force"
del "%ZIP%" >nul 2>nul

if exist "python-embed\python.exe" (
    echo [DONE] Runtime ready. Double-click start_bridge.vbs to start.
) else (
    echo [ERROR] Extract failed. Please extract the zip into python-embed\ manually.
)
endlocal

@echo off
rem skills-hub launcher for Windows (Win10/11 cmd).
rem Probes py -3 / python / python3 in order and starts webui.py with the
rem first interpreter that actually works. The Microsoft Store "python"
rem stub fails the probe (non-zero exit), so it is skipped automatically.
rem Output is intentionally English-only to avoid codepage garbling.
setlocal
set "HERE=%~dp0"
set "PYCMD="

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"

if not defined PYCMD (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYCMD=python"
)

if not defined PYCMD (
    python3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYCMD=python3"
)

if not defined PYCMD (
    echo [skills-hub] Python 3 was not found on this computer.
    echo.
    echo   1. Download Python from  https://www.python.org/downloads/
    echo   2. In the installer, tick "Add python.exe to PATH".
    echo   3. Run this script again.
    echo.
    echo Note: the "python" command that comes with Windows may be a fake
    echo Microsoft Store stub. Installing from python.org avoids that.
    echo.
    pause
    exit /b 1
)

echo [skills-hub] Using interpreter: %PYCMD%
%PYCMD% "%HERE%webui.py" %*
if errorlevel 1 (
    echo.
    echo [skills-hub] webui.py exited with an error. See messages above.
    pause
)
endlocal

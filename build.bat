@echo off
REM ============================================================
REM  Build standalone ZTPopup.exe (Windows / PyInstaller onefile)
REM  Requires: Python 3.9+ from python.org (must include tkinter)
REM  Usage:    double-click, or:  set PYTHON=C:\path\to\python.exe && build.bat
REM ============================================================
setlocal
cd /d "%~dp0"

if "%PYTHON%"=="" set "PYTHON=python"

"%PYTHON%" -V >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install from https://www.python.org/downloads/
  echo         and make sure "tcl/tk and IDLE" is checked.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating virtual environment .venv ...
  "%PYTHON%" -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
  )
) else (
  echo [1/4] Reusing existing .venv
)

echo [2/4] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet

echo [3/4] Installing PyInstaller ...
".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller --quiet
if errorlevel 1 (
  echo [ERROR] pip install failed. Check your network or proxy.
  pause
  exit /b 1
)

set "ICON_OPT="
if exist "assets\ztpopup.ico" set "ICON_OPT=--icon assets\ztpopup.ico"
set "DATA_OPT="
if exist "assets\ztpopup.ico" set "DATA_OPT=--add-data assets;assets"

echo [4/4] Building dist\ZTPopup.exe ...
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --noconsole ^
  --name ZTPopup %ICON_OPT% %DATA_OPT% --exclude-module numpy --exclude-module PIL zt_popup.py
if errorlevel 1 (
  echo [ERROR] Build failed.
  pause
  exit /b 1
)

echo.
echo Done: %~dp0dist\ZTPopup.exe
pause

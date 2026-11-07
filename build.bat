@echo off
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python not found. Install it from python.org and tick Add Python to PATH, then run this again.
    pause
    exit /b 1
)

if not exist venv py -m venv venv
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\python -m pip install PySide6 icoextract pillow pyinstaller winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Media.Control winrt-Windows.Storage.Streams

taskkill /F /IM EXEHUB.exe >nul 2>nul
venv\Scripts\python -m PyInstaller --clean --noconfirm --onefile --windowed --name EXEHUB --icon exehub.ico --add-data "exehub.ico;." --hidden-import icoextract --hidden-import PIL --hidden-import winrt.system --hidden-import winrt.windows.foundation --hidden-import winrt.windows.foundation.collections --hidden-import winrt.windows.media.control --hidden-import winrt.windows.storage.streams exehub.py

echo.
echo Done. Your launcher is at: dist\EXEHUB.exe
pause

@echo off
rem Build DiskRecover.exe (needs Python 3.10+ on the BUILD PC only)
cd /d "%~dp0"

set PY=py -3
%PY% --version >nul 2>&1
if errorlevel 1 set PY=python
%PY% --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ and check "Add to PATH".
  pause
  exit /b 1
)

%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

rem onedir: no temp-folder unpacking on C: (protects deleted data). uac-admin: ask for admin rights.
%PY% -m PyInstaller --noconfirm --clean --onedir --uac-admin --name DiskRecover disk_recover_tool.py
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Done. Copy the WHOLE dist\DiskRecover folder to the USB drive.
pause

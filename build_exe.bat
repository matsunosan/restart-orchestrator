@echo off
REM Build restart-orchestrator.exe
REM Run on Windows in the program folder. Python must be installed.

echo Installing build dependencies...
python -m pip install -r requirements.txt pyinstaller

echo.
echo Building the exe...
python -m PyInstaller build.spec --clean --noconfirm

echo.
echo Done. Output: dist\restart-orchestrator.exe
echo Place next to the exe: config.json, secrets.json, settings.ini, the certs folder
pause

@echo off
rem Double-clic : deploie la seedbox puis la Raspberry Pi (192.168.1.103).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
pause

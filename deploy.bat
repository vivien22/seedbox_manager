@echo off
rem Double-clic : deploie la seedbox puis la Raspberry Pi.
cd /d "%~dp0"
set /p PI=Adresse IP de la Raspberry Pi (vide = seedbox seulement) : 
if "%PI%"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" -SeedboxOnly
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" -PiHost %PI%
)
pause

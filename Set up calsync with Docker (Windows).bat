@echo off
title Set up calsync with Docker and Tailscale
rem Downloads setup-docker-windows.ps1 from this project's GitHub page and runs it.
rem Prefer to read it first? It is in the repository: setup-docker-windows.ps1
cd /d "%~dp0"
if exist "setup-docker-windows.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "setup-docker-windows.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/StoutCrow/calsync/main/setup-docker-windows.ps1 -UseBasicParsing -OutFile $env:TEMP\setup-calsync.ps1; & $env:TEMP\setup-calsync.ps1"
)
echo.
pause

@echo off
setlocal
cd /d "%~dp0"
title Deploy calsync to your home server

rem One click: copies calsync to your home server over Tailscale/SSH and sets it up there.
rem First time it asks for the server login (for example  me@my-server ). It remembers it.

if not exist config mkdir config
set "SERVER="
if exist config\server.txt set /p SERVER=<config\server.txt
if "%SERVER%"=="" (
  echo Type your server login as user@name, for example  me@my-server
  echo ^(the name is the machine's Tailscale name; Tailscale must be on here^)
  set /p SERVER=Server:
)
if "%SERVER%"=="" goto :eof
> config\server.txt echo %SERVER%

where ssh >nul 2>nul || (echo Windows needs the "OpenSSH Client" feature: Settings ^> Optional features. & pause & goto :eof)

set "PKG=%TEMP%\calsync-deploy.tar.gz"
echo.
echo Packing calsync...
tar -czf "%PKG%" --exclude=lib --exclude=window --exclude=.venv --exclude=__pycache__ --exclude=calsync.exe --exclude=*.log --exclude=config/calsync.env --exclude=config/server.txt .
if errorlevel 1 (echo Could not pack the folder. & pause & goto :eof)

echo Copying to %SERVER% ^(you may be asked for the server password once^)...
scp -q "%PKG%" %SERVER%:calsync-deploy.tar.gz
if errorlevel 1 (echo Could not reach %SERVER%. Is Tailscale on, and is the login right? & pause & goto :eof)

ssh %SERVER% "mkdir -p ~/calsync && tar -xzf ~/calsync-deploy.tar.gz -C ~/calsync && rm ~/calsync-deploy.tar.gz"
if errorlevel 1 (echo Unpacking on the server failed. & pause & goto :eof)

rem Bring your keys along only if the server has none yet (they travel over your private SSH link).
ssh %SERVER% "test -f ~/calsync/config/calsync.env"
if errorlevel 1 (
  if exist config\calsync.env (
    echo Copying your settings to the server for the first time...
    scp -q config\calsync.env %SERVER%:calsync/config/calsync.env
  )
)

echo.
echo Setting up on the server. This can take a minute...
ssh -t %SERVER% "bash ~/calsync/install-server.sh"
del "%PKG%" >nul 2>nul
echo.
pause

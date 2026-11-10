@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 launcher.py %* & goto end)
where python >nul 2>nul && (python launcher.py %* & goto end)
echo Python is not installed on this laptop.
echo Get it from https://www.python.org/downloads/  (tick "Add python.exe to PATH"), then try again.
:end
pause

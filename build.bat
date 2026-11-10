@echo off
rem Builds calsync.exe (Windows) with PyInstaller. Needs Python 3.10+ on PATH.
rem   build.bat            -> calsync.exe and SHA256SUMS in this folder
rem Tip: run it inside a fresh virtual environment so nothing is installed system-wide:
rem   python -m venv .venv ^& .venv\Scripts\activate ^& build.bat
setlocal
cd /d "%~dp0"
set "HERE=%~dp0"
python -m pip install --quiet -r requirements.txt pyinstaller || goto :fail
python -m unittest discover -s tests || goto :fail
python -m PyInstaller --noconfirm --onefile --noconsole --icon "%HERE%calsync.ico" --name calsync ^
  --distpath "%HERE%dist" --workpath "%HERE%build" --specpath "%HERE%build" ^
  --paths "%HERE%app" --add-data "%HERE%app\web;web" --add-data "%HERE%calsync.ico;." ^
  --collect-submodules anthropic --collect-submodules pydantic ^
  --collect-all icalendar --collect-all recurring_ical_events --collect-all x_wr_timezone ^
  --collect-data tzdata --hidden-import app --hidden-import calsync --hidden-import update --hidden-import version ^
  "%HERE%launcher.py" || goto :fail
copy /y dist\calsync.exe calsync.exe >nul
powershell -NoProfile -Command "$h=(Get-FileHash calsync.exe -Algorithm SHA256).Hash.ToLower(); \"$h  calsync.exe\" | Set-Content -Encoding ascii SHA256SUMS"
echo.
echo Built calsync.exe. SHA256SUMS written.
goto :eof
:fail
echo Build failed.
exit /b 1

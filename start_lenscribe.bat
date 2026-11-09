@echo off
title Lenscribe
cd /d "%~dp0"
if not exist "%~dp0lenscribe.py" (
  echo lenscribe.py is missing. Keep lenscribe.py and start_lenscribe.bat in the SAME folder.
  pause
  exit /b
)
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b
)
echo [1/3] Installing or updating yt-dlp...
%PY% -m pip install -q -U "yt-dlp[default]"
echo [2/3] Checking Deno (needed for YouTube)...
where deno >nul 2>nul || (echo Installing Deno... & winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements >nul 2>nul)
echo [3/3] Checking ffmpeg (HD video and frames)...
where ffmpeg >nul 2>nul || (echo Installing ffmpeg... & winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements >nul 2>nul)
echo.
echo Lenscribe is starting. Keep this window open while you use it.
echo 1) Your browser opens by itself at the line that says ">>> OPEN THIS ADDRESS". If a different page opens, type that address yourself.
echo 2) The line above it must say "JS runtime for YouTube: deno". If it says NONE, close this window and run this file once more.
%PY% lenscribe.py
pause

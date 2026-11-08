@echo off
title Lenscribe
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b
)
echo Installing or updating yt-dlp...
%PY% -m pip install -q -U yt-dlp
where ffmpeg >nul 2>nul || (echo Installing ffmpeg, optional... & winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements >nul 2>nul)
echo.
echo Lenscribe is starting. Your browser will open. Keep this window open while you use it.
%PY% lenscribe_server.py
pause

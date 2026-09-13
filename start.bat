@echo off
title YouTube MP3 Bot
cd /d "%~dp0"

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

python main.py
pause

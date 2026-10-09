@echo off
cd /d "%~dp0"
call npm run preview -- --port 5179

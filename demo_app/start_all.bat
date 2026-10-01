@echo off
echo Launching backend and frontend in separate windows...
start "backend"  cmd /k "%~dp0start_backend.bat"
timeout /t 2 /nobreak >nul
start "frontend" cmd /k "%~dp0start_frontend.bat"

@echo off
cd /d "%~dp0frontend"
echo Serving frontend on http://127.0.0.1:8080 ...
start http://127.0.0.1:8080
python -m http.server 8080
pause

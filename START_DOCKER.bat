@echo off
cd /d "%~dp0"
docker compose up -d --build
if errorlevel 1 (echo Docker build failed. Install Docker Desktop and check the log.& pause & exit /b 1)
echo.
echo Open http://127.0.0.1:5173
start "" http://127.0.0.1:5173
pause

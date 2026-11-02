@echo off
cd /d "%~dp0"
if not exist "backend\.venv\Scripts\python.exe" (
  echo First run setup_windows.bat
  pause
  exit /b 1
)
cd backend
.venv\Scripts\python.exe -m app.bootstrap_cli
pause

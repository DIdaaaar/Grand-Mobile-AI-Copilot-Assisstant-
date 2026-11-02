@echo off
cd /d "%~dp0frontend"
if not exist node_modules (
  echo Frontend packages are missing. Run setup_windows.bat first.
  pause
  exit /b 1
)
call npm run dev

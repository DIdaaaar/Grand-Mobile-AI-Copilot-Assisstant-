@echo off
cd /d "%~dp0"
if exist "backend\.venv\Scripts\python.exe" (
  "backend\.venv\Scripts\python.exe" -m unittest discover -s tests -v
) else (
  py -3.11 -m unittest discover -s tests -v
)
pause

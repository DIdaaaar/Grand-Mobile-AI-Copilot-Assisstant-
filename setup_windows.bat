@echo off
setlocal
cd /d "%~dp0"
echo [1/4] Checking Python 3.11 or newer...
set "PY_EXE="
for %%V in (3.11 3.12 3.13) do (
  if not defined PY_EXE (
    py -%%V --version >nul 2>nul
    if not errorlevel 1 set "PY_EXE=py -%%V"
  )
)
if not defined PY_EXE (
  echo Python 3.11-3.13 was not found via Windows Python launcher.
  echo Install Python 3.11 or 3.12 from https://www.python.org/downloads/
  pause
  exit /b 1
)
echo Using %PY_EXE%
echo [2/4] Creating backend virtual environment...
if not exist "backend\.venv\Scripts\python.exe" %PY_EXE% -m venv backend\.venv
if errorlevel 1 goto failed
call backend\.venv\Scripts\activate.bat
echo [3/4] Installing backend packages...
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
if errorlevel 1 goto failed
echo [4/4] Installing frontend packages...
where npm >nul 2>nul
if errorlevel 1 (
  echo Node.js 20+ is required. Install it from https://nodejs.org/ and run this script again.
  pause
  exit /b 1
)
cd frontend
call npm install
if errorlevel 1 goto failed
cd ..
echo.
echo Setup complete. Start start_backend.bat and start_frontend.bat in two separate windows.
pause
exit /b 0
:failed
echo Setup failed. Read the error above and check your internet connection.
pause
exit /b 1

@echo off
setlocal
cd /d "%~dp0"
set "PYEXE="
for %%V in (3.11 3.12 3.13) do (
 if not defined PYEXE (
  py -%%V --version >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -%%V"
 )
)
if not defined PYEXE (
 echo Python 3.11-3.13 not detected. Please install Python.
 pause
 exit /b 1
)
%PYEXE% "%~dp0UPDATE_PROJECT.py" --project "%~dp0"
pause

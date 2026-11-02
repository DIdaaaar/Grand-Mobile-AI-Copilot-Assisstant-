@echo off
setlocal
cd /d "%~dp0"
if not exist ".git" (echo This folder is not a Git repository. Use UPDATE_PROJECT.bat for ZIP updates.& pause & exit /b 1)
git pull --ff-only
if errorlevel 1 (echo Could not fast-forward. Existing changes were not overwritten.& pause & exit /b 1)
echo Source updated. For Docker run START_DOCKER.bat; for Windows restart backend if Python files changed.
pause

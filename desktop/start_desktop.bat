@echo off
REM Launch the Zaytoon desktop shell.
REM
REM Prefers backend\.venv so the shell and the backend share one interpreter.
REM Run with --diagnose to check the environment without opening a window:
REM     start_desktop.bat --diagnose

setlocal
set "ROOT=%~dp0.."
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"

if not exist "%PY%" set "PY=python"

cd /d "%ROOT%"
"%PY%" "%~dp0main.py" %*
if errorlevel 1 pause
endlocal

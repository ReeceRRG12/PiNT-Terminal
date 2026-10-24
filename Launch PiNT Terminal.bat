@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create the virtual environment first using the README instructions.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pint_terminal

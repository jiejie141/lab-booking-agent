@echo off
REM ---------------------------------------------------------------------------
REM  lab-booking-agent - one click start (Windows)
REM  First run creates .venv and installs deps; afterwards it just starts.
REM  ASCII-only on purpose: .cmd is parsed with the OEM code page, and
REM  non-ASCII text in a batch file is the classic source of garbled output.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [setup] creating virtualenv and installing dependencies...
  python -m venv .venv
  if errorlevel 1 goto :fail
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :fail
)

echo [run] http://127.0.0.1:8200  -  press Ctrl+C to stop
".venv\Scripts\python.exe" main.py serve
exit /b %errorlevel%

:fail
echo [fail] setup did not complete, see the messages above.
pause
exit /b 1

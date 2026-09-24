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

REM JWT secret: the service now refuses to start with the public built-in
REM default (fail-closed), so a fresh clone would stop right here.
REM Keep this launcher one-click by generating a random secret per run --
REM random, never the published value, and never written to a file.
if not defined LAB_JWT_SECRET (
  for /f %%S in ('".venv\Scripts\python.exe" -c "import secrets;print(secrets.token_hex(32))"') do set "LAB_JWT_SECRET=%%S"
)
if not defined LAB_JWT_SECRET (
  echo [fail] could not generate a JWT secret.
  pause
  exit /b 1
)

echo [run] http://127.0.0.1:8200  -  press Ctrl+C to stop
echo [run] one-time random JWT secret generated for this session
".venv\Scripts\python.exe" main.py serve
exit /b %errorlevel%

:fail
echo [fail] setup did not complete, see the messages above.
pause
exit /b 1

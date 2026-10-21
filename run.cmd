@echo off
rem Launches tapo_switch_api on Windows. Creates .venv and installs
rem requirements on first run (and again whenever requirements.txt changes).
rem Any arguments are passed through, e.g. run.cmd --port 9000 -v
setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "STAMP=.venv\requirements.installed"

if not exist "%VENV_PY%" (
    echo Creating virtual environment in .venv ...
    where py >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 goto :fail
)

fc /b requirements.txt "%STAMP%" >nul 2>nul
if errorlevel 1 (
    echo Installing requirements ...
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
    copy /y requirements.txt "%STAMP%" >nul
)

"%VENV_PY%" -m tapo_switch_api %*
exit /b %errorlevel%

:fail
echo.
echo Setup failed. Make sure Python 3.11+ is installed and on PATH.
pause
exit /b 1

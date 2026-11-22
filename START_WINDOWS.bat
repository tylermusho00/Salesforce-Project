@echo off
rem Lead Router: double-click to start the dashboard. It opens in your browser.
rem First run installs everything into a private .venv folder (about 1-2 minutes). Needs Python 3.11+.
cd /d "%~dp0"
title Lead Router dashboard
if exist ".venv\installed.ok" goto run

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.11 or newer is required. Get it from https://www.python.org/downloads/
  echo During setup, tick "Add python.exe to PATH". Then double-click this file again.
  pause
  exit /b 1
)
echo First run: installing (about 1-2 minutes)...
%PY% -m venv .venv || goto fail
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto fail
echo ok> .venv\installed.ok

:run
.venv\Scripts\python.exe run_dashboard.py
pause
exit /b 0

:fail
echo.
echo Install failed. Check your internet connection and that Python is 3.11 or newer, then try again.
if exist .venv rmdir /s /q .venv
pause
exit /b 1

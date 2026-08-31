@echo off
REM ---------------------------------------------------------------------------
REM MIRI TOSS bridge collector - scheduled-task wrapper (ASCII only, no window).
REM   Usage:  toss_bridge_run.cmd <kinds>       e.g.  toss_bridge_run.cmd prices
REM   Why a wrapper: schtasks /tr quoting for ">>" redirection is fragile.
REM   The task is registered to run as SYSTEM (session 0) so NO window appears.
REM ---------------------------------------------------------------------------
setlocal
set "BRIDGE_DIR=%~dp0"
set "KINDS=%~1"
if "%KINDS%"=="" set "KINDS=prices"

REM Python executable. Override with env PY_EXE if installed elsewhere.
if not defined PY_EXE set "PY_EXE=C:\Program Files\Python312\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

if not exist "%BRIDGE_DIR%logs" mkdir "%BRIDGE_DIR%logs"
set "LOG=%BRIDGE_DIR%logs\bridge_%KINDS%.log"

REM Keep the log bounded (~2MB) - rotate to .1 and start fresh.
for %%F in ("%LOG%") do if %%~zF GTR 2000000 move /y "%LOG%" "%LOG%.1" >nul 2>&1

echo. >> "%LOG%"
echo ==== %DATE% %TIME% kinds=%KINDS% ==== >> "%LOG%"
"%PY_EXE%" "%BRIDGE_DIR%toss_bridge.py" --kinds %KINDS% >> "%LOG%" 2>&1
echo exit=%ERRORLEVEL% >> "%LOG%"
endlocal

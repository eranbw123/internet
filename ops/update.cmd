@echo off
rem Entry point for the internet-discovery-update Scheduled Task: fast-forward
rem the deployed checkout to origin and redeploy. Mirrors run.cmd's console/log
rem hardening; GIT_TERMINAL_PROMPT=0 so a missing credential fails fast instead
rem of hanging the task on a hidden prompt.
set PYTHONIOENCODING=utf-8
set GIT_TERMINAL_PROMPT=0

rem %~dp0 is ops\; the repo root (one up) is where self_update.py, .env and the
rem git checkout live.
cd /d "%~dp0.."

if not exist logs mkdir logs

rem Locale-proof YYYYMMDD (see run.cmd -- %date% is not safe to slice).
for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set LOGDATE=%%D

python "%~dp0self_update.py" >> "logs\update-%LOGDATE%.log" 2>&1
exit /b %ERRORLEVEL%

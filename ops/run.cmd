@echo off
rem Entry point every installed task shells out to (see install_tasks.py).
rem Task Scheduler's redirected stdout is not a console, so a model-generated
rem character that would kill a narrow-codepage console must not kill the job.
set PYTHONIOENCODING=utf-8

rem %~dp0 is this file's own directory (ops\); cd up one to the repo root so
rem config.REPO_ROOT, .env and watch.py all resolve regardless of Task
rem Scheduler's own working directory.
cd /d "%~dp0.."

if not exist logs mkdir logs

rem Locale-proof YYYYMMDD -- %date% is locale-dependent and not safe to slice.
for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set LOGDATE=%%D

set LOGFILE=logs\%1-%LOGDATE%.log
python -m app %* >> "%LOGFILE%" 2>&1
exit /b %ERRORLEVEL%

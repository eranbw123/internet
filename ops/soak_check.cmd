@echo off
rem One-shot 24h soak checkpoint (see ops/install_tasks.py --soak / ops/SOAK.md).
rem Registered as a single TimeTrigger with no <Repetition>, so this fires
rem exactly once per registration. Copies ops/run.cmd's proven idioms
rem (utf-8 stdout, cd to repo root, locale-proof date) rather than inventing
rem a second convention.
set PYTHONIOENCODING=utf-8

rem %~dp0 is this file's own directory (ops\); cd up one to the repo root so
rem config.REPO_ROOT, .env and watch.py all resolve regardless of Task
rem Scheduler's own working directory.
cd /d "%~dp0.."

if not exist logs mkdir logs

rem Locale-proof YYYYMMDD -- %date% is locale-dependent and not safe to slice.
for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set LOGDATE=%%D

set LOGFILE=logs\soak-%LOGDATE%.txt
python -m app stats --days 1 >> "%LOGFILE%" 2>&1
python -m app health >> "%LOGFILE%" 2>&1
schtasks /query /fo LIST /v 2>&1 | findstr /c:"internet-discovery-" >> "%LOGFILE%"
exit /b %ERRORLEVEL%

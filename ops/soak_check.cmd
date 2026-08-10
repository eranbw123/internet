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
set STATS_RC=%ERRORLEVEL%
python -m app health >> "%LOGFILE%" 2>&1
set HEALTH_RC=%ERRORLEVEL%
rem Reuse install_tasks.py's own block-aware /fo LIST /v reader instead of a
rem findstr pipeline: findstr /c:"internet-discovery-" only matches the
rem TaskName/Comment lines of that format, discarding every Status/Last Run
rem Time/Last Result/Next Run Time line the soak readout needs.
python ops\install_tasks.py --status >> "%LOGFILE%" 2>&1
set STATUS_RC=%ERRORLEVEL%

rem Reflect the actual python outcomes in Last Result rather than a
rem findstr no-match (1 even on a clean run). `health` legitimately exits 1
rem when degraded -- that is evidence for the readout, not a script defect --
rem so a crash in stats/status (the two calls with no such legitimate
rem non-zero exit) is what should fail the task; health's own exit code is
rem left out of that decision and is visible in the readout file instead.
if not %STATS_RC%==0 exit /b %STATS_RC%
if not %STATUS_RC%==0 exit /b %STATUS_RC%
exit /b 0

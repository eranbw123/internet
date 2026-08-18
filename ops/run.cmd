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

rem Log basename comes from the FULL argument list, not just %1: the three
rem collect tasks all start with "run-once", so a %1-only name gave all
rem three the same log file (logs\run-once-<date>.log). cmd's `>>` opens that
rem file without FILE_SHARE_WRITE and holds it for the whole run, so a second
rem task hitting the same name exits rc=1 before python even starts -- a
rem silently dropped run with no log line, no heartbeat, no counter.
set LOGNAME=%*
set LOGNAME=%LOGNAME: =_%
set LOGFILE=logs\%LOGNAME%-%LOGDATE%.log

rem A run must never be dropped just because its log file is unavailable.
rem cmd's `>>` opens the log without FILE_SHARE_WRITE, so anything else
rem holding that handle makes the redirect fail and the whole run vanish
rem before python starts -- no line, no heartbeat, no counter, and a task
rem that looks like it ran. It happened for real: a Chrome launched by
rem health.preflight_gate inherited this very handle and held it for days
rem (see discovery/health.py). That leak is fixed at the source now; this is
rem the belt to its braces, because "silently did nothing" is the one
rem outcome this appliance must never have again.
(type nul >> "%LOGFILE%") 2>nul || set LOGFILE=logs\%LOGNAME%-%LOGDATE%-alt.log

python -m app %* >> "%LOGFILE%" 2>&1
exit /b %ERRORLEVEL%

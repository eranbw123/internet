@echo off
rem Trivial one-command launcher for the real Observatory web UI (see
rem README's "Observatory" section) -- the auth-capable trace/graph
rem explorer, in place of pointing `datasette discovery.db` at the raw file
rem by hand.
rem
rem %~dp0 is this file's own directory (ops\); cd up one to the repo root so
rem config.REPO_ROOT, .env and discovery.db all resolve regardless of the
rem caller's own working directory (same convention as run.cmd).
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

rem Binds to 127.0.0.1:8010 by default -- 8010, not the CLI's own 8001
rem default, to match a standing external ngrok tunnel already forwarding
rem there. Any arg after the script name is passed straight through to
rem `ui` and overrides the corresponding default (argparse keeps the last
rem occurrence of a flag), e.g.:
rem   ops\observatory.cmd --port 9000
rem   ops\observatory.cmd --public
python -m app ui --host 127.0.0.1 --port 8010 %*

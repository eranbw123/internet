#!/usr/bin/env python3
"""Register/remove/inspect the six Windows Task Scheduler tasks that run the
discovery engine as an appliance -- no in-process scheduler, no session-child
tick loop. Every task shells out to ops/run.cmd, which calls `python -m app`.

    python ops/install_tasks.py --dry-run       # print XML + commands, register nothing
    python ops/install_tasks.py --install       # create/update all six tasks
    python ops/install_tasks.py --uninstall     # delete only tasks this script created
    python ops/install_tasks.py --status        # state/last-run/last-result/next-run

Cadence for the collect-* tasks and the digest time come from `config.load()`
(interval_stocks_seconds / interval_web_seconds / interval_youtube_seconds /
digest_time) -- they are never hardcoded here, so changing a `.env` interval
and re-running --install is enough to reschedule.

Registration goes through generated Task Scheduler XML + `schtasks /create
/XML`, not `schtasks /create /sc minute`, which can't express the settings
that make a task survive a sleeping machine (StartWhenAvailable) or run as
the interactive logged-on user (Chrome/CDP for the default provider only
exists in that session, not under a service/S4U principal).
"""
import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discovery import config  # noqa: E402

PREFIX = "internet-discovery-"

# (name suffix, `python -m app` args, trigger kind, cfg field name or a literal
# seconds/HH:MM value, ExecutionTimeLimit). Collect jobs get a longer time
# limit than the rest -- they're the ones that can spend an LLM budget.
_TASK_SPECS = [
    ("collect-stocks", ["run-once", "--source", "stocks"], "interval", "interval_stocks_seconds", "PT30M"),
    ("collect-web", ["run-once", "--source", "web_search"], "interval", "interval_web_seconds", "PT30M"),
    ("collect-youtube", ["run-once", "--source", "youtube"], "interval", "interval_youtube_seconds", "PT30M"),
    ("digest", ["digest"], "daily", "digest_time", "PT10M"),
    ("feedback", ["listen", "--drain"], "interval", 5 * 60, "PT10M"),
    ("health", ["health", "--notify"], "interval", 3 * 3600, "PT10M"),
]

TASK_NAMES = [f"{PREFIX}{suffix}" for suffix, *_ in _TASK_SPECS]


@dataclass
class TaskDef:
    name: str
    app_args: list
    trigger_kind: str   # "interval" | "daily"
    trigger_value: object   # seconds (interval) or "HH:MM" (daily)
    exec_time_limit: str    # ISO-8601 duration


def build_tasks(cfg):
    """Every trigger value is read from `cfg`, not hardcoded -- the 5-minute
    feedback poll and the 3-hour health check are the two literal exceptions
    the plan calls for; nothing per-source or per-day is a literal here."""
    tasks = []
    for suffix, app_args, kind, field, limit in _TASK_SPECS:
        value = getattr(cfg, field) if isinstance(field, str) else field
        tasks.append(TaskDef(f"{PREFIX}{suffix}", app_args, kind, value, limit))
    return tasks


def _iso8601_duration(seconds):
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if not days and not hours and not minutes:
        minutes = 1   # Task Scheduler's repetition granularity floors at 1 minute
    body = (f"{days}D" if days else "") + "T" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
    return "P" + body.rstrip("T")


def _current_user():
    domain = os.environ.get("USERDOMAIN", "")
    name = os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain else name


def _action_command(app_args):
    repo_root = config.REPO_ROOT
    run_cmd = str(repo_root / "ops" / "run.cmd")
    command = r"C:\Windows\System32\cmd.exe"
    # /d is mandatory: this machine has a cmd AutoRun hook that otherwise runs
    # on every cmd.exe invocation and breaks the working directory.
    arguments = f'/d /c "{run_cmd}" {" ".join(app_args)}'
    return command, arguments, str(repo_root)


def _trigger_xml(task):
    now = datetime.now().replace(microsecond=0)
    if task.trigger_kind == "daily":
        hour, minute = (int(p) for p in task.trigger_value.split(":"))
        start = now.replace(hour=hour, minute=minute, second=0).isoformat()
        return f"""    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>"""
    interval = _iso8601_duration(task.trigger_value)
    return f"""    <TimeTrigger>
      <StartBoundary>{now.isoformat()}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>{interval}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>"""


def render_xml(task):
    """Task Scheduler XML for one task. All the survive-a-sleeping-machine
    settings the plan requires live in <Settings>; the principal runs as the
    interactive logged-on user (LogonType=InteractiveToken), not a service/S4U
    principal, because the default provider's Chrome/CDP session only exists
    there."""
    command, arguments, workdir = _action_command(task.app_args)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(task.name)} -- managed by ops/install_tasks.py, do not hand-edit.</Description>
  </RegistrationInfo>
  <Triggers>
{_trigger_xml(task)}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(_current_user())}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{task.exec_time_limit}</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>2</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _default_runner(args):
    proc = subprocess.run(args, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def install(cfg, runner=None, dry_run=False):
    runner = runner or _default_runner
    ok = True
    for task in build_tasks(cfg):
        xml = render_xml(task)
        if dry_run:
            print(f"--- {task.name} ---")
            print(xml)
            print(f'schtasks /create /tn "{task.name}" /xml "<generated>.xml" /f\n')
            continue
        fd, path = tempfile.mkstemp(suffix=".xml")
        os.close(fd)
        try:
            # schtasks /XML rejects some UTF-8 files; UTF-16 with BOM (the
            # `"utf-16"` codec picks the native, little-endian order and
            # writes the BOM) is what it wants.
            Path(path).write_bytes(xml.encode("utf-16"))
            cmd = ["schtasks", "/create", "/tn", task.name, "/xml", path, "/f"]
            code, out, err = runner(cmd)
            if code == 0:
                print(f"{task.name}: installed")
            else:
                ok = False
                print(f"{task.name}: FAILED: {(err or out).strip()}", file=sys.stderr)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    return 0 if ok else 1


def uninstall(runner=None, dry_run=False):
    """Only ever touches the six names this script creates -- TASK_NAMES is a
    fixed, prefix-scoped whitelist, so this can never reach a task (e.g. an
    `ec-*` canary task) it did not itself create."""
    runner = runner or _default_runner
    for name in TASK_NAMES:
        cmd = ["schtasks", "/delete", "/tn", name, "/f"]
        if dry_run:
            print(" ".join(cmd))
            continue
        code, out, err = runner(cmd)
        if code == 0:
            print(f"{name}: deleted")
        else:
            print(f"{name}: {(err or out).strip()}")
    return 0


def _parse_query_blocks(output):
    """`schtasks /query /fo LIST /v` prints one blank-line-separated block of
    `Key:      Value` lines per task on the whole machine; pull out the ones
    under our prefix."""
    blocks = {}
    current = {}
    for raw_line in output.splitlines() + [""]:
        line = raw_line.rstrip()
        if not line.strip():
            name = current.get("TaskName", "").lstrip("\\")
            if name:
                blocks[name] = current
            current = {}
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip()
    return blocks


def status(runner=None):
    runner = runner or _default_runner
    code, out, err = runner(["schtasks", "/query", "/fo", "LIST", "/v"])
    if code != 0:
        print(f"schtasks /query failed: {(err or out).strip()}", file=sys.stderr)
        return 1
    blocks = _parse_query_blocks(out)
    for name in TASK_NAMES:
        block = blocks.get(name)
        if block is None:
            print(f"{name}: not installed")
            continue
        print(f"{name}:")
        for key in ("Status", "Last Run Time", "Last Result", "Next Run Time"):
            print(f"  {key}: {block.get(key, '?')}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="print XML + schtasks commands, register nothing")
    group.add_argument("--install", action="store_true", help="create/update all six tasks")
    group.add_argument("--uninstall", action="store_true", help="delete only the tasks this script created")
    group.add_argument("--status", action="store_true", help="state/last-run/last-result/next-run per task")
    args = parser.parse_args(argv)

    if args.status:
        return status()
    if args.uninstall:
        return uninstall()
    cfg = config.load()
    return install(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

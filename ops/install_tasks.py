#!/usr/bin/env python3
"""Register/remove/inspect the six Windows Task Scheduler tasks that run the
discovery engine as an appliance -- no in-process scheduler, no session-child
tick loop. Every task shells out to ops/run.cmd, which calls `python -m app`,
via wscript + ops/hidden.vbs so no console window ever appears on a trigger.

    python ops/install_tasks.py --dry-run       # print XML + commands, register nothing
    python ops/install_tasks.py --install       # create/update all six tasks
    python ops/install_tasks.py --uninstall     # delete only tasks this script created
    python ops/install_tasks.py --status        # state/last-run/last-result/next-run
    python ops/install_tasks.py --soak [--soak-hours 24] [--dry-run]
        # register the one-shot 24h soak-checkpoint task (see ops/SOAK.md);
        # composable with --dry-run to preview without registering

Cadence for the collect-* tasks and the digest come from `config.load()`
(interval_stocks_seconds / interval_web_seconds / interval_youtube_seconds /
digest_time / digest_interval_seconds / digest_window_end -- the digest
re-fires every digest_interval_seconds from digest_time until
digest_window_end, same day) -- they are never hardcoded here, so changing a
`.env` interval and re-running --install is enough to reschedule.

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
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discovery import config  # noqa: E402

PREFIX = "internet-discovery-"

# (name suffix, `python -m app` args, trigger kind, cfg field name or a literal
# seconds/HH:MM value, ExecutionTimeLimit[, script]). Collect jobs get a longer
# time limit than the rest -- they're the ones that can spend an LLM budget.
# The optional 6th element overrides the run.cmd action for tasks that aren't a
# `python -m app` subcommand (the updater shells to ops/update.cmd instead).
_TASK_SPECS = [
    ("collect-stocks", ["run-once", "--source", "stocks"], "interval", "interval_stocks_seconds", "PT30M"),
    # Continuous Council-driven web discovery (discovery/missions.py) --
    # `web-tick` runs every interval_web_seconds (default 60s), not a
    # periodic full-interest batch collect.
    ("collect-web", ["web-tick"], "interval", "interval_web_seconds", "PT30M"),
    ("collect-youtube", ["run-once", "--source", "youtube"], "interval", "interval_youtube_seconds", "PT30M"),
    ("digest", ["digest"], "daily", "digest_time", "PT10M"),
    ("feedback", ["listen", "--drain"], "interval", 5 * 60, "PT10M"),
    ("health", ["health", "--notify"], "interval", 3 * 3600, "PT10M"),
    # Self-update: fast-forward the checkout to origin and redeploy, so a merged
    # fix reaches production without a hand deploy. Literal 30-min cadence (like
    # feedback/health); shells to ops/update.cmd, not `python -m app`.
    ("update", [], "interval", 30 * 60, "PT10M", "update.cmd"),
]

TASK_NAMES = [f"{PREFIX}{suffix}" for suffix, *_ in _TASK_SPECS]

# The one-shot 24h soak checkpoint (objective C). Deliberately NOT a
# _TASK_SPECS entry: build_tasks()/install() must keep creating exactly the
# six recurring tasks, so a normal --install never touches this one, and
# a re-run of --install never reschedules or recreates it.
SOAK_TASK = f"{PREFIX}soak-check"


@dataclass
class TaskDef:
    name: str
    app_args: list
    trigger_kind: str   # "interval" | "daily" | "once"
    trigger_value: object   # seconds (interval), "HH:MM" (daily), or hours (once)
    exec_time_limit: str    # ISO-8601 duration
    script: str = "run.cmd"   # ops/<script>, the .cmd the action shells out to
    # "daily" only: re-fire every repeat_seconds from trigger_value's HH:MM
    # until window_end, same day (Task Scheduler's own Repetition+Duration on
    # the CalendarTrigger) -- so a daily task can drain throughout the day
    # instead of firing once. 0/"" = plain single daily fire (unchanged).
    repeat_seconds: int = 0
    window_end: str = ""


def build_tasks(cfg):
    """Every trigger value is read from `cfg`, not hardcoded -- the 5-minute
    feedback poll and the 3-hour health check are the two literal exceptions
    the plan calls for; nothing per-source or per-day is a literal here."""
    tasks = []
    for spec in _TASK_SPECS:
        suffix, app_args, kind, field, limit = spec[:5]
        script = spec[5] if len(spec) > 5 else "run.cmd"
        value = getattr(cfg, field) if isinstance(field, str) else field
        task = TaskDef(f"{PREFIX}{suffix}", app_args, kind, value, limit, script=script)
        if kind == "daily":
            task.repeat_seconds = cfg.digest_interval_seconds
            task.window_end = cfg.digest_window_end
        tasks.append(task)
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
    """The account SID, not a name: on workgroup machines USERDOMAIN can be
    the literal 'WORKGROUP', which schtasks cannot map to a SID ('No mapping
    between account names and security IDs', observed on first live install).
    A SID is always mappable and locale-proof. Fallback builds
    COMPUTERNAME\\USERNAME — the real authority for local accounts."""
    try:
        out = subprocess.run(["whoami", "/user", "/fo", "csv"],
                             capture_output=True, text=True, errors="replace",
                             timeout=15).stdout or ""
        sid = out.strip().splitlines()[-1].split(",")[-1].strip('" ')
        if sid.upper().startswith("S-1-"):
            return sid
    except (OSError, IndexError, subprocess.TimeoutExpired):
        pass
    name = os.environ.get("USERNAME", "")
    domain = os.environ.get("COMPUTERNAME", "")
    return f"{domain}\\{name}" if domain else name


def _action_command(app_args, script="run.cmd"):
    repo_root = config.REPO_ROOT
    run_cmd = str(repo_root / "ops" / script)
    hidden_vbs = str(repo_root / "ops" / "hidden.vbs")
    # wscript (a GUI-subsystem host) runs ops/hidden.vbs, which starts the
    # real command with a hidden window: an InteractiveToken console action
    # would otherwise flash a cmd window in the user's session on every
    # trigger (collect-web alone fires every 60s). hidden.vbs waits on the
    # child and propagates its exit code, so IgnoreNew / RestartOnFailure /
    # Last Result behave exactly as with a direct console action.
    command = r"C:\Windows\System32\wscript.exe"
    # /d is mandatory: this machine has a cmd AutoRun hook that otherwise runs
    # on every cmd.exe invocation and breaks the working directory.
    arguments = " ".join(
        ["//B", "//Nologo", f'"{hidden_vbs}"', r"C:\Windows\System32\cmd.exe",
         f'/d /c "{run_cmd}"'] + list(app_args))
    return command, arguments, str(repo_root)


def _trigger_xml(task):
    now = datetime.now().replace(microsecond=0)
    if task.trigger_kind == "daily":
        hour, minute = (int(p) for p in task.trigger_value.split(":"))
        start = now.replace(hour=hour, minute=minute, second=0)
        if start <= now:
            # Today's occurrence already elapsed -- with StartWhenAvailable,
            # a past StartBoundary is a missed run Task Scheduler catches up
            # on right after registration, firing an unscheduled extra
            # digest. Roll to tomorrow instead.
            start += timedelta(days=1)
        repetition = ""
        if task.repeat_seconds and task.window_end:
            end_hour, end_minute = (int(p) for p in task.window_end.split(":"))
            end = start.replace(hour=end_hour, minute=end_minute)
            if end <= start:
                end += timedelta(days=1)   # window wraps past midnight
            duration = _iso8601_duration((end - start).total_seconds())
            interval = _iso8601_duration(task.repeat_seconds)
            repetition = f"""
      <Repetition>
        <Interval>{interval}</Interval>
        <Duration>{duration}</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>"""
        return f"""    <CalendarTrigger>
      <StartBoundary>{start.isoformat()}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>{repetition}
    </CalendarTrigger>"""
    if task.trigger_kind == "once":
        # The soak checkpoint: a single firing `trigger_value` hours out, no
        # <Repetition> -- that element would turn a one-shot checkpoint into
        # a recurring task.
        start = now + timedelta(hours=task.trigger_value)
        return f"""    <TimeTrigger>
      <StartBoundary>{start.isoformat()}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>"""
    # Stagger each interval task's initial StartBoundary by its position in
    # TASK_NAMES: two tasks sharing an interval (e.g. collect-web and
    # collect-youtube, both 4h by default) would otherwise get an identical
    # StartBoundary and fire in the same second on every occurrence --
    # including every StartWhenAvailable catch-up -- and ops/run.cmd's
    # per-task log file is exclusively locked for the whole run, so the
    # second task to start silently never runs at all.
    start = now + timedelta(minutes=7 * TASK_NAMES.index(task.name))
    interval = _iso8601_duration(task.trigger_value)
    return f"""    <TimeTrigger>
      <StartBoundary>{start.isoformat()}</StartBoundary>
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
    command, arguments, workdir = _action_command(task.app_args, task.script)
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
    # errors="replace": the machine-wide `schtasks /query /fo LIST /v` dump
    # contains bytes the ANSI codepage (cp1255 here) cannot decode; bare
    # text=True crashed the reader thread and handed back stdout=None
    # (observed on first live --status). The fields we parse are ASCII.
    proc = subprocess.run(args, capture_output=True, text=True, errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


def _start_boundary(xml):
    """Pull <StartBoundary> back out of already-rendered XML, so a caller
    that needs to report the timestamp Task Scheduler actually holds uses
    the exact string that was registered, instead of re-deriving it with a
    second `datetime.now()` call that can drift by a second or two."""
    return xml.split("<StartBoundary>")[1].split("</StartBoundary>")[0]


def _register_task(task, runner, dry_run, xml=None):
    """Render, write and register ONE task via `schtasks /create /XML`, then
    verify with a `/query` follow-up. The single registration path for both
    the six recurring tasks (`install`) and the one-shot soak checkpoint
    (`install_soak`) -- there is no second, parallel way this script talks
    to `schtasks /create`. Returns True on success. `xml` lets a caller
    (install_soak) pass in the XML it already rendered, so the StartBoundary
    it later reports matches the one actually registered."""
    xml = xml if xml is not None else render_xml(task)
    fd, path = tempfile.mkstemp(suffix=".xml", prefix=f"{task.name}-")
    os.close(fd)
    # schtasks /XML rejects some UTF-8 files; UTF-16 with BOM (the
    # `"utf-16"` codec picks the native, little-endian order and writes
    # the BOM) is what it wants.
    Path(path).write_bytes(xml.encode("utf-16"))
    cmd = ["schtasks", "/create", "/tn", task.name, "/xml", path, "/f"]
    if dry_run:
        # The real path, not a placeholder -- this line has to be
        # copy-pasteable to actually reproduce the registration.
        print(f"--- {task.name} ---")
        print(xml)
        print(subprocess.list2cmdline(cmd) + "\n")
        return True
    try:
        code, out, err = runner(cmd)
        if code != 0:
            print(f"{task.name}: FAILED: {(err or out).strip()}", file=sys.stderr)
            return False
        # Confirm the import actually landed rather than trusting
        # schtasks' exit code alone.
        vcode, vout, verr = runner(["schtasks", "/query", "/tn", task.name])
        if vcode == 0:
            print(f"{task.name}: installed")
            return True
        print(
            f"{task.name}: created but not found on verification query: "
            f"{(verr or vout).strip()}",
            file=sys.stderr,
        )
        return False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def install(cfg, runner=None, dry_run=False):
    runner = runner or _default_runner
    ok = True
    for task in build_tasks(cfg):
        if not _register_task(task, runner, dry_run):
            ok = False
    return 0 if ok else 1


def install_soak(cfg, runner=None, dry_run=False, hours=24):
    """Register the one-shot 24h soak checkpoint (objective C): fires once,
    `hours` after this call, running ops/soak_check.cmd (no `python -m app`
    args) which appends a stats+health+status readout to
    `logs\\soak-<date>.txt`. Not a `_TASK_SPECS` entry -- `--install` never
    creates, recreates or reschedules it; see SOAK_TASK. `cfg` is accepted
    for symmetry with `install()` and future cfg-derived settings; nothing
    here reads it yet."""
    runner = runner or _default_runner
    task = TaskDef(SOAK_TASK, [], "once", hours, "PT15M", script="soak_check.cmd")
    xml = render_xml(task)
    ok = _register_task(task, runner, dry_run, xml=xml)
    if not dry_run:
        start = _start_boundary(xml)
        fire_date = (datetime.now() + timedelta(hours=hours)).strftime("%Y%m%d")
        readout = str(config.REPO_ROOT / "logs" / f"soak-{fire_date}.txt")
        print(f"soak checkpoint StartBoundary: {start}")
        print(f"soak readout path (approx, actual date is when it fires): {readout}")
    return 0 if ok else 1


def uninstall(runner=None, dry_run=False):
    """Only ever touches the six names this script creates plus the one-shot
    soak checkpoint -- TASK_NAMES + [SOAK_TASK] is a fixed, prefix-scoped
    whitelist, so this can never reach a task (e.g. an `ec-*` canary task) it
    did not itself create. Deleting a soak task that was never registered is
    a harmless no-op reported the same as any other `schtasks /delete` miss."""
    runner = runner or _default_runner
    for name in TASK_NAMES + [SOAK_TASK]:
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
    for raw_line in (output or "").splitlines() + [""]:
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
    # The soak checkpoint is one-shot and optional -- only report it (never
    # "not installed") when a --soak call has actually registered it.
    soak_block = blocks.get(SOAK_TASK)
    if soak_block is not None:
        print(f"{SOAK_TASK}:")
        for key in ("Status", "Last Run Time", "Last Result", "Next Run Time"):
            print(f"  {key}: {soak_block.get(key, '?')}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # --dry-run is deliberately NOT in the mutually-exclusive group below: it
    # has to compose with --soak (preview the checkpoint without registering
    # it) the same way it already implicitly composes with plain --install
    # (the bare `--dry-run` invocation in the usage block above). The other
    # four actions stay mutually exclusive with each other, checked by hand
    # since argparse's own group can't express "exclusive among these, but
    # not with that other flag".
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--install", action="store_true", help="create/update all six tasks")
    group.add_argument("--uninstall", action="store_true", help="delete only the tasks this script created")
    group.add_argument("--status", action="store_true", help="state/last-run/last-result/next-run per task")
    group.add_argument("--soak", action="store_true", help="register the one-shot 24h soak-checkpoint task")
    parser.add_argument("--dry-run", action="store_true", help="print XML + schtasks commands, register nothing")
    parser.add_argument("--soak-hours", type=float, default=24.0, help="hours until --soak's checkpoint fires (default 24)")
    args = parser.parse_args(argv)
    if not (args.install or args.uninstall or args.status or args.soak or args.dry_run):
        parser.error("one of --install/--uninstall/--status/--soak/--dry-run is required")
    if args.status and args.dry_run:
        # --status is already read-only; unlike --install/--uninstall/--soak,
        # --dry-run has nothing to preview there. Reject rather than silently
        # ignoring the flag.
        parser.error("--dry-run has no effect with --status")

    if args.status:
        return status()
    if args.uninstall:
        return uninstall(dry_run=args.dry_run)
    cfg = config.load()
    if args.soak:
        return install_soak(cfg, dry_run=args.dry_run, hours=args.soak_hours)
    return install(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

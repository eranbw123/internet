# 24h soak checkpoint -- operator runbook

Companion to `ops/install_tasks.py --soak`. Because no session can wait 24h,
the soak "check-in" is itself a one-shot Windows Scheduled Task
(`internet-discovery-soak-check`) that fires once, long after the session
that started it has ended, and writes its own readout to `logs\`. This file
is the runbook a *later* session (or the same operator, next day) uses to
resume from state without rework.

## Starting a soak

Only after the six appliance tasks are installed and confirmed `Ready`
(`python ops/install_tasks.py --status`):

```bash
python ops/install_tasks.py --soak                  # fires ~24h from now
python ops/install_tasks.py --soak --soak-hours 48   # custom window
python ops/install_tasks.py --soak --dry-run         # preview XML, register nothing
```

`--soak` registers exactly one Scheduled Task, a single `<TimeTrigger>` with
no `<Repetition>` (fires once, not recurring) and `StartWhenAvailable=true`
(so a sleeping machine still catches it up). It is **not** re-created or
rescheduled by a plain `--install` -- `--install` only ever touches the six
recurring tasks. `--uninstall` deletes it if present, same as the other six.

**Record these two lines from the `--soak` output in the step handoff:**
- `soak checkpoint StartBoundary: <timestamp>` -- when it will fire.
- `soak readout path: logs\soak-<YYYYMMDD>.txt` -- where to look after it
  fires (the date is when the task actually runs, which may differ from the
  install date if the window crosses midnight).

## What the checkpoint does

`ops/soak_check.cmd` (same idioms as `ops/run.cmd`: utf-8 stdout, cd to repo
root, locale-proof date) appends, in order, to `logs\soak-<date>.txt`:

1. `python -m app stats --days 1`
2. `python -m app health`
3. `python ops\install_tasks.py --status` (every task's Status/Last Run
   Time/Last Result/Next Run Time -- a plain `schtasks /query /fo LIST /v`
   filtered by `findstr` on the `internet-discovery-` prefix only matches
   the TaskName/Comment lines of that output format, not the per-field
   lines, so this reuses install_tasks.py's own block-aware reader instead)

## Resuming a later session

1. `python ops/install_tasks.py --status` -- confirm
   `internet-discovery-soak-check` is present; its `Last Run Time`/`Status`
   tell you whether the checkpoint has fired yet.
2. If it has fired: read the readout file recorded above (or glob
   `logs\soak-*.txt` if the exact date wasn't recorded) -- it is the full
   evidence bundle, no need to re-derive anything.
3. If it has not fired yet: nothing to do, wait for `Next Run Time`.

## Pass criteria (objective C)

The soak passes when, over the readout window:
- Every one of the six tasks ran at its configured cadence with no
  unexplained gap (`Next Run Time` progressing normally in `--status`, no
  stale `job:*:last_ok` heartbeats in `health`'s output).
- `run_failed` in the `stats --days 1` counters shows only failures
  attributable to a deliberate drill (see drills 1-2 in the step handoff),
  not unexplained ones.
- The feedback drain never lost a button press (no gap between consecutive
  `getUpdates` offsets beyond what a single missed interval explains).
- `health` ends `ok` (not degraded) at the end of the window.

Paste the raw `logs\soak-<date>.txt` contents into the step handoff as the
evidence for this criterion -- this file's job is to make sure a session
knows where to find that evidence, not to re-summarize it in advance.

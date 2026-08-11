#!/usr/bin/env python3
"""Keep the deployed appliance checkout in sync with origin, automatically.

The appliance runs `python -m app` from whatever this checkout has checked out,
so a merged fix only reaches production once the checkout is fast-forwarded --
which nothing did, leaving it stranded on a stale branch. This script closes
that gap. It is registered as the recurring `internet-discovery-update`
Scheduled Task (see ops/install_tasks.py) and, each run:

  * git fetch origin <deploy-branch>
  * ONLY when the checkout is on <deploy-branch> and the tree is clean, it
    fast-forwards to origin/<deploy-branch>. It never resets, never switches
    off a feature branch (the same checkout is used to author PRs -- clobbering
    a colleague's or another session's in-progress branch is not worth "never
    stale"), and never touches a dirty tree.
  * on an actual fast-forward it redeploys: reloads interests (`python -m app
    init`, which also applies any additive DB migrations) and re-registers the
    Scheduled Tasks (idempotent), so a shipped task/interests change lands with
    no hand-holding.

An actual update is announced once over Telegram; a plain "already current" is
silent. A divergence (someone committed on the deploy branch, or history was
rewritten) or a git failure is surfaced once -- deduped by a marker file so a
persistent fault doesn't repeat every cycle -- and left for a human, because
auto-resolving it would mean discarding commits.

    python ops/self_update.py            # the real thing (what the task runs)
    python ops/self_update.py --dry-run  # fetch + report the decision, change nothing
    python ops/self_update.py --status   # local vs origin, no fetch, no changes

Deploy branch is DISCOVERY_DEPLOY_BRANCH (default 'main').
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discovery import config  # noqa: E402
from discovery import notify  # noqa: E402

DEFAULT_BRANCH = os.environ.get("DISCOVERY_DEPLOY_BRANCH", "main")
FETCH_TIMEOUT_SECONDS = 90
STATE_FILE = "logs/.update_state.json"   # dedups the "something's wrong" notification

# Decisions plan() can reach. Only UPDATE mutates anything.
SKIP_BRANCH = "skip-branch"   # on a feature branch -- leave it alone
SKIP_DIRTY = "skip-dirty"     # deploy branch has local edits -- don't clobber
CURRENT = "current"           # already at origin -- nothing to do
DIVERGED = "diverged"         # local has commits origin doesn't -- a human's call
UPDATE = "update"             # clean fast-forward available -- take it


class GitError(Exception):
    pass


def plan(branch, deploy_branch, dirty, local, remote, is_ancestor):
    """Pure decision: given the repo facts, what should happen? No I/O, so the
    whole policy is unit-testable without a git repo."""
    if branch != deploy_branch:
        return SKIP_BRANCH, f"on '{branch}', not the deploy branch '{deploy_branch}'"
    if dirty:
        return SKIP_DIRTY, f"'{deploy_branch}' has uncommitted changes"
    if local == remote:
        return CURRENT, f"up to date at {local[:7]}"
    if not is_ancestor:
        return DIVERGED, f"local {local[:7]} has commits not in origin/{deploy_branch} {remote[:7]}"
    return UPDATE, f"{local[:7]} -> {remote[:7]}"


def default_git(root, timeout=FETCH_TIMEOUT_SECONDS):
    """A git runner bound to `root`. GIT_TERMINAL_PROMPT=0 guarantees a missing
    credential fails fast instead of hanging the Scheduled Task on a prompt."""
    def run(args, check=True):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, errors="replace", timeout=timeout, env=env,
        )
        out = (proc.stdout or "").strip()
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {(proc.stderr or out).strip()[:300]}")
        return proc.returncode, out
    return run


def gather(git, deploy_branch, fetch=True):
    """Run the read-only git queries plan() needs. `fetch` off (for --status)
    reports against whatever the last fetch left in origin/<branch>."""
    if fetch:
        git(["fetch", "--prune", "origin", deploy_branch])
    _, branch = git(["rev-parse", "--abbrev-ref", "HEAD"])
    _, porcelain = git(["status", "--porcelain"])
    _, local = git(["rev-parse", "HEAD"])
    _, remote = git(["rev-parse", f"origin/{deploy_branch}"])
    rc, _ = git(["merge-base", "--is-ancestor", local, remote], check=False)
    return branch, bool(porcelain.strip()), local, remote, rc == 0


def default_redeploy(root, log=print):
    """Post-fast-forward steps: reload interests/DB, then re-register the
    Scheduled Tasks (idempotent -- picks up a shipped task/interval/interests
    change and re-creates any task that went missing). Best-effort per step;
    a step failure is reported but the others still run."""
    ok = True
    steps = [
        ([sys.executable, "-X", "utf8", "-m", "app", "init"], "reload interests + migrations"),
        ([sys.executable, "-X", "utf8", str(Path(root) / "ops" / "install_tasks.py"), "--install"],
         "re-register scheduled tasks"),
    ]
    for cmd, what in steps:
        try:
            proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                                  errors="replace", timeout=180)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"redeploy: {what} -> FAILED to run: {e}")
            ok = False
            continue
        log(f"redeploy: {what} -> rc {proc.returncode}")
        if proc.returncode != 0:
            log((proc.stderr or proc.stdout or "").strip()[:500])
            ok = False
    return ok


def _notify_once(cfg, root, key, text):
    """Send `text`, but only if the last thing we notified about differs --
    a persistent fault must not spam the owner every cycle. Key None clears the
    marker (a recovery), so the next distinct fault notifies again."""
    marker = Path(root) / STATE_FILE
    last = None
    try:
        last = json.loads(marker.read_text(encoding="utf-8")).get("key")
    except (OSError, ValueError):
        pass
    if key is None:
        if last is not None:
            try:
                marker.write_text(json.dumps({"key": None}), encoding="utf-8")
            except OSError:
                pass
        return
    if key == last:
        return
    notify.send(cfg, text)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"key": key}), encoding="utf-8")
    except OSError:
        pass


def run(root, cfg, git=None, deploy_branch=DEFAULT_BRANCH, dry_run=False,
        redeploy=default_redeploy, notify_once=_notify_once, log=print):
    """One update cycle. Returns the plan() action taken. Injectable git /
    redeploy / notify so the whole flow is testable without a repo or network."""
    git = git or default_git(root)
    try:
        branch, dirty, local, remote, is_ancestor = gather(git, deploy_branch)
    except GitError as e:
        log(f"git error: {e}")
        notify_once(cfg, root, f"git-error:{e}", f"⚠ news auto-update: git failed\n{e}")
        return "error"

    action, note = plan(branch, deploy_branch, dirty, local, remote, is_ancestor)
    log(f"plan: {action} ({note})")

    if action == DIVERGED:
        notify_once(cfg, root, f"diverged:{local}:{remote}",
                    f"⚠ news auto-update held: origin/{deploy_branch} diverged from the "
                    f"deployed checkout ({note}). A human needs to reconcile it; "
                    f"the appliance keeps running the current code meanwhile.")
        return action
    if action != UPDATE:
        notify_once(cfg, root, None, "")   # healthy state -> clear any prior fault marker
        return action

    if dry_run:
        log(f"dry-run: would fast-forward {note} and redeploy")
        return action

    _, subjects = git(["log", "--oneline", f"{local}..{remote}"], check=False)
    git(["merge", "--ff-only", f"origin/{deploy_branch}"])
    redeploy_ok = redeploy(root, log=log)
    notify_once(cfg, root, None, "")   # a successful deploy is a healthy state
    head = subjects.splitlines()
    body = "\n".join(head[:12]) + ("\n…" if len(head) > 12 else "")
    warn = "" if redeploy_ok else "\n⚠ a redeploy step failed -- check logs/update-*.log"
    notify.send(cfg, f"🔄 news appliance updated on {deploy_branch}: {note}"
                     f" ({len(head)} commit{'s' if len(head) != 1 else ''}){warn}\n{body}")
    return action


def status(root, cfg, deploy_branch=DEFAULT_BRANCH, log=print):
    git = default_git(root)
    try:
        branch, dirty, local, remote, is_ancestor = gather(git, deploy_branch, fetch=False)
    except GitError as e:
        log(f"git error: {e}")
        return 2
    action, note = plan(branch, deploy_branch, dirty, local, remote, is_ancestor)
    log(f"branch={branch} dirty={dirty} local={local[:7]} "
        f"origin/{deploy_branch}={remote[:7]} -> {action} ({note})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="fetch + report the decision, change nothing")
    g.add_argument("--status", action="store_true", help="local vs origin (no fetch, no changes)")
    args = parser.parse_args(argv)
    cfg = config.load()
    root = config.REPO_ROOT
    if args.status:
        return status(root, cfg)
    action = run(root, cfg, dry_run=args.dry_run)
    # Non-zero only for states a human should look at; routine outcomes are 0
    # so the task's Last Result stays green.
    return 2 if action in ("error", DIVERGED) else 0


if __name__ == "__main__":
    sys.exit(main())

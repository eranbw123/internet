"""Shared mechanics for engine-lab experiments (see LAB.md).

Every experiment is the same loop the x prompt lab proved out: propose ->
execute -> measure in code -> council judge writes guidance -> scorecard
persisted -> next generation starts from the scorecard. This module holds the
loop's mechanics; each experiment owns its prompts and metrics.

Artifacts live under experiments/lab/artifacts/<experiment>/ (gitignored):
state.json (budget + all generations) and runs.jsonl (every provider call,
full prompt + raw response, so any conclusion can be audited later).
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from discovery.providers.base import ProviderError  # noqa: E402

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"


def utf8_streams():
    # cp1255 console: one non-ASCII char in a printed title would otherwise
    # kill a run that has already spent budget.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Lab:
    """Budget, state, and run log for one experiment."""

    def __init__(self, name, budget_cap=40):
        self.name = name
        self.budget_cap = budget_cap
        self.dir = ARTIFACTS / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.runs_path = self.dir / "runs.jsonl"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            self.state = {"budget_used": 0, "generations": []}

    def save(self):
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def log(self, **record):
        with self.runs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now(), **record}, ensure_ascii=False) + "\n")

    def spend(self, n=1):
        self.state["budget_used"] += n
        if self.state["budget_used"] > self.budget_cap:
            self.save()
            raise SystemExit(
                f"BUDGET CAP {self.budget_cap} exceeded ({self.state['budget_used']})"
            )

    def call(self, call_type, fn, **ctx):
        """One budgeted provider call with one retry. Returns fn() or None if
        both attempts raise ProviderError; the caller decides whether a None
        kills the generation or just one measurement."""
        for attempt in (1, 2):
            self.spend(1)
            try:
                return fn()
            except ProviderError as e:
                self.log(call=call_type, attempt=attempt, error=str(e), **ctx)
                if attempt == 2:
                    return None
                time.sleep(5)

    def add_generation(self, record):
        record = {"gen": len(self.state["generations"]) + 1, "ts": now(), **record}
        self.state["generations"].append(record)
        self.save()
        return record


# --- council judge -----------------------------------------------------------
# The deliberation pattern from ai/council_bot.py, generic over subject matter:
# the experiment supplies the brief and the data, the council supplies guidance
# for the next generation. Metrics are always computed in code BEFORE this call
# -- the judge interprets numbers, it never produces them.

COUNCIL_SYSTEM = (
    "You run an internal council deliberation to evaluate experiment results. "
    "All deliberation happens internally in your reasoning; your visible "
    "output is ONLY the final JSON object."
)

COUNCIL_PROMPT = """\
{brief}

Measured results of the current generation (all numbers were computed by
code, not by a model -- treat them as ground truth):

{data_json}

Run a three-stage council internally:

STAGE 1 -- three judges assess independently:
- The Analyst: what do the numbers actually show, including anything that
  undermines the obvious reading?
- The Skeptic: what alternative explanation, confound, or artifact could
  produce these numbers without the hypothesis being true?
- The Owner's Proxy: what change would most improve the owner's real outcome,
  judged strictly by the brief above?

STAGE 2 -- anonymize the three assessments as A/B/C; each judge reviews the
others and flags disagreements.

STAGE 3 -- as Chairman, settle disagreements and produce the consolidated
answer.

{question}

Output ONLY this JSON object:
{{"verdict": "<3-5 sentences: consolidated answer>",
 "guidance": "<concrete instructions for the next generation of this
   experiment; empty string if the experiment should stop>"}}"""

COUNCIL_SCHEMA = {
    "type": "object",
    "required": ["verdict", "guidance"],
    "properties": {"verdict": {"type": "string"}, "guidance": {"type": "string"}},
}


def council_judge(provider, lab, brief, data, question, **ctx):
    """Returns {"verdict": ..., "guidance": ...} or None if the call failed."""
    prompt = COUNCIL_PROMPT.format(
        brief=brief,
        data_json=json.dumps(data, ensure_ascii=False, indent=1),
        question=question,
    )
    result = lab.call(
        "judge",
        lambda: provider.complete_json(COUNCIL_SYSTEM, prompt, COUNCIL_SCHEMA),
        **ctx,
    )
    if result is not None:
        lab.log(call="judge", prompt=prompt, raw=result, **ctx)
    return result

"""Personal internet discovery engine.

Periodically collects candidate content, scores it against the user's declared
interests with an LLM, and pushes only the high scorers to Telegram.

Run it with `python -m app <command>` (or `python -m discovery <command>`, the
same CLI) from the repo root.
"""

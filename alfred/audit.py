"""The audit log: one line for every action the agent tried, with the decision (allow,
approved, declined, no answer, deny) and why.

It lives at ~/.alfred/audit.jsonl, one JSON object per line, only ever added to. Read it
with `cat` or `tail -f`. The agent can't change it: permissions.py keeps it out of reach.
"""

import json
import time
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path

    def record(self, task_id: int, tool: str, tool_input: dict, outcome: str, reason: str) -> None:
        """outcome: allow, deny, approved, declined, declined before or no answer."""
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task": task_id,
            "tool": tool,
            "input": _short(tool_input),
            "outcome": outcome,
            "reason": reason,
        }
        with self._path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _short(tool_input: dict) -> dict:
    """Long values (a whole file being written, say) are cut to keep the log readable."""
    return {key: str(value)[:300] for key, value in tool_input.items()}

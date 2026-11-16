"""What the agent may do: three rules, decided in plain code before every tool call, so
nothing the agent reads (a web page, a README) can change them.

    free    work inside the current project, the web, reading skills, local git, any
            other sandboxed command
    ask     anything public or hard to undo: pushing, publishing, merging, deleting
            files, installing system software, sending data out, saving a skill
    never   secrets outside the project (keys, SSH, Alfred's own settings), sudo,
            leaving the sandbox

"Ask" pauses the task until you answer out loud; anything but a clear yes is a no.
"Always" is a yes for that kind of action (pushing, say) in that project until midnight,
or until Alfred restarts. Force-pushing is a kind of its own. Every decision goes to the
audit log. Shell commands also run in the operating system's
sandbox (agent.py), which keeps their writes inside the project.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

from alfred.audit import AuditLog
from alfred.config import PROJECT_ROOT
from alfred.questions import Questions, is_always, is_yes

FREE_TOOLS = {"WebSearch", "WebFetch", "Skill", "TodoWrite", "TaskStop"} | {
    "mcp__alfred__ask_user",
    "mcp__alfred__save_lesson",
}
FILE_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit"}
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}
NEVER = re.compile(
    r"\bsudo\b|\.ssh\b|\.aws\b|\.gnupg\b|\bkeychain\b|security\s+find-|\balfred\.db\b"
    + rf"|{re.escape(str(PROJECT_ROOT))}",
    re.IGNORECASE,
)
ASK = [  # (kind, pattern): the first match names the kind, for "always" answers
    ("force-push", r"\bgit\s+push\b.*(\s-\w*f|--force)"),
    ("push", r"\bgit\s+push\b"),
    ("undo", r"\bgit\s+reset\s+--hard\b|\bgit\s+clean\b|\bgit\s+branch\s+-D\b"),
    (
        "github",
        r"\bgh\s+(pr|repo|release|issue|gist|secret|workflow)\s+"
        r"(create|merge|delete|edit|close|comment|review|fork|rename|archive|run|set)\b"
        r"|\bgh\s+api\b.*(-X|--method)\s*(POST|PUT|PATCH|DELETE)",
    ),
    ("delete", r"\brm\s+(-\w*[rf]\w*\s+)+|\bshred\b"),
    (
        "install",
        r"\b(apt|apt-get|brew|snap|port)\s+(install|upgrade|remove|purge)\b"
        r"|\bnpm\s+(i|install)\s+(-g|--global)\b",
    ),
    ("publish", r"\b(npm|pnpm|yarn|uv|twine)\s+publish\b"),
    ("send", r"\bcurl\b.*(-X\s*(POST|PUT|PATCH|DELETE)|--data|-d\s|-F\s)"),
    ("run-download", r"\b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b"),
]
ASK = [(kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in ASK]


@dataclass(frozen=True)
class Decision:
    action: str  # allow | ask | deny
    reason: str  # deny: why; ask: the action, in words Alfred reads out
    kind: str = ""  # ask: what an "always" answer covers (push, delete, skill...)


def decide(tool: str, tool_input: dict, project: Path, skills: Path) -> Decision:
    if tool in FREE_TOOLS:
        return Decision("allow", "free")
    if tool in FILE_TOOLS:
        return _decide_file(tool, tool_input, project, skills)
    if tool == "Bash":
        return _decide_command(tool_input)
    if tool == "mcp__alfred__serve_app":  # runs outside the sandbox
        return Decision("ask", f"start your app with: {tool_input.get('command', '')[:200]}", "app")
    return Decision("ask", f"use {tool} with {str(tool_input)[:150]}", f"tool {tool}")


def _decide_file(tool: str, tool_input: dict, project: Path, skills: Path) -> Decision:
    pattern = tool_input.get("pattern" if tool == "Glob" else "glob") or ""
    if pattern.startswith(("/", "~")) or ".." in pattern:
        return Decision("deny", "file searches stay inside the project")
    raw = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    path = (project / Path(raw).expanduser()).resolve() if raw else project.resolve()
    if path.is_relative_to(project.resolve()):
        return Decision("allow", "inside the project")
    if path.is_relative_to(skills.resolve()):
        if tool not in WRITE_TOOLS:
            return Decision("allow", "reading a skill")
        name = path.relative_to(skills.resolve()).parts[1:2] or ("skills",)
        return Decision("ask", f"save what I learned as the {name[0]} skill", "skill")
    alfreds_own = path.is_relative_to(skills.resolve().parent) or path.is_relative_to(PROJECT_ROOT)
    if alfreds_own or path.name.startswith(".env") or NEVER.search(str(path)):
        return Decision("deny", "secrets and Alfred's own files are off limits")
    if tool in WRITE_TOOLS:
        return Decision("deny", f"{path} is outside the project")
    return Decision("allow", "reading outside the project")


def _decide_command(tool_input: dict) -> Decision:
    command = tool_input.get("command", "")
    if tool_input.get("dangerouslyDisableSandbox"):
        return Decision("deny", "commands run in the sandbox")
    if NEVER.search(command):
        return Decision("deny", "that touches secrets or needs admin rights")
    for kind, pattern in ASK:
        if pattern.search(command):
            return Decision("ask", f"run: {command[:200]}", kind)
    return Decision("allow", "a sandboxed command")


class Guard:
    """Checks every tool call the agent makes (the SDK's PreToolUse hook)."""

    def __init__(self, questions: Questions, audit: AuditLog, skills: Path, tz: ZoneInfo):
        self._questions, self._audit = questions, audit
        self._skills, self._tz = skills, tz
        self._declined: set[tuple[int, str]] = set()  # (task, action) you said no to
        self._always: dict[tuple[str, str], date] = {}  # (project, kind) -> the day allowed
        self.refused: set[str] = set()  # tool calls refused here

    def hook_for(self, task_id: int, project: Path):
        async def pre_tool_use(hook_input: dict, tool_use_id, _context) -> dict:
            allowed, reason = await self.check(
                task_id, project, hook_input["tool_name"], hook_input.get("tool_input") or {}
            )
            if not allowed:
                self.refused.add(tool_use_id or hook_input.get("tool_use_id", ""))
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow" if allowed else "deny",
                    "permissionDecisionReason": reason,
                }
            }

        return pre_tool_use

    async def check(
        self, task_id: int, project: Path, tool: str, tool_input: dict
    ) -> tuple[bool, str]:
        """Returns (may it run, what the agent is told)."""
        decision = decide(tool, tool_input, project, self._skills)
        allowed, outcome = decision.action == "allow", decision.action
        if decision.action == "ask":
            allowed, outcome = await self._ask(task_id, (str(project), decision.kind), decision)
        self._audit.record(task_id, tool, tool_input, outcome, decision.reason)
        if allowed:
            return True, decision.reason
        logger.info(f"Task {task_id}: {tool} refused ({outcome}): {decision.reason}")
        if outcome == "deny":
            return False, f"Not allowed: {decision.reason}."
        if outcome == "no answer":
            return False, "The user did not answer in time, so this was not done. Don't retry."
        return False, "The user said no. Don't try this again; finish and say it wasn't done."

    async def _ask(
        self, task_id: int, standing: tuple[str, str], decision: Decision
    ) -> tuple[bool, str]:
        today = datetime.now(self._tz).date()
        if self._always.get(standing) == today:
            return True, "approved (standing)"
        if (task_id, decision.reason) in self._declined:
            return False, "declined before"
        answer = await self._questions.ask(task_id, "approve", f"May I {decision.reason}?")
        if is_always(answer):
            self._always[standing] = today
            return True, "approved for today"
        if is_yes(answer):
            return True, "approved"
        self._declined.add((task_id, decision.reason))
        return False, "no answer" if answer is None else "declined"

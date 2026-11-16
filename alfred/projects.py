"""Projects: one folder each under ~/.alfred/projects, where the agent works.

A project is any folder: a cloned repository, a new app, notes. Work with no project
named goes to "general". Each project keeps one lasting agent session (agent.py), and its
own instructions in CLAUDE.md or AGENTS.md, which the agent reads and keeps up to date.
"""

import re
from pathlib import Path

GENERAL = "general"
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")
MAX_INSTRUCTIONS = 8000  # characters of a project's instructions put into the prompt


class Projects:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def names(self) -> list[str]:
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    def resolve(self, name: str) -> str:
        """The project meant by a spoken name: an existing one ("Portfolio" or "the
        portfolio project" is portfolio), or a new folder name made from it."""
        slug = slugify(re.sub(r"\b(the|project|repo|repository)\b", " ", name.lower()))
        if not slug:
            return GENERAL
        existing = {slugify(n): n for n in self.names()}
        return existing.get(slug, slug)

    def path(self, name: str) -> Path:
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def instructions(self, name: str) -> str:
        """The project's own instructions, for the agent's prompt (empty if none)."""
        for file in INSTRUCTION_FILES:
            path = self.root / name / file
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")[:MAX_INSTRUCTIONS]
                return f"\n\n## This project's instructions ({file})\n{text}"
        return ""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]

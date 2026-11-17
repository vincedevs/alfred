"""Skills: how-to knowledge Alfred writes for himself and uses when a task needs it.

A skill is a folder with a SKILL.md (the standard Agent Skills format: a name, a one-line
description of when to use it, then the steps), plus any scripts it needs. The agent sees
every skill's name and description and loads a skill only when a task matches.

The skills live in ~/.alfred/skills, laid out as a local Claude Code plugin so the agent
loads them natively, and kept in git: every new or changed skill is a commit you can read
and undo. The agent asks before saving or changing a skill (permissions.py). Alfred starts
with the skills in the project's skills/ folder (how to write a skill, how to answer from
documents); a starter skill added in a later version is copied in once, so one you told
him to forget stays forgotten.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

from loguru import logger

from alfred.config import PROJECT_ROOT

STARTER_SKILLS = PROJECT_ROOT / "skills"
GIT = ["git", "-c", "user.name=alfred", "-c", "user.email=alfred@localhost"]


class Skills:
    def __init__(self, root: Path):
        self.root = root  # the plugin folder the agent loads
        self.folder = root / "skills"  # one folder per skill
        self._starters = root / ".claude-plugin" / "starters.json"  # the ones copied in
        if not (root / ".claude-plugin" / "plugin.json").exists():
            self._create()
        self._add_new_starters()

    def _create(self) -> None:
        (self.root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        manifest = {"name": "alfred", "description": "Skills Alfred has learned."}
        (self.root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2))
        self.folder.mkdir(parents=True, exist_ok=True)
        self._starters.write_text("[]")
        self._git("init", "-q")

    def _add_new_starters(self) -> None:
        copied = set(json.loads(self._starters.read_text())) if self._starters.exists() else set()
        new = [s for s in sorted(STARTER_SKILLS.glob("*/SKILL.md")) if s.parent.name not in copied]
        for skill in new:
            if not (self.folder / skill.parent.name).exists():
                shutil.copytree(skill.parent, self.folder / skill.parent.name)
            copied.add(skill.parent.name)
        if new:
            self._starters.write_text(json.dumps(sorted(copied)))
            self.commit("Starting skills: " + ", ".join(s.parent.name for s in new))

    def list(self) -> list[dict]:
        """Every skill: its name and when to use it."""
        found = []
        for file in sorted(self.folder.glob("*/SKILL.md")):
            meta = frontmatter(file.read_text(encoding="utf-8", errors="replace"))
            found.append(
                {
                    "name": meta.get("name", file.parent.name),
                    "use_when": meta.get("description", ""),
                }
            )
        return found

    def remove(self, name: str) -> bool:
        folder = self.folder / name
        if not (folder / "SKILL.md").is_file() or folder.parent != self.folder:
            return False
        shutil.rmtree(folder)
        self.commit(f"Forget the {name} skill")
        return True

    def commit(self, message: str) -> bool:
        """Commit whatever changed in the skills. True if anything did."""
        self._git("add", "-A")
        if not self._git("status", "--porcelain"):
            return False
        self._git("commit", "-q", "-m", message)
        logger.info(f"Skills: {message}")
        return True

    def _git(self, *args: str) -> str:
        done = subprocess.run(
            [*GIT, *args], cwd=self.root, capture_output=True, text=True, timeout=30
        )
        return done.stdout.strip()


def frontmatter(text: str) -> dict[str, str]:
    """The `key: value` lines between the opening `---` lines of a SKILL.md."""
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    pairs = (line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    return {key.strip(): value.strip().strip("\"'") for key, value in pairs}

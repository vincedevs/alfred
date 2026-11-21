"""What Alfred remembers.

- Facts about you, in the database: each with when it was learned, where it came from
  (you said "remember", or it was learned from a conversation) and when it was last used.
  The newest go into every prompt; recall finds the rest.
- Lessons the agent saved from its own work ("in alfred, run uv run pytest"), for one
  project or everywhere. They reach the agent's instructions, not the voice's.
- History: task results and a short summary of each conversation, searchable by words.

After each conversation, learn() asks the agent's model for new lasting facts and a short
summary. `uv run alfred-memory` lists, adds, forgets and exports facts.
"""

import argparse
import json
import re
import sys

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from loguru import logger

from alfred.config import Config, ConfigError, load_config, load_prompt
from alfred.db import Database, Fact
from alfred.health import Health, claude_problem

FACTS_IN_PROMPTS = 30  # newest facts every prompt gets; recall finds the older ones
LESSONS_IN_PROMPTS = 20  # newest lessons for the project the agent works in
TOO_SHORT_TO_LEARN = 8  # words you said: "thanks, Alfred" isn't worth a model call
COMMON_WORDS = set(
    "the a an and or of to in on at for is are was were it my me i you your what did do "
    "about with that this from have has had be been find found tell".split()
)


def words_in(text: str) -> list[str]:
    """The words worth searching for: no common words, no punctuation."""
    found = re.findall(r"[a-z0-9]+", text.lower())
    return [word for word in found if word not in COMMON_WORDS and len(word) > 1]


class Memory:
    def __init__(self, db: Database, config: Config, health: Health | None = None):
        self._db, self._config, self._health = db, config, health

    def facts(self) -> list[Fact]:
        return self._db.facts()

    def remember(self, text: str, source: str = "you", project: str | None = None) -> bool:
        """Add a fact (or a lesson) unless it's already known. True if it was new."""
        text = text.strip().rstrip(".") + "."
        if text.lower() in (fact.text.lower() for fact in self.facts()):
            return False
        self._db.add_fact(text, source, project)
        logger.info(f"Remembered ({source}{', ' + project if project else ''}): {text}")
        return True

    def about_user(self) -> list[Fact]:
        return [fact for fact in self.facts() if fact.source != "lesson"]

    def lessons_text(self, project: str) -> str:
        """The newest lessons for this project and for everywhere, for the agent."""
        lessons = [
            f for f in self.facts() if f.source == "lesson" and f.project in (None, project)
        ][-LESSONS_IN_PROMPTS:]
        if not lessons:
            return ""
        lines = "\n".join(f"- {lesson.text}" for lesson in lessons)
        return f"\n\n## Lessons from earlier work (follow them)\n{lines}"

    def forget(self, about: str) -> list[str]:
        """Remove the facts and history about something. Returns the facts removed."""
        wanted = set(words_in(about))
        if not wanted:
            return []
        gone = [f for f in self.facts() if len(wanted & set(words_in(f.text))) / len(wanted) >= 0.5]
        self._db.delete_facts([fact.id for fact in gone])
        entries = self._db.delete_history(sorted(wanted))
        logger.info(f"Forgot {len(gone)} fact(s) and {entries} history entries about {about!r}")
        return [fact.text for fact in gone]

    def recall(self, about: str) -> dict:
        """Facts and lessons sharing a word with `about` (if none do, every fact about the
        user), and history."""
        wanted = set(words_in(about))
        matching = [f for f in self.facts() if wanted & set(words_in(f.text))]
        matching = matching or self.about_user()
        self._db.mark_facts_used([fact.id for fact in matching])
        found = self._db.search_history(sorted(wanted))
        history = [f"({self._config.local(when):%a %d %b %Y}) {text}" for text, when in found]
        return {"facts": [fact.text for fact in matching], "history": history}

    def prompt_text(self) -> str:
        """The newest facts, as a section for prompts (empty when there are none)."""
        facts = self.about_user()
        if not facts:
            return ""
        lines = "\n".join(f"- {fact.text}" for fact in facts[-FACTS_IN_PROMPTS:])
        older = len(facts) - FACTS_IN_PROMPTS
        more = f"\n({older} older facts: recall finds them.)" if older > 0 else ""
        return f"\n\n## What you know about the user\n{lines}{more}"

    async def learn(self, transcript: list[str]) -> None:
        """Keep what's worth keeping from a conversation. Never raises."""
        yours = [line for line in transcript if line.startswith("You:")]
        if sum(len(line.split()) - 1 for line in yours) < TOO_SHORT_TO_LEARN:
            return
        try:
            notes = await self._ask("\n".join(transcript)[-12000:])
        except Exception as error:  # memory is a nice-to-have: never break Alfred over it
            logger.warning(f"Couldn't learn from the conversation: {error}")
            if self._health and (problem := claude_problem(str(error))):
                self._health.failed("Claude", problem)
            return
        if self._health:
            self._health.ok("Claude")
        for fact in notes.get("new_facts", []):
            self.remember(str(fact), source="learned")
        if summary := str(notes.get("summary", "")).strip():
            self._db.add_history("conversation", summary)

    async def _ask(self, transcript: str) -> dict:
        options = ClaudeAgentOptions(
            model=self._config.agent_model,
            system_prompt=load_prompt("memory"),
            tools=[],
            max_turns=1,
            setting_sources=[],
            cwd=self._config.data_dir,
        )
        known = "\n".join(f"- {fact.text}" for fact in self.about_user()) or "(nothing yet)"
        answer = ""
        async for message in query(
            prompt=f"Already known:\n{known}\n\nConversation:\n{transcript}", options=options
        ):
            if isinstance(message, ResultMessage):
                answer = message.result or ""
        return json.loads(answer[answer.find("{") : answer.rfind("}") + 1] or "{}")


def main() -> None:
    """`uv run alfred-memory [list | add TEXT | forget ID | export]`"""
    parser = argparse.ArgumentParser(prog="alfred-memory", description="What Alfred knows.")
    parser.add_argument(
        "action", nargs="?", default="list", choices=["list", "add", "forget", "export"]
    )
    parser.add_argument("value", nargs="?", help="the fact to add, or the number to forget")
    args = parser.parse_args()
    try:
        config = load_config()
    except ConfigError as error:
        sys.exit(f"Config error: {error}")
    db = Database(config.database_path)
    memory = Memory(db, config)
    if args.action == "add" and args.value:
        print("Added." if memory.remember(args.value) else "Already known.")
    elif args.action == "forget" and args.value and args.value.isdigit():
        db.delete_facts([int(args.value)])
        print("Forgotten.")
    elif args.action == "export":
        print(json.dumps([fact.__dict__ for fact in memory.facts()], indent=2))
    else:
        for fact in memory.facts():
            learned = f"{config.local(fact.created_at):%d %b %Y}"
            where = f" in {fact.project}" if fact.project else ""
            print(f"{fact.id:>4}  {fact.text}  ({fact.source}{where}, {learned})")
    db.close()

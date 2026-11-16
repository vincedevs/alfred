"""Memory: facts in the database, recalled and forgotten by voice, learned after talks."""

import asyncio
import sqlite3

from alfred import memory as memory_module
from alfred.config import Config
from alfred.db import Database
from alfred.memory import FACTS_IN_PROMPTS, Memory, words_in


def make(tmp_path):
    config = Config(openai_api_key="sk-test", data_dir=tmp_path)
    db = Database(config.database_path)
    return db, Memory(db, config)


def test_facts_are_kept_once_with_their_source(tmp_path):
    db, memory = make(tmp_path)
    assert memory.remember("Prefers pull requests over direct pushes")
    assert not memory.remember("prefers pull requests over direct pushes.")  # already known
    memory.remember("Works on the portfolio site", source="learned")
    facts = memory.facts()
    assert [f.text for f in facts] == [
        "Prefers pull requests over direct pushes.",
        "Works on the portfolio site.",
    ]
    assert [f.source for f in facts] == ["you", "learned"]
    assert "Prefers pull requests" in memory.prompt_text()


def test_recall_finds_facts_and_history_and_marks_use(tmp_path):
    db, memory = make(tmp_path)
    memory.remember("Deploys the portfolio with Netlify")
    memory.remember("Has a dog called Ace")
    db.add_history("conversation", "Talked about the portfolio's dark mode.")
    found = memory.recall("portfolio")
    assert found["facts"] == ["Deploys the portfolio with Netlify."]
    assert "dark mode" in found["history"][0]
    assert memory.facts()[0].used_at is not None and memory.facts()[1].used_at is None
    assert len(memory.recall("")["facts"]) == 2  # nothing matching: every fact


def test_forget_removes_facts_and_history(tmp_path):
    db, memory = make(tmp_path)
    memory.remember("Has a dog called Ace")
    db.add_history("conversation", "Asked for dog food for Ace.")
    assert memory.forget("the dog Ace") == ["Has a dog called Ace."]
    assert memory.facts() == [] and memory.recall("Ace")["history"] == []


def test_prompts_get_only_the_newest_facts(tmp_path):
    _, memory = make(tmp_path)
    for n in range(FACTS_IN_PROMPTS + 5):
        memory.remember(f"Fact number {n}")
    text = memory.prompt_text()
    assert "Fact number 0." not in text and f"Fact number {FACTS_IN_PROMPTS + 4}." in text
    assert "5 older facts" in text


def test_learning_keeps_new_facts_and_a_summary(tmp_path, monkeypatch):
    db, memory = make(tmp_path)

    async def notes(self, transcript):
        return {"new_facts": ["Uses uv for Python"], "summary": "Set up the budget app."}

    monkeypatch.setattr(Memory, "_ask", notes)
    talk = ["You: please set up the budget app with uv for me, the usual way", "Alfred: Done."]
    asyncio.run(memory.learn(talk))
    assert memory.facts()[0].text == "Uses uv for Python." and memory.facts()[0].source == "learned"
    assert "budget app" in memory.recall("budget")["history"][0]
    asyncio.run(memory.learn(["You: thanks"]))  # too short to be worth it: nothing asked


def test_an_old_database_is_moved_aside(tmp_path):
    old = tmp_path / "alfred.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE reminders (id INTEGER)")
    conn.close()
    db = Database(old)
    assert (tmp_path / "alfred-old.db").exists()
    assert db.facts() == []


def test_words_worth_searching():
    assert words_in("What did you find about the Portfolio?") == ["portfolio"]
    assert memory_module.COMMON_WORDS  # used by words_in


def test_lessons_reach_the_agent_for_their_project_only(tmp_path):
    _, memory = make(tmp_path)
    memory.remember("Prefers direct pushes")
    memory.remember("In alfred, run uv run pytest", source="lesson", project="alfred")
    memory.remember("Vince wants tests before a commit", source="lesson")  # everywhere
    assert not memory.remember("in alfred, run uv run pytest.", source="lesson")
    alfred = memory.lessons_text("alfred")
    assert "uv run pytest" in alfred and "tests before a commit" in alfred
    portfolio = memory.lessons_text("portfolio")
    assert "uv run pytest" not in portfolio and "tests before a commit" in portfolio
    assert "uv run pytest" not in memory.prompt_text()  # the voice doesn't need them
    assert memory.forget("uv pytest") == ["In alfred, run uv run pytest."]

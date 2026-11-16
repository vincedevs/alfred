"""Alfred's one SQLite database: the agent's tasks and sessions, the questions tasks ask
you, the facts Alfred knows about you, reminders, and a searchable history.

A database from an earlier Alfred prototype has a different layout: it is moved aside
to alfred-old.db, untouched, and a new one is started. (VERSION is the database layout's
number, not Alfred's version.)
"""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    project     TEXT    NOT NULL,           -- the project folder it works in
    request     TEXT    NOT NULL,           -- what you asked for
    status      TEXT    NOT NULL,           -- running | done | failed | stopped
    result      TEXT,                       -- the answer, or what went wrong
    created_at  REAL    NOT NULL,
    finished_at REAL,
    told_user   INTEGER NOT NULL DEFAULT 0  -- 1 once Alfred has said the result
);
CREATE TABLE IF NOT EXISTS sessions (
    project     TEXT    PRIMARY KEY,        -- one lasting agent session per project
    session_id  TEXT    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL,           -- the task asking
    kind        TEXT    NOT NULL,           -- approve (yes or no) | clarify (any answer)
    text        TEXT    NOT NULL,           -- what Alfred asks you, word for word
    status      TEXT    NOT NULL,           -- waiting | answered | expired
    answer      TEXT,
    created_at  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY,
    text        TEXT    NOT NULL,           -- "Prefers pull requests over direct pushes."
    source      TEXT    NOT NULL,           -- you | learned (from a talk) | lesson (from work)
    created_at  REAL    NOT NULL,
    used_at     REAL,                       -- last found by recall
    project     TEXT                        -- a lesson's project; NULL: everywhere
);
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY,
    text        TEXT    NOT NULL,           -- "take the pasta off"
    kind        TEXT    NOT NULL,           -- reminder | timer | alarm (these two ring)
    due_at      REAL    NOT NULL,           -- the next time it is due
    status      TEXT    NOT NULL,           -- pending | said | cancelled
    created_at  REAL    NOT NULL,
    repeat      TEXT    NOT NULL DEFAULT '' -- '' | daily | weekdays | weekends | weekly
);
-- What happened, searchable: task results and short conversation summaries.
CREATE VIRTUAL TABLE IF NOT EXISTS history USING fts5(kind, text, created_at UNINDEXED);
"""


@dataclass
class Task:
    id: int
    project: str
    request: str
    status: str
    result: str | None
    created_at: float
    finished_at: float | None
    told_user: bool


@dataclass
class Question:
    id: int
    task_id: int
    kind: str
    text: str
    status: str
    answer: str | None
    created_at: float


@dataclass
class Reminder:
    id: int
    text: str
    kind: str
    due_at: float
    status: str
    created_at: float
    repeat: str = ""


@dataclass
class Fact:
    id: int
    text: str
    source: str
    created_at: float
    used_at: float | None
    project: str | None = None


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        _move_aside_if_old(path)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {VERSION}")
        self._conn.commit()

    def _one(self, sql: str, args=()) -> sqlite3.Row | None:
        return self._conn.execute(sql, args).fetchone()

    def _write(self, sql: str, args=()) -> int:
        cursor = self._conn.execute(sql, args)
        self._conn.commit()
        return cursor.lastrowid

    # Tasks and sessions

    def add_task(self, project: str, request: str) -> int:
        return self._write(
            "INSERT INTO tasks (project, request, status, created_at) VALUES (?, ?, 'running', ?)",
            (project, request, time.time()),
        )

    def finish_task(self, task_id: int, status: str, result: str) -> None:
        self._write(
            "UPDATE tasks SET status = ?, result = ?, finished_at = ? WHERE id = ?",
            (status, result, time.time(), task_id),
        )
        if status == "done":  # findable later: "what did we change in the portfolio?"
            task = self.get_task(task_id)
            self.add_history("task", f"[{task.project}] {task.request}\nResult: {result}")

    def get_task(self, task_id: int) -> Task | None:
        row = self._one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return _task(row) if row else None

    def recent_tasks(self, limit: int = 3) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
        return [_task(row) for row in rows]

    def running_tasks(self) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM tasks WHERE status = 'running' ORDER BY id")
        return [_task(row) for row in rows]

    def untold_results(self) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status != 'running' AND told_user = 0 ORDER BY id"
        )
        return [_task(row) for row in rows]

    def mark_told(self, task_ids: list[int]) -> None:
        self._conn.executemany(
            "UPDATE tasks SET told_user = 1 WHERE id = ?", [(i,) for i in task_ids]
        )
        self._conn.commit()

    def session(self, project: str) -> str | None:
        row = self._one("SELECT session_id FROM sessions WHERE project = ?", (project,))
        return row["session_id"] if row else None

    def set_session(self, project: str, session_id: str | None) -> None:
        if session_id is None:
            self._write("DELETE FROM sessions WHERE project = ?", (project,))
        else:
            self._write(
                "INSERT OR REPLACE INTO sessions (project, session_id, updated_at) "
                "VALUES (?, ?, ?)",
                (project, session_id, time.time()),
            )

    # Questions

    def add_question(self, task_id: int, kind: str, text: str) -> Question:
        question_id = self._write(
            "INSERT INTO questions (task_id, kind, text, status, created_at) "
            "VALUES (?, ?, ?, 'waiting', ?)",
            (task_id, kind, text, time.time()),
        )
        return Question(**dict(self._one("SELECT * FROM questions WHERE id = ?", (question_id,))))

    def close_question(self, question_id: int, status: str, answer: str | None = None) -> None:
        self._write(
            "UPDATE questions SET status = ?, answer = ? WHERE id = ?",
            (status, answer, question_id),
        )

    def waiting_questions(self) -> list[Question]:
        rows = self._conn.execute("SELECT * FROM questions WHERE status = 'waiting' ORDER BY id")
        return [Question(**dict(row)) for row in rows]

    def expire_waiting_questions(self) -> None:
        self._write("UPDATE questions SET status = 'expired' WHERE status = 'waiting'")

    # Facts

    def add_fact(self, text: str, source: str, project: str | None = None) -> int:
        return self._write(
            "INSERT INTO facts (text, source, created_at, project) VALUES (?, ?, ?, ?)",
            (text, source, time.time(), project),
        )

    def facts(self) -> list[Fact]:
        return [Fact(**dict(row)) for row in self._conn.execute("SELECT * FROM facts ORDER BY id")]

    def delete_facts(self, fact_ids: list[int]) -> None:
        self._conn.executemany("DELETE FROM facts WHERE id = ?", [(i,) for i in fact_ids])
        self._conn.commit()

    def mark_facts_used(self, fact_ids: list[int]) -> None:
        now = time.time()
        self._conn.executemany(
            "UPDATE facts SET used_at = ? WHERE id = ?", [(now, i) for i in fact_ids]
        )
        self._conn.commit()

    # Reminders

    def add_reminder(self, text: str, kind: str, due_at: float, repeat: str = "") -> Reminder:
        reminder_id = self._write(
            "INSERT INTO reminders (text, kind, due_at, status, created_at, repeat) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (text, kind, due_at, time.time(), repeat),
        )
        return Reminder(**dict(self._one("SELECT * FROM reminders WHERE id = ?", (reminder_id,))))

    def pending_reminders(self) -> list[Reminder]:
        rows = self._conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' ORDER BY due_at"
        )
        return [Reminder(**dict(row)) for row in rows]

    def cancel_reminder(self, reminder_id: int) -> bool:
        cursor = self._conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (reminder_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def reschedule_reminder(self, reminder_id: int, due_at: float) -> None:
        self._write("UPDATE reminders SET due_at = ? WHERE id = ?", (due_at, reminder_id))

    def reminders_said(self, reminder_ids: list[int]) -> None:
        self._conn.executemany(
            "UPDATE reminders SET status = 'said' WHERE id = ?", [(i,) for i in reminder_ids]
        )
        self._conn.commit()

    # History: full-text search with SQLite's built-in FTS5

    def add_history(self, kind: str, text: str) -> None:
        self._write(
            "INSERT INTO history (kind, text, created_at) VALUES (?, ?, ?)",
            (kind, text, time.time()),
        )

    def search_history(self, words: list[str], limit: int = 5) -> list[tuple[str, float]]:
        """Entries matching any of the words, best first: (text, created_at)."""
        if not words:
            return []
        rows = self._conn.execute(
            "SELECT text, created_at FROM history WHERE history MATCH ? ORDER BY rank LIMIT ?",
            (" OR ".join(f'"{word}"' for word in words), limit),
        )
        return [(row["text"], row["created_at"]) for row in rows]

    def delete_history(self, words: list[str]) -> int:
        """Delete entries containing all of the words. Returns how many."""
        if not words:
            return 0
        query = " AND ".join(f'"{word}"' for word in words)
        cursor = self._conn.execute("DELETE FROM history WHERE history MATCH ?", (query,))
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()


def _move_aside_if_old(path: Path) -> None:
    if not path.exists():
        return
    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[
            0
        ]
    conn.close()
    if version < VERSION and tables:
        old = path.with_name("alfred-old.db")
        path.rename(old)
        logger.warning(f"The database was made by an older Alfred: moved to {old}")


def _task(row: sqlite3.Row) -> Task:
    return Task(**{**dict(row), "told_user": bool(row["told_user"])})

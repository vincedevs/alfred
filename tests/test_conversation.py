"""The conversation's parts: closing when quiet, the delegate's tools, and announcements."""

import asyncio
from zoneinfo import ZoneInfo

from alfred import idle
from alfred.agent import Agent
from alfred.announcer import Announcer, opening_messages
from alfred.apps import Apps
from alfred.audit import AuditLog
from alfred.config import Config
from alfred.db import Database
from alfred.idle import QuietTimer
from alfred.memory import Memory
from alfred.projects import Projects
from alfred.questions import Questions
from alfred.reminders import Reminders
from alfred.skills import Skills
from alfred.tools import delegate_tools


class Fake:
    """Stands in for the pipeline worker and the audio engine."""

    def __init__(self):
        self.played, self.frames = [], []
        self.speaking = False

    def play(self, sound):
        self.played.append(sound)

    async def queue_frame(self, frame):
        self.frames.append(type(frame).__name__)

    async def cancel(self, reason=None):
        self.frames.append("cancel")


def timer(monkeypatch, busy=False):
    monkeypatch.setattr(idle, "WARNING_SECONDS", 0.05)
    monkeypatch.setattr(idle, "GOODBYE_STARTS_WITHIN", 0.05)
    monkeypatch.setattr(idle, "GOODBYE_PAUSE", 0.02)
    monkeypatch.setattr(idle, "LISTEN_EVERY", 0.005)
    fake, closed = Fake(), []
    return fake, closed, QuietTimer(fake, fake, "tone", 25, lambda: busy, lambda: closed.append(1))


def test_quiet_warns_then_closes(monkeypatch):
    async def main():
        fake, closed, quiet = timer(monkeypatch)
        await quiet.on_idle()
        assert fake.played == ["tone"] and fake.frames == []
        await asyncio.sleep(0.1)
        assert fake.frames == ["EndFrame"] and closed

    asyncio.run(main())


def test_speaking_after_the_warning_keeps_it_open(monkeypatch):
    async def main():
        fake, _, quiet = timer(monkeypatch)
        await quiet.on_idle()
        quiet.you_spoke()
        await asyncio.sleep(0.1)
        assert fake.frames == []

    asyncio.run(main())


def test_stays_open_while_the_agent_works(monkeypatch):
    async def main():
        fake, _, quiet = timer(monkeypatch, busy=True)
        await quiet.on_idle()
        assert fake.played == []

    asyncio.run(main())


def test_go_to_sleep_closes_as_soon_as_the_goodbye_ends(monkeypatch):
    async def main():
        fake, closed, quiet = timer(monkeypatch)
        monkeypatch.setattr(idle, "GOODBYE_STARTS_WITHIN", 5)  # not waited for once he spoke
        quiet.sleep_after_goodbye()
        fake.speaking = True
        await asyncio.sleep(0.03)
        fake.speaking = False
        await asyncio.sleep(0.1)
        assert fake.frames == ["cancel"] and closed

    asyncio.run(main())


def test_go_to_sleep_closes_when_the_goodbye_turn_ends(monkeypatch):
    async def main():
        fake, closed, quiet = timer(monkeypatch)
        monkeypatch.setattr(idle, "GOODBYE_PAUSE", 5)  # the turn's end is enough
        monkeypatch.setattr(idle, "GOODBYE_STARTS_WITHIN", 5)
        quiet.sleep_after_goodbye()
        fake.speaking = True
        await asyncio.sleep(0.03)
        fake.speaking = False
        quiet.goodbye_said()  # a second goodnight would come next: it isn't waited for
        await asyncio.sleep(0.3)
        assert fake.frames == ["cancel"] and closed

    asyncio.run(main())


def test_go_to_sleep_closes_after_the_goodbye(monkeypatch):
    async def main():
        fake, closed, quiet = timer(monkeypatch)
        quiet.sleep_after_goodbye()
        fake.speaking = True  # "Goodnight, sir."
        await asyncio.sleep(0.08)
        assert fake.frames == []
        fake.speaking = False
        await asyncio.sleep(0.08)
        assert fake.frames == ["cancel"] and closed and fake.played == []  # no tone

    asyncio.run(main())


class Params:
    async def result_callback(self, result):
        self.result = result


def services(tmp_path, run):
    config = Config(openai_api_key="sk-test", data_dir=tmp_path)
    db = Database(config.database_path)
    questions = Questions(db)
    memory = Memory(db, config)
    agent = Agent(
        db,
        config,
        questions,
        AuditLog(config.audit_path),
        Projects(config.projects_dir),
        Skills(config.skills_dir),
        run=run,
    )
    slept = []
    tools = {
        t.__name__: t
        for t in delegate_tools(
            agent,
            db,
            questions,
            memory,
            Reminders(db, config.tz),
            Apps(agent.projects, lambda port: f"http://host:{port}/"),
            lambda: slept.append(1),
        )
    }
    return db, questions, agent, tools, slept


def test_the_delegate_hands_work_to_the_agent(tmp_path):
    async def run(task_id, project, request, resuming):
        return f"Did it in {project}."

    async def main():
        db, _, _, tools, _ = services(tmp_path, run)
        params = Params()
        await tools["do_task"](params, request="add a dark mode", project="the Portfolio")
        assert params.result["status"] == "done" and params.result["project"] == "portfolio"
        assert db.untold_results() == []  # answered now, not again later
        await tools["task_status"](params)
        assert params.result["tasks"][0]["result"] == "Did it in portfolio."

    asyncio.run(main())


def test_memory_skills_and_sleep_tools(tmp_path):
    async def main():
        _, _, _, tools, slept = services(tmp_path, None)
        params = Params()
        await tools["remember"](params, fact="Prefers direct pushes")
        await tools["recall"](params, about="pushes")
        assert params.result["facts"] == ["Prefers direct pushes."]
        await tools["list_skills"](params)
        names = [skill["name"] for skill in params.result["skills"]]
        assert {"writing-skills", "answering-from-documents"} <= set(names)
        await tools["sleep"](params)
        assert slept == [1]

    asyncio.run(main())


class FakeLLM:
    def __init__(self):
        self.said = []

    def create_task(self, coroutine, name):
        coroutine.close()

    async def send_client_event(self, event):
        pass


def test_finished_work_and_questions_are_announced(tmp_path, monkeypatch):
    async def run(task_id, project, request, resuming):
        await asyncio.sleep(0.01)
        return "Tests pass."

    async def main():
        db, questions, agent, _, _ = services(tmp_path, run)
        announcer = Announcer(FakeLLM(), db, agent, questions, Reminders(db, ZoneInfo("UTC")))
        notes = []
        monkeypatch.setattr(announcer, "tell", notes.append)
        announcer.start()
        announcer.session_open = True
        agent.start("fix the test", "app")
        await asyncio.sleep(0.05)
        assert "Tests pass." in notes[-1] and db.untold_results() == []
        asking = asyncio.create_task(questions.ask(9, "approve", "May I push?"))
        await asyncio.sleep(0.01)
        assert "May I push?" in notes[-1]
        asking.cancel()
        announcer.stop()

    asyncio.run(main())


def test_news_from_between_conversations_opens_the_next(tmp_path):
    db, questions, _, _, _ = services(tmp_path, None)
    reminders = Reminders(db, ZoneInfo("UTC"))
    assert opening_messages(db, questions, reminders) == ([], [], [])
    task_id = db.add_task("app", "run the tests")
    db.finish_task(task_id, "done", "All green.")
    messages, told, said = opening_messages(db, questions, reminders)
    assert "All green." in messages[0]["content"] and told == [task_id]

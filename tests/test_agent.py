"""The agent: tasks in projects, one at a time per project, lasting sessions, and the
options it runs with (never with Claude itself)."""

import asyncio

from alfred import agent as agent_module
from alfred.agent import Agent, agent_environment
from alfred.audit import AuditLog
from alfred.config import Config
from alfred.db import Database
from alfred.memory import Memory
from alfred.projects import Projects
from alfred.questions import Questions
from alfred.skills import Skills


def make(tmp_path, run):
    config = Config(openai_api_key="sk-test", data_dir=tmp_path, github_token="github_pat_x")
    db = Database(config.database_path)
    questions = Questions(db)
    agent = Agent(
        db,
        config,
        questions,
        AuditLog(config.audit_path),
        Projects(config.projects_dir),
        Skills(config.skills_dir),
        about_user=lambda: "\n\n## What you know about the user\n- Likes uv.",
        run=run,
        memory=Memory(db, config),
    )
    return db, agent


def test_a_quick_task_is_answered_and_recorded(tmp_path):
    async def run(task_id, project, request, resuming):
        return f"{project}: {request}"

    async def main():
        db, agent = make(tmp_path, run)
        finished = []
        agent.on_finished.append(finished.append)
        task_id = agent.start("add a dark mode", "portfolio")
        task = await agent.wait(task_id, 1)
        assert task.status == "done" and task.result == "portfolio: add a dark mode"
        assert finished == []  # handed over directly, not announced
        assert "dark mode" in db.search_history(["dark"])[0][0]
        assert not agent.busy

    asyncio.run(main())


def test_tasks_in_one_project_take_turns(tmp_path):
    running, most = [0], [0]

    async def run(task_id, project, request, resuming):
        running[0] += 1
        most[0] = max(most[0], running[0])
        await asyncio.sleep(0.02)
        running[0] -= 1
        return "ok"

    async def main():
        _, agent = make(tmp_path, run)
        ids = [agent.start(f"step {n}", "portfolio") for n in range(3)]
        await asyncio.gather(*(agent.wait(i, 1) for i in ids))
        assert most[0] == 1  # one session, one conversation at a time

    asyncio.run(main())


def test_stopping_and_failing(tmp_path):
    async def run(task_id, project, request, resuming):
        if request == "break":
            raise RuntimeError("tests failed")
        await asyncio.sleep(10)

    async def main():
        db, agent = make(tmp_path, run)
        finished = []
        agent.on_finished.append(finished.append)
        slow = agent.start("long job", "general")
        await asyncio.sleep(0.01)
        assert await agent.stop(slow)
        broken = agent.start("break", "other")
        await asyncio.sleep(0.05)
        assert [t.status for t in finished] == ["stopped", "failed"]
        assert "tests failed" in db.get_task(broken).result

    asyncio.run(main())


def test_unfinished_tasks_carry_on_after_a_restart(tmp_path):
    seen = []

    async def run(task_id, project, request, resuming):
        seen.append((request, resuming))
        return "done"

    async def main():
        db, agent = make(tmp_path, run)
        db.add_task("portfolio", "half-done work")  # left running by a crash
        assert agent.resume_unfinished() == 1
        await asyncio.sleep(0.05)
        assert seen == [("half-done work", True)]

    asyncio.run(main())


def test_a_session_that_cant_be_resumed_starts_anew(tmp_path, monkeypatch):
    calls = []

    async def fake_query(self, task_id, project, prompt, session):
        calls.append(session)
        if session:
            raise RuntimeError("No conversation found")
        return "fresh"

    monkeypatch.setattr(Agent, "_query", fake_query)

    async def main():
        db, agent = make(tmp_path, None)
        db.set_session("portfolio", "old-session")
        task_id = agent.start("carry on", "portfolio")
        task = await agent.wait(task_id, 1)
        assert task.result == "fresh" and calls == ["old-session", None]
        assert db.session("portfolio") is None

    asyncio.run(main())


def test_the_options_the_agent_runs_with(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "sandbox_installed", lambda: True)
    _, agent = make(tmp_path, None)
    folder = agent.projects.path("portfolio")
    (folder / "CLAUDE.md").write_text("Run tests with `npm test`.")
    agent._db.set_session("portfolio", "abc")
    agent._memory.remember("Run npm ci before the tests", source="lesson", project="portfolio")
    options = agent._options(1, "portfolio", "abc")
    assert options.cwd == folder and options.resume == "abc"
    assert options.setting_sources == []  # never a repository's own hooks
    assert options.skills == "all" and options.plugins[0]["path"] == str(agent.skills.root)
    assert {"Bash", "Skill", "TaskStop"} <= set(options.tools)
    assert set(options.mcp_servers) == {"alfred"}  # ask_user and save_lesson
    assert options.max_budget_usd is None  # no budget, only a turn limit
    for text in ("npm test", "Likes uv", "npm ci before", "Project: portfolio"):
        assert text in options.system_prompt
    assert "github.com" in options.sandbox["network"]["allowedDomains"]
    assert options.env["GH_TOKEN"] == "github_pat_x"


def test_git_is_signed_in_and_ignores_caches(tmp_path):
    env = agent_environment(tmp_path, "tok", tmp_path / "gitignore")
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(2)}
    assert pairs["core.excludesFile"] == str(tmp_path / "gitignore")
    assert pairs["credential.https://github.com.helper"] == "!gh auth git-credential"
    assert "GH_TOKEN" not in agent_environment(tmp_path, None, tmp_path / "gitignore")

"""Apps Alfred runs for you to try: started in the project, listed, stopped, kept apart
from his keys."""

import asyncio
import sys

from alfred import apps as apps_module
from alfred.apps import Apps, app_environment
from alfred.projects import Projects

SERVER = f"{sys.executable} -m http.server $PORT --bind 127.0.0.1"


def make(tmp_path):
    return Apps(Projects(tmp_path), lambda port: f"http://host:{port}/")


def test_an_app_runs_until_stopped(tmp_path):
    async def main():
        apps = make(tmp_path)
        started = await apps.start("game", SERVER)
        assert started["status"] == "running" and "note" not in started  # it answers
        assert started["address"].startswith("http://host:81")
        assert [a["project"] for a in apps.list()] == ["game"]
        assert (await apps.restart("game"))["status"] == "running"
        assert await apps.stop("game") and apps.list() == []
        assert not await apps.stop("game")

    asyncio.run(main())


def test_a_crashing_app_shows_its_output(tmp_path):
    async def main():
        apps = make(tmp_path)
        crashed = await apps.start("app", "echo 'ModuleNotFoundError: flask'; exit 1")
        assert crashed["status"].startswith("stopped") and "flask" in crashed["last_output"]
        assert crashed["note"]  # never answered
        assert (tmp_path / "app" / ".alfred" / "app.log").exists()  # the agent can read it

    asyncio.run(main())


def test_only_a_few_at_once_and_ports_in_range(tmp_path, monkeypatch):
    async def main():
        monkeypatch.setattr(apps_module, "MAX_APPS", 1)
        apps = make(tmp_path)
        await apps.start("one", SERVER)
        assert "already running" in (await apps.start("two", SERVER))["error"]
        assert "port" in (await apps.start("one", SERVER, port=3000))["error"]
        assert apps.list()[0]["status"] == "running"  # a bad request leaves it running
        await apps.stop_all()
        assert apps.list() == []

    asyncio.run(main())


def test_apps_get_no_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("ALFRED_GITHUB_TOKEN", "github_pat_secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    env = app_environment(8123)
    assert "secret" not in " ".join(env.values())
    assert env["PORT"] == "8123" and env["HOST"] == "0.0.0.0"

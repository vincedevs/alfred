"""The three rules: free inside the project, ask before public or irreversible actions,
never secrets. And the spoken yes they wait for."""

import asyncio
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alfred.audit import AuditLog
from alfred.config import PROJECT_ROOT
from alfred.db import Database
from alfred.permissions import Guard, decide
from alfred.questions import Questions


@pytest.fixture
def places(tmp_path):
    project, skills = tmp_path / "projects" / "app", tmp_path / "skills"
    for folder in (project, skills / "skills"):
        folder.mkdir(parents=True)
    return project, skills


def action(places, tool, **tool_input):
    return decide(tool, tool_input, *places).action


def test_inside_the_project_is_free(places):
    project = places[0]
    assert action(places, "Write", file_path=str(project / "src/app.py")) == "allow"
    assert action(places, "Edit", file_path="README.md") == "allow"  # relative: in the project
    assert action(places, "Write", file_path=str(project / ".alfred/pages/r.html")) == "allow"
    assert action(places, "WebFetch", url="https://example.com") == "allow"
    assert action(places, "Skill", skill="writing-skills") == "allow"


@pytest.mark.parametrize(
    "command",
    ["uv run pytest -q", "git status", "git commit -m 'Add dark mode'", "git checkout -b x", "ls"],
)
def test_local_commands_are_free(places, command):
    assert action(places, "Bash", command=command) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "gh pr create --fill",
        "gh pr merge 3 --squash",
        "gh repo create vincedevs/notes --public",
        "gh api -X DELETE repos/vincedevs/x",
        "rm -rf build",
        "git reset --hard HEAD~2",
        "apt install ffmpeg",
        "npm publish",
        "curl -X POST https://example.com/hook -d hi",
    ],
)
def test_public_or_irreversible_commands_ask(places, command):
    assert action(places, "Bash", command=command) == "ask"


@pytest.mark.parametrize(
    "command",
    ["sudo reboot", "cat ~/.ssh/id_ed25519", f"cat {PROJECT_ROOT}/.env", "cp alfred.db /tmp"],
)
def test_secrets_and_admin_are_never(places, command):
    assert action(places, "Bash", command=command) == "deny"


def test_the_sandbox_cant_be_left(places):
    assert action(places, "Bash", command="ls", dangerouslyDisableSandbox=True) == "deny"


def test_saving_a_skill_asks_reading_one_is_free(places):
    skill = places[1] / "skills" / "deploy" / "SKILL.md"
    decision = decide("Write", {"file_path": str(skill)}, *places)
    assert decision.action == "ask" and "deploy skill" in decision.reason
    assert action(places, "Read", file_path=str(skill)) == "allow"


def test_secrets_and_alfreds_own_files_are_off_limits(places, tmp_path):
    assert action(places, "Read", file_path=str(PROJECT_ROOT / ".env")) == "deny"
    assert action(places, "Read", file_path=str(tmp_path / "alfred.db")) == "deny"
    assert action(places, "Read", file_path="/home/me/.ssh/id_rsa") == "deny"
    assert action(places, "Write", file_path="/etc/hosts") == "deny"  # writing outside
    assert action(places, "Glob", pattern="/home/**") == "deny"


def test_asking_waits_for_a_spoken_yes_and_logs_it(places, tmp_path):
    async def main():
        questions = Questions(Database(tmp_path / "alfred.db"))
        audit = tmp_path / "audit.jsonl"
        guard = Guard(questions, AuditLog(audit), places[1], ZoneInfo("UTC"))
        check = asyncio.create_task(
            guard.check(
                7, places[0], "Bash", {"command": "git push origin main", "description": "Ship it."}
            )
        )
        await asyncio.sleep(0.01)  # in plain words: what kind (decided here) and what for
        assert questions.waiting()[0].text == "May I push to GitHub, to ship it?"
        questions.answer(7, "yes")
        assert (await check)[0] is True

        no = asyncio.create_task(guard.check(7, places[0], "Bash", {"command": "rm -rf dist"}))
        await asyncio.sleep(0.01)
        questions.answer(7, "no")
        allowed, told = await no
        assert not allowed and "said no" in told
        again = await guard.check(7, places[0], "Bash", {"command": "rm -rf dist"})
        assert again[0] is False  # not asked twice
        outcomes = [json.loads(line)["outcome"] for line in audit.read_text().splitlines()]
        assert outcomes == ["approved", "declined", "declined before"]

    asyncio.run(main())


def test_always_allows_that_kind_in_that_project_today(places, tmp_path):
    async def main():
        questions = Questions(Database(tmp_path / "alfred.db"))
        audit = tmp_path / "audit.jsonl"
        guard = Guard(questions, AuditLog(audit), places[1], ZoneInfo("UTC"))
        push = {"command": "git push origin main"}
        first = asyncio.create_task(guard.check(7, places[0], "Bash", push))
        await asyncio.sleep(0.01)
        questions.answer(7, "Always.")
        assert (await first)[0] is True
        assert (await guard.check(8, places[0], "Bash", push))[0] is True  # not asked
        assert questions.waiting() == []

        force = asyncio.create_task(
            guard.check(8, places[0], "Bash", {"command": "git push --force origin main"})
        )
        await asyncio.sleep(0.01)
        assert "force-push" in questions.waiting()[0].text  # a kind of its own: still asked
        questions.answer(8, "no")
        assert (await force)[0] is False
        other = asyncio.create_task(guard.check(9, tmp_path, "Bash", push))
        await asyncio.sleep(0.01)
        assert questions.waiting()  # another project: still asked
        questions.answer(9, "no")
        await other
        outcomes = [json.loads(line)["outcome"] for line in audit.read_text().splitlines()]
        assert outcomes[:2] == ["approved for today", "approved (standing)"]

    asyncio.run(main())


def test_kinds_of_action():
    def kind(command):
        return decide("Bash", {"command": command}, Path("/p"), Path("/s")).kind

    assert kind("git push -f origin main") == "force-push"
    assert kind("git push -u origin fix-login") == "push"
    assert kind("rm -rf build") == "delete"
    assert kind("gh pr create --fill") == "github"
    assert kind("sudo apt install ffmpeg") == ""  # never: no kind to allow


def test_starting_an_app_is_asked_first(places):
    decision = decide("mcp__alfred__serve_app", {"command": "npm run dev", "port": 0}, *places)
    assert decision.action == "ask" and decision.detail == "npm run dev"  # shown on screen
    assert decision.kind == "app"  # "always" works for it


def test_the_library_is_read_only_and_its_tools_are_free(places, tmp_path):
    library = tmp_path / "library"
    (library / "files").mkdir(parents=True)
    sheet = str(library / "files" / "abc.xlsx")
    assert decide("Read", {"file_path": sheet}, *places, library).action == "allow"
    assert decide("Write", {"file_path": sheet}, *places, library).action == "deny"
    for name in ("list_documents", "search_documents", "read_document", "cite_answer"):
        assert decide(f"mcp__alfred__{name}", {}, *places).action == "allow"

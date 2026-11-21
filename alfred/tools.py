"""The tools GPT-Live's delegate calls (besides web search): hand work to the agent, check
and answer it, memory, reminders, apps, skills, a self-check, and (once connected) music,
calendar and email.

Each tool is a plain async function: Pipecat reads its name, docstring and arguments to
describe it to the model, and runs it here when the model calls it.
"""

import time

import httpx
from pipecat.services.llm_service import FunctionCallParams

from alfred import display
from alfred.agent import Agent, check_answer
from alfred.apps import Apps
from alfred.db import Database, Task
from alfred.google import Google, GoogleError
from alfred.health import Health
from alfred.library import Library
from alfred.memory import Memory
from alfred.music import Music, MusicError
from alfred.questions import Questions
from alfred.reminders import Reminders

QUICK_ANSWER_SECONDS = 12  # work done within this is answered straight away


def delegate_tools(
    agent: Agent,
    db: Database,
    questions: Questions,
    memory: Memory,
    reminders: Reminders,
    apps: Apps,
    library: Library | None = None,
    health: Health | None = None,
    music: Music | None = None,
    google: Google | None = None,
) -> list:
    async def do_task(params: FunctionCallParams, request: str, project: str = ""):
        """Hand work to the agent: anything beyond a quick web search. It can read, write
        and run code, use git and GitHub, research, make files and pages, and learn skills.
        Quick tasks return the answer; longer ones keep running and are announced when done.
        Not for reminders, memory, apps, skills, music, calendar or email: their own tools
        are much faster.

        Args:
            request: The task in full: the user's goal, every detail they gave, their latest
                correction. For a follow-up, say what it follows on from.
            project: The project it belongs to, as the user named it ("portfolio"). For
                something new to build (a game, a site, an app), a short name for it
                ("flappy bird"): it gets its own project. Empty for anything else.
        """
        name = agent.projects.resolve(project)
        display.step("🧠", "handed to Claude", route="by Claude")
        task_id = agent.start(request, name)
        task = await agent.wait(task_id, QUICK_ANSWER_SECONDS)
        if task.status == "running":
            asked = any(q.task_id == task_id for q in questions.waiting())
            note = (
                "It has asked the user something; say nothing more."
                if asked
                else ("Still working. Say in a few words that you're on it.")
            )
            await params.result_callback({"task_id": task_id, "project": name, "note": note})
        else:
            db.mark_told([task_id])
            await params.result_callback(describe(task))

    async def task_status(params: FunctionCallParams, task_id: int = 0):
        """How the agent's tasks are going ("how's it going?"), their results, and questions
        they're waiting on. Not for running apps: list_apps.

        Args:
            task_id: The task to check, or 0 for the most recent ones.
        """
        tasks = [db.get_task(task_id)] if task_id else db.recent_tasks(3)
        tasks = [task for task in tasks if task]
        db.mark_told([task.id for task in tasks if task.status != "running"])
        waiting = [{"task_id": q.task_id, "question": q.text} for q in questions.waiting()]
        await params.result_callback({"tasks": [describe(t) for t in tasks], "questions": waiting})

    async def answer_task(params: FunctionCallParams, task_id: int, answer: str):
        """Pass the user's answer to a question a task asked.

        Args:
            task_id: The task that asked.
            answer: For a yes-or-no question, "yes" only if the user clearly said yes,
                "always" for "yes, and don't ask again", otherwise "no". Otherwise the
                user's answer in their own words.
        """
        passed = questions.answer(task_id, answer) is not None
        note = "Passed on. Acknowledge in a word or two." if passed else "No question was waiting."
        await params.result_callback({"task_id": task_id, "note": note})

    async def stop_task(params: FunctionCallParams, task_id: int):
        """Stop a task that is still running ("stop that").

        Args:
            task_id: The task to stop.
        """
        stopped = await agent.stop(task_id)
        db.mark_told([task_id])
        await params.result_callback({"task_id": task_id, "stopped": stopped})

    async def remember(params: FunctionCallParams, fact: str):
        """Remember a lasting fact about the user ("prefers pull requests"), when they tell
        you one. Not for questions about what you know: recall.

        Args:
            fact: The fact, as a short sentence.
        """
        await params.result_callback({"remembered": memory.remember(fact) or "already known"})

    async def recall(params: FunctionCallParams, about: str = ""):
        """What Alfred knows about the user and what happened before ("what do you know
        about…?", "what did we do…?"): facts, past task results and conversation summaries.
        Not for the user's files: search_files.

        Args:
            about: A few words on what to look for; empty for every fact.
        """
        await params.result_callback(memory.recall(about))

    async def forget(params: FunctionCallParams, about: str):
        """Forget facts and history about something, when the user asks.

        Args:
            about: What to forget, in a few words.
        """
        await params.result_callback({"forgotten": memory.forget(about)})

    async def remind(
        params: FunctionCallParams,
        text: str,
        kind: str,
        at: str = "",
        in_minutes: float = 0,
        repeat: str = "",
    ):
        """Set a reminder, timer or alarm: Alfred speaks up when it is due (timers and
        alarms also ring). Not for work to do now: do_task.

        Args:
            text: What it is for, in a few words ("take the pasta off").
            kind: "reminder", "timer" or "alarm".
            at: The local date and time it is due, as 2026-11-17T07:00. Use for a clock time.
            in_minutes: Minutes from now, for "in 10 minutes" and every timer.
            repeat: For "every…": "daily", "weekdays", "weekends" or "weekly" (the same
                day each week, from `at`). Empty for once.
        """
        try:
            reminder = reminders.add(text, kind, at, in_minutes, repeat)
        except ValueError as error:
            await params.result_callback({"error": str(error)})
            return
        await params.result_callback({"set": reminders.describe(reminder)})

    async def list_reminders(params: FunctionCallParams):
        """The reminders, timers and alarms still to come, soonest first."""
        await params.result_callback(
            {"upcoming": [reminders.describe(r) for r in reminders.upcoming()]}
        )

    async def cancel_reminder(params: FunctionCallParams, reminder_id: int):
        """Cancel a reminder, timer or alarm (a repeating one stops for good).

        Args:
            reminder_id: As list_reminders gives it.
        """
        await params.result_callback({"cancelled": reminders.cancel(reminder_id)})

    async def list_apps(params: FunctionCallParams):
        """The apps Alfred is running for the user to try, with their addresses."""
        await params.result_callback({"apps": apps.list()})

    async def stop_app(params: FunctionCallParams, project: str):
        """Stop a project's running app.

        Args:
            project: The project, as list_apps gives it.
        """
        await params.result_callback({"stopped": await apps.stop(project)})

    async def restart_app(params: FunctionCallParams, project: str):
        """Restart a project's app (after a change, or if it stopped).

        Args:
            project: The project, as list_apps gives it.
        """
        await params.result_callback(await apps.restart(project))

    async def list_documents(params: FunctionCallParams):
        """The files in the user's library (uploaded on the apps page), and whether each is
        ready. Questions about what they say go to do_task."""
        docs = [d.describe(with_file=False) for d in library.documents()] if library else []
        await params.result_callback({"documents": docs})

    async def search_files(params: FunctionCallParams, query: str):
        """Search the user's files, fast: the best passages for a question of fact, each
        with its document and location.

        Args:
            query: The question's key words.
        """
        started = time.monotonic()
        found = (await library.search(query))[:5] if library else []
        display.step("🔎", f"searched your files · {time.monotonic() - started:.1f} s")
        for hit in found[:1]:
            display.step("📄", f"{hit['document']} · {hit['location']}")
        waiting = library.waiting_text() if library else ""
        await params.result_callback(
            {"passages": found} | ({"not yet read": waiting} if waiting else {})
        )

    async def check_answer_from_files(
        params: FunctionCallParams, answer: str, citations: list[dict]
    ):
        """Check an answer from the user's files before saying it: for every claim, the
        document, location and an exact quote, as search_files gave them. Say the answer
        only once it is accepted.

        Args:
            answer: The answer, as you'll say it.
            citations: For every claim: {"document": ..., "location": ..., "quote": ...}.
        """
        pages = agent.projects.path("general") / ".alfred" / "pages"
        args = {"answer": answer, "citations": citations}
        await params.result_callback(await check_answer(library, "general", pages, args, {}))

    async def read_large_scan(params: FunctionCallParams, document: str):
        """The user said yes to reading a file with many scanned pages (it uses their Claude
        plan): it goes back in the queue.

        Args:
            document: The file's name, as list_documents gives it.
        """
        doc = library.approve(document) if library else None
        result = {"reading": doc.name} if doc else {"error": "no file is waiting for a yes"}
        await params.result_callback(result)

    async def list_skills(params: FunctionCallParams):
        """The skills Alfred has learned, and when each is used."""
        await params.result_callback({"skills": agent.skills.list()})

    async def forget_skill(params: FunctionCallParams, name: str):
        """Delete a skill, when the user asks. It stays in the skills' git history.

        Args:
            name: The skill's name, as list_skills gives it.
        """
        await params.result_callback({"forgotten": agent.skills.remove(name)})

    async def self_check(params: FunctionCallParams):
        """Check that everything Alfred needs works (OpenAI, Claude, and music, calendar and
        email if set up), when the user asks if he's OK or what's wrong. Takes seconds.
        Not for how tasks are going: task_status."""
        display.step("🩺", "checking everything")
        result = await health.check(tell=False) if health else {}
        docs = library.documents() if library else []
        ready = sum(d.status == "ready" for d in docs)
        await params.result_callback(
            result
            | {"tasks running": len(db.running_tasks()), "files": f"{ready} of {len(docs)} ready"}
        )

    async def play_music(
        params: FunctionCallParams,
        action: str,
        query: str = "",
        kind: str = "song",
        volume: int = 0,
    ):
        """Spotify: play, pause, skip, go back, set the volume, or say what's playing.

        Args:
            action: play, pause, skip, back, volume or now_playing.
            query: For play: what to play, in the user's words ("Bohemian Rhapsody",
                "lo-fi beats"). Empty: carry on where it stopped.
            kind: For play: song, artist, album or playlist; a mood or genre is a playlist.
            volume: For volume: 0 to 100.
        """
        actions = {
            "play": lambda: music.play(query, kind),
            "pause": music.pause,
            "skip": music.skip,
            "back": lambda: music.skip(back=True),
            "volume": lambda: music.volume(volume),
            "now_playing": music.now_playing,
        }
        display.step("🎵", f"{action} {query}".strip())
        await params.result_callback(await account_call(actions.get(action), "Spotify"))

    async def calendar(params: FunctionCallParams, start: str = "", days: int = 1):
        """What's on the user's Google calendars.

        Args:
            start: The first day, as YYYY-MM-DD; empty for today.
            days: How many days: 1 for that day, 7 for a week.
        """
        display.step("📅", "checked your calendar")
        result = await account_call(lambda: google.events(start, days), "Google")
        await params.result_callback(result if "error" in result else {"events": result})

    async def find_emails(
        params: FunctionCallParams, search: str = "is:unread in:inbox", limit: int = 5
    ):
        """The user's Gmail, read-only: emails matching a Gmail search, newest first, each
        with an id for read_email.

        Args:
            search: A Gmail search: "is:unread in:inbox" (new mail), "from:maria",
                "subject:invoice newer_than:7d".
            limit: How many, at most 10.
        """
        display.step("✉", f"searched your email: {search}")
        result = await account_call(lambda: google.emails(search, limit), "Google")
        await params.result_callback(result if "error" in result else {"emails": result})

    async def read_email(params: FunctionCallParams, email_id: str):
        """One email's text, by the id find_emails gave.

        Args:
            email_id: The email's id.
        """
        await params.result_callback(await account_call(lambda: google.email(email_id), "Google"))

    return [
        do_task, task_status, answer_task, stop_task,
        remember, recall, forget, remind, list_reminders, cancel_reminder,
        list_apps, stop_app, restart_app, list_documents, search_files, check_answer_from_files,
        read_large_scan,
        list_skills, forget_skill, self_check,
        *([play_music] if music else []), *([calendar, find_emails, read_email] if google else []),
    ]  # fmt: skip


def not_set_up(music: Music | None, google: Google | None) -> str:
    """For the delegate: what isn't connected, so a request for it is answered honestly."""
    missing = [
        n for n, on in (("music (Spotify)", music), ("calendar and email", google)) if not on
    ]
    if not missing:
        return ""
    said = " and ".join(missing)
    return f"\n\n## Not set up yet\n{said[0].upper()}{said[1:]}: if asked, say so."


async def account_call(call, service: str):
    """Run a music, calendar or email call; its problem, in words, if it fails."""
    if call is None:
        return {"error": "Unknown action."}
    try:
        return await call()
    except (MusicError, GoogleError, ValueError) as error:
        return {"error": str(error)}
    except httpx.HTTPError:
        return {"error": f"I couldn't reach {service}."}


def describe(task: Task) -> dict:
    return {
        "task_id": task.id,
        "project": task.project,
        "status": task.status,
        "result": task.result,
    }

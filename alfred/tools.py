"""The tools GPT-Live's delegate calls (besides web search): hand work to the agent, check
and answer it, memory, reminders, apps, skills, and going to sleep.

Each tool is a plain async function: Pipecat reads its name, docstring and arguments to
describe it to the model, and runs it here when the model calls it.
"""

from collections.abc import Callable

from pipecat.services.llm_service import FunctionCallParams

from alfred.agent import Agent
from alfred.apps import Apps
from alfred.db import Database, Task
from alfred.memory import Memory
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
    go_to_sleep: Callable[[], None],
) -> list:
    async def do_task(params: FunctionCallParams, request: str, project: str = ""):
        """Hand work to the agent: anything beyond a quick web search. It can read, write
        and run code, use git and GitHub, research, make files and pages, and learn skills.
        Quick tasks return the answer; longer ones keep running and are announced when done.

        Args:
            request: The task in full: the user's goal, every detail they gave, their latest
                correction. For a follow-up, say what it follows on from.
            project: The project it belongs to, as the user named it ("portfolio"). Empty
                for anything not about a project.
        """
        name = agent.projects.resolve(project)
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
        """How the agent's tasks are going, their results, and questions they're waiting on.

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
        """Stop a task that is still running.

        Args:
            task_id: The task to stop.
        """
        stopped = await agent.stop(task_id)
        db.mark_told([task_id])
        await params.result_callback({"task_id": task_id, "stopped": stopped})

    async def remember(params: FunctionCallParams, fact: str):
        """Remember a lasting fact about the user ("prefers pull requests").

        Args:
            fact: The fact, as a short sentence.
        """
        await params.result_callback({"remembered": memory.remember(fact) or "already known"})

    async def recall(params: FunctionCallParams, about: str = ""):
        """What Alfred knows about the user and what happened before: facts, past task
        results and conversation summaries.

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
        alarms also ring).

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

    async def list_skills(params: FunctionCallParams):
        """The skills Alfred has learned, and when each is used."""
        await params.result_callback({"skills": agent.skills.list()})

    async def forget_skill(params: FunctionCallParams, name: str):
        """Delete a skill, when the user asks. It stays in the skills' git history.

        Args:
            name: The skill's name, as list_skills gives it.
        """
        await params.result_callback({"forgotten": agent.skills.remove(name)})

    async def sleep(params: FunctionCallParams):
        """The user told Alfred to go to sleep: the conversation ends after his goodbye."""
        go_to_sleep()
        await params.result_callback(
            {"note": "Say nothing more, unless you haven't said goodnight."}
        )

    return [
        do_task, task_status, answer_task, stop_task,
        remember, recall, forget, remind, list_reminders, cancel_reminder,
        list_apps, stop_app, restart_app, list_skills, forget_skill, sleep,
    ]  # fmt: skip


def describe(task: Task) -> dict:
    return {
        "task_id": task.id,
        "project": task.project,
        "status": task.status,
        "result": task.result,
    }

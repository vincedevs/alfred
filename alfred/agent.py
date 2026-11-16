"""The agent: Claude, through the Claude Agent SDK, doing the work you ask for.

Each request is a task in a project (projects.py). A project keeps one lasting session,
resumed for every new task, so the agent remembers what it did there ("now fix the test
you just broke"). It has general tools only: files, a shell, the web, git, and skills
(skills.py). permissions.py checks every tool call; shell commands also run inside the
operating system's sandbox (Seatbelt on macOS, bubblewrap on Linux), which keeps their
writes inside the project and their network to package sites and GitHub.

One task per project runs at a time (a session is one conversation), and at most
MAX_AT_ONCE in all (each is a Claude Code process of a few hundred MB). Tasks are written
to the database as they run; after a restart, unfinished ones carry on.
"""

import asyncio
import json
import os
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)
from loguru import logger

from alfred.apps import Apps
from alfred.audit import AuditLog
from alfred.config import PROJECT_ROOT, Config, load_prompt
from alfred.db import Database, Task
from alfred.memory import Memory
from alfred.permissions import Guard
from alfred.projects import Projects
from alfred.questions import QUESTION_TIMEOUT, Questions
from alfred.skills import Skills

TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "WebSearch",
    "WebFetch",
    "Skill",
    "TodoWrite",
    "TaskStop",  # stops a command run with run_in_background
]
MAX_AT_ONCE = 2  # enough for a Raspberry Pi's memory
SHELL_SITES = [  # the only sites shell commands reach; web pages go through WebFetch
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
    "github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com",
]  # fmt: skip
RESUME = "Alfred restarted while you were working on this. Carry on and give your final answer."

# run(task_id, project, request, resuming) -> the answer. Tests pass a fake one.
Runner = Callable[[int, str, str, bool], Awaitable[str]]


class Agent:
    def __init__(
        self,
        db: Database,
        config: Config,
        questions: Questions,
        audit: AuditLog,
        projects: Projects,
        skills: Skills,
        about_user: Callable[[], str] = lambda: "",
        run: Runner | None = None,
        memory: Memory | None = None,
        apps: Apps | None = None,
    ):
        self._db, self._config, self._questions, self._audit = db, config, questions, audit
        self.projects, self.skills = projects, skills
        self._about_user, self._memory, self._apps = about_user, memory, apps
        self._run_agent = run or self._run_claude
        self._guard = Guard(questions, audit, skills.root, config.tz)
        self._ignore_file = config.data_dir / "gitignore"  # the caches agent work leaves
        self._ignore_file.parent.mkdir(parents=True, exist_ok=True)
        self._ignore_file.write_text(".cache/\n.alfred/\n")
        self._slots = asyncio.Semaphore(MAX_AT_ONCE)
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._waiting: set[int] = set()
        self._closing = False
        self.on_finished: list[Callable[[Task], None]] = []

    @property
    def busy(self) -> bool:
        return bool(self._tasks)

    def start(self, request: str, project: str) -> int:
        task_id = self._db.add_task(project, request)
        logger.info(f"Task {task_id} started in {project}: {request}")
        self._launch(task_id, project, request, resuming=False)
        return task_id

    def resume_unfinished(self) -> int:
        unfinished = self._db.running_tasks()
        for task in unfinished:
            logger.info(f"Task {task.id} resumed after a restart")
            self._launch(task.id, task.project, task.request, resuming=True)
        return len(unfinished)

    async def wait(self, task_id: int, timeout: float) -> Task | None:
        """Wait up to `timeout` seconds, then return the task either way. A task that ends
        while someone waits for it skips on_finished: its result is handed over directly."""
        if running := self._tasks.get(task_id):
            self._waiting.add(task_id)
            try:
                await asyncio.wait([running], timeout=timeout)
            finally:
                self._waiting.discard(task_id)
        return self._db.get_task(task_id)

    async def stop(self, task_id: int) -> bool:
        running = self._tasks.get(task_id)
        if not running:
            return False
        running.cancel()
        await asyncio.wait([running])
        return True

    def begin_shutdown(self) -> None:
        """Ctrl+C also reaches Claude's process: from now on an error isn't a failure."""
        self._closing = True

    async def close(self) -> None:
        """Interrupt tasks but leave them running in the database, to resume next start."""
        self._closing = True
        for running in self._tasks.values():
            running.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks.values())

    def _launch(self, task_id: int, project: str, request: str, resuming: bool) -> None:
        self._tasks[task_id] = asyncio.create_task(
            self._run(task_id, project, request, resuming), name=f"task-{task_id}"
        )

    async def _run(self, task_id: int, project: str, request: str, resuming: bool) -> None:
        lock = self._project_locks.setdefault(project, asyncio.Lock())
        try:
            async with lock, self._slots:
                result = await self._run_agent(task_id, project, request, resuming)
            status = "done"
        except asyncio.CancelledError:
            if self._closing:
                raise
            status, result = "stopped", "Stopped at your request."
        except Exception as error:
            if self._closing:
                return
            logger.exception(f"Task {task_id} failed")
            status, result = "failed", f"{type(error).__name__}: {error}"
        finally:
            self._tasks.pop(task_id, None)
            self._questions.cancel_for_task(task_id)
        self.skills.commit(f"Skills after task {task_id}: {request[:60]}")
        self._db.finish_task(task_id, status, result)
        logger.info(f"Task {task_id} {status}: {result[:200]}")
        if task_id not in self._waiting:
            for listener in list(self.on_finished):
                listener(self._db.get_task(task_id))

    async def _run_claude(self, task_id: int, project: str, request: str, resuming: bool) -> str:
        session = self._db.session(project)
        try:
            return await self._query(task_id, project, RESUME if resuming else request, session)
        except Exception:
            if not session or self._closing:
                raise
            logger.warning(f"Task {task_id}: couldn't resume the {project} session; starting anew")
            self._db.set_session(project, None)
            return await self._query(task_id, project, request, None)

    async def _query(self, task_id: int, project: str, prompt: str, session: str | None) -> str:
        outcome: ResultMessage | None = None
        async for message in query(prompt=prompt, options=self._options(task_id, project, session)):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                self._db.set_session(project, message.data["session_id"])
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        logger.debug(f"Task {task_id} uses {block.name}: {str(block.input)[:200]}")
            elif isinstance(message, ResultMessage):
                outcome = message
        if outcome is None:
            raise RuntimeError("Claude stopped without a result")
        logger.info(
            f"Task {task_id}: {outcome.num_turns} turns, {outcome.duration_ms / 1000:.0f} s, "
            f"about ${outcome.total_cost_usd or 0:.3f}"
        )
        if outcome.is_error:
            raise RuntimeError(f"{outcome.subtype}: {outcome.errors or outcome.result}")
        return outcome.result or "Done, but no answer was given."

    def _options(self, task_id: int, project: str, session: str | None) -> ClaudeAgentOptions:
        config, folder = self._config, self.projects.path(project)
        guard = HookMatcher(
            hooks=[self._guard.hook_for(task_id, folder)], timeout=QUESTION_TIMEOUT + 60
        )
        prompt = (
            load_prompt("agent")
            + config.context_text()
            + self._about_user()
            + f"\n\n## Where you are\nProject: {project}, folder {folder}. Skills folder: "
            f"{self.skills.folder}. Pages to show the user go in {folder}/.alfred/pages/."
            + (self._memory.lessons_text(project) if self._memory else "")
            + self.projects.instructions(project)
        )
        return ClaudeAgentOptions(
            model=config.agent_model,
            system_prompt=prompt,
            tools=TOOLS if sandbox_installed() else [t for t in TOOLS if t != "Bash"],
            skills="all",
            plugins=[{"type": "local", "path": str(self.skills.root)}],
            setting_sources=[],  # never a cloned repository's own settings or hooks
            mcp_servers={
                "alfred": alfred_server(task_id, project, self._questions, self._memory, self._apps)
            },
            hooks={"PreToolUse": [guard]},
            can_use_tool=refuse_unchecked,
            sandbox={
                "enabled": True,
                "allowUnsandboxedCommands": False,
                "autoAllowBashIfSandboxed": False,  # every command still goes past permissions.py
                "network": {"allowedDomains": SHELL_SITES},
            },
            settings=json.dumps({"permissions": {"deny": secret_reads(config.data_dir)}}),
            env=agent_environment(folder, config.github_token, self._ignore_file),
            cwd=folder,
            max_turns=config.agent_max_turns,
            resume=session,
        )


def alfred_server(
    task_id: int,
    project: str,
    questions: Questions,
    memory: Memory | None,
    apps: Apps | None = None,
):
    """The tools Alfred adds: asking you a question out loud, saving a lesson, and
    starting the project's app so the user can try it."""

    @tool(
        "ask_user",
        "Ask the user one short question out loud and wait for the answer.",
        {"question": str},
    )
    async def ask_user(args: dict) -> dict:
        answer = await questions.ask(task_id, "clarify", args["question"])
        text = f"The user said: {answer}" if answer else "No answer: assume, carry on, and say so."
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "save_lesson",
        "Save one sentence worth knowing next time: a mistake you fixed, a correction from "
        "the user, a project fact you had to work out. everywhere: true if it applies "
        "beyond this project. Never secrets.",
        {"lesson": str, "everywhere": bool},
    )
    async def save_lesson(args: dict) -> dict:
        everywhere = bool(args.get("everywhere"))
        new = memory and memory.remember(
            args["lesson"], source="lesson", project=None if everywhere else project
        )
        return {"content": [{"type": "text", "text": "Saved." if new else "Already known."}]}

    @tool(
        "serve_app",
        "Start this project's app (a web app, game or API server) so the user can try it; "
        "it keeps running after you finish. command: how to start it, listening on $PORT "
        "and $HOST. port: 0 for any free one. Static sites need nothing: the apps page "
        "serves them.",
        {"command": str, "port": int},
    )
    async def serve_app(args: dict) -> dict:
        if not apps:
            return {"content": [{"type": "text", "text": "Apps can't be served here."}]}
        result = await apps.start(project, args["command"], int(args.get("port") or 0))
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return create_sdk_mcp_server("alfred", tools=[ask_user, save_lesson, serve_app])


async def refuse_unchecked(tool_name: str, _input: dict, _context) -> PermissionResultDeny:
    """permissions.py answers first; anything that still asks is refused, not run."""
    logger.warning(f"Refused {tool_name}: it skipped the permission check")
    return PermissionResultDeny(message="Not allowed.")


def sandbox_installed() -> bool:
    """The shell needs the OS sandbox: built into macOS; `bubblewrap socat` on Linux."""
    needed = ["sandbox-exec"] if sys.platform == "darwin" else ["bwrap", "socat"]
    missing = [name for name in needed if not shutil.which(name)]
    if missing:
        logger.warning(f"Sandbox tools missing ({', '.join(missing)}): the agent gets no shell")
    return not missing


def secret_reads(data_dir: Path) -> list[str]:
    """What sandboxed commands may not even read ("//" starts an absolute path)."""
    secrets = ".ssh .aws .gnupg .config/gh Library/Keychains".split()
    return [
        f"Read(/{PROJECT_ROOT}/**)",  # Alfred's own code and .env
        f"Read(/{data_dir / 'alfred.db'})",
        *(f"Read(~/{folder}/**)" for folder in secrets),
    ]


def agent_environment(folder: Path, github_token: str | None, ignore_file: Path) -> dict[str, str]:
    """Caches inside the project (the sandbox can't write elsewhere) and never committed,
    Alfred's own Python off the PATH, and git and gh signed in with the GitHub token."""
    cache = folder / ".cache"
    venv = str(PROJECT_ROOT / ".venv")
    path = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep) if not p.startswith(venv)
    )
    git = {"core.excludesFile": str(ignore_file)}
    env = {
        "PATH": path,
        "XDG_CACHE_HOME": str(cache),
        "UV_CACHE_DIR": str(cache / "uv"),
        "npm_config_cache": str(cache / "npm"),
    }
    if github_token:
        env["GH_TOKEN"] = github_token
        git["credential.https://github.com.helper"] = "!gh auth git-credential"
    env["GIT_CONFIG_COUNT"] = str(len(git))
    for i, (key, value) in enumerate(git.items()):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    return env

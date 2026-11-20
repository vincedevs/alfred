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
import time
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

from alfred import display, webcheck
from alfred.apps import Apps
from alfred.audit import AuditLog
from alfred.config import PROJECT_ROOT, Config, load_prompt
from alfred.db import Database, Task
from alfred.library import Library, answer_page
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
        library: Library | None = None,
    ):
        self._db, self._config, self._questions, self._audit = db, config, questions, audit
        self.projects, self.skills = projects, skills
        self._about_user, self._memory, self._apps = about_user, memory, apps
        self.library = library
        self._run_agent = run or self._run_claude
        self._guard = Guard(
            questions, audit, skills.root, config.tz, library.root if library else None
        )
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
        display.status(tasks=len(self._tasks))

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
            display.status(tasks=len(self._tasks))
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
                        display.tool(block.name, block.input)
            elif isinstance(message, ResultMessage):
                outcome = message
        if outcome is None:
            raise RuntimeError("Claude stopped without a result")
        display.status(cost=outcome.total_cost_usd or 0)
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
        answer = {"read_files": False, "checked": False, "held": 0}  # for the Stop hook
        prompt = (
            load_prompt("agent")
            + config.context_text()
            + self._about_user()
            + f"\n\n## Where you are\nProject: {project}, folder {folder}. Skills folder: "
            f"{self.skills.folder}. Pages to show the user go in {folder}/.alfred/pages/."
            + (self.library.contents_text() if self.library else "")
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
                "alfred": alfred_server(
                    task_id,
                    project,
                    self._questions,
                    self._memory,
                    self._apps,
                    self.library,
                    folder / ".alfred" / "pages",
                    answer,
                )
            },
            hooks={"PreToolUse": [guard], "Stop": [HookMatcher(hooks=[checked_answers(answer)])]},
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
    library: Library | None = None,
    pages: Path | None = None,
    answer: dict | None = None,
):
    """The tools Alfred adds: asking you a question out loud, saving a lesson, starting
    the project's app so the user can try it, and the library's search, reading and
    citation check."""

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

    return create_sdk_mcp_server(
        "alfred",
        tools=[ask_user, save_lesson, serve_app, *library_tools(library, project, pages, answer)],
    )


def checked_answers(answer: dict):
    """The Stop hook: a task that read the user's files is held back once if cite_answer
    hasn't accepted its answer (or heard it isn't in them)."""

    async def before_stopping(_input, _tool_use_id, _context) -> dict:
        if not answer["read_files"] or answer["checked"] or answer["held"]:
            return {}
        answer["held"] += 1
        return {"decision": "block", "reason": UNCHECKED}

    return before_stopping


UNCHECKED = (
    "You read the user's files but gave no checked answer. If your answer states anything "
    "from them, call cite_answer with a citation for every claim (or found: false if they "
    "don't have it) and answer as it says; if it states nothing from them, just finish."
)


def library_tools(
    library: Library | None, project: str, pages: Path | None, answer: dict | None = None
) -> list:
    """The library's tools: list, search, read, and the citation check. `answer` records,
    for the Stop hook, whether the files were read and the answer checked."""
    answer = answer if answer is not None else {"read_files": False, "checked": False}
    fetched: dict = {}  # the web pages read for this task, so each is fetched once

    def reply(value) -> dict:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "list_documents",
        "The files in the user's library, whether each is ready, and its file (to calculate "
        "from, or to look at a page or picture with Read).",
        {},
    )
    async def list_documents(_args: dict) -> dict:
        return reply([d.describe() for d in library.documents()] if library else [])

    @tool(
        "search_documents",
        "Search the user's library by keyword and by meaning; returns passages with their "
        "document and location, best first. Search again with other words when results "
        "are thin. document: limit to one file (name or id), or empty.",
        {"query": str, "document": str},
    )
    async def search_documents(args: dict) -> dict:
        if not library:
            return reply("There is no library here.")
        answer["read_files"] = True
        found = await library.search(args["query"], args.get("document") or "")
        return reply(found or "Nothing found: try other words, synonyms or a shorter query.")

    @tool(
        "read_document",
        "The full text at a location of a document: 'page 12', '12-14', 'slide 4', "
        "'sheet Sales', a section heading, or 'lines 81-160'. Read the whole section before "
        "you answer from it.",
        {"document": str, "location": str},
    )
    async def read_document(args: dict) -> dict:
        if not library:
            return reply("There is no library here.")
        answer["read_files"] = True
        return reply(library.read(args["document"], args["location"]))

    @tool(
        "cite_answer",
        "Check an answer before you give it: every claim's citation, from the user's files "
        "(document, location, an exact quote there) or, for what they don't have, the web "
        "(url and an exact quote from that page). Fix or drop any citation it rejects and "
        "call it again; answer only once all pass. found: false when the files don't have it.",
        {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "found": {"type": "boolean"},
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "document": {"type": "string"},
                            "location": {"type": "string"},
                            "url": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                        "required": ["quote"],
                    },
                },
            },
            "required": ["answer"],
        },
    )
    async def cite_answer(args: dict) -> dict:
        if not library:
            return reply("There is no library here.")
        result = await check_answer(library, project, pages, args, fetched)
        answer["checked"] = bool(result.get("checked") or result.get("noted"))
        return reply(result)

    @tool(
        "read_web_page",
        "The text of a web page, the same text cite_answer checks web quotes against: quote "
        "from this, not from WebFetch (which summarizes).",
        {"url": str},
    )
    async def read_web_page(args: dict) -> dict:
        text = await webcheck.page(args["url"], fetched)
        return reply(f"Couldn't open it: {text}" if isinstance(text, Exception) else text[:24000])

    return [list_documents, search_documents, read_document, cite_answer, read_web_page]


async def check_answer(library: Library, project: str, pages: Path | None, args: dict, fetched):
    """Every citation checked: a file's quote at its location, a web quote on its page (the
    pages fetched together). Accepted: recorded, and the answer page written."""
    citations = list(args.get("citations") or [])
    if args.get("found") is False and not citations:
        waiting = library.waiting_text()
        say = (
            f"Not found so far, but {waiting}: say so, and that it may be in them."
            if waiting
            else "Say it isn't in their files, and what you searched for."
        )
        return {"noted": True, "say": say}
    urls = {c["url"] for c in citations if c.get("url")}
    await asyncio.gather(*(webcheck.page(url, fetched) for url in urls))
    files = [c for c in citations if not c.get("url")]
    checked = dict(zip(map(id, files), library.check(files), strict=True))
    problems = [
        await webcheck.quote_on_page(c["url"], c.get("quote", ""), fetched)
        if c.get("url")
        else checked[id(c)]
        for c in citations
    ]
    failed = [{"citation": i + 1, "problem": p} for i, p in enumerate(problems) if p]
    if failed or not citations:
        return {"checked": False, "fix": failed or "an answer needs citations, or found: false"}
    library.record(project, args["answer"], citations)
    display.quotes(citations)
    result: dict = {"checked": True}
    if pages:
        pages.mkdir(parents=True, exist_ok=True)
        page = pages / f"answer-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**9:09d}.html"
        page.write_text(answer_page(args["answer"], citations, library), encoding="utf-8")
        result["page"] = f"pages/{project}/{page.name} on the apps page"
    say = []
    if urls:
        say.append("Say which facts are from their files and which from the web.")
    if any(library.scanned_at(c.get("document", ""), c.get("location", "")) for c in files):
        say.append("Say 'from the scanned copy' where it is.")
    return result | ({"say": " ".join(say)} if say else {})


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

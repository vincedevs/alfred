"""Apps the agent built, kept running so you can try them: a game, a website, an API.

The agent's own commands end with its task, so Alfred starts an app's server himself, in
the project's folder, and keeps it running: one app per project, at most MAX_APPS at once,
each on a port from PORTS (passed in $PORT) so other devices on your network can open it.
An app stops after MAX_HOURS, when you say so, or when Alfred stops. It runs outside the
agent's sandbox, so starting one is asked first (permissions.py), and it gets no keys.

Its output goes to <project>/.alfred/app.log, where the agent can read it.
"""

import asyncio
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from alfred.config import PROJECT_ROOT
from alfred.projects import Projects

PORTS = range(8100, 8200)
MAX_APPS = 3  # a Raspberry Pi's memory
MAX_HOURS = 4
STARTS_WITHIN = 20  # seconds to start listening before it's reported as not up yet
SECRETS = ("OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GH_TOKEN")


@dataclass
class App:
    project: str
    command: str
    port: int
    process: asyncio.subprocess.Process
    log: Path
    started: float = field(default_factory=time.time)

    @property
    def running(self) -> bool:
        return self.process.returncode is None


class Apps:
    def __init__(self, projects: Projects, address):
        self._projects = projects
        self._address = address  # port -> the URL other devices use (web.address)
        self._apps: dict[str, App] = {}

    async def start(self, project: str, command: str, port: int = 0) -> dict:
        """Start (or restart) the project's app. port 0: pick a free one."""
        if port and port not in PORTS:
            return {"error": f"use a port from {PORTS.start} to {PORTS.stop - 1}, or 0"}
        running = [a for a in self._apps.values() if a.running and a.project != project]
        if len(running) >= MAX_APPS:
            names = ", ".join(a.project for a in running)
            return {"error": f"{MAX_APPS} apps are already running ({names}): stop one first"}
        await self.stop(project)  # a restart, or a new version of its app
        port = port or free_port()
        if port is None:
            return {"error": "no free port"}
        folder = self._projects.path(project)
        log = folder / ".alfred" / "app.log"
        log.parent.mkdir(exist_ok=True)
        with log.open("wb") as output:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=folder,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
                env=app_environment(port),
                start_new_session=True,  # its own process group: stopping it stops its children
            )
        app = self._apps[project] = App(project, command, port, process, log)
        logger.info(f"App {project} started on port {port}: {command}")
        asyncio.get_running_loop().call_later(
            MAX_HOURS * 3600, lambda: asyncio.ensure_future(self._expire(app))
        )
        up = await listening(port, process, STARTS_WITHIN)
        return self.describe(app) | ({} if up else {"note": "not answering yet: see the log"})

    async def stop(self, project: str) -> bool:
        app = self._apps.pop(project, None)
        if not app or not app.running:
            return False
        try:
            os.killpg(app.process.pid, signal.SIGTERM)
            await asyncio.wait_for(app.process.wait(), 5)
        except TimeoutError:
            os.killpg(app.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        logger.info(f"App {project} stopped")
        return True

    async def restart(self, project: str) -> dict:
        app = self._apps.get(project)
        if not app:
            return {"error": f"no app was started for {project}"}
        return await self.start(project, app.command, app.port)

    async def stop_all(self) -> None:
        for project in list(self._apps):
            await self.stop(project)

    def list(self) -> list[dict]:
        return [self.describe(app) for app in self._apps.values()]

    def describe(self, app: App) -> dict:
        status = "running" if app.running else f"stopped (exit code {app.process.returncode})"
        log_tail = "" if app.running else app.log.read_text(errors="replace")[-800:]
        return {
            "project": app.project,
            "address": self._address(app.port),
            "status": status,
            "log": str(app.log),
        } | ({"last_output": log_tail} if log_tail else {})

    async def _expire(self, app: App) -> None:
        if self._apps.get(app.project) is app:
            logger.info(f"App {app.project} ran {MAX_HOURS} h: stopping it")
            await self.stop(app.project)


def app_environment(port: int) -> dict[str, str]:
    """Alfred's environment without his keys or his own Python, plus where to listen."""
    venv = str(PROJECT_ROOT / ".venv")
    env = {k: v for k, v in os.environ.items() if k not in SECRETS and not k.startswith("ALFRED_")}
    env["PATH"] = os.pathsep.join(
        p for p in env.get("PATH", "").split(os.pathsep) if not p.startswith(venv)
    )
    env.pop("VIRTUAL_ENV", None)
    return env | {"PORT": str(port), "HOST": "0.0.0.0"}


def free_port() -> int | None:
    for port in PORTS:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return None


async def listening(port: int, process, seconds: float) -> bool:
    """Wait until something answers on the port (False if the app exits or is slow)."""
    ends = time.monotonic() + seconds
    while time.monotonic() < ends and process.returncode is None:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return True
        except OSError:
            await asyncio.sleep(0.25)
    return False

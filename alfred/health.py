"""Is everything Alfred needs working? Checked at start (in the background, so starting
isn't slower), when you ask "are you OK?", and whenever something fails along the way.

A problem shows in the status bar at once, and Alfred says it once, in plain words: now
if you're talking, or at the start of the next conversation. It's said again only if it
comes back after being fixed.
"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from loguru import logger

from alfred import display
from alfred.config import Config

CHECK_SECONDS = 30


class Health:
    def __init__(self, config: Config):
        self._config = config
        self.problems: dict[str, str] = {}  # service -> what's wrong, in plain words
        self._told: set[str] = set()
        self.on_problem: list[Callable[[str], None]] = []  # the announcer says it now
        self.extra_checks: dict[str, Callable[[], Awaitable[str | None]]] = {}  # Spotify...

    def failed(self, service: str, why: str, tell: bool = True) -> None:
        """tell: False when the answer already says it (the self-check)."""
        if self.problems.get(service) == why:
            if not tell:
                self._told.add(service)
            return
        self.problems[service] = why
        logger.warning(f"{service}: {why}")
        self._show()
        if not tell:
            self._told.add(service)
            return
        self._told.discard(service)
        for listener in list(self.on_problem):
            listener(service)

    def ok(self, service: str) -> None:
        if self.problems.pop(service, None):
            logger.info(f"{service} works again")
            self._told.discard(service)
            self._show()

    def note(self, service: str) -> str:
        """What Alfred says about a problem; it counts as told."""
        self._told.add(service)
        return f"Tell the user briefly that {service} isn't working: {self.problems[service]}"

    def untold(self) -> list[str]:
        return [self.note(s) for s in list(self.problems) if s not in self._told]

    async def check(self, tell: bool = True) -> dict:
        """Check everything now; returns what works and what doesn't."""
        checks = {"OpenAI": self._openai, "Claude": self._claude, **self.extra_checks}
        results = await asyncio.gather(
            *(asyncio.wait_for(c(), CHECK_SECONDS) for c in checks.values()),
            return_exceptions=True,
        )
        for service, result in zip(checks, results, strict=True):
            if isinstance(result, BaseException):
                result = f"its check failed ({type(result).__name__})"
            self.failed(service, result, tell) if result else self.ok(service)
        return {"working": [s for s in checks if s not in self.problems], "problems": self.problems}

    async def _openai(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {self._config.openai_api_key}"},
                )
        except httpx.HTTPError:
            return "I can't reach it: check the internet connection."
        if response.status_code == 401:
            return "it refused the API key: check OPENAI_API_KEY in .env."
        return (
            None if response.is_success else f"it answered with an error ({response.status_code})."
        )

    async def _claude(self) -> str | None:
        options = ClaudeAgentOptions(
            model=self._config.agent_model,
            system_prompt="Reply with OK.",
            tools=[],
            max_turns=1,
            setting_sources=[],
            cwd=self._config.data_dir,
        )
        try:
            async for message in query(prompt="OK?", options=options):
                if isinstance(message, ResultMessage) and message.is_error:
                    error = str(message.errors or message.result)
                    return claude_problem(error) or f"it failed: {error[:120]}"
        except Exception as error:
            return claude_problem(str(error)) or f"it failed: {str(error)[:120]}"
        return None

    def _show(self) -> None:
        display.status(problems=", ".join(self.problems))


def claude_problem(error: str) -> str | None:
    """Claude's error, as Alfred says it; None if it isn't about reaching Claude."""
    text = error.lower()
    signed_out = ("401", "authenticat", "bearer", "oauth", "api key", "/login")
    if any(sign in text for sign in signed_out):
        return "its sign-in was refused: run claude setup-token and put the token in .env."
    if "credit balance" in text:
        return "its account is out of credit."
    if "rate limit" in text or "usage limit" in text or "429" in text:
        return "it hit its usage limit; it should work again later."
    if "connect" in text or "network" in text or "timed out" in text:
        return "I can't reach it: check the internet connection."
    return None

"""Settings, read once at startup from environment variables (and .env)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRET_SETTINGS = ("openai_api_key", "github_token")  # never logged or printed


class ConfigError(Exception):
    """A setting is missing or invalid. The message says how to fix it."""


@dataclass(frozen=True)
class Config:
    openai_api_key: str = field(repr=False)
    live_model: str = "gpt-live-1"  # the voice: hears, thinks and speaks
    delegate_model: str = "gpt-6-luna"  # runs GPT-Live's tool calls and web searches
    voice: str | None = None  # GPT-Live's voice; None lets OpenAI pick
    agent_model: str = "claude-haiku-5-5"  # the agent that does the work
    agent_max_turns: int = 200  # a safety stop for work that goes round in circles
    github_token: str | None = field(default=None, repr=False)  # for git and gh
    wakeword_dir: Path = PROJECT_ROOT / "models"
    wake_threshold: float = 0.5
    idle_seconds: float = 25.0  # quiet this long closes the conversation
    input_device: int | str | None = None  # None: the system default
    output_device: int | str | None = None
    echo_cancellation: bool = True
    log_level: str = "INFO"  # the terminal's; the log file always gets everything
    data_dir: Path = Path.home() / ".alfred"
    home: str = "Manila, Philippines"
    timezone: str = "Asia/Manila"  # a new Raspberry Pi starts on UTC
    web_port: int | None = None  # the apps page on the home network; None: off

    @property
    def database_path(self) -> Path:
        return self.data_dir / "alfred.db"

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def local(self, timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, self.tz)

    def context_text(self) -> str:
        """For every prompt: the time and where the user is."""
        return (
            f"\n\n## Now\n{self.now():%A %d %B %Y, %I:%M %p} where the user is: {self.home} "
            f"({self.timezone}). Assume this place and its units, currency and dates unless "
            "the user says otherwise."
        )


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from `env`, or from the real environment plus .env if `env` is None."""
    if env is None:
        load_dotenv(PROJECT_ROOT / ".env", override=True)  # .env wins over the shell
        env = os.environ

    api_key = env.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in.")

    d = Config(openai_api_key=api_key)
    return Config(
        openai_api_key=api_key,
        live_model=env.get("ALFRED_LIVE_MODEL") or d.live_model,
        delegate_model=env.get("ALFRED_DELEGATE_MODEL") or d.delegate_model,
        voice=env.get("ALFRED_VOICE") or None,
        agent_model=env.get("ALFRED_AGENT_MODEL") or d.agent_model,
        agent_max_turns=int(_number(env, "ALFRED_AGENT_MAX_TURNS", d.agent_max_turns, 1, 10_000)),
        github_token=(env.get("ALFRED_GITHUB_TOKEN") or "").strip() or None,
        wakeword_dir=Path(env.get("ALFRED_WAKEWORD_DIR") or d.wakeword_dir),
        wake_threshold=_number(env, "ALFRED_WAKE_THRESHOLD", d.wake_threshold, 0.0, 1.0),
        idle_seconds=_number(env, "ALFRED_IDLE_SECONDS", d.idle_seconds, 5.0, 600.0),
        input_device=_device(env.get("ALFRED_INPUT_DEVICE")),
        output_device=_device(env.get("ALFRED_OUTPUT_DEVICE")),
        echo_cancellation=_on_off(env, "ALFRED_ECHO_CANCELLATION", d.echo_cancellation),
        log_level=(env.get("ALFRED_LOG_LEVEL") or d.log_level).upper(),
        data_dir=Path(env.get("ALFRED_DATA") or d.data_dir).expanduser(),
        home=env.get("ALFRED_HOME") or d.home,
        timezone=_timezone(env.get("ALFRED_TIMEZONE") or d.timezone),
        web_port=_port(env.get("ALFRED_WEB_PORT")),
    )


def load_prompt(name: str) -> str:
    """Prompts live as plain text in prompts/<name>.md."""
    return (PROJECT_ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8").strip()


def _number(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = env.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}.")
    return value


def _on_off(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("on", "true", "yes", "1"):
        return True
    if raw in ("off", "false", "no", "0"):
        return False
    raise ConfigError(f"{name} must be on or off, got {raw!r}.")


def _timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"ALFRED_TIMEZONE must be like Asia/Manila, got {name!r}.") from None
    return name


def _port(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise ConfigError(f"ALFRED_WEB_PORT must be a port from 1024 to 65535, got {raw!r}.")
    return int(raw)


def _device(raw: str | None) -> int | str | None:
    """sounddevice takes a device index or part of a device name."""
    raw = (raw or "").strip()
    if not raw:
        return None
    return int(raw) if raw.isdigit() else raw

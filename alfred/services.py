"""Alfred's long-lived parts, built once at startup and shared by every conversation."""

from dataclasses import dataclass

from loguru import logger

from alfred import accounts
from alfred.accounts import Account
from alfred.agent import Agent
from alfred.apps import Apps
from alfred.audit import AuditLog
from alfred.config import Config
from alfred.db import Database
from alfred.google import Google
from alfred.health import Health
from alfred.library import Library, Limits, claude_describer, claude_vision, openai_embedder
from alfred.memory import Memory
from alfred.music import Music
from alfred.projects import Projects
from alfred.questions import Questions
from alfred.reminders import Reminders
from alfred.skills import Skills
from alfred.web import WebShare, lan_ip, start_web


@dataclass
class Services:
    config: Config
    db: Database
    questions: Questions
    memory: Memory
    agent: Agent
    reminders: Reminders
    apps: Apps
    library: Library
    health: Health
    web: WebShare | None = None  # the apps page (web.py)
    music: Music | None = None  # Spotify, once connected
    google: Google | None = None  # calendar and email, once connected

    @classmethod
    def start(cls, config: Config) -> "Services":
        """Open the database and carry on with unfinished tasks."""
        db = Database(config.database_path)
        questions = Questions(db)
        health = Health(config)
        memory = Memory(db, config, health)
        projects = Projects(config.projects_dir)
        apps = Apps(projects, lambda port: f"http://{lan_ip()}:{port}/")
        library = Library(
            config.library_dir,
            claude_vision(config.vision_model or config.agent_model),
            Limits(
                config.library_max_mb,
                config.library_max_gb,
                config.vision_ask_pages,
                config.vision_daily_pages,
                config.vision_per_call,
                config.vision_at_once,
            ),
            openai_embedder(config.openai_api_key, config.embedding_model),
            config.embedding_model,
            claude_describer(config.agent_model),
        )
        library.start()
        web = start_web(projects, apps, config.web_port, library)
        pages = web.address_text() if web else ""
        agent = Agent(
            db,
            config,
            questions,
            AuditLog(config.audit_path),
            projects,
            Skills(config.skills_dir),
            about_user=lambda: memory.prompt_text() + pages,
            memory=memory,
            apps=apps,
            library=library,
            health=health,
        )
        music = google = None
        if (spotify := connected(accounts.spotify(config), config, health)) is not None:
            music = Music(spotify, config.spotify_speaker)
        if (gmail := connected(accounts.google(config), config, health)) is not None:
            google = Google(gmail, config)
        if resumed := agent.resume_unfinished():
            logger.info(f"Carrying on with {resumed} unfinished task(s)")
        reminders = Reminders(db, config.tz)
        return cls(
            config, db, questions, memory, agent, reminders, apps, library, health,
            web=web, music=music, google=google,
        )  # fmt: skip

    def has_news(self) -> bool:
        """Is there anything Alfred should speak up about?"""
        return bool(
            self.db.untold_results() or self.questions.waiting() or self.reminders.new_due()
        )


def connected(provider, config: Config, health: Health) -> Account | None:
    """The account, if it's set up in .env (the health check says if it's logged in)."""
    if provider is None:
        return None
    account = Account(provider, config.data_dir)
    health.extra_checks[provider.name] = account.check
    return account

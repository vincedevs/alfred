"""Alfred's long-lived parts, built once at startup and shared by every conversation."""

from dataclasses import dataclass

from loguru import logger

from alfred.agent import Agent
from alfred.apps import Apps
from alfred.audit import AuditLog
from alfred.config import Config
from alfred.db import Database
from alfred.memory import Memory
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
    web: WebShare | None = None  # the apps page (web.py)

    @classmethod
    def start(cls, config: Config) -> "Services":
        """Open the database and carry on with unfinished tasks."""
        db = Database(config.database_path)
        questions = Questions(db)
        memory = Memory(db, config)
        projects = Projects(config.projects_dir)
        apps = Apps(projects, lambda port: f"http://{lan_ip()}:{port}/")
        web = start_web(projects, apps, config.web_port)
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
        )
        if resumed := agent.resume_unfinished():
            logger.info(f"Carrying on with {resumed} unfinished task(s)")
        return cls(config, db, questions, memory, agent, Reminders(db, config.tz), apps, web)

    def has_news(self) -> bool:
        """Is there anything Alfred should speak up about?"""
        return bool(
            self.db.untold_results() or self.questions.waiting() or self.reminders.new_due()
        )

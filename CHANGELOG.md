# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## 1.0.0 (2026-11-16)

Alfred, a general-purpose voice agent with the manner of an English
butler, built for coding, running on a Raspberry Pi 5.

### Voice
- "Hey Alfred" wakes him (a local wake word); a chime, then a GPT-Live conversation you
  can interrupt at any time, with echo cancellation.
- Quick answers and web search straight from the conversation; everything else is handed
  to the agent, and he keeps talking while it works. A chime marks the result.
- He speaks up by himself when a task finishes, asks something, or a reminder is due;
  what happened while he slept opens the next conversation.
- The conversation closes when it goes quiet (after a warning tone) or when you say
  "go to sleep".

### The agent
- A Claude agent (Claude Agent SDK) with one lasting session per project, in real folders
  under `~/.alfred/projects`, with files, a shell, the web, git and GitHub (a fine-grained
  token, `ALFRED_GITHUB_TOKEN`).
- A project's own `CLAUDE.md` or `AGENTS.md` reaches it; a repository's settings and
  hooks never do.
- Long builds and tests run in the background. It states only figures it read or worked
  out, and where from.

### Apps
- Apps the agent builds keep running so you can try them: it starts a project's server
  with `serve_app`, on a port from 8100–8199, at most three at once, each for up to four
  hours, without Alfred's keys. "What's running?", "stop the game", "restart it".
- The apps page (`ALFRED_WEB_PORT`) lists them, serves each project's static site, and the
  pages the agent made for you (reports, diffs).

### Memory, lessons and skills
- Facts about you, with their source and last use, learned from each conversation or when
  you say "remember"; a searchable history of tasks and conversations. `alfred-memory`
  lists, adds, forgets and exports them.
- Lessons the agent saves from its own work, for one project or everywhere.
- Skills (SKILL.md folders, in git under `~/.alfred/skills`): used when a task matches,
  written or fixed after asking. He starts with one: how to write a skill.

### Reminders, timers and alarms
- Set, listed and cancelled by voice; once, daily, on weekdays, at weekends or weekly.
  Kept in the database, so missed ones are said when he's back. Timers and alarms ring.

### Staying in control
- Three rules in code: free inside the project, asked out loud before anything public or
  hard to undo, never secrets or admin rights. "Always" allows a kind of action in a
  project until midnight; force-pushing is always asked.
- Shell commands run in the operating system's sandbox, reaching only package sites and
  GitHub. Every decision is in `~/.alfred/audit.jsonl`.
- One `.env` for settings and keys; lint and tests on every push; runs as a systemd user
  service.

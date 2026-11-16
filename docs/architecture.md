# Architecture

Alfred is a thin voice in front of one strong agent, with two things he keeps: memory
(what is true) and skills (how to do things). Everything runs on the Raspberry Pi except
the cloud models.

```
mic ─► wake word ─► GPT-Live (voice) ─► delegate ─► tools ─► agent (Claude) ─► projects
        (on the Pi)      ▲  hears, speaks     web search            │           skills
                         └──── questions and results, spoken ───────┘           memory
```

## The parts

| Part | Modules | Job |
|---|---|---|
| Audio | `audio`, `transport`, `wakeword` | One duplex stream with echo cancellation; the local "Hey Alfred" |
| Conversation | `voice`, `idle`, `announcer` | A GPT-Live session; closing when quiet; speaking up about tasks and reminders |
| Delegate tools | `tools` | What GPT-Live's delegate calls: hand work over, answer, memory, reminders, skills, sleep |
| Agent | `agent`, `projects`, `permissions`, `questions`, `audit` | Claude doing the work, in project folders, within three rules |
| Memory | `memory`, `db` | Facts, the agent's lessons and history in SQLite; learning after each conversation |
| Reminders | `reminders` | Reminders, timers and alarms in SQLite; the wake loop speaks up (and rings) when one is due |
| Skills | `skills` | A git-tracked plugin folder the agent loads natively |
| Running | `__main__`, `services`, `config`, `logs`, `lock`, `reset` | Start-up, settings, logs |
| Apps | `apps`, `web` | Running the apps Alfred builds; the apps page listing them, with static sites and pages |

## A request, end to end

1. "Hey Alfred" is heard on the Pi; nothing is sent before it.
2. GPT-Live listens and talks at once. It can't call tools itself: it delegates to a text
   model (the delegate, `ALFRED_DELEGATE_MODEL`), which searches the web on OpenAI's side
   or calls Alfred's tools here.
3. Real work goes to `do_task`: a task in a project (or `general`). The agent resumes that
   project's session, so it remembers what it did there. Quick tasks are answered at once;
   longer ones run on while you talk, and a chime marks the result.
4. When a task needs you ("May I push to main?"), Alfred asks out loud and your answer goes
   back to it. Unanswered questions count as no after 5 minutes.
5. After the conversation, the transcript is read once for lasting facts and a summary.

## Decisions

- **The delegate stays.** GPT-Live delegates all tool calls to a backend model, so a thin
  delegate (a short prompt, ten tools) sits between the voice and the agent. A Claude
  backend for every delegation would make quick look-ups slow and block during long tasks.
- **One agent, general tools.** No integrations: the agent reaches services the way a
  developer would (a CLI, a script, an API), and saves what worked as a skill.
- **Lasting sessions per project.** One task per project at a time, two at most in all
  (each is a Claude Code process of a few hundred MB).
- **A project's settings are never loaded.** A cloned repository could carry hooks that
  run commands outside the permission checks. Its `CLAUDE.md` or `AGENTS.md` is read as
  text instead.
- **Skills are a local plugin** (`~/.alfred/skills/.claude-plugin`), so the SDK loads them
  natively; git makes every change reviewable.
- **Memory is a database,** not a file: facts can be searched, de-duplicated, corrected and
  traced to their source, in one store with the history.
- **Permissions are code,** checked before every tool call, so nothing the agent reads can
  change them. The OS sandbox adds a second wall for shell commands.

## Running as a service

On the Pi, as the user who runs Alfred:

```ini
# ~/.config/systemd/user/alfred.service
[Unit]
Description=Alfred
After=network-online.target sound.target

[Service]
WorkingDirectory=%h/alfred
ExecStart=%h/.local/bin/uv run alfred
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now alfred
sudo loginctl enable-linger $USER     # start at boot, without logging in
journalctl --user -u alfred -f        # follow the log
```

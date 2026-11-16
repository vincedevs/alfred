# Alfred

A general-purpose voice agent with the manner of an English butler, built for coding. Say
**"Hey Alfred"**, hear a chime, and talk. You can interrupt him at any time.

Alfred has no built-in features. He does what an agent can do: read, write and run code,
use git and GitHub, research the web, make files and pages. He **remembers** what you tell
him, and **learns skills**: when he works out how to do something, or you teach him, he
saves it (after asking) and uses it next time.

- "Open the portfolio project and add a dark mode." Each project keeps its own lasting
  session: "now make the toggle remember my choice" carries on from there.
- "Push it." He asks first, then pushes; "yes, and don't ask again" lets him push in that
  project for the rest of the day. "Open a pull request instead."
- "What's new in Pipecat this month?" "Write a script that renames my photos by date."
- "Remember that I always want tests before a commit." "What do you know about me?"
- "This is how we deploy: … Learn it." "What skills do you have?"
- "Set a timer for ten minutes." "Wake me at seven." "Remind me to call the bank at three."
  "Every weekday at 8:45, remind me about stand-up."
- "Make a snake game I can play on my phone." He builds it, starts it and says where to
  open it. "What's running?" "Stop the game."
- "How's it going?" "Stop that." "Go to sleep."

How it works: [docs/architecture.md](docs/architecture.md).

## What it sounds like

| Sound | Meaning |
|---|---|
| Wake chime | He heard "Hey Alfred" and is listening |
| Soft chime | A task finished |
| Single soft tone | He stops listening in 5 seconds unless you speak |
| Low two-note sound | He has stopped listening |
| Two short low beeps | He couldn't connect: check the internet and the keys |

## Staying in control

- **Free:** anything inside the project he's working in, the web, and local git.
- **Asks you out loud first:** anything public or hard to undo: pushing, opening or merging
  pull requests, publishing, deleting, installing system software, sending data out, and
  saving a skill. Anything but a clear yes is a no; "always" means yes to that kind of
  action in that project until midnight (force-pushing is always asked).
- **Never:** secrets outside the project (keys, SSH, his own settings), admin rights.
- Shell commands run in the operating system's sandbox: writes stay in the project, and
  the network reaches only package sites and GitHub.
- Every action is in `~/.alfred/audit.jsonl`. Skills are a git repository in
  `~/.alfred/skills`: every change is a commit you can read and undo.

## Setup

You need [uv](https://docs.astral.sh/uv/), an OpenAI API key (`gpt-live-1`, `gpt-6-luna`),
and a Claude sign-in (your Claude plan via `claude setup-token`, or an API key). For
GitHub: `git`, the [GitHub CLI](https://cli.github.com/) and a fine-grained token.

```bash
uv sync
cp .env.example .env    # then fill in your keys
```

On the Raspberry Pi, the agent's shell needs the sandbox tools:

```bash
sudo apt install bubblewrap socat git gh portaudio19-dev
```

On a Mac, allow microphone access for your terminal when asked.

## Run

```bash
uv run alfred
```

Wait for `Waiting for "Hey Alfred"...`, then speak. Ctrl+C stops him; unfinished tasks
carry on next time.

To run him as a service on the Pi (starts on boot, restarts on failure), see
[docs/architecture.md](docs/architecture.md#running-as-a-service).

## Where things live

Everything Alfred keeps is in `~/.alfred` (set by `ALFRED_DATA`):

| Path | What |
|---|---|
| `alfred.db` | Facts about you, history, tasks, sessions |
| `projects/` | One folder per project (`general` for the rest); its `.alfred/` holds the app's log and pages he made for you |
| `skills/` | His skills, in git |
| `audit.jsonl` | Every action the agent tried, and the decision |

```bash
uv run alfred-memory            # what he knows about you (also: add, forget, export)
uv run alfred-reset             # wipe everything (also: memory, logs)
```

Logs are in `logs/`, one per run, deleted after 14 days.

## Development

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

Versions follow [Semantic Versioning](https://semver.org/); see [CHANGELOG.md](CHANGELOG.md).
Latency is measured with fd-benchmark: [benchmark/README.md](benchmark/README.md).

## Costs and privacy

Talking costs about $0.05 a minute (OpenAI). The agent's tasks are billed by Claude; each
one's cost is in the log. Nothing is sent before "Hey Alfred", except when he speaks up
about a task. Your voice goes to OpenAI and the agent's work to Claude; everything he keeps
stays in `~/.alfred`.

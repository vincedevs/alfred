# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## 1.1.0 (unreleased)

### Added
- **The library: answers from your own files, with checked citations.** Upload files on
  the apps page (`/library`); they are read in the background, page by page, and are
  searchable as they go. PDF text, Word, PowerPoint, Excel, CSV, EPUB, HTML, Outlook
  messages, text and code; scanned pages and images are read by Claude vision
  (`ALFRED_VISION_MODEL`). Same file twice is skipped; a new version replaces the old.
- Uploads go in 8 MB pieces and resume after a dropped connection; several files or a
  whole folder at once, two at a time; asks before replacing a file of the same name.
  Files up to 500 MB (100 MB for Word, PowerPoint, EPUB and web pages), 64 GB in all, and
  the card always keeps 10 GB free. Spreadsheets and text are read in batches, whatever
  their size; small files are read before large ones.
- Scans under control: a file with more than 200 scanned pages waits for your yes (on the
  page, or by voice), and at most 1,000 scanned pages are read a day; the rest carry on
  the next day.
- **Hybrid search:** keyword search plus meaning search (OpenAI `text-embedding-3-large`,
  1,024 numbers per chunk, embedded with its document and location, in `sqlite-vec`),
  their rankings merged. Offline, keyword search carries on and embeddings catch up later;
  a new embedding model re-embeds the library. Past 40 files, prompts get a summary of the
  library instead of every name.
- The agent searches, reads whole pages and sections,
  calculates spreadsheet figures in code, and checks every citation with `cite_answer`:
  each quote must be at the location it cites, or it isn't spoken. The full answer, with
  quotes and links to the page, goes on the apps page.
- A starter skill, `answering-from-documents`; starter skills added in later versions
  now reach existing installs (once).
- "What's in my library?" by voice; Alfred says when an uploaded file is ready. He knows
  which files the library holds, so a question they may answer goes to them first, even
  when you don't mention a file.
- The apps page, the library and answer pages share one design, a manor at night
  (midnight and candlelight, with a daylight version in light mode), with Alfred's bow-tie
  logo in the header, as the browser tab's icon and in the README: upload progress per
  file, live status while files are read, type, size and page counts.

### Removed
- The `benchmark/` folder. How to run the voice benchmark (fd-benchmark) is now in
  `docs/benchmarking.md`, with the one file it needs.

## 1.0.0 (2026-11-16)

The first release: Alfred, a general-purpose voice agent with the manner of an English
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

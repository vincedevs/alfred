"""The terminal, for watching Alfred work (a demo, a presentation): the conversation as a
chat, each answer's steps and checked quotes, and a status bar. The full log still goes
to logs/. `uv run alfred --plain` shows the plain log instead."""

import time

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

console = Console(highlight=False)
on = False  # main() turns it on unless --plain
_live: Live | None = None
_state = {"mode": "Starting", "files": "no files", "tasks": 0, "cost": 0.0, "problems": ""}
_asked = {"at": 0.0, "route": ""}
_reply = {"text": "", "note": ""}  # Alfred's reply so far: live until something else prints
COLOURS = {"Listening": "green", "Thinking": "yellow", "Speaking": "cyan"}


def start() -> None:
    global _live
    if on:
        _live = Live(_view(), console=console, refresh_per_second=4)
        _live.start()


def stop() -> None:
    _flush()
    if _live:
        _live.stop()


def status(**changes) -> None:
    """mode ("Listening"), files ("9 files ready"), tasks (running), cost (dollars to add)."""
    _state["cost"] += changes.pop("cost", 0.0)
    _state.update(changes)
    if _live:
        _live.update(_view())


def _view() -> Group:
    return Group(*([_bubble("Alfred", **_reply)] if _reply["text"] else []), _bar())


def _bar() -> Text:
    mode = _state["mode"]
    return Text.assemble(
        ("● ", COLOURS.get(mode, "dim")), (f"{mode:<14}", "bold"),
        (f"library: {_state['files']}   tasks: {_state['tasks']}   "
         f"${_state['cost']:.2f} this session", "dim"),
        (f"   ⚠ not working: {_state['problems']}" if _state["problems"] else "", "bold red"),
    )  # fmt: skip


def _bubble(who: str, text: str, note: str = "") -> Panel | Align:
    """A chat bubble: yours on the right, Alfred's on the left."""
    you = who == "You"
    width = min(max(len(text), len(note), len(who)) + 6, int(console.width * 0.7))
    side = "right" if you else "left"
    bubble = Panel(
        text, title=f"[bold]{who}[/]", title_align=side, subtitle=note or None,
        subtitle_align=side, border_style="magenta" if you else "cyan", width=width,
    )  # fmt: skip
    return Align.right(bubble) if you else bubble


def _flush() -> None:
    if _reply["text"]:
        console.print(_bubble("Alfred", **_reply))
        _reply.update(text="", note="")


def _print(thing) -> None:
    """Print below the chat so far (Alfred's reply first, if it's still growing)."""
    _flush()
    console.print(thing)
    status()


def card(version: str, address: str | None) -> None:
    """At start: the version, and where to drop files."""
    if not on:
        return
    about = f"[bold]▶◀  Alfred {version}[/]"
    if address:
        about += f"\nDrop files at [link={address}library]{address}library[/link]"
    _print(Panel(about, border_style="yellow", expand=False))


def said(who: str, text: str) -> None:
    if not on or not text.strip():
        return
    if who == "You":
        _asked.update(at=time.monotonic(), route="")
        _print(_bubble("You", text))
        status(mode="Thinking")
        return
    if not _reply["text"] and _asked["at"]:  # how long since the question, and how
        took = time.monotonic() - _asked["at"]
        _reply["note"] = " · ".join(filter(None, [f"{took:.1f} s", _asked["route"]]))
    _reply["text"] = f"{_reply['text']} {text.strip()}".strip()
    status(mode="Listening")
    if not _live:
        _flush()


def step(icon: str, text: str, route: str = "") -> None:
    """One thing Alfred did for the question: "🔎 searched your files · 0.3 s"."""
    if route:
        _asked["route"] = route
    if on:
        _print(Text(f"  {icon} {text}", "dim"))


def quotes(citations: list[dict]) -> None:
    """The checked quotes behind an answer, with where each is."""
    if not on or not citations:
        return
    web = any(c.get("url") for c in citations)
    files = any(not c.get("url") for c in citations)
    route = ("from your files" + (" and the web" if web else "")) if files else "from the web"
    by = ", by Claude" if _asked["route"] == "by Claude" else ""
    n = len(citations)
    step("✅", f"{n} quote{'s' * (n > 1)} checked", route + by)
    body = Group(*(
        Text.assemble((f"“{c.get('quote', '')}”\n", "italic"),
                      (c.get("url") or f"{c.get('document')}, {c.get('location')}", "dim"))
        for c in citations
    ))  # fmt: skip
    _print(Padding(Panel(body, border_style="green", expand=False), (0, 0, 0, 2)))


def tool(name: str, args: dict) -> None:
    """A step the agent (Claude) took, in a few words."""
    name = name.removeprefix("mcp__alfred__")
    where = args.get("url") or args.get("query") or args.get("file_path") or ""
    words = {
        "search_documents": ("🔎", f"Claude searched your files: {args.get('query', '')}"),
        "read_document": ("📄", f"{args.get('document', '')} · {args.get('location', '')}"),
        "read_web_page": ("🌐", f"read {where}"), "WebFetch": ("🌐", f"read {where}"),
        "WebSearch": ("🌐", f"searched the web: {where}"), "Bash": ("⚙", "ran a command"),
        "Skill": ("📘", f"used the {args.get('skill', '')} skill"),
    }  # fmt: skip
    if name in words:
        step(*words[name])

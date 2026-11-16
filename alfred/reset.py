"""`uv run alfred-reset`: wipe what Alfred keeps, for a fresh start.

    uv run alfred-reset            everything in ~/.alfred, and the logs
    uv run alfred-reset memory     only the database (facts, history, tasks, sessions)
    uv run alfred-reset logs       only the logs
    add --yes to skip the question

Your settings (.env) and the code are never touched. Alfred must be stopped first.
"""

import argparse
import shutil
import sys
from pathlib import Path

from alfred.config import ConfigError, load_config
from alfred.lock import only_one_alfred
from alfred.logs import LOG_DIR

WHAT = {"all": "everything Alfred keeps", "memory": "what Alfred remembers", "logs": "the logs"}


def targets(what: str, data_dir: Path) -> list[Path]:
    """The files and folders to delete; ones that don't exist are left out."""
    logs = sorted(LOG_DIR.glob("*.log"))
    everything = sorted(data_dir.iterdir()) if data_dir.exists() else []
    paths = {"all": everything + logs, "memory": [data_dir / "alfred.db"], "logs": logs}[what]
    return [p for p in paths if p.exists() and p.name != "alfred.lock"]


def main() -> None:
    parser = argparse.ArgumentParser(prog="alfred-reset", description=__doc__.split("\n")[0])
    parser.add_argument("what", nargs="?", default="all", choices=WHAT)
    parser.add_argument("--yes", action="store_true", help="don't ask, just delete")
    args = parser.parse_args()
    try:
        config = load_config()
    except ConfigError as error:
        sys.exit(f"Config error: {error}")
    lock = only_one_alfred(config.data_dir)
    if lock is None:
        sys.exit("Alfred is running. Stop him first.")
    found = targets(args.what, config.data_dir)
    if not found:
        print(f"Nothing to delete: {WHAT[args.what]} is already empty.")
        return
    print(f"This deletes {WHAT[args.what]}:")
    for path in found:
        print(f"  {path}")
    if not args.yes and input("Delete? Type yes: ").strip().lower() != "yes":
        print("Nothing deleted.")
        return
    for path in found:
        shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
    print("Done. Alfred starts fresh next time.")


if __name__ == "__main__":
    main()

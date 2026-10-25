"""One answer to "where is the bot?" - for every process that reads its files.

Why this module exists (2026-10-23). The dashboard, the ops reports and the bot
itself each resolved the bot's files with their own guess, and the guesses were
mostly *relative to the code*:

* `channels_config.channels_json_path()` fell back to the directory of
  `channels_config.py` - the CHECKOUT - so on the server the dashboard wrote
  watch entries into `<checkout>/bestgaa/channels.json` while the running bot
  read `.../bestgaa-bot/bestgaa-bot/channels.json`. The operator saved a source,
  the bot never saw it, and nothing said so.
* `dashboard/app.py` read the DB from `<checkout>/bestgaa/bestgaa.sqlite3` (a
  file that does not exist there) while it read the `.env` from the real bot
  directory - so the API-keys panel showed live keys next to an empty pipeline.
* `ops/category_report.py` looked for `.env` and `channels.json` in the checkout,
  found neither, and reported every shelf as `no_channel` - the deploy report's
  "2 problems" that were really one missing file.

A guess that is silently wrong is worse than a failure, so this module resolves
once, in one order, and can explain itself (`describe()`): which rule fired, the
path, and whether it exists. Everything else asks it. The bot running inside its
own directory resolves to that directory exactly as before (rule 4), so nothing
about the running bot changes.

Resolution order (first match wins):

1. `BESTGAA_DIR`                        - explicit operator override
2. `BOT_DB_PATH`                        - the DB names the directory that owns it
3. `CHANNELS_JSON_PATH`                 - same, for the routing file
4. this module's own directory          - when it IS a bot directory (the bot)
5. the known install paths              - /home/ubuntu/bestgaa-bot/bestgaa-bot…
6. `BOT_DB_PATH` / `CHANNELS_JSON_PATH` inside the bot's own `.env`
7. the repository checkout              - development, offline tests

Read-only: nothing here creates, moves or writes a file.
"""
from __future__ import annotations

import os
from pathlib import Path

# A directory that owns a bot: the two files the running bot cannot start
# without. `channels.json` alone is not enough (a checkout can have one) and
# neither is `main_bot_new.py` (every checkout has that).
BOT_MARKERS = (".env", "bestgaa.sqlite3", "state/bot_state.sqlite3")

# Install layouts that actually exist on the server. Checked only after the
# bot's own directory, so a bot running in its app dir never looks elsewhere.
INSTALL_HINTS = ("/home/ubuntu/bestgaa-bot/bestgaa-bot", "~/bestgaa-bot/bestgaa-bot")

HERE = Path(__file__).resolve().parent


def repo_root() -> Path:
    """The checkout this module was imported from."""
    return HERE.parent


def _is_bot_dir(path: Path) -> bool:
    try:
        return any((path / marker).exists() for marker in BOT_MARKERS)
    except OSError:
        return False


def _clean(value: str | None) -> str:
    return (value or "").strip().strip('"').strip("'")


def env_value(env_file: Path, *keys: str) -> str:
    """One value out of a `.env`, without loading it into this process.

    Deliberately tiny and forgiving: the file is written by an operator (and by
    the dashboard's own editor), so a malformed line must not raise here.
    """
    try:
        text = Path(env_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    wanted = set(keys)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in wanted:
            cleaned = _clean(value)
            if cleaned:
                return cleaned
    return ""


def resolve() -> dict:
    """(directory, rule) - the one place a directory gets chosen."""
    explicit = _clean(os.getenv("BESTGAA_DIR"))
    if explicit:
        return {"dir": Path(explicit).expanduser(), "rule": "BESTGAA_DIR"}
    db_env = _clean(os.getenv("BOT_DB_PATH"))
    if db_env:
        return {"dir": Path(db_env).expanduser().parent, "rule": "BOT_DB_PATH"}
    channels_env = _clean(os.getenv("CHANNELS_JSON_PATH"))
    if channels_env:
        return {"dir": Path(channels_env).expanduser().parent, "rule": "CHANNELS_JSON_PATH"}
    if _is_bot_dir(HERE):
        return {"dir": HERE, "rule": "the module's own directory"}
    for hint in INSTALL_HINTS:
        candidate = Path(hint).expanduser()
        if _is_bot_dir(candidate):
            return {"dir": candidate, "rule": f"installed layout {hint}"}
    # The bot's own .env may put its database somewhere else entirely (a data
    # disk, a different user). If we found a bot directory, believe its .env.
    for hint in INSTALL_HINTS + (str(HERE),):
        candidate = Path(hint).expanduser()
        env_file = candidate / ".env"
        if not env_file.exists():
            continue
        moved = env_value(env_file, "BOT_DB_PATH", "CHANNELS_JSON_PATH")
        if moved:
            return {"dir": Path(moved).expanduser().parent,
                    "rule": f"the bot's .env at {env_file}"}
    return {"dir": repo_root() / "bestgaa", "rule": "the repository checkout"}


def bot_dir() -> Path:
    """The directory that owns the bot's `.env`, database and `channels.json`."""
    return resolve()["dir"]


def env_path() -> Path:
    """The bot's `.env` (never the dashboard's own `/etc/bestgaa/dashboard.env`)."""
    explicit = _clean(os.getenv("BESTGAA_BOT_ENV"))
    return Path(explicit).expanduser() if explicit else bot_dir() / ".env"


def db_path() -> Path:
    """`bestgaa.sqlite3` as the bot itself would open it."""
    explicit = _clean(os.getenv("BOT_DB_PATH"))
    if explicit:
        return Path(explicit).expanduser()
    directory = bot_dir()
    from_env = env_value(env_path(), "BOT_DB_PATH")
    if from_env:
        return Path(from_env).expanduser()
    legacy = directory / "state" / "bot_state.sqlite3"
    main = directory / "bestgaa.sqlite3"
    if main.exists() or not legacy.exists():
        return main
    return legacy


def channels_json_path() -> Path:
    """`channels.json` - what the DASHBOARD writes and the BOT reads."""
    explicit = _clean(os.getenv("CHANNELS_JSON_PATH"))
    if explicit:
        return Path(explicit).expanduser()
    from_env = env_value(env_path(), "CHANNELS_JSON_PATH")
    if from_env:
        return Path(from_env).expanduser()
    return bot_dir() / "channels.json"


def snapshot_path() -> Path:
    """The routing snapshot the running bot publishes next to channels.json."""
    return channels_json_path().parent / "routing_snapshot.json"


def directory_path() -> Path:
    """The channel list the running bot publishes (written by the bot only)."""
    explicit = _clean(os.getenv("CHANNEL_DIRECTORY_PATH"))
    if explicit:
        return Path(explicit).expanduser()
    return channels_json_path().parent / "telegram_channels.json"


def pending_path() -> Path:
    """The dashboard's sidecar, cleared once the bot has applied the change."""
    return channels_json_path().parent / ".channels_pending"


def log_path() -> Path:
    """`logs/bot.log` unless the bot was told to log elsewhere."""
    explicit = _clean(os.getenv("BOT_LOG"))
    if explicit:
        return Path(explicit).expanduser()
    log_dir = _clean(os.getenv("BOT_LOG_DIR"))
    base = Path(log_dir).expanduser() if log_dir else bot_dir() / "logs"
    return base / "bot.log"


def describe() -> list[dict]:
    """Every resolved path, the rule that chose the directory, and existence.

    This is what an operator sees from `python3 bestgaa/bot_paths.py`, so
    "the dashboard shows the wrong thing" becomes a one-line answer instead of
    an hour of SSH.
    """
    chosen = resolve()
    rows = [
        ("bot directory", chosen["dir"], chosen["rule"]),
        (".env", env_path(), "BESTGAA_BOT_ENV"),
        ("database", db_path(), "BOT_DB_PATH"),
        ("channels.json", channels_json_path(), "CHANNELS_JSON_PATH"),
        ("routing snapshot", snapshot_path(), "beside channels.json"),
        ("channel directory", directory_path(), "CHANNEL_DIRECTORY_PATH"),
        ("pending sidecar", pending_path(), "beside channels.json"),
        ("bot log", log_path(), "BOT_LOG"),
    ]
    out = []
    for name, path, rule in rows:
        try:
            exists = Path(path).exists()
        except OSError:
            exists = False
        out.append({"name": name, "path": str(path), "exists": bool(exists), "rule": rule})
    return out


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    rows = describe()
    if as_json:
        import json

        print(json.dumps(rows, indent=2))
        return 0
    width = max(len(row["name"]) for row in rows)
    print(f"WHERE IS THE BOT?  (rule: {resolve()['rule']})")
    for row in rows:
        mark = "ok  " if row["exists"] else "MISSING"
        print(f"  {mark}  {row['name']:<{width}}  {row['path']}")
    missing = [row["name"] for row in rows if not row["exists"]]
    if missing:
        print("\n  missing: " + ", ".join(missing))
        print("  set BESTGAA_DIR (or BOT_DB_PATH) to the bot's app directory and re-run.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

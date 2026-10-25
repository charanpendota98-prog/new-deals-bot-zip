"""BestGAA operator dashboard: add channels easily, watch the bot live.

Run from the repo root::

    DASHBOARD_TOKEN='<long-random>' python3 -m dashboard.app --port 8080

then open http://127.0.0.1:8080 and paste the token. Keep it on localhost
(SSH tunnel to reach it) and never expose it to the internet: anyone with the
token can change where deals post.

What it does
  * Channels: list every posting channel (built-in + dashboard-added), add a
    new channel (name, sources, category shelf, Amazon-tag opt-in),
    enable/disable/remove it.
    Changes land in channels.json next to the bot database. New destinations
    and already-joined source channels can activate on an upgraded running bot
    after access checks. Existing policy edits/removals need an approved restart.
    Saving never starts the bot or joins Telegram channels.
  * Status: queue depth, today's real sends vs skips per the ledger, last
    posted deal, bot uptime.
  * Recent: latest jobs with per-job send/skip counts.
  * Categories: per-shelf truth from the live database, the bot log, the saved
    config and the installed rules - sends/skips/holds, pending backlog, how many
    deals the classifier routed to the shelf versus withheld from it (and why),
    the last time anything landed there, and actionable findings (rules parity,
    a stale bot rules version, an unresolved or colliding shelf target, a shelf
    that stayed silent while Main posted). Same engine as
    `python3 ops/category_report.py`.
  * Audit: the same SENDS vs SKIPS + findings summary ops/quality_audit.py
    produces, over the live database (read-only).
  * Console: self-diagnosis + live timeline (`dashboard/insight.py`). Health
    checks the database, schema, backlog, silence, bot-vs-bridge policy and
    rules parity, channels config, routing snapshot, log errors and disk, and
    gives every non-green check a fix line. The timeline merges intake, send
    attempts, refusals, link conversions and owned-link proofs into one
    newest-first stream, with the age of the last event of each kind so a
    silence is visible too.
  * Trace: one queue id, the whole chain - what the planner predicted, what
    the readers and the safety gate made of it, which targets actually
    received it, which were skipped and with what error, the delivered link,
    and any refusal recorded against it.
  * Search: posts, channels and refusals by text, source, @channel, price or
    queue id, each hit shown with the line it came from.
  * Scorecard: per channel and per source - sends, skips with the top reason,
    last send, average quality, best IST hour, silence.
  * Safety: why a post was NOT published. Every refusal by the public-channel
    content policy (adult, abusive, betting, solicitation, intimate products,
    suggestive wording) with its category, masked match, human reason and the
    policy version, plus counters for today / the window / all time and the
    pre-send gate. The policy itself (categories, severities, switches) is read
    from bestgaa/safety_filter.py so the panel can never describe a policy the
    bot is not running.

Read-only on the bot database always; the only writes are channels.json, its
.pending sidecar, and the optional restart command.
"""
import argparse
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bestgaa"))
import bot_paths  # noqa: E402
import channels_config  # noqa: E402
from category_classifier import load_rules  # noqa: E402
from dashboard.preview import preview_batch, inspect_product_link  # noqa: E402
import posts  # noqa: E402 - bestgaa/posts.py: shared with the BOT (it drains the queue)
import channel_suggest  # noqa: E402 - the ask; the BOT answers it
import channels_attach  # noqa: E402 - attach what already runs, pause it
from dashboard import (forensics, insight, pipeline as dashboard_pipeline,
                       source_target, trends, vault)  # noqa: E402

IST_OFFSET = 5 * 3600 + 30 * 60  # Asia/Kolkata, no tzdata needed
SERVICE_DEFAULT = "bestgaa"


def db_path() -> Path:
    """The bot's database, resolved by ONE rule (`bestgaa/bot_paths.py`).

    This used to fall back to `<checkout>/bestgaa/bestgaa.sqlite3`, a file that
    does not exist on the server, while the same process read the `.env` from
    the real bot directory. The panel then showed live API keys above an empty
    pipeline and looked like "the bot stopped posting".
    """
    return bot_paths.db_path()


def ist_day_start(exact: float | None = None) -> float:
    now = exact if exact is not None else time.time()
    return (now + IST_OFFSET) // 86400 * 86400 - IST_OFFSET


def read_only_db() -> sqlite3.Connection | None:
    path = db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def delivery_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(deliveries)")}
    except sqlite3.OperationalError:
        return set()


def classify_counts(rows) -> tuple[int, int]:
    """Real sends vs skips from (status, last_error, n) rows (both skip shapes)."""
    sends = skips = 0
    for status, error, count in rows:
        error = error or ""
        if status == "sent" and not error:
            sends += count
        elif status == "skipped" or (status == "sent" and error):
            skips += count
    return sends, skips


def snapshot() -> dict:
    try:
        return json.loads(channels_config.snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def pending_restart() -> bool:
    """True when channels.json changed since the bot last loaded it."""
    snap = snapshot()
    if snap.get("live_reload"):
        return snap.get("applied_revision") != config_revision()
    sidecar = channels_config.pending_path()
    try:
        pending = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    snap = snapshot()
    if not snap:
        return True  # changed, and the bot never reported back
    try:
        return float(snap.get("channels_mtime") or 0) != float(pending.get("channels_mtime") or 0)
    except (TypeError, ValueError):
        return True


def mark_pending() -> None:
    sidecar = channels_config.pending_path()
    try:
        mtime = os.path.getmtime(channels_config.channels_json_path())
    except OSError:
        mtime = 0.0
    try:
        sidecar.write_text(json.dumps({"channels_mtime": mtime, "changed_at": time.time()}),
                           encoding="utf-8")
    except OSError:
        pass


def clear_pending() -> None:
    try:
        channels_config.pending_path().unlink()
    except OSError:
        pass


def in_thread(handler):
    """Run a read-only handler in a worker thread instead of on the event loop.

    Every panel here queries SQLite synchronously. On a 50k-post database
    /api/categories takes ~1.3 s and /api/audit ~0.5 s, and because those ran on
    the event loop the whole dashboard froze while they did - measured: eight
    concurrent requests took as long as eight sequential ones (5.8 s vs 6.0 s),
    so one refresh blocked every other tab, button and the 5 s auto-refresh.
    sqlite3 releases the GIL while it runs, so a thread is real parallelism.

    Only handlers whose body contains no `await` may use this (POST/PATCH
    handlers read the request body, so they stay async).
    """

    async def wrapper(request: web.Request):
        return await asyncio.to_thread(handler, request)

    wrapper.__name__ = getattr(handler, "__name__", "handler")
    return wrapper


def require_token(handler):
    async def wrapper(request: web.Request):
        expected = os.environ.get("DASHBOARD_TOKEN", "")
        if not expected and os.environ.get("DASHBOARD_INSECURE", "") != "1":
            return web.json_response({"error": "dashboard has no token configured"}, status=503)
        if expected:
            given = request.headers.get("X-Dashboard-Token", "") or request.query.get("token", "")
            if not given or not hmac.compare_digest(given, expected):
                return web.json_response({"error": "bad or missing token"}, status=401)
        return await handler(request)

    return wrapper


@require_token
@in_thread
def api_status(request: web.Request):
    conn = read_only_db()
    queue_counts: dict[str, int] = {}
    sends = skips = 0
    last_post = 0.0
    if conn is not None:
        try:
            try:
                for row in conn.execute("SELECT status, COUNT(*) c FROM queue GROUP BY status"):
                    queue_counts[str(row["status"] or "?")] = int(row["c"])
            except sqlite3.OperationalError:
                pass
            cols = delivery_columns(conn)
            if cols:
                error_expr = "COALESCE(d.last_error,'')" if "last_error" in cols else "''"
                rows = conn.execute(
                    f"SELECT d.status, {error_expr}, COUNT(*) FROM deliveries d "
                    "JOIN queue q ON q.id=d.queue_id WHERE q.created_at>=? "
                    f"GROUP BY d.status, {error_expr}", (ist_day_start(),)).fetchall()
                sends, skips = classify_counts(
                    (str(r[0] or ""), r[1] or "", int(r[2])) for r in rows)
                try:
                    last_post = conn.execute(
                        "SELECT MAX(q.created_at) FROM queue q JOIN deliveries d "
                        "ON d.queue_id=q.id WHERE d.status='sent' "
                        + ("AND COALESCE(d.last_error,'')='' " if "last_error" in cols else "")
                    ).fetchone()[0] or 0.0
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()
    snap = snapshot()
    db = db_path()
    return web.json_response({
        "ok": True,
        "now": time.time(),
        "db_exists": db.exists(),
        "db_mtime": os.path.getmtime(db) if db.exists() else 0,
        "bot_started_at": snap.get("started_at", 0),
        "queue": queue_counts,
        "today_sends": sends,
        "today_skips": skips,
        "last_post_at": last_post,
        "pending_restart": pending_restart(),
        "insecure": not os.environ.get("DASHBOARD_TOKEN", ""),
    })


def bot_app_dir() -> Path:
    """Where the RUNNING bot lives - the directory that owns `.env`.

    The dashboard often runs from a git checkout while the bot runs from its app
    directory (that split caused a whole evening of "the dashboard shows an empty
    list"). Resolving the app dir here, in one place, and SHOWING it in the vault
    panel is what makes "which keys is the bot actually reading?" answerable
    without SSH.

    The rule itself lives in `bestgaa/bot_paths.py`, shared with the DB, the
    channels file, the ops reports and the bot - so the panel cannot show live
    keys next to another directory's empty database.
    """
    return bot_paths.bot_dir()


def bot_env_path() -> Path:
    """The bot's `.env`. Never the dashboard's own /etc/bestgaa/dashboard.env."""
    return bot_paths.env_path()


def bot_log_paths() -> list[Path]:
    app = bot_app_dir()
    log_dir = Path(os.getenv("BOT_LOG_DIR", "").strip() or bot_paths.log_path().parent)
    # The app's own logs directory is scanned even when BOT_LOG_DIR points
    # somewhere else: a key pasted into the deployed log is still a key on disk.
    candidates = [log_dir / "bot.log", app / "logs" / "bot.log", app / "bot.log",
                  channels_config.channels_json_path(),
                  channels_config.snapshot_path(),
                  channels_config.directory_path()]
    seen: list[Path] = []
    for path in candidates:
        if path not in seen:
            seen.append(path)
    return seen


def bot_started_at() -> float:
    snap = snapshot()
    for key in ("started_at", "heartbeat_at", "boot_at"):
        try:
            value = float(snap.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value:
            return value
    return 0.0


def vault_state() -> dict:
    """Everything the vault panel shows - masked, validated, and honest."""
    path = bot_env_path()
    text = vault.read_env_text(path)
    state = vault.file_state(path)
    values = vault.env_values(text)
    started = bot_started_at()
    bot_has_it = bool(started and state["mtime"] and state["mtime"] <= started + 1)
    # Only REAL slots count as secrets; an unknown line is reported, not scanned.
    secrets: list[str] = []
    for key, raw in values.items():
        slot = vault.SLOT_BY_KEY.get(key)
        if slot is None or not slot.secret:
            continue
        secrets.extend(vault.split_values(slot, raw))
    leaks = vault.leak_scan(secrets, bot_log_paths())
    all_values = {key: vault.split_values(vault.SLOT_BY_KEY[key], raw)
                  for key, raw in values.items() if key in vault.SLOT_BY_KEY}
    slots = []
    for slot in vault.SLOTS:
        found = all_values.get(slot.key, [])
        verdict = vault.validate_slot(slot, found, all_values=all_values) if found else None
        slots.append({
            "key": slot.key, "label": slot.label, "group": slot.group,
            "help": slot.help, "hint": slot.hint, "secret": slot.secret,
            "multi": slot.multi, "required": slot.required,
            "set": bool(found),
            "values": [{"masked": vault.mask(value, secret=slot.secret),
                        "tail": "" if not slot.secret else vault.tail(value),
                        "fp": vault.fingerprint(value)}
                       for value in found],
            "errors": (verdict or {}).get("errors", []),
            "warnings": (verdict or {}).get("warnings", []),
            "info": (verdict or {}).get("info", []),
        })
    unknown = sorted(key for key in values
                     if key not in vault.SLOT_BY_KEY
                     and any(marker in key for marker in ("TOKEN", "KEY", "SECRET", "API")))
    db = read_only_db()
    if db is not None:
        db.close()
    return {
        "path": str(path), "app_dir": str(bot_app_dir()),
        "exists": state["exists"], "writable": state["writable"],
        "mode": state["mode"], "owner": state["owner"], "bytes": state["bytes"],
        "revision": state["revision"], "saved_at": state["mtime"],
        "saved_ist": insight.ist(state["mtime"]) if state["mtime"] else "—",
        "bot_started_at": started,
        "bot_started_ist": insight.ist(started) if started else "—",
        "bot_has_it": bot_has_it,
        "groups": [{"key": key, "label": label, "help": help_} for key, label, help_ in vault.GROUPS],
        "slots": slots,
        "unknown_keys": unknown,
        "leaks": leaks,
        "journal": vault.journal_read(path, limit=20),
        "readiness": vault.readiness(values, bot_running=bool(started and
                                                             time.time() - started < 900),
                                     bot_has_it=bot_has_it, db_ok=db is not None,
                                     leaks=leaks),
        "restart_command": "sudo systemctl restart bestgaa",
    }


@require_token
@in_thread
def api_vault(request: web.Request):
    return web.json_response(vault_state())


@require_token
async def api_vault_save(request: web.Request):
    """Save keys: validated first, written atomically, answered in masks only."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = None
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    path = bot_env_path()
    # Saving into a path that does not exist would CREATE an .env holding only the
    # key in this form - and the bot would start without its Telegram session, its
    # tag and everything else. A wrong app directory looks exactly like this, so
    # refuse and name the path instead of writing a half-file.
    if not path.exists():
        return web.json_response(
            {"errors": [f"the bot's .env was not found at {path}. Nothing was written: "
                        f"creating a new file here would leave the bot without the keys "
                        f"it already relies on. Check BESTGAA_DIR / BOT_DB_PATH (see the "
                        f"API keys panel's 'env file' row) or place the file there first."],
             "written": False, "path": str(path)}, status=409)
    text = vault.read_env_text(path)
    revision = vault.env_revision(text)
    expected = request.headers.get("If-Match")
    if expected and expected != revision:
        return web.json_response(
            {"errors": [".env changed since you opened this form. Reload and review "
                        "before saving."], "revision": revision}, status=409)
    dry_run = bool(body.get("dry_run"))
    current = vault.env_values(text)
    current_values = {slot.key: vault.split_values(slot, current.get(slot.key, ""))
                      for slot in vault.SLOTS}

    def _bad(message: str) -> web.Response:
        return web.json_response({"errors": [message], "revision": revision,
                                  "written": False}, status=400)

    updates: dict[str, list[str]] = {}
    raw_set = body.get("set") or {}
    if not isinstance(raw_set, dict):
        return _bad("set must be an object of key -> value(s)")
    for key, raw in raw_set.items():
        slot = vault.SLOT_BY_KEY.get(str(key))
        if slot is None:
            continue  # reported as an unknown key by validate_update
        try:
            updates[slot.key] = vault.split_values(slot, raw)
        except ValueError as exc:
            return _bad(f"{key}: {exc}")

    # "remove this one" is by INDEX, because the dashboard only ever held the
    # masked form: an operator can drop the third Bitly token without ever sending
    # the other two through the browser again.
    raw_remove_values = body.get("remove_values") or {}
    if not isinstance(raw_remove_values, dict):
        return _bad("remove_values must be an object of key -> [index, ...]")
    for key, indices in raw_remove_values.items():
        slot = vault.SLOT_BY_KEY.get(str(key))
        if slot is None or str(key) in raw_set:
            continue
        if not isinstance(indices, list):
            return _bad(f"remove_values[{key}] must be a list of indices")
        base = list(current_values[slot.key])
        try:
            wanted = sorted({int(index) for index in indices}, reverse=True)
        except (TypeError, ValueError):
            return _bad(f"remove_values[{key}] must be whole numbers")
        for index in wanted:
            if index < 0 or index >= len(base):
                return _bad(f"{key}: no value at position {index + 1}")
            base.pop(index)
        updates[slot.key] = base

    # "add another key" keeps the ones already there, which is what a second
    # Bitly token or a second EarnKaro failover key actually means.
    raw_append = body.get("append") or {}
    if not isinstance(raw_append, dict):
        return _bad("append must be an object of key -> value(s)")
    for key, raw in raw_append.items():
        slot = vault.SLOT_BY_KEY.get(str(key))
        if slot is None:
            continue
        try:
            additions = vault.split_values(slot, raw)
        except ValueError as exc:
            return _bad(f"{key}: {exc}")
        base = list(updates.get(slot.key, current_values[slot.key]))
        for value in additions:
            if value not in base:
                base.append(value)
        updates[slot.key] = base

    removals = body.get("remove") or []
    if isinstance(removals, str):
        removals = [removals]
    if not isinstance(removals, list):
        return _bad("remove must be a list of keys")
    for key in removals:
        if str(key) in vault.SLOT_BY_KEY:
            updates[str(key)] = []
    if not updates:
        return _bad("nothing to save")

    report = vault.validate_update(updates, current)
    if report["errors"]:
        return web.json_response({"errors": report["errors"], "warnings": report["warnings"],
                                  "changes": report["changed"], "revision": revision,
                                  "dry_run": dry_run, "written": False}, status=400)
    rendered: dict[str, str | None] = {}
    for key, values in updates.items():
        slot = vault.SLOT_BY_KEY[key]
        if not values:
            rendered[key] = None
        elif slot.multi:
            rendered[key] = ",".join(values)
        else:
            rendered[key] = values[0]
    new_text = vault.render_env(text, rendered)
    new_revision = vault.env_revision(new_text)
    payload = {
        "dry_run": dry_run, "written": False,
        "changes": report["changed"], "unchanged": report["unchanged"],
        "warnings": report["warnings"], "revision": revision,
        "new_revision": new_revision, "path": str(path),
    }
    if new_revision == revision:
        payload.update(next="Nothing changed: the file already holds these values.",
                       bot_has_it=vault_state()["bot_has_it"])
        return web.json_response(payload)
    if dry_run:
        payload.update(next="Dry run only - nothing was written. Review the changes, "
                            "then save for real.")
        return web.json_response(payload)
    try:
        result = vault.write_env(path, new_text)
    except PermissionError as exc:
        return web.json_response(
            {"errors": [f"the dashboard cannot write {path} ({exc.strerror or exc}). "
                        f"Run the same save on the server as the owner, or make the file "
                        f"writable by the dashboard user."],
             "changes": report["changed"], "revision": revision}, status=409)
    except OSError as exc:
        return web.json_response({"errors": [f"could not write {path}: {exc}"]},
                                 status=500)
    vault.journal_append(path, {
        "action": "save", "keys": report["changed"],
        "unchanged": report["unchanged"],
        "backup": result["backup"], "revision_before": revision[:12],
        "revision_after": new_revision[:12],
    })
    payload.update(written=True, backup=result["backup"], saved_at=result["written_at"],
                   saved_ist=insight.ist(result["written_at"]),
                   bot_has_it=False,
                   next=("Saved. The running bot reads .env at start, so restart it to "
                         "apply - or use the restart button below."))
    return web.json_response(payload)


def config_revision() -> str:
    try:
        data = channels_config.channels_json_path().read_bytes()
    except FileNotFoundError:
        data = b""
    return hashlib.sha256(data).hexdigest()


def stale_config(request):
    expected = request.headers.get("If-Match")
    if expected and expected != config_revision():
        return web.json_response({"errors": ["Channels changed since you opened this form. Refresh and review before saving."],
                                  "revision": config_revision()}, status=409)
    return None


async def category_preview_response(request, batch=False):
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    try:
        result = preview_batch(body.get("items") if batch else [body],
                               channels_config.load_channels(), snapshot())
    except ValueError as exc:
        return web.json_response({"errors": [str(exc)]}, status=400)
    result["config_revision"] = config_revision()
    result["pending_restart"] = pending_restart()
    if not batch:
        result = {**result["results"][0], "notice": result["notice"],
                  "config_revision": result["config_revision"], "pending_restart": result["pending_restart"]}
    return web.json_response(result)


@require_token
async def api_category_preview(request: web.Request):
    return await category_preview_response(request)


@require_token
async def api_category_batch(request: web.Request):
    return await category_preview_response(request, batch=True)


@require_token
async def api_source_to_target(request: web.Request):
    """One source post → the whole pipeline's verdict, offline and read-only.

    Link plan (what the wrapper becomes), the price the bot will route on, the
    shelf + its evidence, the content-policy verdict, and which of OUR channels
    receive it - each with its reason. The routing map is the bot's own
    published snapshot, so this cannot disagree with the real router.
    """
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    text = body.get("text")
    source = body.get("source") or ""
    links = body.get("links") or []
    if text is None:
        text = ""
    if not isinstance(text, str) or not isinstance(source, str):
        return web.json_response({"errors": ["text and source must be strings"]}, status=400)
    if len(text) > 12000:
        return web.json_response({"errors": ["post is too long (12000 characters max)"]},
                                 status=400)
    if not isinstance(links, list) or any(not isinstance(link, str) for link in links):
        return web.json_response({"errors": ["links must be a list of URLs"]}, status=400)
    if not text.strip() and not links:
        return web.json_response({"errors": ["paste the source post (or a link)"]}, status=400)
    try:
        result = source_target.analyze(text, source, links,
                                       snapshot=snapshot(),
                                       rules=load_rules())
    except Exception as exc:  # noqa: BLE001 - a bad post must not 500 the dashboard
        return web.json_response({"errors": [f"could not analyse this post: {exc}"]},
                                 status=400)
    result["config_revision"] = config_revision()
    result["pending_restart"] = pending_restart()
    result["policy"] = source_target.policy()
    return web.json_response(result)


@require_token
async def api_inspect_link(request: web.Request):
    try:
        body = await request.json()
        result = inspect_product_link(body, channels_config.load_channels(), snapshot())
    except (ValueError, TypeError) as exc:
        return web.json_response({"errors": [str(exc)]}, status=400)
    result["config_revision"] = config_revision()
    return web.json_response(result)


def category_catalog() -> dict:
    # The installed classifier is authoritative, even while the bot is OFF or
    # its snapshot is from an older release. Configuration applies on restart.
    return {c["key"]: c["label"] for c in load_rules().get("categories", [])}


def source_catalog(snap=None, data=None):
    """All configured choices, without importing/starting Telegram or the bot."""
    snap = snapshot() if snap is None else snap
    data = channels_config.load_channels() if data is None else data
    try:
        defaults = json.loads((ROOT / "bestgaa" / "source_catalog.json").read_text())["sources"]
    except (OSError, ValueError, KeyError):
        defaults = []
    known = snap.get("sources", [])
    metadata = snap.get("source_details", {})
    main = set(snap.get("main_sources", [row["key"] for row in defaults if row.get("main")]))
    keys = [row["key"] for row in defaults] + list(known)
    keys += [source for channel in data.get("channels", []) for source in channel["sources"] if source != "*"]
    result = {}
    for value in keys:
        try:
            key = channels_config.normalize_source(value, known)
        except ValueError:
            continue
        identity = key if "/+" in key else key.lower()
        detail = metadata.get(key, {})
        title = detail.get("title") or ("Private source · …" + key[-6:] if "/+" in key else "@" + key)
        result[identity] = {"key": key, "title": str(title), "main": key in main,
                            "private": "/+" in key, "state": detail.get("state", "configured"),
                            "detail": detail.get("detail", "Access is checked by the running bot, not by saving this form.")}
    return sorted(result.values(), key=lambda item: (item["title"].casefold(), item["key"]))


def activation_status(snap=None):
    snap = snapshot() if snap is None else snap
    try:
        age = time.time() - float(snap.get("heartbeat_at") or 0)
    except (TypeError, ValueError):
        age = float("inf")
    fresh = bool(snap.get("live_reload") and 0 <= age < 40)
    same = snap.get("observed_revision") == config_revision()
    return {"watcher_online": fresh, "heartbeat_at": snap.get("heartbeat_at"),
            "new_channels_live": fresh, "new_sources_live": fresh and bool(snap.get("live_sources")), "config_observed": same,
            "channels": snap.get("live_channels", {}) if fresh and same else {},
            "error": snap.get("live_error", "") if fresh else "",
            "message": ("Bot online: new channels get access checks and live activation; joined custom sources need the source-enabled release. Existing edits/removals need an approved restart."
                        if fresh else "Live activation not confirmed: bot may be OFF, starting, or on older code. Saving alone cannot start it.")}


@require_token
@in_thread
def api_channels(request: web.Request):
    snap = snapshot()
    data = channels_config.load_channels()
    dashboard_names = {c["name"].lower() for c in data["channels"]}
    dashboard_names.update(str(n).lower() for n in snap.get("dashboard_added", []))
    targets = [str(t) for t in snap.get("targets", [])]
    try:
        wa_module = load_category_report()
        wa = wa_module.wa_surfaces(category_env(wa_module), load_rules(),
                                   channels_config.load_channels())
        whatsapp = {"live_channels": wa["channels"], "live_core": wa["core"],
                    "file": wa["file"], "file_rows": wa["file_rows"],
                    "pending": wa["pending"],
                    "env_lines": wa["env_lines"], "collisions": wa["collisions"],
                    "masked": wa["masked"]}
    except Exception as exc:                       # the panel degrades, never lies
        whatsapp = {"error": str(exc), "live_channels": {}, "live_core": {},
                    "file": {}, "file_rows": [], "pending": [], "env_lines": []}
    payload_extra = {"whatsapp": whatsapp}
    return web.json_response({**payload_extra,
        "builtin": sorted(t for t in targets if t.lower() not in dashboard_names),
        "dashboard": data["channels"],
        "revision": config_revision(),
        "activation": activation_status(snap),
        "sources": [item["key"] for item in source_catalog(snap, data)],
        "source_catalog": source_catalog(snap, data),
        "source_limit": channels_config.MAX_SOURCES,
        "categories": category_catalog(),
        "category_rules_version": load_rules().get("version", 0),
        "bot_category_rules_version": snap.get("category_rules_version"),
        "snapshot_at": snap.get("started_at", 0),
        "pending_restart": pending_restart(),
    })


@require_token
async def api_add_channel(request: web.Request):
    try:
        entry = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    snap = snapshot()
    cleaned, errors, warnings = channels_config.validate_channel(
        entry or {}, {item["key"] for item in source_catalog(snap)},
        set(snap.get("targets", [])) if snap else set(),
        set(category_catalog()))
    if errors:
        return web.json_response({"errors": errors, "warnings": warnings}, status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    data = channels_config.load_channels()
    if any(c["name"].lower() == cleaned["name"].lower() for c in data["channels"]):
        return web.json_response({"errors": [f"@{cleaned['name']} is already a dashboard channel"]},
                                 status=400)
    data["channels"].append(cleaned)
    channels_config.save_channels(data)
    mark_pending()
    return web.json_response({"channel": cleaned, "warnings": warnings, "pending_restart": True,
                              "activation": activation_status()})


# --------------------------------------------------------------------------- #
#  watched SOURCES - "channel link petti source add chesi post cheyali"
# --------------------------------------------------------------------------- #
def watch_catalog(snap=None, data=None):
    """What a source can be routed to: our channels + the main-feed default."""
    snap = snapshot() if snap is None else snap
    data = channels_config.load_channels() if data is None else data
    targets = list(snap.get("targets") or [])
    main_feed = [str(t) for t in (snap.get("main_feed") or [])]
    names = {str(c.get("name")) for c in data.get("channels", []) if c.get("name")}
    return {"targets": sorted({*targets, *names}),
            "main_feed": main_feed,
            "reported": bool(targets),
            "watch_live": bool(snap.get("live_sources")),
            "main_available": bool(main_feed)}


def watch_rows():
    data = channels_config.load_channels()
    snap = snapshot()
    states = (snap.get("watch_states") or {}) if snap.get("live_reload") else {}
    details = snap.get("source_details") or {}
    rows = []
    for entry in channels_config.watch_entries(data):
        channel = entry["channel"]
        state = states.get(channel) or {}
        detail = details.get(channel) or {}
        if not state:
            state = ({"state": "restart_required",
                      "detail": "Saved. The running bot applies it on its next sync "
                                "or an approved restart."}
                     if not snap else
                     {"state": "pending", "detail": "Saved; waiting for the bot's next sync"})
        rows.append({**entry,
                     "categories": entry.get("categories") or [],
                     "state": state.get("state", "pending"),
                     "detail": state.get("detail", ""),
                     "title": detail.get("title", ""),
                     "checked_at": detail.get("checked_at"),
                     "activated_at": detail.get("activated_at")})
    return rows


def directory_rows():
    """The channels the bot's own session can read, as the last refresh left them.

    The bot writes this file (bestgaa/channel_directory.py); the dashboard never
    asks Telegram and never guesses. A missing or corrupt file is an empty
    directory with a reason, not an error: the page must still work on a fresh
    install before the bot has ever started.
    """
    path = channels_config.directory_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"available": False, "reason": "the bot has not published its channel list yet",
                "channels": [], "generated_at": None, "age_s": None, "count": 0,
                "addable": 0, "path": str(path)}
    except (OSError, ValueError) as exc:
        return {"available": False, "reason": f"channel list unreadable ({exc})",
                "channels": [], "generated_at": None, "age_s": None, "count": 0,
                "addable": 0, "path": str(path)}
    if not isinstance(payload, dict) or not isinstance(payload.get("channels"), list):
        return {"available": False, "reason": "channel list has an unexpected shape",
                "channels": [], "generated_at": None, "age_s": None, "count": 0,
                "addable": 0, "path": str(path)}
    try:
        generated = float(payload.get("generated_at") or 0) or None
    except (TypeError, ValueError):
        generated = None
    age = (time.time() - generated) if generated else None
    return {
        "available": True,
        "reason": "",
        "channels": [row for row in payload["channels"] if isinstance(row, dict)],
        "generated_at": generated,
        "age_s": round(age, 1) if age is not None else None,
        "stale": bool(age is not None and age > 3600),
        "count": int(payload.get("count") or len(payload.get("channels") or [])),
        "addable": int(payload.get("addable") or 0),
        "our_targets": [str(t) for t in (payload.get("our_targets") or [])],
        "our_sources": [str(t) for t in (payload.get("our_sources") or [])],
        "path": str(path),
    }


def directory_view() -> dict:
    """Directory + everything needed to decide, in one object the page renders."""
    snap = snapshot()
    data = channels_config.load_channels()
    directory = directory_rows()
    watched = {str(row.get("channel", "")).lower(): row
               for row in channels_config.watch_entries(data)}
    snap_watch = snap.get("watch_states") or {}
    our_targets = {str(t).lower() for t in (snap.get("targets") or [])}
    our_targets |= {str(t).lower() for t in (directory.get("our_targets") or [])}
    channels = []
    for row in directory.get("channels") or []:
        username = str(row.get("username") or "")
        key = username.lower()
        state = (snap_watch.get(username) or snap_watch.get(str(row.get("title") or "")) or {})
        watch_row = watched.get(key) or {}
        channels.append({
            "id": row.get("id"),
            "title": str(row.get("title") or username or row.get("id")),
            "username": username,
            "kind": str(row.get("kind") or ""),
            "addable": bool(row.get("addable")),
            "private": bool(row.get("private")),
            "members": int(row.get("members") or 0),
            "last_message_at": row.get("last_message_at") or 0,
            "last_message": (insight_ts(row.get("last_message_at"))),
            "watched": bool(watch_row),
            "watch_state": state.get("state", ""),
            "watch_targets": watch_row.get("targets") or [],
            "watch_categories": watch_row.get("categories") or [],
            "is_our_target": bool(username) and key in our_targets,
            # "manam admin ga vunna channel anni chupinchali": the rights the bot
            # read off the dialog, so the page can show (and filter) them.
            # "channel link kuda": a link only when there is a real @username -
            # a made-up URL that 404s is worse than the title.
            "link": posts.channel_link(username, username),
            "role": str(row.get("role") or "member"),
            "admin": bool(row.get("admin")),
            "creator": bool(row.get("creator")),
            "can_post": bool(row.get("can_post")),
            "can_invite": bool(row.get("can_invite")),
        })
    channels.sort(key=lambda row: (not row["admin"], row["watched"], not row["addable"],
                                   row["title"].lower()))
    admin_rows = [row for row in channels if row["admin"]]
    return {
        "directory": {key: value for key, value in directory.items() if key != "channels"},
        "channels": channels,
        "admin": {
            "count": len(admin_rows),
            "creator": sum(1 for row in admin_rows if row["creator"]),
            "can_invite": sum(1 for row in admin_rows if row["can_invite"]),
            "names": [row["username"] or row["title"] for row in admin_rows],
        },
        "join_requests": join_request_rows(data),
        "watched": watch_rows(),
        "catalog": watch_catalog(snap, data),
        "categories": category_catalog(),
        "revision": config_revision(),
        "pending_restart": pending_restart(),
    }


def insight_ts(value) -> str:
    """Small local formatter so this module does not depend on a panel helper."""
    try:
        stamp = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if not stamp:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(stamp + IST_OFFSET)) + " IST"


@require_token
@in_thread
def api_directory(request: web.Request):
    """Every channel this bot session can read - the source picker's truth."""
    return web.json_response(directory_view())


# --------------------------------------------------------------------------- #
#  the operator's own inventory: every channel where WE are admin
# --------------------------------------------------------------------------- #
def join_request_rows(data: dict | None = None) -> dict:
    """Auto-approval settings + what the bot has actually done, for the page."""
    try:
        sys.path.insert(0, str(ROOT / "bestgaa"))
        import join_gate  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        join_gate = None
    data = channels_config.load_channels() if data is None else data
    setting = join_gate.section(data) if join_gate else {"enabled": False, "delay": 2.5,
                                                         "channels": {}}
    log_file = channels_config.pending_path().parent / "join_requests_log.json"
    try:
        log_rows = json.loads(log_file.read_text(encoding="utf-8"))
        if not isinstance(log_rows, list):
            log_rows = []
    except (OSError, ValueError):
        log_rows = []
    approved = [row for row in log_rows if row.get("action") == "approved"]
    return {
        "enabled": setting["enabled"],
        "delay": setting["delay"],
        "channels": [{"name": row["name"], "enabled": row["enabled"],
                      "delay": row["delay"], "per_minute": row["per_minute"]}
                     for row in setting["channels"].values()],
        "approved": len(approved),
        "refused": sum(1 for row in log_rows if row.get("action") in ("skip", "hold", "failed")),
        "last": [{"at": insight_ts(row.get("at")), "channel": row.get("channel"),
                  "user_id": row.get("user_id"), "action": row.get("action"),
                  "reason": str(row.get("reason") or "")[:160]}
                 for row in log_rows[-8:]][::-1],
        "log_path": str(log_file),
        "note": ("Off by default. When a channel is ticked, the bot approves join "
                 "requests to it after the chosen delay (2.5s by default), capped per "
                 "minute and written to the log. It never joins anything itself."),
    }


@require_token
async def api_join_requests(request: web.Request) -> web.Response:
    """Read or change auto-approval. Body: {enabled?, delay?, channels?: [...]}."""
    data = channels_config.load_channels()
    if request.method == "GET":
        return web.json_response({**join_request_rows(data),
                                  "revision": config_revision(), "admin": (
                                      directory_view().get("admin") or {})})
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    try:
        sys.path.insert(0, str(ROOT / "bestgaa"))
        import join_gate  # noqa: PLC0415
    except ImportError:
        return web.json_response({"errors": ["join_gate.py is not deployed yet"]}, status=409)

    # Only channels where the bot reported admin rights may be offered: approving
    # join requests without the "invite users" right fails at Telegram anyway, and
    # a silent failure is what this whole page exists to avoid.
    admin = {str(name).lower() for name in (directory_view().get("admin") or {}).get("names") or []}
    known = admin | {str(row.get("name", "")).lower() for row in data.get("channels", [])}
    raw = body.get("channels")
    errors: list[str] = []
    warnings: list[str] = []
    if raw is None:
        current = (data.get("join_requests") or {}).get("channels") or []
        keep = []
        for row in current:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            if body.get("enabled") is not None or "delay" in body:
                keep.append({**row, "name": name})
            else:
                keep.append(row)
        channels = keep
    else:
        if not isinstance(raw, list):
            return web.json_response({"errors": ["channels must be a list"]}, status=400)
        if len(raw) > 200:
            return web.json_response({"errors": ["at most 200 channels"]}, status=400)
        channels = []
        for value in raw:
            if not isinstance(value, dict):
                errors.append("each channel must be an object")
                continue
            clean, bad, notes = join_gate.validate(value.get("name") or value.get("channel"),
                                                   value, known)
            warnings.extend(notes)
            if bad:
                errors.extend(bad)
                continue
            channels.append(clean)
    if errors:
        return web.json_response({"errors": errors, "warnings": warnings}, status=400)
    # ONE schema, two processes: the delay is judged by the bot's own module, so
    # the bot can never read a shape the dashboard never validated.
    current = data.get("join_requests") or {}
    delay = body.get("delay", current.get("delay", 2.5))
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        return web.json_response({"errors": ["delay must be a number of seconds"]}, status=400)
    if not join_gate.MIN_DELAY <= delay <= join_gate.MAX_DELAY:
        return web.json_response(
            {"errors": [f"delay must be between {join_gate.MIN_DELAY} and "
                        f"{join_gate.MAX_DELAY} seconds"]}, status=400)
    data["join_requests"] = {"enabled": bool(body.get("enabled", current.get("enabled", False))),
                             "delay": round(delay, 1),
                             "channels": channels}
    channels_config.save_channels(data)
    mark_pending()
    return web.json_response({**join_request_rows(channels_config.load_channels()),
                              "saved": True, "revision": config_revision(),
                              "warnings": sorted(set(warnings))})


# --------------------------------------------------------------------------- #
#  one click: OUR post -> our other channels
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  every post, with the buttons that act on it
# --------------------------------------------------------------------------- #
def our_channel_names() -> list[str]:
    """Every channel this account may post to, for the "Send to…" picker."""
    names: list[str] = []
    snap = snapshot()
    names.extend(str(name) for name in (snap.get("targets") or []))
    names.extend(str(row.get("name")) for row in channels_config.load_channels().get(
        "channels", []) if row.get("name"))
    try:
        for row in directory_rows().get("channels") or []:
            if row.get("can_post") or row.get("admin"):
                handle = row.get("username") or row.get("title")
                if handle:
                    names.append(str(handle))
    except Exception:  # noqa: BLE001 - a missing directory must not blank the panel
        pass
    seen: list[str] = []
    for name in names:
        handle = posts.clean_handle(name)
        if handle and handle.lower() not in {item.lower() for item in seen}:
            seen.append(handle)
    return seen


def job_ids(conn: sqlite3.Connection) -> set[int]:
    try:
        return {int(row[0]) for row in conn.execute("SELECT id FROM queue")}
    except sqlite3.OperationalError:
        return set()


@require_token
@in_thread
def api_posts(request: web.Request) -> web.Response:
    """The posts feed: where each post went, where it came from, what it can do."""
    try:
        limit = max(1, min(100, int(request.query.get("limit", "40"))))
    except ValueError:
        limit = 40
    conn = read_only_db()
    rows: list[dict] = []
    if conn is not None:
        try:
            rows = posts.feed(conn, limit=limit, ours=our_channel_names())
        except sqlite3.OperationalError as exc:
            return web.json_response({"posts": [], "errors": [f"queue unreadable: {exc}"]})
        finally:
            conn.close()
    return web.json_response({
        "posts": rows,
        "ours": our_channel_names(),
        "queued": posts.pending(done=posts.finished_ids()),
        "log": posts.read_log()[-10:][::-1],
        "paths": {"requests": str(posts.request_path()), "log": str(posts.log_path())},
        "note": ("'Send to…' queues one request; the BOT re-checks the post, the channel "
                 "and whether that channel already carries it, then sends and records the "
                 "outcome. The dashboard never writes the bot's database."),
    })


@require_token
async def api_post_send(request: web.Request) -> web.Response:
    """One press: put this post into another of our channels."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    conn = read_only_db()
    known: set[int] = set()
    if conn is not None:
        try:
            known = job_ids(conn)
        finally:
            conn.close()
    if conn is None:
        return web.json_response(
            {"errors": ["the bot's database is not readable - nothing was queued. "
                        "Check BOT_DB_PATH / BESTGAA_DIR."]}, status=409)
    clean, errors = posts.submit(body, job_ids=known, ours=our_channel_names())
    if errors:
        return web.json_response({"errors": errors, "request": clean}, status=400)
    return web.json_response({"queued": clean, "next": (
        "The bot sends it within a few seconds (MANUAL_SEND_POLL_SECONDS, default 5) "
        "and records the result in the send log. A channel that already carries this "
        "post is skipped, and the log says so.")})


@require_token
async def api_repost(request: web.Request) -> web.Response:
    """Queue a forward of one of our posts. The BOT does the forwarding."""
    try:
        sys.path.insert(0, str(ROOT / "bestgaa"))
        import repost  # noqa: PLC0415
    except ImportError:
        return web.json_response({"errors": ["repost.py is not deployed yet"]}, status=409)
    # "Our channels" = everything this account may post to: the bot's routing
    # targets, the configured dashboard channels, and the admin inventory it
    # published (a channel can be ours before it is ever configured).
    try:
        inventory = [row for row in directory_rows().get("channels") or []
                     if row.get("can_post") or row.get("admin")]
    except Exception:  # noqa: BLE001 - a missing directory must not block the button
        inventory = []
    ours = sorted({*(snapshot().get("targets") or []),
                   *(row.get("name") for row in channels_config.load_channels().get(
                       "channels", []) if row.get("name")),
                   *(row.get("username") or row.get("title") for row in inventory
                     if (row.get("username") or row.get("title")))})
    ours = [name for name in ours if name]
    if request.method == "GET":
        return web.json_response({
            "ours": ours,
            "queued": repost.queued(repost.request_path(), repost.done_ids()),
            "log": repost.read_log()[-10:][::-1],
            "note": ("The bot forwards, not the dashboard: it owns the session. Each press "
                     "is one request in the log, forwarded once."),
        })
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    clean, errors = repost.submit(body, ours)
    if errors:
        return web.json_response({"errors": errors, "request": clean}, status=400)
    return web.json_response({"queued": clean, "next": (
        "The bot forwards it within a few seconds (REPOST_POLL_SECONDS, default 5) and "
        "writes the result to the repost log. Nothing is re-sent: each request id is "
        "forwarded once.")})


# --------------------------------------------------------------------------- #
#  OUR CHANNELS: attach what already runs, pause it, suggest the next one
# --------------------------------------------------------------------------- #
def own_inputs() -> tuple[list, list, list]:
    """(directory rows, our target names, dashboard names) - one lookup, three uses."""
    try:
        rows = [row for row in directory_rows().get("channels") or [] if isinstance(row, dict)]
    except Exception:  # noqa: BLE001 - a missing directory must not blank the card
        rows = []
    snap = snapshot()
    targets = [str(name) for name in (snap.get("targets") or [])]
    dashboard_names = [str(row.get("name")) for row in channels_config.load_channels().get(
        "channels", []) if row.get("name")]
    return rows, targets, dashboard_names


def own_view() -> dict:
    """Everything the card renders: our channels, the candidates, the pause state."""
    rows, targets, dashboard_names = own_inputs()
    data = channels_config.load_channels()
    watched = [str(row.get("channel")) for row in channels_config.watch_entries(data)]
    view = channels_attach.inventory(data=data, targets=targets,
                                     dashboard_names=dashboard_names, directory_rows=rows,
                                     watch_channels=watched)
    snap = snapshot()
    results = channel_suggest.read_results()
    answers = list((results.get("requests") or {}).values())
    answers.sort(key=lambda row: row.get("at") or 0, reverse=True)
    view.update({
        "attached": channels_attach.entries(data),
        "paused": sorted(channels_attach.paused(data)),
        "suggestions": answers[:5],
        "suggest_queue": channel_suggest.pending(done=channel_suggest.done_ids()),
        "directory_available": bool(directory_rows().get("available")),
        "directory_age_s": directory_rows().get("age_s"),
        "revision": config_revision(),
        "pending_restart": pending_restart(),
        "paths": {"requests": str(channel_suggest.request_path()),
                  "answers": str(channel_suggest.result_path())},
        "bot_online": activation_status(snap).get("watcher_online", False),
    })
    return view


@require_token
@in_thread
def api_own(request: web.Request):
    """Our channels in ONE list: what runs from code, from the dashboard, attached."""
    return web.json_response(own_view())


def _own_name(body) -> str:
    return channels_attach.handle(body.get("name") or body.get("channel") or body.get("link"))


@require_token
async def api_own_attach(request: web.Request) -> web.Response:
    """Attach an EXISTING running channel so the dashboard can manage it."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    rows, targets, dashboard_names = own_inputs()
    catalog = watch_catalog()
    data = channels_config.load_channels()
    watched = [str(row.get("channel")) for row in channels_config.watch_entries(data)]
    result = channels_attach.attach(
        body, directory_rows=rows,
        targets=list(dict.fromkeys([*targets, *dashboard_names])),
        known_sources=set(catalog["targets"]) | set(catalog["main_feed"]),
        known_categories=set(category_catalog()), watch_channels=watched)
    if result["errors"]:
        return web.json_response({"errors": result["errors"], "warnings": result["warnings"],
                                  "view": own_view()}, status=400)
    mark_pending()
    attached = result["attached"]
    return web.json_response({
        "attached": attached,
        "channel": result["channel"],
        "warnings": result["warnings"],
        "view": own_view(),
        "next": (f"@{attached['name']} is attached as {'a dashboard channel' if attached['mode'] == 'new' else 'an existing target'}. "
                 + ("The bot starts posting there once it picks the config up."
                    if attached["mode"] == "new" else
                    "Nothing about its routing changed - pause, note and suggest now apply to it.")),
    })


@require_token
async def api_own_detach(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    result = channels_attach.detach(_own_name(body))
    if result["errors"]:
        return web.json_response({"errors": result["errors"], "view": own_view()}, status=400)
    mark_pending()
    return web.json_response({**result, "view": own_view(),
                              "next": ("Detached. A channel the dashboard created is no longer "
                                       "a target; a code target keeps posting, because code is "
                                       "not something a detach can edit.")})


@require_token
async def api_own_posting(request: web.Request) -> web.Response:
    """Pause / resume one channel - effective on the NEXT post, no restart."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    posting = body.get("posting")
    if not isinstance(posting, bool):
        return web.json_response({"errors": ["posting must be true or false"]}, status=400)
    result = channels_attach.set_posting(_own_name(body), posting)
    if result["errors"]:
        return web.json_response({"errors": result["errors"], "view": own_view()}, status=400)
    mark_pending()
    name = (result["row"] or {}).get("name") or _own_name(body)
    return web.json_response({**result, "view": own_view(),
                              "next": (f"@{name} is paused: it receives nothing new, and no "
                                       "delivery row is written for it. Resuming applies to "
                                       "future posts - nothing is replayed."
                                       if not posting else
                                       f"@{name} is posting again from the next post on. "
                                       "Posts that arrived while it was paused are not replayed.")})


@require_token
async def api_own_suggest(request: web.Request) -> web.Response:
    """GET: answers so far. POST: ask the bot to suggest channels from one of ours."""
    if request.method == "GET":
        view = own_view()
        return web.json_response({"suggestions": view["suggestions"],
                                  "queued": view["suggest_queue"],
                                  "paths": view["paths"],
                                  "note": ("The BOT answers (it owns the session): Telegram's "
                                           "similar channels plus the channels this one links "
                                           "to. Nothing is joined or posted.")})
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    ours = our_channel_names()
    clean, errors = channel_suggest.submit(body, ours)
    if errors:
        return web.json_response({"errors": errors, "ours": ours}, status=400)
    return web.json_response({
        "queued": clean,
        "next": (f"The bot will look at @{clean['channel']} within a few seconds "
                 "(SUGGEST_POLL_SECONDS, default 6) and write what it finds here. "
                 "Nothing is joined and nothing is posted."),
        "poll": "/api/own/suggest",
    })


@require_token
async def api_channels_bulk(request: web.Request) -> web.Response:
    """Several channels, one action, one atomic save - the "multiple buttons" bar.

    Actions: enable, disable, remove, category (add/clear), target (add selected
    as destinations with the same sources/categories). Partial success is explicit:
    everything that worked is saved, everything refused comes back with a reason.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    action = str(body.get("action") or "").strip().lower()
    names = body.get("channels") or body.get("names") or []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not names:
        return web.json_response({"errors": ["select at least one channel"]}, status=400)
    if len(names) > 100:
        return web.json_response({"errors": ["at most 100 channels per action"]}, status=400)
    if action not in ("enable", "disable", "remove", "category", "target"):
        return web.json_response(
            {"errors": [f"unknown action {action!r} - use enable/disable/remove/category/target"]},
            status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    data = channels_config.load_channels()
    rows = data.setdefault("channels", [])
    by_name = {str(row.get("name", "")).lower(): row for row in rows}
    done: list[str] = []
    skipped: list[dict] = []
    snap = snapshot()
    known_targets = set(snap.get("targets", [])) if snap else set()
    known_sources = {item["key"] for item in source_catalog(snap)}
    known_categories = set(category_catalog())

    for value in names:
        key = str(value or "").strip().lstrip("@").lower()
        if not key:
            continue
        if action == "target":
            if key in by_name:
                skipped.append({"channel": key, "errors": ["already a dashboard channel"]})
                continue
            entry = {"name": key,
                     "sources": body.get("sources") or ["*"],
                     "categories": body.get("categories") or [],
                     "amazon_tag": bool(body.get("amazon_tag", False)),
                     "enabled": True,
                     "note": str(body.get("note", "added in bulk"))[:200]}
            cleaned, errors, _warnings = channels_config.validate_channel(
                entry, known_sources, known_targets, known_categories)
            if errors:
                skipped.append({"channel": key, "errors": errors})
                continue
            rows.append(cleaned)
            by_name[cleaned["name"].lower()] = cleaned
            done.append(cleaned["name"])
            continue
        row = by_name.get(key)
        if row is None:
            skipped.append({"channel": key, "errors": ["not a dashboard channel"]})
            continue
        if action == "enable":
            row["enabled"] = True
        elif action == "disable":
            row["enabled"] = False
        elif action == "remove":
            rows.remove(row)
            by_name.pop(key, None)
        elif action == "category":
            cats = body.get("categories")
            if cats is None:
                row["categories"] = []
            else:
                if isinstance(cats, str):
                    cats = [cats]
                bad = [c for c in cats
                       if known_categories and str(c).strip().lower() not in
                       {k.lower() for k in known_categories}]
                if bad:
                    skipped.append({"channel": key,
                                    "errors": [f"unknown category {bad[0]!r}"]})
                    continue
                row["categories"] = [str(c).strip().lower() for c in cats if str(c).strip()]
        done.append(key)
    if not done:
        return web.json_response({"done": [], "skipped": skipped,
                                  "revision": config_revision()}, status=400)
    channels_config.save_channels(data)
    mark_pending()
    return web.json_response({"action": action, "done": done, "skipped": skipped,
                              "revision": config_revision(), "pending_restart": True})


@require_token
async def api_watch_bulk(request: web.Request):
    """Add MANY sources in one save: pick from the directory, choose targets+categories.

    Partial success is deliberate and explicit: one bad link must not throw away
    the nineteen good ones, but every refusal comes back with its reason and the
    count, so nothing is silently dropped from the operator's selection.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = None
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    raw = body.get("channels")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return web.json_response({"errors": ["channels must be a non-empty list"]}, status=400)
    if len(raw) > 50:
        return web.json_response({"errors": ["select at most 50 channels per save"]}, status=400)
    targets = body.get("targets", ["*"])
    if isinstance(targets, str):
        targets = [targets]
    categories = body.get("categories", [])
    if isinstance(categories, str):
        categories = [categories]
    if not isinstance(targets, list) or not isinstance(categories, list):
        return web.json_response({"errors": ["targets and categories must be lists"]}, status=400)

    catalog = watch_catalog()
    known_targets = set(catalog["targets"]) | set(catalog["main_feed"])
    known_categories = set(category_catalog())
    data = channels_config.load_channels()
    existing = {str(row.get("channel", "")).lower() for row in data.get("watch", [])}
    if len(data.get("watch") or []) + len(raw) > channels_config.WATCH_MAX:
        return web.json_response(
            {"errors": [f"at most {channels_config.WATCH_MAX} watched sources "
                        f"({len(data.get('watch') or [])} saved already)"]}, status=400)

    added: list[dict] = []
    skipped: list[dict] = []
    warnings: list[str] = []
    for value in raw:
        entry = {"channel": value, "targets": targets, "categories": categories,
                 "enabled": bool(body.get("enabled", True)),
                 "note": str(body.get("note", ""))}
        cleaned, errors, notes = channels_config.validate_watch(
            entry, known_targets, known_targets=known_targets,
            known_categories=known_categories)
        warnings.extend(notes)
        if errors:
            skipped.append({"channel": str(value)[:80], "errors": errors})
            continue
        if cleaned["channel"].lower() in existing:
            skipped.append({"channel": cleaned["channel"],
                            "errors": ["already watched"]})
            continue
        existing.add(cleaned["channel"].lower())
        added.append(cleaned)
    if not added:
        return web.json_response({"added": [], "skipped": skipped,
                                  "warnings": sorted(set(warnings)),
                                  "revision": config_revision()}, status=400)
    data.setdefault("watch", []).extend(added)
    channels_config.save_channels(data)
    mark_pending()
    plan_targets: list[str] = []
    if catalog["main_feed"]:
        plan = channels_config.apply_watch_to_routing(
            {}, {"watch": added}, catalog["main_feed"])
        if plan["watching"]:
            plan_targets = plan["watching"][0]["targets"]
    return web.json_response({
        "added": [row["channel"] for row in added],
        "skipped": skipped,
        "warnings": sorted(set(warnings)),
        "revision": config_revision(),
        "plan": {"targets": plan_targets, "main_feed": catalog["main_feed"]},
        "next": ("The bot checks access with its own session and activates each source "
                 "on its next pass; every new post from them is converted and posted to "
                 + (", ".join(plan_targets) if plan_targets else "the main feed once it reports")
                 + ". Nothing is joined and no old post is replayed."),
    })


@require_token
@in_thread
def api_watch_list(request: web.Request):
    snap = snapshot()
    return web.json_response({"watch": watch_rows(), "catalog": watch_catalog(snap),
                              "activation": activation_status(snap),
                              "revision": config_revision()})


@require_token
async def api_watch_add(request: web.Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = None
    if not isinstance(body, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    snap = snapshot()
    catalog = watch_catalog(snap)
    cleaned, errors, warnings = channels_config.validate_watch(
        body, set(catalog["targets"]) | set(catalog["main_feed"]),
        known_targets=set(catalog["targets"]) | set(catalog["main_feed"]),
        known_categories=set(category_catalog()))
    if errors:
        return web.json_response({"errors": errors, "warnings": warnings,
                                  "catalog": catalog}, status=400)
    data = channels_config.load_channels()
    existing = {str(row.get("channel", "")).lower() for row in data.get("watch", [])}
    if cleaned["channel"].lower() in existing:
        return web.json_response({"errors": [f"{cleaned['channel']} is already watched"],
                                  "warnings": warnings}, status=400)
    data.setdefault("watch", []).append(cleaned)
    channels_config.save_channels(data)
    mark_pending()
    # Say where it will post, using the bot's own report. With no report there is
    # nothing honest to promise, so the plan is empty rather than invented.
    routes_to: list[str] = []
    if catalog["main_feed"]:
        plan = channels_config.apply_watch_to_routing(
            {str(k): list(v) for k, v in (snap.get("source_targets") or {}).items()},
            {"watch": [cleaned]}, catalog["main_feed"])
        if plan["watching"]:
            routes_to = plan["watching"][0]["targets"]
    return web.json_response({
        "source": cleaned,
        "warnings": warnings,
        "revision": config_revision(),
        "plan": {"routes_to": routes_to, "main_feed": catalog["main_feed"]},
        "next": ("The bot will check access with its own session and start watching "
                 "this channel; its next posts are converted and posted to "
                 + (", ".join(routes_to) if routes_to else "the main feed once it reports its channels")
                 + ". Nothing is joined automatically and old posts are not replayed."),
        "activation": activation_status(snap),
    })


def _watch_entry(data: dict, channel: str):
    key = str(channel or "").lower()
    for index, row in enumerate(data.get("watch") or []):
        if str(row.get("channel", "")).lower() == key:
            return index, row
    return None, None


@require_token
async def api_watch_edit(request: web.Request):
    channel = request.match_info.get("channel", "")
    try:
        patch = await request.json()
    except (json.JSONDecodeError, ValueError):
        patch = None
    if not isinstance(patch, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    data = channels_config.load_channels()
    index, entry = _watch_entry(data, channel)
    if entry is None:
        return web.json_response({"errors": ["no such watched source"]}, status=404)
    catalog = watch_catalog()
    merged = {**entry, **{k: v for k, v in patch.items()
                          if k in ("targets", "categories", "enabled", "note")}}
    cleaned, errors, warnings = channels_config.validate_watch(
        merged, set(catalog["targets"]) | set(catalog["main_feed"]),
        known_targets=set(catalog["targets"]) | set(catalog["main_feed"]),
        known_categories=set(category_catalog()))
    if errors:
        return web.json_response({"errors": errors, "warnings": warnings}, status=400)
    data["watch"][index] = cleaned
    channels_config.save_channels(data)
    mark_pending()
    return web.json_response({"source": cleaned, "warnings": warnings,
                              "revision": config_revision()})


@require_token
async def api_watch_delete(request: web.Request):
    channel = request.match_info.get("channel", "")
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    data = channels_config.load_channels()
    index, entry = _watch_entry(data, channel)
    if entry is None:
        return web.json_response({"errors": ["no such watched source"]}, status=404)
    data["watch"].pop(index)
    channels_config.save_channels(data)
    mark_pending()
    return web.json_response({"removed": entry["channel"], "revision": config_revision()})


@require_token
async def api_edit_channel(request: web.Request):
    name = channels_config.normalize_handle(request.match_info.get("name", "")).lower()
    try:
        patch = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(patch, dict):
        return web.json_response({"errors": ["body must be a JSON object"]}, status=400)
    conflict = stale_config(request)
    if conflict is not None:
        return conflict
    data = channels_config.load_channels()
    entry = next((c for c in data["channels"] if c["name"].lower() == name), None)
    if entry is None:
        return web.json_response({"errors": ["no such dashboard channel"]}, status=404)
    merged = dict(entry)
    for field in ("sources", "categories", "amazon_tag", "enabled", "note"):
        if field in (patch or {}):
            merged[field] = patch[field]
    snap = snapshot()
    others = {t for t in snap.get("targets", [])} if snap else set()
    cleaned, errors, warnings = channels_config.validate_channel(
        merged, {item["key"] for item in source_catalog(snap)}, others - {entry["name"]},
        set(category_catalog()))
    # validate_channel rejects names already in use: ours is the entry itself.
    errors = [e for e in errors if "already a channel" not in e]
    if errors:
        return web.json_response({"errors": errors, "warnings": warnings}, status=400)
    entry.update({k: v for k, v in cleaned.items() if k != "name"})
    channels_config.save_channels(data)
    mark_pending()
    return web.json_response({"channel": entry, "warnings": warnings, "pending_restart": True})


@require_token
@in_thread
def api_delete_channel(request: web.Request):
    name = channels_config.normalize_handle(request.match_info.get("name", "")).lower()
    data = channels_config.load_channels()
    kept = [c for c in data["channels"] if c["name"].lower() != name]
    if len(kept) == len(data["channels"]):
        return web.json_response({"errors": ["no such dashboard channel"]}, status=404)
    data["channels"] = kept
    channels_config.save_channels(data)
    mark_pending()
    return web.json_response({"deleted": name, "pending_restart": True})


@require_token
@in_thread
def api_recent(request: web.Request):
    try:
        limit = max(1, min(100, int(request.query.get("limit", "20"))))
    except ValueError:
        limit = 20
    conn = read_only_db()
    jobs: list[dict] = []
    if conn is not None:
        try:
            cols = delivery_columns(conn)
            if "last_error" in cols:
                sends_expr = "SUM(CASE WHEN d.status='sent' AND COALESCE(d.last_error,'')='' THEN 1 ELSE 0 END)"
                skips_expr = ("SUM(CASE WHEN d.status='skipped' OR (d.status='sent' AND "
                              "COALESCE(d.last_error,'')<>'') THEN 1 ELSE 0 END)")
            else:
                sends_expr = "SUM(CASE WHEN d.status='sent' THEN 1 ELSE 0 END)"
                skips_expr = "SUM(CASE WHEN d.status='skipped' THEN 1 ELSE 0 END)"
            try:
                rows = conn.execute(
                    f"SELECT q.id, q.source, q.status, q.created_at, q.last_error, "
                    f"q.rendered_text, "
                    f"{sends_expr} AS sends, {skips_expr} AS skips, "
                    "SUM(CASE WHEN d.status='held' THEN 1 ELSE 0 END) AS held FROM queue q "
                    "LEFT JOIN deliveries d ON d.queue_id=q.id "
                    "GROUP BY q.id ORDER BY q.id DESC LIMIT ?", (limit,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                text = str(row["rendered_text"] or "")
                match = re.search(r"https?://[^\s<>()\[\]]+", text)
                jobs.append({
                    "id": row["id"], "source": row["source"], "status": row["status"],
                    "created_at": row["created_at"], "error": (row["last_error"] or "")[:120],
                    "snippet": " ".join(text.split())[:240],
                    "link": match.group(0) if match else "",
                    "sends": int(row["sends"] or 0), "skips": int(row["skips"] or 0),
                    "held": int(row["held"] or 0),
                })
        finally:
            conn.close()
    return web.json_response({"jobs": jobs})


def load_quality_audit():
    spec = importlib.util.spec_from_file_location("dash_quality_audit", ROOT / "ops" / "quality_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_category_report():
    """The category-wise report engine, shared with `python3 ops/category_report.py`.

    One implementation, two front doors: the CLI an operator runs on the server
    and this dashboard read the same code, so the numbers cannot disagree.
    """
    spec = importlib.util.spec_from_file_location(
        "dash_category_report", ROOT / "ops" / "category_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_deal_card():
    """The deal-card engine, shared with `python3 ops/deal_card.py`.

    Same reason the categories endpoint loads its engine by path: the CLI an
    operator runs on the server and this dashboard must draw the same card from
    the same caption, so there is one implementation and two front doors.
    """
    spec = importlib.util.spec_from_file_location(
        "dash_deal_card", ROOT / "ops" / "deal_card.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def category_env(module) -> dict[str, str]:
    """Env-declared shelf targets, without ever echoing a secret value.

    Only the channel-plumbing keys are read (TG_CATEGORY_*_TARGET,
    AMAZON_TAG*, WA_CHANNEL_*, WA_AMAZON_TAG_CATEGORY_TARGETS); the module keeps
    nothing else from a .env, so a token in the same file cannot reach this
    response. WhatsApp values are masked by the module unless they are a plain
    JID, so an invite link is never echoed either.
    """
    env = module.load_env()
    for key, value in os.environ.items():
        if (key.startswith(("TG_CATEGORY_", "WA_CHANNEL_"))
                or key in ("AMAZON_TAG_CATEGORY_TARGETS", "WA_AMAZON_TAG_CATEGORY_TARGETS",
                           "AMAZON_TAG")):
            env.setdefault(key, str(value).strip())
    return env


@require_token
@in_thread
def api_categories(request: web.Request):
    """Per-shelf truth: sends, skips, backlog, routed vs withheld, and findings.

    Also per shelf: whether the links it published pay us (`money`), the IST-day
    trend (`history`), and how the same shelf is configured on Telegram and on
    WhatsApp (`surfaces`). `expand=<key>` adds the newest delivery rows for that
    one shelf so a number can be opened instead of argued with.

    Read-only like every other endpoint here: the database is opened mode=ro and
    nothing is queued, restarted or sent.
    """
    try:
        hours = max(1.0, min(168.0, float(request.query.get("hours", "24"))))
    except (TypeError, ValueError):
        hours = 24.0
    try:
        days = max(1, min(30, int(request.query.get("days", "7"))))
    except (TypeError, ValueError):
        days = 7
    try:
        expand_limit = max(1, min(50, int(request.query.get("expand_limit", "12"))))
    except (TypeError, ValueError):
        expand_limit = 12
    # The drill-down key is a shelf key, "main", or the "target:Name" the report
    # itself produced. Anything else is dropped rather than passed on, so a query
    # string can never become anything the engine did not already name.
    expand = request.query.get("expand", "") or ""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", expand):
        expand = ""
    try:
        module = load_category_report()
        report = module.build_report(
            db=db_path(), log=module.default_log(), hours=hours,
            channels_data=channels_config.load_channels(), rules=load_rules(),
            env=category_env(module), snapshot=snapshot(), days=days,
            expand=expand or None, expand_limit=expand_limit)
    except sqlite3.OperationalError as exc:
        return web.json_response({"error": f"database not readable yet ({exc})"}, status=503)
    payload = module.jsonable(report)
    payload["db_exists"] = db_path().exists()
    return web.json_response(payload)


@require_token
@in_thread
def api_card_png(request: web.Request):
    """The raster deal card for one caption, exactly as the CLI would draw it.

    Preview only: it renders and returns bytes. Nothing is queued, nothing is
    sent, and the send path still posts what it posts today.
    """
    text = request.query.get("text", "") or ""
    if len(text) > 6000 or not text.strip():
        return web.json_response({"error": "text is required (max 6000 chars)"}, status=400)
    try:
        width = max(320, min(1600, int(request.query.get("width", "1080"))))
    except (TypeError, ValueError):
        width = 1080
    try:
        module = load_deal_card()
        png = module.card_png(module.deal_facts(text), width=width)
    except module.CardUnavailable as exc:
        return web.json_response({"error": str(exc)}, status=503)
    except Exception as exc:                       # a bad photo must not 500 the UI
        return web.json_response({"error": f"card render failed: {exc}"}, status=400)
    return web.Response(body=png, content_type="image/png",
                        headers={"Cache-Control": "no-store"})


@require_token
@in_thread
def api_card_studio(request: web.Request):
    """Everything the card studio shows for one caption: the bot's own reading of
    price/discount/coupon, the caption per surface, and which destinations would
    receive card+caption (and which would skip it, with the reason).

    Read-only: the link verdict opens the database mode=ro via ops/deal_card.py,
    and the plan reads channels.json and the category rules the bot and bridge
    both use. Nothing is written and nothing is sent.
    """
    text = request.query.get("text", "") or ""
    link = request.query.get("link", "") or ""
    if len(text) > 6000 or not text.strip():
        return web.json_response({"error": "text is required (max 6000 chars)"}, status=400)
    if len(link) > 500:
        return web.json_response({"error": "link too long (max 500 chars)"}, status=400)
    try:
        module = load_deal_card()
        payload = module.studio(text, link=link, db=str(db_path()))
    except Exception as exc:
        return web.json_response({"error": f"card studio failed: {exc}"}, status=503)
    payload["db_exists"] = db_path().exists()
    payload["preview_only"] = True
    return web.json_response(payload)


@require_token
async def api_put_whatsapp(request: web.Request):
    """Save the WhatsApp channels the dashboard declares (channels.json
    `whatsapp`), with the exact env lines the bridge needs to activate them.

    The bridge builds its WhatsApp policy from `WA_CHANNEL_<KEY>` env at startup
    and this dashboard never restarts it, so saving here records intent and
    generates the lines; `/api/categories` then shows the row as pending until
    the env catches up. Collisions (one channel for two keys) are refused at
    save time because the bridge would silently stop filtering one shelf.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    # A PUT replaces the whole list, so a body without the key must be refused:
    # `{}` used to mean "empty list" and silently wiped every WhatsApp channel
    # the operator had configured (same trap as the button row below).
    if not isinstance(body, dict) or "whatsapp" not in body:
        return web.json_response(
            {"errors": ["whatsapp must be a list - send the whole list you want "
                        "saved, or [] to clear it on purpose"]}, status=400)
    cleaned, errors = channels_config.validate_whatsapp_list(
        body.get("whatsapp"), set(category_catalog()))
    if errors:
        return web.json_response({"errors": errors}, status=400)
    data = channels_config.load_channels()
    data["whatsapp"] = cleaned
    channels_config.save_channels(data)
    return web.json_response({"saved": True, "whatsapp": cleaned,
                              "env_lines": channels_config.wa_env_lines(cleaned)})


def buttons_view(config: dict | None = None) -> dict:
    """The button config AND what it will actually draw under each channel's posts.

    "mana existing and new ga add chese channels ki easy ga cheselaga undali" -
    the page must show the resolved row per channel, not just the config: a
    channel on `auto` shows the buttons it will carry, built from our OTHER
    channels, with the channel itself excluded. `effective_row` is the same
    function the bot's send path uses, so the preview cannot disagree with what
    goes out.
    """
    import post_buttons
    config = post_buttons.load_config() if config is None else config
    ours = post_buttons.our_channels()
    titles = post_buttons.title_map(ours)
    names = [row["name"] for row in ours]
    channels = []
    for row in ours:
        preview = post_buttons.effective_row(row["name"], ours=names, titles=titles)
        channels.append({**row, **{key: preview[key] for key in
                                   ("mode", "buttons", "where", "configured", "every",
                                    "every_own")},
                         "is_self_excluded": True,
                         # What the next posts will do, so "every 10th" is visible
                         # as a schedule instead of a number.
                         "schedule": post_buttons.preview(preview["every"], 12)})
    return {
        "buttons": config["buttons"],
        "per_channel": [{"name": row["name"], "mode": row["mode"],
                         "buttons": row["buttons"]}
                        for row in config["per_channel"].values()],
        "auto_label": config["auto_label"],
        "auto_limit": config["auto_limit"],
        "every": config["every"],
        "schedule": post_buttons.preview(config["every"], 12),
        "channels": channels,
        "presets": post_buttons.preset_buttons(),
        "max_buttons": post_buttons.MAX_BUTTONS,
        "max_label": post_buttons.MAX_LABEL,
        "allowed_hosts": list(post_buttons.ALLOWED_HOSTS),
        "path": str(post_buttons.config_path()),
        "note": ("One row per channel: 'default' uses the shared row, 'auto' builds it from "
                 "your other channels (never the channel itself), 'custom' is an explicit "
                 "row, 'none' posts without buttons. The bot resolves the same way, so this "
                 "preview is what a reader sees."),
    }


@require_token
@in_thread
def api_post_buttons_get(request: web.Request):
    """The configured Telegram button rows - the default one and per channel."""
    return web.json_response(buttons_view())


@require_token
async def api_post_buttons_put(request: web.Request):
    """Save the button rows. Validation lives in post_buttons, so the CLI, the bot
    and this endpoint can never disagree about what a button is.

    A body may carry `buttons` (the default row) and/or `per_channel` (a list of
    {name, mode, buttons}) and/or `auto_label` - whatever it does NOT carry is
    kept, so the page can save one channel without wiping the others. Omitting
    everything is refused: that is a silent wipe, not a save.
    """
    import post_buttons
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    if not isinstance(body, dict) or not any(
            key in body for key in ("buttons", "per_channel", "auto_label", "auto_limit",
                                    "every")):
        return web.json_response(
            {"errors": ["send buttons (the default row), per_channel, auto_label or "
                        "auto_limit - an empty body would wipe the configuration"]},
            status=400)
    current = post_buttons.load_config()
    per_channel = {row["name"]: {"mode": row["mode"], "buttons": row["buttons"]}
                   for row in current["per_channel"].values()}
    if "per_channel" in body:
        raw = body.get("per_channel")
        if isinstance(raw, dict):
            entries = [{"name": name, **(entry if isinstance(entry, dict) else {})}
                       for name, entry in raw.items()]
        elif isinstance(raw, list):
            entries = [entry for entry in raw if isinstance(entry, dict)]
        else:
            return web.json_response(
                {"errors": ["per_channel must be a list of {name, mode, buttons} or an "
                            "object of channel -> entry"]}, status=400)
        for entry in entries:
            name = post_buttons.handle_of(entry.get("name"))
            if not name:
                return web.json_response({"errors": ["every per_channel entry needs a "
                                                     "channel name"]}, status=400)
            mode = str(entry.get("mode") or "default").strip().lower()
            every = entry.get("every")
            if every not in (None, ""):
                try:
                    every = post_buttons.every_of(int(every))
                except (TypeError, ValueError):
                    return web.json_response(
                        {"errors": [f"@{name}: every must be a number (1 = every post, "
                                    "10 = every 10th)"]}, status=400)
            else:
                every = (per_channel.get(name) or {}).get("every") or 0
            if mode == "default":
                per_channel.pop(name, None)        # back to the shared row
            else:
                per_channel[name] = {"mode": mode, "every": every,
                                     "buttons": entry.get("buttons") or []}
    payload = {
        "buttons": body.get("buttons", current["buttons"]),
        "per_channel": per_channel,
        "auto_label": body.get("auto_label", current["auto_label"]),
        "auto_limit": body.get("auto_limit", current["auto_limit"]),
        "every": body.get("every", current["every"]),
    }
    target, errors, cleaned = post_buttons.save_config(payload)
    if errors:
        return web.json_response({"errors": errors}, status=400)
    view = buttons_view(cleaned)
    return web.json_response({"saved": True, "path": str(target), **view})


@require_token
@in_thread
def api_link_conversion(request: web.Request):
    """"Mana links maruthunnaya?" - answered per link, from the converter's book.

    link_cache holds source URL -> published affiliate URL -> resolved
    destination; each published URL is classified with the same url_money() the
    money report uses, so "converted" here means exactly what it means there:
    our tag, our shortener, or a proof row. Read-only, mode=ro.
    """
    try:
        limit = max(1, min(100, int(request.query.get("limit", "25"))))
    except (TypeError, ValueError):
        limit = 25
    conn = read_only_db()
    try:
        module = load_category_report()
        read = module.our_amazon_tags()
        tags = frozenset(read.get("bestgaa") or set()) | frozenset(read.get("bridge") or set())
        payload = module.link_conversion(conn, tags, limit=limit)
        try:
            days = max(1, min(30, int(request.query.get("days", "7"))))
        except (TypeError, ValueError):
            days = 7
        payload["trend"] = module.conversion_trend(conn, tags, days=days)
        # The same verdicts, split finer: which published links are hops nobody
        # can prove are ours. One classifier, so the card and the brief agree.
        payload["hygiene"] = trends.link_hygiene(conn, module.link_hygiene, tags)
    except Exception as exc:
        return web.json_response({"error": f"conversion view failed: {exc}"}, status=503)
    finally:
        if conn is not None:
            conn.close()
    payload["db_exists"] = db_path().exists()
    return web.json_response(payload)


@require_token
@in_thread
def api_channel_pipe(request: web.Request):
    """Open one source -> channel pipe: the posts it sent, each with its money
    verdict. The channel list shows the pipe counts; this is the evidence
    inside a single pipe, newest first. Read-only, mode=ro.
    """
    target = (request.query.get("target") or "").strip()
    source = (request.query.get("source") or "").strip()
    if not target or not source:
        return web.json_response({"error": "target and source are required"}, status=400)
    try:
        limit = max(1, min(100, int(request.query.get("limit", "15"))))
    except (TypeError, ValueError):
        limit = 15
    try:
        hours = max(1, min(24 * 60, int(request.query.get("hours", "168"))))
    except (TypeError, ValueError):
        hours = 168
    conn = read_only_db()
    try:
        module = load_category_report()
        read = module.our_amazon_tags()
        tags = frozenset(read.get("bestgaa") or set()) | frozenset(read.get("bridge") or set())
        ledger = module.ledger_rows(conn, time.time() - hours * 3600)
        links = module.link_index(conn) if conn is not None else {
            "by_affiliate": {}, "by_source": {}, "proofs": {}}
        payload = module.pipe_rows(ledger.get("rows") or [], links, tags,
                                   target, source, limit=limit)
        payload["hours"] = hours
        payload["ledger_error"] = ledger.get("error") or ""
    except Exception as exc:
        return web.json_response({"error": f"pipe view failed: {exc}"}, status=503)
    finally:
        if conn is not None:
            conn.close()
    payload["db_exists"] = db_path().exists()
    return web.json_response(payload)


_RATE_BUCKETS: dict[str, list[float]] = {}


def _rate_limit_allowed(key: str) -> tuple[bool, float]:
    """Token bucket per peer for MUTATING /api calls (enterprise review #3).

    The dashboard token is shared by everyone who has it, so without a bucket
    one loop (or one slipped token) can hammer writes. Capacity and refill come
    from DASHBOARD_RATE_LIMIT="capacity:refill_per_second" (default 60:2); the
    bucket refills continuously and idle peers are pruned after an hour.
    """
    spec = os.getenv("DASHBOARD_RATE_LIMIT", "60:2")
    try:
        capacity, refill = (float(part) for part in spec.split(":", 1))
    except ValueError:
        capacity, refill = 60.0, 2.0
    capacity = max(1.0, capacity)
    refill = max(0.001, refill)
    now = time.time()
    bucket = _RATE_BUCKETS.get(key)
    if bucket is None or now - bucket[1] > 3600:
        bucket = [capacity, now]
        _RATE_BUCKETS[key] = bucket
    tokens = min(capacity, bucket[0] + (now - bucket[1]) * refill)
    if tokens < 1.0:
        bucket[0] = tokens
        bucket[1] = now
        return False, (1.0 - tokens) / refill
    bucket[0] = tokens - 1.0
    bucket[1] = now
    if len(_RATE_BUCKETS) > 4096:
        for stale in [k for k, v in _RATE_BUCKETS.items() if now - v[1] > 3600]:
            _RATE_BUCKETS.pop(stale, None)
    return True, 0.0


def _database_message(exc: BaseException) -> str:
    text = str(exc)
    if "not a database" in text or "file is encrypted" in text:
        return ("the file at BOT_DB_PATH is not a SQLite database (or is encrypted). "
                "The dashboard never writes: point BOT_DB_PATH at the bot's real "
                "bestgaa.sqlite3, or restore it from a backup.")
    if "unable to open" in text or "permission" in text.lower():
        return ("the database file exists but cannot be opened. The dashboard only "
                "reads, so this is a path or a file-permission problem.")
    if "locked" in text or "busy" in text:
        return ("the database is busy (another process holds a write lock). Retry "
                "in a moment; the panel reads only.")
    if "no such table" in text:
        return ("this database has no bot tables yet. Start the bot once on the merged "
                "code so it creates the schema.")
    return f"database error: {text}"


@web.middleware
async def json_errors(request: web.Request, handler):
    """Every /api/* failure answers JSON with a sentence, never an HTML 500.

    A corrupt or half-restored database used to reach the browser as
    "500 Internal Server Error" for /api/recent, /api/audit, /api/categories and
    /api/safety, which tells an operator nothing. All four now answer 503 with
    what is wrong and what to do, like the conversion and pipe views already did.
    """
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except sqlite3.DatabaseError as exc:      # corrupt file, not-a-database, ...
        return web.json_response({"error": _database_message(exc), "database": "unreadable"},
                                 status=503)
    except sqlite3.Error as exc:              # locked, busy, no such table, ...
        return web.json_response({"error": _database_message(exc), "database": "error"},
                                 status=503)
    except Exception as exc:  # noqa: BLE001 - a panel must never take the page down
        if request.path.startswith("/api/"):
            return web.json_response(
                {"error": f"{type(exc).__name__}: {exc}", "unexpected": True}, status=500)
        raise


@web.middleware
async def security_headers(request: web.Request, handler):
    """Baseline response hardening: no framing, no MIME sniffing, no referrer
    leaks, and nothing cacheable behind the token."""
    if request.path.startswith("/api/") and request.method in (
            "POST", "PATCH", "PUT", "DELETE"):
        allowed, retry = _rate_limit_allowed(f"{request.remote or '?'}:write")
        if not allowed:
            limited = web.json_response(
                {"error": "too many write requests, slow down",
                 "retry_after": round(retry, 1)}, status=429)
            limited.headers["Retry-After"] = str(max(1, int(retry + 0.999)))
            return limited
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    # Enterprise review #4: a CSP for a single-file dashboard. The panel ships
    # its script and styles inline, so those two keep 'unsafe-inline' (a nonce
    # would need per-response HTML rewriting); everything ELSE is locked down -
    # no plugins, no frames, no forms posting out, no base-tag hijack, and
    # images/connects only from this origin (plus the data:/blob: URLs the card
    # studio and the PNG endpoint legitimately use).
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'")
    return response


@require_token
@in_thread
def api_audit(request: web.Request):
    try:
        limit = max(1, min(500, int(request.query.get("limit", "200"))))
    except ValueError:
        limit = 200
    conn = read_only_db()
    if conn is None:
        # Not a missing page: the bot has not created its database yet (or
        # BOT_DB_PATH is wrong). 503 says "try again", 404 would say "wrong URL".
        return web.json_response({"error": "no database yet", "database": "missing"},
                                 status=503)
    try:
        audit_mod = load_quality_audit()
        findings, posts, link_counts, delivery = audit_mod.audit(
            conn, limit, os.getenv("AMAZON_TAG", "").strip(),
            os.getenv("EARNKARO_PUBLISHER_ID", "").strip())
    except sqlite3.OperationalError as exc:
        return web.json_response({"error": f"database not readable yet ({exc})"}, status=503)
    finally:
        conn.close()
    by_kind: dict[str, int] = {}
    for item in findings:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return web.json_response({
        "posts_checked": posts, "link_summary": link_counts,
        "delivery_summary": delivery, "findings_by_kind": by_kind,
        "findings": findings[:50],
    })


def load_safety_policy() -> dict:
    """The live policy (categories, severities, switches) from the bot module.

    Read from `bestgaa/safety_filter.py` so the dashboard can never describe a
    policy that is not the one the bot is running.
    """
    try:
        from safety_filter import policy_summary
        return policy_summary()
    except Exception as exc:  # noqa: BLE001 - the panel must still render
        return {"version": "", "enabled": None, "categories": [],
                "error": f"policy unavailable: {exc}"}


@require_token
@in_thread
def api_safety(request: web.Request):
    """Why something was NOT posted: the public-channel content-safety panel.

    Read-only over the bot database. Every refusal the bot or the WhatsApp
    bridge made is in `blocked_content`, with its category, the matched term
    (masked in the excerpt) and the human reason - plus the policy that was
    active at the time. Nothing here can change what posts.
    """
    try:
        days = max(1, min(30, int(request.query.get("days", "7"))))
    except ValueError:
        days = 7
    try:
        limit = max(1, min(200, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    now = time.time()
    since = now - days * 86400
    today = ist_day_start()
    policy = load_safety_policy()
    payload: dict = {
        "policy": policy,
        "window_days": days,
        "generated_at": now,
        "totals": {"today": 0, "window": 0, "all_time": 0, "by_category": {},
                   "by_stage": {}, "by_source": {}},
        "blocked": [],
        "presend_skips": 0,
        "presend_examples": [],
        "database": "missing",
    }
    conn = read_only_db()
    if conn is None:
        return web.json_response(payload)
    payload["database"] = "ok"
    try:
        tables = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "blocked_content" in tables:
            def count(where: str, args: tuple) -> int:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM blocked_content WHERE {where}", args).fetchone()
                return int(row[0] or 0)

            payload["totals"]["today"] = count("created_at>=?", (today,))
            payload["totals"]["window"] = count("created_at>=?", (since,))
            payload["totals"]["all_time"] = count("1=1", ())
            for key, group in (("by_category", "category"), ("by_stage", "stage"),
                              ("by_source", "source")):
                rows = conn.execute(
                    f"SELECT COALESCE(NULLIF({group},''),'unknown') AS k, COUNT(*) n "
                    "FROM blocked_content WHERE created_at>=? GROUP BY k ORDER BY n DESC "
                    "LIMIT 12", (since,)).fetchall()
                payload["totals"][key] = {row["k"]: int(row["n"]) for row in rows}
            rows = conn.execute(
                "SELECT stage, source, msg_id, queue_id, target, category, label, matched, "
                "reason, policy_version, excerpt, created_at FROM blocked_content "
                "WHERE created_at>=? ORDER BY created_at DESC LIMIT ?", (since, limit)).fetchall()
            payload["blocked"] = [{
                "stage": row["stage"], "source": row["source"], "msg_id": row["msg_id"],
                "queue_id": row["queue_id"], "target": row["target"],
                "category": row["category"], "label": row["label"],
                "matched": row["matched"], "reason": row["reason"],
                "policy_version": row["policy_version"],
                "excerpt": row["excerpt"], "created_at": row["created_at"],
            } for row in rows]
        # Belt and braces: a pre-send refusal recorded on the delivery ledger
        # (the bridge's gate) counts too, even if no blocked_content row exists.
        if "deliveries" in tables and "last_error" in delivery_columns(conn):
            row = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE COALESCE(last_error,'') "
                "LIKE '%content policy%' AND status IN ('skipped','held','failed')").fetchone()
            payload["presend_skips"] = int(row[0] or 0)
            rows = conn.execute(
                "SELECT d.queue_id, d.target, d.status, d.last_error, q.source "
                "FROM deliveries d LEFT JOIN queue q ON q.id=d.queue_id "
                "WHERE COALESCE(d.last_error,'') LIKE '%content policy%' "
                "ORDER BY d.rowid DESC LIMIT 10").fetchall()
            payload["presend_examples"] = [{
                "queue_id": r["queue_id"], "target": r["target"], "status": r["status"],
                "source": r["source"] or "", "error": str(r["last_error"])[:200],
            } for r in rows]
    except sqlite3.OperationalError as exc:
        payload["database"] = f"unreadable: {exc}"
    finally:
        conn.close()
    return web.json_response(payload)


@require_token
@in_thread
def api_health(request: web.Request):
    """Self-diagnosis: every check the operator would otherwise run by hand.

    Read-only. `?services=1` adds `systemctl is-active` for the three services
    (still read-only, and it degrades to `unknown` where systemd is absent).
    A non-green check always carries a `fix` line, so the panel says what to do
    instead of only that something is wrong.
    """
    want_services = request.query.get("services", "") in ("1", "true", "yes")
    try:
        payload = insight.health(
            db=db_path(), root=ROOT,
            channels_json=channels_config.channels_json_path(),
            snapshot=snapshot(), log_path=_dashboard_log_path(),
            services=want_services)
    except Exception as exc:  # noqa: BLE001 - a health check must never 500 the page
        return web.json_response({"overall": "unknown", "headline": f"health check failed: {exc}",
                                  "counts": {"ok": 0, "warn": 0, "fail": 0, "unknown": 1},
                                  "checks": [], "facts": {}, "error": str(exc)}, status=200)
    payload["pending_restart"] = pending_restart()
    return web.json_response(payload)


def _dashboard_log_path() -> Path:
    env = os.getenv("BOT_LOG", "").strip()
    if env:
        return Path(env)
    try:
        module = load_category_report()
        return module.default_log()
    except Exception:  # noqa: BLE001
        return ROOT / "bestgaa" / "logs" / "bot.log"


@require_token
@in_thread
def api_pulse(request: web.Request):
    """One merged timeline: intake, send attempts, refusals, conversions, proofs."""
    conn = read_only_db()
    if conn is None:
        return web.json_response({"events": [], "counts": {}, "last": {}, "silences": [],
                                  "database": "missing"}, status=200)
    try:
        hours = max(0.25, min(168.0, float(request.query.get("hours", "6"))))
    except (TypeError, ValueError):
        hours = 6.0
    try:
        limit = max(1, min(500, int(request.query.get("limit", "200"))))
    except (TypeError, ValueError):
        limit = 200
    kind = request.query.get("kind", "") if request.query.get("kind") in insight.EVENT_KINDS else ""
    source = request.query.get("source", "")[:80]
    target = request.query.get("target", "")[:80]
    try:
        payload = insight.pulse(conn, hours=hours, kind=kind, source=source,
                                target=target, limit=limit)
    except sqlite3.OperationalError as exc:
        return web.json_response({"error": f"database not readable yet ({exc})"}, status=503)
    finally:
        conn.close()
    payload["database"] = "ok"
    return web.json_response(payload)


@require_token
@in_thread
def api_forensics(request: web.Request):
    """One queue id: every link, every rewrite, every stage - with the proof.

    `?queue_id=newest` (or no id) opens the most recent post that was refused or
    never attempted, which is the one an operator wants at 2 a.m.
    """
    raw = (request.query.get("queue_id") or "").strip()
    conn = read_only_db()
    if conn is None:
        return web.json_response({"error": "no database yet"}, status=503)
    try:
        if raw in ("", "newest"):
            queue_id = forensics.newest_refused(conn)
            if queue_id is None:
                return web.json_response(
                    {"error": "nothing has been refused or left unattempted yet"}, status=404)
        else:
            try:
                queue_id = int(raw)
            except (TypeError, ValueError):
                return web.json_response({"error": "queue_id must be a number or 'newest'"},
                                         status=400)
        payload = forensics.dossier(
            conn, queue_id,
            amazon_tag=(os.getenv("AMAZON_TAG", "") or "").strip(),
            snapshot=snapshot(), channels=channels_config.load_channels())
    except KeyError as exc:      # no queue table: the database, not the id
        message = str(exc.args[0]) if exc.args else str(exc)
        return web.json_response({"error": message, "database": "unreadable"}, status=503)
    except LookupError as exc:   # no such queue row: the id
        return web.json_response({"error": str(exc)}, status=404)
    except sqlite3.OperationalError as exc:
        return web.json_response({"error": f"database not readable yet ({exc})"}, status=503)
    finally:
        conn.close()
    return web.json_response(payload)


@require_token
@in_thread
def api_trace(request: web.Request):
    """One post, the whole chain: predicted vs what really happened, per target."""
    try:
        queue_id = int(request.query.get("queue_id", ""))
    except (TypeError, ValueError):
        return web.json_response({"error": "queue_id must be a number"}, status=400)
    conn = read_only_db()
    if conn is None:
        return web.json_response({"error": "no database yet"}, status=503)
    try:
        payload = insight.trace(conn, queue_id, analyze=source_target.analyze,
                                snapshot=snapshot(),
                                channels=channels_config.load_channels())
    except KeyError as exc:      # no queue table: the database, not the id
        message = str(exc.args[0]) if exc.args else str(exc)
        return web.json_response({"error": message, "database": "unreadable"}, status=503)
    except LookupError as exc:   # no such queue row: the id
        return web.json_response({"error": str(exc)}, status=404)
    except sqlite3.OperationalError as exc:
        return web.json_response({"error": f"database not readable yet ({exc})"}, status=503)
    finally:
        conn.close()
    payload["policy"] = load_safety_policy()
    return web.json_response(payload)


@require_token
@in_thread
def api_search(request: web.Request):
    """Find a post, a target, or a refusal. Never guesses: no hit is a hit."""
    query = (request.query.get("q") or "").strip()
    try:
        limit = max(1, min(100, int(request.query.get("limit", "40"))))
    except (TypeError, ValueError):
        limit = 40
    conn = read_only_db()
    if conn is None:
        return web.json_response({"query": query, "results": [], "count": 0,
                                  "database": "missing"}, status=200)
    try:
        payload = insight.search(conn, query, limit=limit)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except sqlite3.OperationalError as exc:
        return web.json_response({"error": f"database not readable yet ({exc})"}, status=503)
    finally:
        conn.close()
    payload["database"] = "ok"
    return web.json_response(payload)


@require_token
@in_thread
def api_trends(request: web.Request):
    """"Getting better or worse?" - one window against the window before it.

    Six metrics (sent, skipped, refused, converted, intake, failed) plus the
    live backlog, each with the exact delta, the same figure for the preceding
    window, a bar per equal slice of it, and a per-channel split with the top
    skip reasons. Equal slices mean a 7-day view is twenty-four 7-hour bars
    whose sum still equals the headline. Read-only, one connection, no writes.
    """
    try:
        hours = max(0.25, min(168.0, float(request.query.get("hours", "24"))))
    except (TypeError, ValueError):
        hours = 24.0
    try:
        buckets = max(4, min(48, int(request.query.get("buckets", "24"))))
    except (TypeError, ValueError):
        buckets = 24
    conn = read_only_db()
    if conn is None:
        return web.json_response({"error": "no database yet", "database": "missing",
                                  "metrics": [], "channels": [], "skip_reasons": [],
                                  "verdict": "no bot database on this host yet - nothing to compare"},
                                 status=200)
    try:
        payload = trends.compare(conn, hours=hours, buckets=buckets)
    finally:
        conn.close()
    payload["pending_restart"] = pending_restart()
    return web.json_response(payload)


@require_token
@in_thread
def api_brief(request: web.Request):
    """The one screen that says what to do now: health + drift + spike + move.

    Returns the full health payload it just computed (so the console panel can
    render the same object instead of asking twice), plus a ranked attention
    list from `trends.attention()`: every non-green check, a rules-version
    drift, a refusal spike and the biggest movement in the window, each with
    its fix and the panel that can explain it. Cheap on purpose - it recomputes
    nothing the caller already asked for. Read-only.
    """
    try:
        hours = max(0.25, min(168.0, float(request.query.get("hours", "1"))))
    except (TypeError, ValueError):
        hours = 1.0
    want_services = request.query.get("services", "") in ("1", "true", "yes")
    try:
        health_payload = insight.health(
            db=db_path(), root=ROOT,
            channels_json=channels_config.channels_json_path(),
            snapshot=snapshot(), log_path=_dashboard_log_path(),
            services=want_services)
    except Exception as exc:  # noqa: BLE001 - a brief must never 500 the page
        health_payload = {"overall": "unknown", "headline": f"health check failed: {exc}",
                          "counts": {}, "checks": [], "facts": {}, "error": str(exc)}

    snap = snapshot()
    rules = load_rules().get("version", 0)
    running = snap.get("category_rules_version")
    drift = trends.drift_facts(installed=rules, running=running,
                               pending_restart=pending_restart())
    conn = read_only_db()
    spike = {"last_hour": 0, "avg_hour": 0.0, "week": 0}
    movement: dict = {}
    links: dict = {"available": False}
    if conn is not None:
        try:
            spike = trends.safety_spike(conn)
            movement = trends.compare(conn, hours=hours, buckets=4)
            try:
                module = load_category_report()
                read = module.our_amazon_tags()
                tags = frozenset(read.get("bestgaa") or set()) | frozenset(
                    read.get("bridge") or set())
                links = trends.link_hygiene(conn, module.link_hygiene, tags)
            except Exception as exc:  # noqa: BLE001 - a stat is never a broken brief
                links = {"available": False, "error": str(exc)[:200]}
        finally:
            conn.close()

    # A post that left the queue without a receipt outranks everything cosmetic:
    # it is the only finding that can mean a lost deal or a lost commission, so
    # the ranked attention list must know about it (the scan is memoised for a
    # few seconds, so this costs nothing on the 20 s full refresh).
    hole: dict = {}
    try:
        body = pipeline_report(24.0)
        if body:
            hole = {"overall": body.get("overall"),
                    "unaccounted": next((tile["value"] for tile in body.get("tiles", [])
                                         if tile.get("key") == "unaccounted"), 0),
                    "unaccounted_ids": body.get("unaccounted_ids") or [],
                    "findings": body.get("findings") or []}
    except Exception as exc:  # noqa: BLE001 - a stat is never a broken brief
        hole = {"overall": "unknown", "unaccounted": 0, "error": str(exc)[:160]}
    brief = trends.attention(health=health_payload, drift=drift, safety=spike,
                             trends=movement or None, links=links, pipeline=hole)
    try:
        max_posts = int(snap.get("max_posts") or 0)
    except (TypeError, ValueError):
        max_posts = 0
    payload = {
        **brief,
        "health": health_payload,
        "pending_restart": pending_restart(),
        "window_hours": hours,
        "movement": movement.get("verdict", "") if movement else "",
        "drift": drift,
        "safety": spike,
        "links": links,
        "pipeline": hole,
        "rules": {"installed": rules, "running": running, "max_posts": max_posts},
        "db_exists": db_path().exists(),
    }
    return web.json_response(payload)


@require_token
@in_thread
def api_scorecard(request: web.Request):
    """Per channel and per source: is it receiving, is it silent, is it worth it."""
    try:
        days = max(1, min(30, int(request.query.get("days", "7"))))
    except (TypeError, ValueError):
        days = 7
    conn = read_only_db()
    if conn is None:
        return web.json_response({"channels": [], "sources": [], "database": "missing"},
                                 status=200)
    try:
        payload = insight.scorecard(conn, days=days)
    except sqlite3.OperationalError as exc:
        return web.json_response({"error": f"database not readable yet ({exc})"}, status=503)
    finally:
        conn.close()
    payload["database"] = "ok"
    payload["pending_restart"] = pending_restart()
    return web.json_response(payload)



# --------------------------------------------------------------------------- #
#  pipeline integrity - "edho okati miss avvakoodadu"
# --------------------------------------------------------------------------- #
def declared_sources(snap=None, data=None) -> set[str]:
    """Every source the operator asked for, from the bot's own report + config.

    A `*` in a saved channel means "the bot's main feeds", which only the bot
    knows; the snapshot carries them. Watch rows are sources in their own right.
    """
    snap = snapshot() if snap is None else snap
    data = channels_config.load_channels() if data is None else data
    names: set[str] = set()
    main = [str(s) for s in (snap.get("main_sources") or [])]
    if not main:
        main = [str(s) for s in (snap.get("sources") or [])]
    for channel in data.get("channels") or []:
        if not isinstance(channel, dict):
            continue
        sources = [str(s) for s in (channel.get("sources") or [])]
        if "*" in sources:
            names.update(main)
        else:
            names.update(s for s in sources if s and not s.startswith("https://t.me/+"))
    names.update(str(row.get("channel", "")) for row in channels_config.watch_entries(data))
    names.update(str(s) for s in (snap.get("sources") or []))
    return {name for name in names if name}


# The integrity scan reads every receipt in the window (~0.6 s on a 50k-post
# window, which is 50k queue rows + 85k delivery rows). Three open tabs and a
# 5 s tick must not multiply that, so one computed report is reused for a few
# seconds - keyed by the database file, its mtime and the window, so a new write
# or a different window can never be served a stale answer.
_PIPELINE_CACHE: dict[tuple, tuple[float, dict]] = {}
PIPELINE_TTL = 8.0


def pipeline_report(hours: float) -> dict:
    path = db_path()
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    key = (str(path), float(stamp), round(float(hours), 3))
    hit = _PIPELINE_CACHE.get(key)
    now_mono = time.monotonic()
    if hit and now_mono - hit[0] < PIPELINE_TTL:
        return hit[1]
    conn = read_only_db()
    if conn is None:
        return {}
    snap = snapshot()
    data = channels_config.load_channels()
    try:
        payload = dashboard_pipeline.funnel(
            conn, hours=hours,
            declared_sources=declared_sources(snap, data),
            declared_targets=[str(t) for t in (snap.get("targets") or [])],
            watched=[str(row.get("channel", "")) for row in channels_config.watch_entries(data)])
        payload["unaccounted_ids"] = dashboard_pipeline.unaccounted_ids(conn, hours=hours)
    finally:
        conn.close()
    if len(_PIPELINE_CACHE) > 8:
        _PIPELINE_CACHE.clear()
    _PIPELINE_CACHE[key] = (now_mono, payload)
    return payload


@require_token
@in_thread
def api_pipeline(request: web.Request):
    """"Did every post we took in come out somewhere?" - one equation, per source.

    Distinct from every other panel: the health/brief panels describe symptoms,
    the ledger panels describe one stage each, and this one reconciles the whole
    intake against the delivery ledger so a hole cannot hide between two panels.
    Read-only, one connection, no writes.
    """
    try:
        hours = max(0.25, min(24 * 14, float(request.query.get("hours", "24"))))
    except (TypeError, ValueError):
        hours = 24.0
    try:
        payload = pipeline_report(hours)
    except sqlite3.OperationalError as exc:
        return web.json_response({"error": f"database not readable yet ({exc})",
                                  "overall": "unknown", "findings": [], "sources": [],
                                  "targets": [], "tiles": [], "intake": 0,
                                  "verdict": "the database is not readable yet",
                                  "equation": "", "database": "unreadable"}, status=503)
    if not payload:
        return web.json_response({
            "database": "missing", "overall": "unknown", "findings": [], "sources": [],
            "targets": [], "tiles": [], "intake": 0, "unaccounted_ids": [],
            "verdict": "no bot database on this host yet - nothing to reconcile",
            "equation": "0 in = 0 posted + 0 withheld + 0 dropped + 0 waiting + 0 failed",
            "first_send_seconds": {"p50": None, "p95": None, "max": None},
            "declared_and_real": {"declared": 0, "watched": 0, "seen": 0,
                                  "declared_but_silent": [], "seen_but_not_declared": []},
            "window_hours": hours, "pending_restart": pending_restart(),
        }, status=200)
    payload["pending_restart"] = pending_restart()
    payload["database"] = "ok"
    return web.json_response(payload)


@require_token
@in_thread
def api_export(request: web.Request):
    """Download the rows a panel is showing, as CSV.

    Same engine, same rows, no new query: an export can never disagree with the
    screen it came from. `what` is one of `scorecard`, `pulse`, `search`,
    `health`, `pipeline`; `format=csv` (default) or `json`.
    """
    what = request.query.get("what", "scorecard")
    wanted = request.query.get("format", "csv")
    if wanted not in ("csv", "json"):
        return web.json_response({"error": "format must be csv or json"}, status=400)
    try:
        days = max(1, min(30, int(request.query.get("days", "7"))))
    except (TypeError, ValueError):
        days = 7
    try:
        hours = max(0.25, min(168.0, float(request.query.get("hours", "24"))))
    except (TypeError, ValueError):
        hours = 24.0
    query = (request.query.get("q") or "").strip()

    conn = read_only_db()
    try:
        if what == "scorecard":
            if conn is None:
                return web.json_response({"error": "no database yet"}, status=503)
            data = insight.scorecard(conn, days=days)
            rows = [{**row, "kind": "channel"} for row in data["channels"]] + \
                   [{**row, "kind": "source"} for row in data["sources"]]
            payload, columns, name = data, [
                "kind", "target", "source", "today", "sends", "skips", "deliver_rate",
                "avg_quality", "best_hour_ist", "last_send_ist", "top_skip_reason",
                "intakes", "done", "failed", "post_rate", "last_intake_ist"], "scorecard"
        elif what == "pulse":
            if conn is None:
                return web.json_response({"error": "no database yet"}, status=503)
            data = insight.pulse(conn, hours=hours, limit=500)
            rows, payload = data["events"], data
            columns, name = ["at_ist", "kind", "label", "detail", "source", "target",
                             "queue_id", "status"], "timeline"
        elif what == "search":
            if conn is None:
                return web.json_response({"error": "no database yet"}, status=503)
            try:
                data = insight.search(conn, query, limit=100)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            rows, payload = data["results"], data
            columns, name = ["at_ist", "kind", "queue_id", "source", "target", "status",
                             "snippet"], "search"
        elif what == "pipeline":
            data = pipeline_report(hours)
            if not data:
                return web.json_response({"error": "no database yet"}, status=503)
            rows = ([{**row, "kind": "source"} for row in data.get("sources", [])]
                    + [{**row, "kind": "channel"} for row in data.get("targets", [])])
            payload, name = data, "pipeline"
            columns = ["kind", "source", "target", "declared", "watched", "intake", "posted",
                       "withheld", "dropped", "waiting", "failed", "unaccounted", "post_share",
                       "latency_p50", "latency_p95", "last_intake", "typical_gap_human",
                       "sends", "state", "verdict", "why"]
        elif what == "health":
            data = insight.health(db=db_path(), root=ROOT,
                                  channels_json=channels_config.channels_json_path(),
                                  snapshot=snapshot(), log_path=_dashboard_log_path())
            rows, payload = data["checks"], data
            columns, name = ["key", "title", "state", "detail", "fix"], "health"
        else:
            return web.json_response(
                {"error": "what must be scorecard, pulse, search, health or pipeline"},
                status=400)
    except sqlite3.OperationalError as exc:
        return web.json_response({"error": f"database not readable yet ({exc})"}, status=503)
    finally:
        if conn is not None:
            conn.close()

    if wanted == "json":
        return web.json_response(payload)
    body = insight.to_csv(rows, columns)
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(time.time() + IST_OFFSET))
    return web.Response(text=body, content_type="text/csv", charset="utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="bestgaa-{name}-{stamp}.csv"'})


@require_token
async def api_restart(request: web.Request):
    cmd = os.getenv("DASHBOARD_RESTART_CMD", f"systemctl restart {SERVICE_DEFAULT}").strip()
    if not cmd:
        return web.json_response({"ok": False, "output": "restart command not configured"}, status=400)
    try:
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return web.json_response({"ok": False, "output": f"could not run restart: {exc}"},
                                 status=500)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()[:2000]
    if proc.returncode == 0:
        clear_pending()
        return web.json_response({"ok": True, "output": output or "restart requested"})
    hint = f"exit={proc.returncode}. If permission was denied, run on the server: sudo {cmd}"
    return web.json_response({"ok": False, "output": f"{output}\n{hint}".strip()}, status=500)


async def index(request: web.Request):
    demo = os.environ.get("DASHBOARD_INSECURE") == "1" and not os.environ.get("DASHBOARD_TOKEN")
    page = UI_HTML.replace("const DEMO_MODE = false;", "const DEMO_MODE = true;") if demo else UI_HTML
    return web.Response(text=page, content_type="text/html")


def create_app() -> web.Application:
    app = web.Application(middlewares=[security_headers, json_errors])
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/channels", api_channels)
    app.router.add_post("/api/category-preview", api_category_preview)
    app.router.add_post("/api/category-batch", api_category_batch)
    app.router.add_post("/api/inspect-link", api_inspect_link)
    app.router.add_post("/api/source-to-target", api_source_to_target)
    app.router.add_get("/api/vault", api_vault)
    app.router.add_put("/api/vault", api_vault_save)
    app.router.add_get("/api/directory", api_directory)
    app.router.add_post("/api/watch/bulk", api_watch_bulk)
    app.router.add_get("/api/watch", api_watch_list)
    app.router.add_post("/api/watch", api_watch_add)
    app.router.add_patch("/api/watch/{channel}", api_watch_edit)
    app.router.add_delete("/api/watch/{channel}", api_watch_delete)
    app.router.add_post("/api/channels", api_add_channel)
    app.router.add_post("/api/channels/bulk", api_channels_bulk)
    app.router.add_get("/api/join-requests", api_join_requests)
    app.router.add_post("/api/join-requests", api_join_requests)
    app.router.add_get("/api/own", api_own)
    app.router.add_post("/api/own/attach", api_own_attach)
    app.router.add_post("/api/own/detach", api_own_detach)
    app.router.add_post("/api/own/posting", api_own_posting)
    app.router.add_get("/api/own/suggest", api_own_suggest)
    app.router.add_post("/api/own/suggest", api_own_suggest)
    app.router.add_get("/api/posts", api_posts)
    app.router.add_post("/api/posts/send", api_post_send)
    app.router.add_get("/api/repost", api_repost)
    app.router.add_post("/api/repost", api_repost)
    app.router.add_patch("/api/channels/{name}", api_edit_channel)
    app.router.add_put("/api/channels/whatsapp", api_put_whatsapp)
    app.router.add_delete("/api/channels/{name}", api_delete_channel)
    app.router.add_get("/api/recent", api_recent)
    app.router.add_get("/api/categories", api_categories)
    app.router.add_get("/api/link-conversion", api_link_conversion)
    app.router.add_get("/api/channel-pipe", api_channel_pipe)
    app.router.add_get("/api/post-buttons", api_post_buttons_get)
    app.router.add_put("/api/post-buttons", api_post_buttons_put)
    app.router.add_get("/api/card.png", api_card_png)
    app.router.add_get("/api/card-studio", api_card_studio)
    app.router.add_get("/api/audit", api_audit)
    app.router.add_get("/api/safety", api_safety)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/pulse", api_pulse)
    app.router.add_get("/api/trace", api_trace)
    app.router.add_get("/api/forensics", api_forensics)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/scorecard", api_scorecard)
    app.router.add_get("/api/pipeline", api_pipeline)
    app.router.add_get("/api/trends", api_trends)
    app.router.add_get("/api/brief", api_brief)
    app.router.add_get("/api/export", api_export)
    app.router.add_post("/api/restart", api_restart)
    return app


UI_HTML = Path(__file__).with_name("index.html").read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BestGAA operator dashboard")
    parser.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8080")))
    args = parser.parse_args(argv)
    if not os.environ.get("DASHBOARD_TOKEN", "") and os.environ.get("DASHBOARD_INSECURE", "") != "1":
        print("Refusing to start: set DASHBOARD_TOKEN (or DASHBOARD_INSECURE=1 for a local demo).",
              file=sys.stderr)
        return 2
    if os.environ.get("DASHBOARD_INSECURE", "") == "1":
        print("WARNING: dashboard running WITHOUT a token (DASHBOARD_INSECURE=1).", file=sys.stderr)
    print(f"Dashboard on http://{args.host}:{args.port}  db={db_path()}", flush=True)
    web.run_app(create_app(), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

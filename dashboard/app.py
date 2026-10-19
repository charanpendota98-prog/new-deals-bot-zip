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

Read-only on the bot database always; the only writes are channels.json, its
.pending sidecar, and the optional restart command.
"""
import argparse
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
import channels_config  # noqa: E402
from category_classifier import load_rules  # noqa: E402
from dashboard.preview import preview_batch, inspect_product_link  # noqa: E402

IST_OFFSET = 5 * 3600 + 30 * 60  # Asia/Kolkata, no tzdata needed
SERVICE_DEFAULT = "bestgaa"


def db_path() -> Path:
    env = os.getenv("BOT_DB_PATH", "").strip()
    if env:
        return Path(env)
    return ROOT / "bestgaa" / "bestgaa.sqlite3"


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
async def api_status(request: web.Request):
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
async def api_channels(request: web.Request):
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
async def api_delete_channel(request: web.Request):
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
async def api_recent(request: web.Request):
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
async def api_categories(request: web.Request):
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
async def api_card_png(request: web.Request):
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
async def api_card_studio(request: web.Request):
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
    cleaned, errors = channels_config.validate_whatsapp_list(
        (body or {}).get("whatsapp"), set(category_catalog()))
    if errors:
        return web.json_response({"errors": errors}, status=400)
    data = channels_config.load_channels()
    data["whatsapp"] = cleaned
    channels_config.save_channels(data)
    return web.json_response({"saved": True, "whatsapp": cleaned,
                              "env_lines": channels_config.wa_env_lines(cleaned)})


@require_token
async def api_post_buttons_get(request: web.Request):
    """The configured Telegram button row (join buttons under every post)."""
    import post_buttons
    return web.json_response({
        "buttons": post_buttons.load_buttons(),
        "presets": post_buttons.preset_buttons(),
        "max_buttons": post_buttons.MAX_BUTTONS,
        "max_label": post_buttons.MAX_LABEL,
        "allowed_hosts": list(post_buttons.ALLOWED_HOSTS),
        "path": str(post_buttons.config_path()),
    })


@require_token
async def api_post_buttons_put(request: web.Request):
    """Save the button row. Validation lives in post_buttons, so the CLI, the bot
    and this endpoint can never disagree about what a button is."""
    import post_buttons
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    target, errors = post_buttons.save_buttons((body or {}).get("buttons"))
    if errors:
        return web.json_response({"errors": errors}, status=400)
    return web.json_response({"saved": True, "buttons": post_buttons.load_buttons(target),
                              "path": str(target)})


@require_token
async def api_link_conversion(request: web.Request):
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
    except Exception as exc:
        return web.json_response({"error": f"conversion view failed: {exc}"}, status=503)
    finally:
        if conn is not None:
            conn.close()
    payload["db_exists"] = db_path().exists()
    return web.json_response(payload)


@require_token
async def api_channel_pipe(request: web.Request):
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
async def api_audit(request: web.Request):
    try:
        limit = max(1, min(500, int(request.query.get("limit", "200"))))
    except ValueError:
        limit = 200
    conn = read_only_db()
    if conn is None:
        return web.json_response({"error": "no database yet"}, status=404)
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
    app = web.Application(middlewares=[security_headers])
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/channels", api_channels)
    app.router.add_post("/api/category-preview", api_category_preview)
    app.router.add_post("/api/category-batch", api_category_batch)
    app.router.add_post("/api/inspect-link", api_inspect_link)
    app.router.add_post("/api/channels", api_add_channel)
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

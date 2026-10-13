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
    Changes land in channels.json next to the bot database and apply on the
    next bot restart (the dashboard shows a pending badge and offers a
    one-click restart when it has permission).
  * Status: queue depth, today's real sends vs skips per the ledger, last
    posted deal, bot uptime.
  * Recent: latest jobs with per-job send/skip counts.
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


def activation_status(snap=None):
    snap = snapshot() if snap is None else snap
    try:
        age = time.time() - float(snap.get("heartbeat_at") or 0)
    except (TypeError, ValueError):
        age = float("inf")
    fresh = bool(snap.get("live_reload") and 0 <= age < 40)
    same = snap.get("observed_revision") == config_revision()
    return {"watcher_online": fresh, "heartbeat_at": snap.get("heartbeat_at"),
            "new_channels_live": fresh, "config_observed": same,
            "channels": snap.get("live_channels", {}) if fresh and same else {},
            "error": snap.get("live_error", "") if fresh else "",
            "message": ("Bot online: new channels get access checks and live activation; existing edits/removals need an approved restart."
                        if fresh else "Live activation not confirmed: bot may be OFF, starting, or on older code. Saving alone cannot start it.")}


@require_token
async def api_channels(request: web.Request):
    snap = snapshot()
    data = channels_config.load_channels()
    dashboard_names = {c["name"].lower() for c in data["channels"]}
    dashboard_names.update(str(n).lower() for n in snap.get("dashboard_added", []))
    targets = [str(t) for t in snap.get("targets", [])]
    return web.json_response({
        "builtin": sorted(t for t in targets if t.lower() not in dashboard_names),
        "dashboard": data["channels"],
        "revision": config_revision(),
        "activation": activation_status(snap),
        "sources": snap.get("sources", []),
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
        entry or {}, set(snap.get("sources", [])) if snap else set(),
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
        merged, set(snap.get("sources", [])) if snap else set(), others - {entry["name"]},
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
                    f"{sends_expr} AS sends, {skips_expr} AS skips FROM queue q "
                    "LEFT JOIN deliveries d ON d.queue_id=q.id "
                    "GROUP BY q.id ORDER BY q.id DESC LIMIT ?", (limit,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                jobs.append({
                    "id": row["id"], "source": row["source"], "status": row["status"],
                    "created_at": row["created_at"], "error": (row["last_error"] or "")[:120],
                    "sends": int(row["sends"] or 0), "skips": int(row["skips"] or 0),
                })
        finally:
            conn.close()
    return web.json_response({"jobs": jobs})


def load_quality_audit():
    spec = importlib.util.spec_from_file_location("dash_quality_audit", ROOT / "ops" / "quality_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/channels", api_channels)
    app.router.add_post("/api/category-preview", api_category_preview)
    app.router.add_post("/api/category-batch", api_category_batch)
    app.router.add_post("/api/inspect-link", api_inspect_link)
    app.router.add_post("/api/channels", api_add_channel)
    app.router.add_patch("/api/channels/{name}", api_edit_channel)
    app.router.add_delete("/api/channels/{name}", api_delete_channel)
    app.router.add_get("/api/recent", api_recent)
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

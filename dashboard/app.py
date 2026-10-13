"""BestGAA operator dashboard: add channels easily, watch the bot live.

Run from the repo root::

    DASHBOARD_TOKEN='<long-random>' python3 -m dashboard.app --port 8080

then open http://127.0.0.1:8080 and paste the token. Keep it on localhost
(SSH tunnel to reach it) and never expose it to the internet: anyone with the
token can change where deals post.

What it does
  * Channels: list every posting channel (built-in + dashboard-added), add a
    new channel (name, sources, Amazon-tag opt-in), enable/disable/remove it.
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
sys.path.insert(0, str(ROOT / "bestgaa"))
import channels_config  # noqa: E402

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


@require_token
async def api_channels(request: web.Request):
    snap = snapshot()
    data = channels_config.load_channels()
    dashboard_names = {c["name"].lower() for c in data["channels"]}
    targets = [str(t) for t in snap.get("targets", [])]
    return web.json_response({
        "builtin": sorted(t for t in targets if t.lower() not in dashboard_names),
        "dashboard": data["channels"],
        "sources": snap.get("sources", []),
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
        set(snap.get("targets", [])) if snap else set())
    if errors:
        return web.json_response({"errors": errors, "warnings": warnings}, status=400)
    data = channels_config.load_channels()
    if any(c["name"].lower() == cleaned["name"].lower() for c in data["channels"]):
        return web.json_response({"errors": [f"@{cleaned['name']} is already a dashboard channel"]},
                                 status=400)
    data["channels"].append(cleaned)
    channels_config.save_channels(data)
    mark_pending()
    return web.json_response({"channel": cleaned, "warnings": warnings, "pending_restart": True})


@require_token
async def api_edit_channel(request: web.Request):
    name = channels_config.normalize_handle(request.match_info.get("name", "")).lower()
    try:
        patch = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"errors": ["body must be JSON"]}, status=400)
    data = channels_config.load_channels()
    entry = next((c for c in data["channels"] if c["name"].lower() == name), None)
    if entry is None:
        return web.json_response({"errors": ["no such dashboard channel"]}, status=404)
    merged = dict(entry)
    for field in ("sources", "amazon_tag", "enabled", "note"):
        if field in (patch or {}):
            merged[field] = patch[field]
    snap = snapshot()
    others = {t for t in snap.get("targets", [])} if snap else set()
    cleaned, errors, warnings = channels_config.validate_channel(
        merged, set(snap.get("sources", [])) if snap else set(), others - {entry["name"]})
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
    return web.Response(text=UI_HTML, content_type="text/html")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/channels", api_channels)
    app.router.add_post("/api/channels", api_add_channel)
    app.router.add_patch("/api/channels/{name}", api_edit_channel)
    app.router.add_delete("/api/channels/{name}", api_delete_channel)
    app.router.add_get("/api/recent", api_recent)
    app.router.add_get("/api/audit", api_audit)
    app.router.add_post("/api/restart", api_restart)
    return app


UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BestGAA dashboard</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
main{max-width:960px;margin:0 auto;padding:16px}
.card{background:#1e293b;border-radius:12px;padding:14px;margin:12px 0}
h1{font-size:20px;margin:6px 0}h2{font-size:16px;margin:4px 0 10px;color:#93c5fd}
.row{display:flex;gap:10px;flex-wrap:wrap}
.stat{flex:1;min-width:130px;background:#0f172a;border-radius:10px;padding:10px;text-align:center}
.stat b{display:block;font-size:22px}.stat span{font-size:12px;color:#94a3b8}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px;border-bottom:1px solid #334155}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:99px;background:#334155}
.badge.ours{background:#14532d}.badge.new{background:#713f12}.badge.warn{background:#7c2d12}
button,input,select,textarea{background:#0f172a;color:#e2e8f0;border:1px solid #475569;border-radius:8px;padding:8px 10px;font-size:14px}
button{cursor:pointer;background:#2563eb;border-color:#2563eb}button.danger{background:#7f1d1d;border-color:#7f1d1d}
button.ghost{background:transparent}button:disabled{opacity:.5}
input,select,textarea{width:100%;box-sizing:border-box;margin:4px 0}
label{font-size:13px;color:#cbd5e1}
.srcs{columns:2;font-size:13px}.srcs label{display:block}
#tokenbar{display:flex;gap:8px;margin:8px 0}pre{white-space:pre-wrap;font-size:12px;color:#fca5a5}
.ok{color:#86efac}.muted{color:#94a3b8;font-size:12px}
</style>
</head>
<body>
<main>
<h1>BestGAA dashboard</h1>
<div id="tokenbar" class="card">
<input id="token" type="password" placeholder="Dashboard token (DASHBOARD_TOKEN)" autocomplete="off">
<button onclick="saveToken()">Use</button>
<span id="authstate" class="muted"></span>
</div>
<div id="pending" class="card" style="display:none;border:1px solid #f59e0b">
<b>Channel changes are waiting for a bot restart.</b>
<button onclick="doRestart()">Restart bot now</button>
<span class="muted">or run on the server: <span id="restartcmd">sudo systemctl restart bestgaa</span></span>
<pre id="restartout"></pre>
</div>
<div class="card"><h2>Status</h2><div class="row" id="stats"></div><div id="uptime" class="muted"></div></div>
<div class="card"><h2>Channels</h2>
<table><thead><tr><th>Channel</th><th>Type</th><th>Sources</th><th>Tag</th><th>On</th><th></th></tr></thead>
<tbody id="channels"></tbody></table>
<h2>Add a channel</h2>
<label>Telegram username (no @)</label><input id="fname" placeholder="MyNewDeals" autocomplete="off">
<label><input type="checkbox" id="fall" checked style="width:auto" onchange="toggleSrcs()"> Every main source (recommended)</label>
<div class="srcs" id="fsrcs" style="display:none"></div>
<label><input type="checkbox" id="ftag" style="width:auto"> Our Amazon tag on this channel (ONLY after declaring it in Associates Central)</label>
<label>Note</label><input id="fnote" placeholder="optional" autocomplete="off">
<button onclick="addChannel()">Add channel</button> <span id="addmsg"></span>
</div>
<div class="card"><h2>Recent jobs</h2>
<table><thead><tr><th>#</th><th>Source</th><th>Status</th><th>Sends</th><th>Skips</th><th>Error</th></tr></thead>
<tbody id="recent"></tbody></table></div>
<div class="card"><h2>Audit (live DB)</h2><div id="audit" class="muted">loading…</div></div>
</main>
<script>
let TOKEN = sessionStorage.getItem('dash_token') || '';
function headers(){ return {'Content-Type':'application/json','X-Dashboard-Token':TOKEN}; }
function saveToken(){ TOKEN = document.getElementById('token').value.trim();
  sessionStorage.setItem('dash_token', TOKEN); document.getElementById('token').value='';
  document.getElementById('authstate').textContent = TOKEN ? 'token set' : ''; refresh(); }
async function api(method, path, body){
  const r = await fetch(path, {method, headers:headers(), body: body ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(()=>({}));
  if(r.status === 401){ document.getElementById('authstate').textContent = 'bad token - paste it above'; }
  if(!r.ok) throw new Error((j.errors||[j.error||r.status]).join('; '));
  return j;
}
function fmtTime(ts){ if(!ts) return '—'; const d = new Date(ts*1000);
  return d.toLocaleString('en-IN',{hour12:false}) ; }
async function refresh(){
  if(!TOKEN){ document.getElementById('authstate').textContent = 'paste the token to begin'; return; }
  try {
    const s = await api('GET','/api/status');
    document.getElementById('authstate').textContent = s.insecure ? 'INSECURE MODE (no token set)' : 'connected';
    const q = s.queue || {};
    document.getElementById('stats').innerHTML =
      stat(s.today_sends,'sent today') + stat(s.today_skips,'skipped today') +
      stat(q.pending||0,'queued') + stat(q.processing||0,'working now') +
      stat(q.failed||0,'failed');
    document.getElementById('uptime').textContent =
      'last posted deal: '+fmtTime(s.last_post_at)+' · bot started: '+fmtTime(s.bot_started_at) +
      (s.db_exists?'':' · NO DATABASE YET');
    document.getElementById('pending').style.display = s.pending_restart ? 'block' : 'none';
    const c = await api('GET','/api/channels');
    let rows = '';
    (c.builtin||[]).forEach(t=>{ rows += `<tr><td>@${t}</td><td><span class="badge ours">built-in</span></td><td colspan="4" class="muted">code-managed</td></tr>`; });
    (c.dashboard||[]).forEach(t=>{ rows += `<tr><td>@${t.name}</td><td><span class="badge new">dashboard</span></td>`
      + `<td class="muted">${t.sources.length===1&&t.sources[0]==='*'?'every main source':t.sources.slice(0,3).join(', ')+(t.sources.length>3?'…':'')}</td>`
      + `<td>${t.amazon_tag?'<span class="badge ours">tag</span>':'<span class="muted">no tag</span>'}</td>`
      + `<td><button class="ghost" onclick="toggleCh('${t.name}',${!t.enabled})">${t.enabled?'ON':'OFF'}</button></td>`
      + `<td><button class="danger" onclick="delCh('${t.name}')">remove</button></td></tr>`; });
    document.getElementById('channels').innerHTML = rows || '<tr><td colspan="6" class="muted">no channels yet</td></tr>';
    const box = document.getElementById('fsrcs');
    if(!box.dataset.filled && (c.sources||[]).length){ box.dataset.filled = '1';
      box.innerHTML = c.sources.map(s=>`<label><input type="checkbox" value="${s}" style="width:auto"> ${s}</label>`).join(''); }
    const r = await api('GET','/api/recent?limit=15');
    document.getElementById('recent').innerHTML = (r.jobs||[]).map(j=>
      `<tr><td>${j.id}</td><td>${j.source}</td><td>${j.status}</td><td>${j.sends}</td><td>${j.skips}</td><td class="muted">${j.error||''}</td></tr>`).join('')
      || '<tr><td colspan="6" class="muted">queue is empty</td></tr>';
    const a = await api('GET','/api/audit?limit=200');
    const d = a.delivery_summary||{}, l = a.link_summary||{};
    document.getElementById('audit').innerHTML =
      `posts checked: <b>${a.posts_checked}</b> · real sends: <b>${d.real_sends||0}</b> · skipped: <b>${d.skipped||0}</b> · ` +
      `links: ${l.links||0} (ours: ${(l['our-short']||0)+(l['our-publisher']||0)}, foreign: ${l.foreign||0}) · ` +
      `findings: ${Object.entries(a.findings_by_kind||{}).map(([k,v])=>k+'='+v).join(', ')||'none'}`;
  } catch(e){ document.getElementById('authstate').textContent = 'error: '+e.message; }
}
function stat(v,l){ return `<div class="stat"><b>${v}</b><span>${l}</span></div>`; }
function toggleSrcs(){ document.getElementById('fsrcs').style.display =
  document.getElementById('fall').checked ? 'none' : 'block'; }
async function addChannel(){
  const msg = document.getElementById('addmsg'); msg.textContent = ''; msg.className = '';
  const useAll = document.getElementById('fall').checked;
  const srcs = useAll ? ['*'] : [...document.querySelectorAll('#fsrcs input:checked')].map(i=>i.value);
  try {
    const j = await api('POST','/api/channels',{name:document.getElementById('fname').value,
      sources:srcs, amazon_tag:document.getElementById('ftag').checked,
      note:document.getElementById('fnote').value});
    msg.textContent = 'added @'+j.channel.name+' - restart the bot to apply' +
      (j.warnings&&j.warnings.length ? ' ('+j.warnings.join('; ')+')' : '');
    msg.className = 'ok'; document.getElementById('fname').value='';
    document.getElementById('fnote').value=''; refresh();
  } catch(e){ msg.textContent = e.message; }
}
async function toggleCh(name, on){
  try { await api('PATCH','/api/channels/'+encodeURIComponent(name),{enabled:on}); refresh(); }
  catch(e){ alert(e.message); } }
async function delCh(name){
  if(!confirm('Remove @'+name+' from dashboard channels? (Built-in routing is untouched.)')) return;
  try { await api('DELETE','/api/channels/'+encodeURIComponent(name)); refresh(); }
  catch(e){ alert(e.message); } }
async function doRestart(){
  const out = document.getElementById('restartout'); out.textContent = 'restarting…';
  try { const j = await api('POST','/api/restart'); out.textContent = j.output||'restart requested';
    setTimeout(refresh, 8000); }
  catch(e){ out.textContent = e.message; } }
if(TOKEN) refresh();
</script>
</body>
</html>
"""


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

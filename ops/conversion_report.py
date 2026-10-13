#!/usr/bin/env python3
"""conversion_report.py — "anni perfectga convert chesthunnava ledaa?" answered from DATA.

Run on the server (this is the honest answer to that question):

    python3 ops/conversion_report.py                  # last 24h
    python3 ops/conversion_report.py --hours 72
    python3 ops/conversion_report.py --db <path> --log <path>
    python3 ops/conversion_report.py --json           # machine-readable

It reads the bot's OWN database and log and reports, per monetization route
(Amazon / EarnKaro / HYPD-Meesho), observed conversion activity and strict link
ownership. Route/hostname counts are diagnostics; only native attribution or
account-bound durable proof is treated as evidence that a link earns for us.

  CONFIG       what is configured - EarnKaro token claims (which account gets
               paid), convert_option, Amazon policy + tag, HYPD store, Bitly.
               Secrets are never printed (only counts / claims).
  ROUTES       every link the bot produced in the window, classified by route:
               earnkaro / amazon / hypd / affiliate / shortened / PASS-THROUGH
               (a pass-through cache row is a clean merchant URL, not ownership proof).
  LOG MARKERS  separate counts for EK SUCCESS, EK MISS, EK AUTH, EK REJECT,
               EK HTTP/NETWORK/FALLBACK, HYPD LINK, HYPD MISSING, Bitly, plus the
               link-health markers that answer "will this click pay us?" -
               LINK ATTRIBUTED, AMAZON LINK SHORTENED, AMAZON SHORT RESOLVED/CUT,
               DEGRADED POST, and the bridge's provenance rescue/cut lines.
  VERDICT      one line per route: WORKING / NEEDS ATTENTION / IDLE, plus the
               exact command for anything that needs attention.

Exit codes: 0 = every route that should be converting did, 1 = something needs
attention (auth failures, unmonetized posts, hypd products waiting for curation).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The DEPLOYED files come FIRST, the checkout's own copies last. 2026-10-04: the
# deploy imported the bot from the checkout, which created logs/bot.log and a
# default DB there, and this report read those TEST lines as if they were live -
# "AMAZON DIRECT 4" from a build that was not even deployed. A file inside the
# checkout is never evidence about the running bot unless the operator asks for it.
DEFAULT_DB_CANDIDATES = (
    Path(os.getenv("BOT_DB_PATH", "")) if os.getenv("BOT_DB_PATH") else None,
    Path("/home/ubuntu/bestgaa-bot/bestgaa-bot/bestgaa.sqlite3"),
    REPO_ROOT / "bestgaa" / "bestgaa.sqlite3",
)
DEFAULT_LOG_CANDIDATES = (
    Path(os.getenv("BOT_LOG", "")) if os.getenv("BOT_LOG") else None,
    Path("/home/ubuntu/bestgaa-bot/bestgaa-bot/logs/bot.log"),
    REPO_ROOT / "bestgaa" / "logs" / "bot.log",
)


def pick_candidate(candidates, explicit=None):
    """(path, note): the first EXISTING candidate, and a warning if it is suspect.

    The old code took the first non-None candidate, so on a server the checkout's
    file shadowed the deployed one even when it did not exist.
    """
    if explicit:
        return Path(explicit), ""
    for path in candidates:
        if path and Path(path).exists():
            note = ("read from the CHECKOUT (test data), not the deployed bot - pass "
                    "--db/--log for the live files") if str(REPO_ROOT) in str(path) else ""
            return Path(path), note
    for path in candidates:                      # nothing exists: name the default
        if path:
            return Path(path), ""
    return None, ""
ENV_CANDIDATES = (
    REPO_ROOT / "bestgaa" / ".env",
    Path("/home/ubuntu/bestgaa-bot/bestgaa-bot/.env"),
    Path.cwd() / ".env",
)

# --- route classification (kept in step with main_bot_new.py) ------------------
HYPD_HOSTS = ("hypd.store",)
EARNKARO_HOSTS = ("ekaro.in", "ekaro.app", "earnkaro.com", "affiliaters.in",
                  "earnkaro.in", "linkredirect.in", "clnk.in", "clnk.app")
SHORTENER_HOSTS = ("bit.ly", "j.mp", "bitly.com", "is.gd", "t.co", "buff.ly")
AMAZON_HOSTS = ("amazon.in", "amazon.com", "amzn.to", "amzn.in")

LOG_MARKERS = (
    ("EK SUCCESS", "EarnKaro: API returned an attributable affiliate link"),
    ("EK MISS", "EarnKaro: API returned no usable converted link"),
    ("EK AUTH", "EarnKaro: the TOKEN was refused (regenerate the key!)"),
    ("EK REJECT", "EarnKaro: extracted API output failed attribution validation"),
    ("EK VERIFIED", "EarnKaro: an unknown redirect domain was followed and PROVED ours"),
    ("EK HTTP", "EarnKaro: API returned a non-success HTTP status"),
    ("EK NETWORK", "EarnKaro: API/network retries were exhausted"),
    ("EK FALLBACK", "Amazon used its native tag because EarnKaro did not convert"),
    ("AMAZON DIRECT", "Amazon published with OUR OWN Associates tag - full commission, "
                      "no network share (account approved 2026-10-04)"),
    ("AMAZON CACHE UPGRADED", "a cached network row for an Amazon source was replaced by "
                              "our own Associates link"),
    ("PROVENANCE rejected", "a link we did not produce was refused"),
    ("UNMONETIZED LINK", "a deal posted with a clean merchant link (earns NOTHING)"),
    ("HYPD LINK", "verified HYPD conversion event (not delivery or payout proof)"),
    ("HYPD MISSING", "a Meesho product with NO curated HYPD link (earns nothing)"),
    ("HYPD WANTED", "the product was added to the curation to-do list"),
    ("BITLY unavailable", "shortener outage: the raw link was kept (never unmonetized)"),
    ("BITLY failed", "Bitly call failed (rate limit/token)"),
    ("SHORTENER fallback=is.gd", "is.gd was used because Bitly was unavailable"),
    # v18.5/v18.6 link-health markers. These are the ones that answer "is a click
    # about to pay nobody?" - a bare page stamped with our id, an Amazon short code
    # resolved to the real product page (or cut when it would not resolve), and a
    # bot-fed post repaired at delivery.
    ("LINK ATTRIBUTED", "a page that would have paid NOBODY was published WITH our "
                        "publisher id instead"),
    ("AMAZON LINK SHORTENED", "Amazon link resolved to the tagged product page, then "
                              "shortened with our own shortener"),
    ("AMAZON SHORT RESOLVED", "an Amazon short code (amzn.to) is a wrapper: it was "
                              "followed to OUR tagged product page"),
    ("AMAZON SHORT CUT", "an Amazon short code that would not resolve was CUT (the "
                         "deal text still posted)"),
    ("AMAZON SHORT REFUSED", "an Amazon short link was refused as a destination"),
    ("DEGRADED POST", "the API never answered: trusted merchant links were posted "
                      "(each one attributed)"),
    ("LINK DROPPED", "a link that never monetizes was cut; the deal still posted"),
    # WhatsApp bridge (lower case on purpose - the bridge logs them that way).
    ("provenance rescue", "the bridge resolved a stranger's wrapper to the merchant "
                          "page and re-attributed it as OURS"),
    ("provenance cut", "the bridge cut a stranger's wrapper that would not resolve "
                       "(the deal still posted)"),
    ("Provenance mismatch", "the bridge refused a link we did not produce"),
)


def host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def in_hosts(host: str, hosts) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def clean(url: str) -> str:
    return (url or "").strip().rstrip("/")


def route_of(affiliate: str, resolved: str) -> str:
    """Which monetization route produced this link_cache row."""
    a, r = affiliate or "", resolved or ""
    if a and r and clean(a) == clean(r):
        return "passthrough"                      # clean merchant link: earns nothing
    joined = (a + " " + r).lower()
    if "hypd.store" in joined:
        return "hypd"
    host = host_of(a)
    if in_hosts(host, EARNKARO_HOSTS) or "ekaro" in a.lower() or "affiliaters" in a.lower():
        return "earnkaro"
    if in_hosts(host, AMAZON_HOSTS) or "amazon.in" in joined:
        return "amazon"
    if in_hosts(host, SHORTENER_HOSTS):
        # A short link hides its destination; the resolved_url usually still knows.
        if "amazon." in r.lower():
            return "amazon"
        if "hypd.store" in r.lower():
            return "hypd"
        if "ekaro" in r.lower():
            return "earnkaro"
        return "shortened"
    low = a.lower()
    if "affextparam2=" in low or "affid=" in low or "tag=" in low:
        return "affiliate"
    return "other"


def load_env(extra: list[str] | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in list(ENV_CANDIDATES) + [Path(p) for p in (extra or [])]:
        if not path or not path.exists():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        except Exception:
            continue
    values.update({k: v for k, v in os.environ.items() if k in values})   # env wins
    return values


def token_claims(token: str) -> dict:
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def as_time(stamp) -> str:
    try:
        return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


# ------------------------------------------------------------------ report parts
def config_report(env: dict[str, str]) -> dict:
    token = env.get("EARNKARO_API_KEY", "")
    claims = token_claims(token)
    publisher = str(claims.get("earnkaro") or env.get("EARNKARO_PUBLISHER_ID") or "")
    bitly_tokens = [t for t in env.get("BITLY_TOKENS", "").split(",") if t.strip()]
    bitly_tokens += [t for t in env.get("WA_BITLY_TOKENS", "").split(",") if t.strip()]
    return {
        "earnkaro_token": bool(token),
        "earnkaro_publisher": publisher,
        "earnkaro_issued": as_time(claims.get("iat")) if claims.get("iat") else "?",
        "earnkaro_endpoint": env.get("EARNKARO_API_URL", "https://ekaro-api.affiliaters.in/api/converter/public"),
        "convert_option": env.get("EARNKARO_CONVERT_OPTION", "convert_only"),
        "amazon_via_earnkaro": env.get("AMAZON_VIA_EARNKARO", "true"),
        "amazon_direct_associates": env.get("AMAZON_DIRECT_ASSOCIATES", "true"),
        "amazon_tag": env.get("AMAZON_TAG", ""),
        "hypd_store": env.get("HYPD_STORE_ID", "93944"),
        "hypd_slug": env.get("HYPD_STORE_SLUG", "smartdeals"),
        "hypd_always_bitly": env.get("HYPD_ALWAYS_BITLY", "true"),
        "hypd_merchants": "meesho.com (pinned; HYPD is Meesho-only)",
        "bitly_tokens": len(bitly_tokens),
    }


def _quality_audit():
    """The ONE definition of "our link" (ops/quality_audit.py), imported not copied.

    A second copy of the ownership rule would drift, and then this report would
    disagree with the auditor about the very posts they both describe.
    """
    import importlib.util
    path = Path(__file__).resolve().parent / "quality_audit.py"
    try:
        spec = importlib.util.spec_from_file_location("quality_audit_shared", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def db_report(db: Path, hours: float, publisher: str = "", our_tag: str = "") -> dict:
    out: dict = {"db": str(db), "exists": db.exists(), "routes": {}, "passthrough_examples": [],
                 "hypd_learned": [], "hypd_wanted": [], "posted_deals": 0,
                 "links": {"links": 0, "our-short": 0, "our-publisher": 0,
                           "unattributed": 0, "clean-merchant": 0, "foreign": 0}}
    out["rendered_links"] = dict(out["links"])
    out["delivery_summary"] = {"evidence": "unavailable", "rendered_queues": 0,
        "queues_with_sends": 0, "target_sends": 0, "policy_skips": 0, "partial_target_sends": 0,
        "queues_only_skipped": 0, "queues_without_send_evidence": 0}
    out["audit_errors"] = []
    out["link_scope"] = ("Saved rendered links, once per queue with recorded successful delivery; "
                         "not per-target final text or live receipts. Window uses queue creation time.")
    if not db.exists():
        return out
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (datetime.now() - timedelta(hours=hours)).timestamp()

    def table_exists(name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return bool(row)

    qa = _quality_audit()
    our_links = qa.our_minted_links(conn, our_tag, publisher) if qa is not None else set()
    stores = {os.getenv("HYPD_STORE_ID", "93944").strip().lstrip("@").lower(),
              os.getenv("HYPD_STORE_SLUG", "smartdeals").strip().lstrip("@").lower()} - {""}
    def valid_hypd(row):
        return bool(qa and qa.mapping_valid(row["afflink_url"], row["resolved_url"],
                                          row["store"], row["product_key"], stores))

    verified_hypd_keys: set[str] = set()
    out_of_scope_hypd_keys: set[str] = set()
    if table_exists("hypd_links"):
        for hypd_row in conn.execute(
                "SELECT afflink_url, resolved_url, product_key, store FROM hypd_links").fetchall():
            key = str(hypd_row["product_key"] or "")
            if not key:
                continue
            if valid_hypd(hypd_row):
                verified_hypd_keys.add(key)
            elif not in_hosts(host_of(hypd_row["resolved_url"] or ""), ("meesho.com",)):
                out_of_scope_hypd_keys.add(key)

    if table_exists("link_cache"):
        rows = conn.execute(
            "SELECT source_url, affiliate_url, resolved_url, deal_key, created_at "
            "FROM link_cache WHERE created_at>=?", (since,)).fetchall()
        for row in rows:
            affiliate = row["affiliate_url"]
            resolved = row["resolved_url"] or ""
            source_host = host_of(row["source_url"] or "")
            resolved_host = host_of(resolved)
            key = str(row["deal_key"] or "")
            route = route_of(affiliate, resolved)
            if route == "hypd" and not in_hosts(resolved_host, ("meesho.com",)):
                route = "hypd_out_of_scope"
            elif route == "shortened":
                if key in out_of_scope_hypd_keys and key not in verified_hypd_keys:
                    route = "hypd_out_of_scope"
                elif key in verified_hypd_keys and affiliate in our_links:
                    route = "hypd"
            if source_host == "hypd.store" and not in_hosts(resolved_host, ("meesho.com",)):
                route = "hypd_out_of_scope"
            if route == "hypd" and affiliate not in our_links:
                route = "hypd_unverified"
            bucket = out["routes"].setdefault(route, {"count": 0, "examples": []})
            bucket["count"] += 1
            if route == "passthrough" and len(out["passthrough_examples"]) < 8:
                out["passthrough_examples"].append(resolved or affiliate)
        out["links_in_window"] = sum(b["count"] for b in out["routes"].values())
    else:
        out["links_in_window"] = 0

    if table_exists("hypd_links"):
        out["hypd_learned"] = [
            {"afflink": row["afflink_url"], "product": row["resolved_url"] or "",
             "created": as_time(row["created_at"])}
            for row in conn.execute(
                "SELECT afflink_url, resolved_url, product_key, store, created_at FROM hypd_links"
                " ORDER BY created_at DESC").fetchall()
            if valid_hypd(row)][:10]
    if table_exists("hypd_wanted"):
        out["hypd_wanted"] = [
            {"product": row["product_url"], "times": row["times"], "last": as_time(row["last_seen"])}
            for row in conn.execute(
                "SELECT product_url, times, last_seen FROM hypd_wanted"
                " ORDER BY times DESC, last_seen DESC").fetchall()
            if in_hosts(host_of(row["product_url"] or ""), ("meesho.com",))][:10]
    # A render/cache/conversion is not a send. Policy skips in old databases
    # may say status=sent but carry a last_error; those never count as sends.
    if qa is not None and table_exists("queue"):
        try:
            deliveries = {}
            summary = out["delivery_summary"]
            if table_exists("deliveries"):
                columns = {row[1] for row in conn.execute("PRAGMA table_info(deliveries)")}
                if {"queue_id", "status", "last_error"}.issubset(columns):
                    summary["evidence"] = "status_and_error"
                    chunks = "COALESCE(d.chunks_sent,0)" if "chunks_sent" in columns else "0"
                    for row in conn.execute(
                            f"SELECT d.queue_id,d.status,d.last_error,{chunks} AS chunks_sent FROM deliveries d "
                            "JOIN queue q ON q.id=d.queue_id WHERE q.created_at>=?", (since,)):
                        if row["chunks_sent"] > 0 and (row["status"] != "sent" or row["last_error"]):
                            summary["partial_target_sends"] += 1
                        sent, skipped = deliveries.setdefault(row["queue_id"], [0, 0])
                        if row["status"] == "sent" and not row["last_error"]:
                            sent += 1
                        elif row["status"] == "skipped" or (row["status"] == "sent" and row["last_error"]):
                            skipped += 1
                        deliveries[row["queue_id"]] = [sent, skipped]
                else:
                    summary["evidence"] = "legacy_schema_unverifiable"
            for row in conn.execute(
                    "SELECT id,rendered_text FROM queue WHERE created_at>=? AND rendered_text IS NOT NULL",
                    (since,)):
                summary["rendered_queues"] += 1
                sent, skipped = deliveries.get(row["id"], [0, 0])
                summary["target_sends"] += sent
                summary["policy_skips"] += skipped
                if sent:
                    summary["queues_with_sends"] += 1
                else:
                    summary["queues_without_send_evidence"] += 1
                    if skipped:
                        summary["queues_only_skipped"] += 1
                for url in qa.ANY_LINK.findall(row["rendered_text"] or ""):
                    url = url.rstrip(")】.,;'\"")
                    bucket = qa.classify_link(url, our_tag, publisher, our_links)
                    out["rendered_links"]["links"] += 1
                    out["rendered_links"][bucket] += 1
                    if sent:
                        out["links"]["links"] += 1
                        out["links"][bucket] += 1
        except Exception as exc:
            out["audit_errors"].append(f"delivery/link audit incomplete: {type(exc).__name__}")
    elif qa is None:
        out["audit_errors"].append("ownership policy module unavailable")

    if table_exists("posted_deals"):
        out["posted_deals"] = conn.execute(
            "SELECT COUNT(*) AS n FROM posted_deals WHERE posted_at>=?",
            (since,)).fetchone()["n"] if _has_column(conn, "posted_deals", "posted_at") else 0
    conn.close()
    return out


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        return column in cols
    except Exception:
        return False


def log_report(log: Path, hours: float) -> dict:
    out: dict = {"log": str(log), "exists": log.exists(), "markers": {}}
    if not log.exists():
        return out
    cutoff = datetime.now() - timedelta(hours=hours)
    stamp_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-200_000:]
    except Exception:
        return out
    for line in lines:
        match = stamp_re.match(line)
        when = None
        if match:
            try:
                when = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            except Exception:
                when = None
        if when and when < cutoff:
            continue
        # Older builds used the same `EK CONVERT` prefix for both success and
        # failure. Split those historical lines by their payload instead of
        # treating every successful conversion as a no-campaign result.
        legacy_markers: set[str] = set()
        if "EK CONVERT |" in line:
            if "| no link for " in line:
                legacy_markers.add("EK MISS")
            elif " -> " in line:
                legacy_markers.add("EK SUCCESS")
        lowered = line.lower()
        for marker, _what in LOG_MARKERS:
            if marker.lower() in lowered or marker in legacy_markers:
                bucket = out["markers"].setdefault(marker, {"count": 0, "last": ""})
                bucket["count"] += 1
                bucket["last"] = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
    return out


def verdict(cfg: dict, db: dict, lg: dict) -> tuple[list[str], list[str]]:
    """(lines, problems)."""
    lines: list[str] = []
    problems: list[str] = []
    routes = db.get("routes", {})
    markers = lg.get("markers", {})

    # --- EarnKaro
    if not cfg["earnkaro_token"]:
        problems.append("EARNKARO_API_KEY is EMPTY - every conversion answers 401")
        lines.append("EarnKaro : BROKEN - no API key configured")
    elif not cfg["earnkaro_publisher"]:
        problems.append("the EarnKaro token carries no 'earnkaro' publisher claim")
        lines.append("EarnKaro : SUSPECT - token does not name a publisher")
    else:
        auth = markers.get("EK AUTH", {}).get("count", 0)
        successes = markers.get("EK SUCCESS", {}).get("count", 0)
        no_link = markers.get("EK MISS", {}).get("count", 0)
        rejects = markers.get("EK REJECT", {}).get("count", 0)
        http_errors = markers.get("EK HTTP", {}).get("count", 0)
        network_errors = markers.get("EK NETWORK", {}).get("count", 0)
        cached_route_rows = routes.get("earnkaro", {}).get("count", 0)
        converted = successes
        problems_found = rejects + http_errors + network_errors
        if auth:
            problems.append(f"{auth} EarnKaro AUTH failure(s) in the window - "
                            "regenerate the key (ops/set_earnkaro_key.sh)")
            lines.append(f"EarnKaro : BROKEN - token refused {auth}x (publisher {cfg['earnkaro_publisher']})")
        elif problems_found:
            problems.append(f"{problems_found} EarnKaro API output/error event(s); inspect EK REJECT / EK HTTP / EK NETWORK logs")
            lines.append(f"EarnKaro : NEEDS REVIEW - {converted} successful link(s), "
                         f"{rejects} rejected output(s), {http_errors + network_errors} API/network error(s)")
        elif converted and no_link:
            problems.append(f"partial EarnKaro conversion: {converted} successful link(s), {no_link} API no-link response(s); "
                            "check Affiliaters network selections and store campaign eligibility")
            lines.append(f"EarnKaro : PARTIAL - {converted} successful link(s), {no_link} no-link response(s) "
                         f"(pays {cfg['earnkaro_publisher']})")
        elif converted:
            lines.append(f"EarnKaro : WORKING - {converted} attributable link(s) observed "
                         f"(pays {cfg['earnkaro_publisher']})")
        elif no_link:
            problems.append(f"zero successful EarnKaro links and {no_link} API no-link response(s); "
                            "check Affiliaters network selections, connected EarnKaro account, and campaign eligibility")
            lines.append(f"EarnKaro : NOT CONVERTING - {no_link} no-link response(s), 0 attributable links")
        elif cached_route_rows:
            lines.append(f"EarnKaro : CACHE ONLY - {cached_route_rows} route-like cache row(s), "
                         "but no fresh API success; these rows do not prove ownership or conversion")
        else:
            lines.append("EarnKaro : IDLE - no fresh API success/no-link activity in this window; "
                         "no live conversion evidence")

    # --- Amazon
    tag = cfg["amazon_tag"] or "(none - tagless!)"
    if not cfg["amazon_tag"]:
        problems.append("AMAZON_TAG is empty - Amazon links would be published tagless")
        lines.append("Amazon   : ATTENTION - no tag configured")
    else:
        fallback_count = markers.get("EK FALLBACK", {}).get("count", 0)
        lines.append(f"Amazon   : tag {tag}; routed via EarnKaro={cfg['amazon_via_earnkaro']}"
                     + (f"; {fallback_count} native-tag fallback(s) used (not EarnKaro commission)"
                        if fallback_count else ""))

    # --- HYPD / Meesho
    learned = db.get("hypd_learned", [])
    wanted = db.get("hypd_wanted", [])
    hypd_conversions = markers.get("HYPD LINK", {}).get("count", 0)
    if learned or hypd_conversions:
        lines.append(f"HYPD     : READY / ACTIVITY - {len(learned)} valid stored Meesho mapping(s), "
                     f"{hypd_conversions} conversion event(s) (store {cfg['hypd_store']}/{cfg['hypd_slug']}); "
                     "not delivery or payout proof")
    else:
        lines.append("HYPD     : IDLE - nothing learned yet; run ops/hypd_links.py with a verified Meesho link")
    unverified = routes.get("hypd_unverified", {}).get("count", 0)
    if unverified:
        problems.append(f"{unverified} HYPD cache row(s) lack current ownership/product proof; reconversion required")
        lines.append(f"           {unverified} unverified HYPD cache row(s) withheld")
    out_of_scope = routes.get("hypd_out_of_scope", {}).get("count", 0)
    if out_of_scope:
        problems.append(f"{out_of_scope} legacy HYPD cache row(s) point outside Meesho; "
                        "they are not valid under the current policy and will be rechecked on use")
        lines.append(f"           {out_of_scope} out-of-scope legacy HYPD cache row(s) detected")
    if wanted:
        problems.append(f"{len(wanted)} Meesho product(s) skipped under strict-all - "
                        "curate a verified HYPD link and learn it (ops/hypd_links.py)")
        lines.append(f"           {len(wanted)} product(s) WAITING for curation "
                     "(strict-all skips them until then)")

    # --- Shortener
    if cfg["bitly_tokens"]:
        lines.append(f"Bitly    : PRESENT - {cfg['bitly_tokens']} token(s); our hypd links are shortened")
    else:
        problems.append("no BITLY_TOKENS on this .env - the is.gd fallback is used instead of Bitly")
        lines.append("Bitly    : MISSING - hypd links fall back to is.gd (still shortened, not Bitly)")
    bitly_fail = markers.get("BITLY failed", {}).get("count", 0)
    if bitly_fail:
        lines.append(f"           {bitly_fail} Bitly call(s) failed in the window (rate limit/token)")

    unmon = markers.get("UNMONETIZED LINK", {}).get("count", 0)
    if unmon:
        problems.append(f"{unmon} logged post(s) contained a clean merchant link "
                        "with no proven earning attribution")
        lines.append(f"Plain    : ATTENTION - {unmon} unmonetized post(s) in the log window")
    for error in db.get("audit_errors", []):
        problems.append(error)
    delivery = db.get("delivery_summary") or {}
    if delivery:
        lines.append(f"DELIVERY : {delivery.get('target_sends', 0)} recorded target send(s); "
                     f"{delivery.get('policy_skips', 0)} policy skip(s); "
                     f"{delivery.get('queues_without_send_evidence', 0)} rendered queue(s) without send evidence")
        if delivery.get("partial_target_sends"):
            problems.append(f"{delivery['partial_target_sends']} partially sent target(s); inspect receipts before retrying")
            lines.append(f"           {delivery['partial_target_sends']} partial target send(s); not counted as complete")
        if delivery.get("evidence") != "status_and_error":
            lines.append("           delivery evidence unavailable/incomplete; no sends inferred from renders")
            if delivery.get("rendered_queues"):
                problems.append("rendered queues exist but delivery evidence is unavailable/incomplete")
    # --- link ownership for queues with recorded sends: use the same
    # data-backed rule as ops/quality_audit.py; cache rows/hostnames never count.
    links = db.get("links") or {}
    if links.get("links"):
        unowned = sum(int(links.get(key, 0) or 0)
                      for key in ("foreign", "unattributed", "clean-merchant"))
        if unowned:
            problems.append(
                f"{unowned} saved link(s) from sent queues lack verified ownership "
                f"({links.get('foreign', 0)} foreign, {links.get('unattributed', 0)} "
                f"without publisher attribution, {links.get('clean-merchant', 0)} "
                "clean merchant); run `python3 ops/quality_audit.py --strict` for queue ids")
            lines.append(f"LINKS    : NEEDS ATTENTION - {unowned}/{links['links']} "
                         "saved link(s) from sent queues lack verified earning attribution")
        else:
            lines.append(f"LINKS    : VERIFIED - {links['links']} saved link(s) from sent queues all have "
                         f"our account attribution ({links['our-short']} short, "
                         f"{links['our-publisher']} publisher, {links['clean-merchant']} "
                         "clean merchant)")
    return lines, problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Is everything converting? (data, not vibes)")
    parser.add_argument("--hours", type=float, default=24.0, help="window (default 24)")
    parser.add_argument("--db", help="path to the bot database")
    parser.add_argument("--log", help="path to the bot log")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--env-file", action="append", default=[],
                        help="extra .env file(s) to read (repeatable)")
    args = parser.parse_args()

    db, db_note = pick_candidate(DEFAULT_DB_CANDIDATES, args.db)
    log, log_note = pick_candidate(DEFAULT_LOG_CANDIDATES, args.log)
    env = load_env(args.env_file)

    cfg = config_report(env)
    db_rep = db_report(db, args.hours, cfg["earnkaro_publisher"], cfg["amazon_tag"]) if db else {}
    lg_rep = log_report(log, args.hours) if log else {}
    lines, problems = verdict(cfg, db_rep or {}, lg_rep or {})

    if args.json:
        print(json.dumps({"config": cfg, "db": db_rep, "log": lg_rep,
                          "verdict": lines, "problems": problems}, indent=2))
        return 1 if problems else 0

    if db_note:
        print(f"NOTE: database {db_note}")
    if log_note:
        print(f"NOTE: log {log_note}")
    print("=" * 78)
    print(f"CONVERSION REPORT — last {args.hours:g}h")
    print("=" * 78)
    print("CONFIG")
    print(f"  EarnKaro : key={'YES' if cfg['earnkaro_token'] else 'NO'}  "
          f"publisher={cfg['earnkaro_publisher'] or '?'} (issued {cfg['earnkaro_issued']})")
    print(f"             endpoint={cfg['earnkaro_endpoint']}  convert_option={cfg['convert_option']}")
    print(f"  Amazon   : tag={cfg['amazon_tag'] or '(none)'}  via EarnKaro={cfg['amazon_via_earnkaro']}")
    print(f"  HYPD     : store={cfg['hypd_store']} ({cfg['hypd_slug']})  "
          f"always Bitly={cfg['hypd_always_bitly']}  merchants={cfg['hypd_merchants']}")
    print(f"  Bitly    : {cfg['bitly_tokens']} token(s)")

    if db_rep.get("exists"):
        print(f"\nROUTES (bot's own database: {db_rep['db']})")
        if not db_rep.get("links_in_window"):
            print("  no links produced in this window")
        for route, bucket in sorted(db_rep.get("routes", {}).items(),
                                    key=lambda kv: -kv[1]["count"]):
            note = "   <- affiliate_url == resolved_url; not proof this row was posted or owned" if route == "passthrough" else ""
            print(f"  {route:<12} {bucket['count']:>6}{note}")
        for url in db_rep.get("passthrough_examples", []):
            print(f"      clean-link cache sample (not ownership proof): {url[:100]}")
        print(f"  posted_deals in window: {db_rep.get('posted_deals', 0)}")
        print("  " + db_rep.get("link_scope", ""))
        links = db_rep.get("links") or {}
        if links.get("links"):
            print("\nLINK OWNERSHIP (saved renders from queues with recorded sends)")
            print(f"  links={links['links']}  our-short={links['our-short']}  "
                  f"our-publisher={links['our-publisher']}  "
                  f"unattributed={links.get('unattributed', 0)}  "
                  f"clean-merchant={links['clean-merchant']}  FOREIGN={links['foreign']}")
            if links.get("foreign") or links.get("unattributed") or links.get("clean-merchant"):
                print("  -> one or more saved links from sent queues have no verified attribution proof; "
                      "cache rows and clean merchant URLs are not proof. Run "
                      "python3 ops/quality_audit.py --strict for queue ids.")
            else:
                print("  -> saved links from sent queues have account-bound or native attribution evidence")
            posture = []
            for marker, label in (("LINK ATTRIBUTED", "page(s) stamped with our id"),
                                  ("AMAZON LINK SHORTENED", "Amazon link(s) shortened"),
                                  ("AMAZON SHORT RESOLVED", "Amazon short code(s) resolved"),
                                  ("AMAZON SHORT CUT", "Amazon short code(s) cut"),
                                  ("DEGRADED POST", "degraded post(s) attributed")):
                count = (lg_rep.get("markers") or {}).get(marker, {}).get("count")
                if count:
                    posture.append(f"{count} {label}")
            if posture:
                print("  LINK POSTURE | " + "; ".join(posture))
    else:
        print(f"\nROUTES: database not found ({db}) - run this ON the server")

    if lg_rep.get("exists") and lg_rep.get("markers"):
        print(f"\nLOG MARKERS ({lg_rep['log']})")
        for marker, what in LOG_MARKERS:
            bucket = lg_rep["markers"].get(marker)
            if bucket:
                print(f"  {marker:<26} {bucket['count']:>5}  last {bucket['last']}   {what}")

    print("\n" + "=" * 78)
    print("VERDICT")
    for line in lines:
        print(f"  {line}")
    if problems:
        print("\nNEEDS ATTENTION")
        for problem in problems:
            print(f"  - {problem}")
        print("\nFixes: python3 ops/earnkaro_check.py   (EarnKaro key + OUR hypd links, live)")
        print("       python3 ops/hypd_links.py --wanted   (products waiting for a hypd link)")
        print("       ./ops/set_earnkaro_key.sh '<token>'  (rotate a refused key)")
        print("       python3 ops/earnkaro_check.py --plan --deal-url '<a real source link>'")
        print("           -> the EXACT link the channel will carry for that deal (converted,")
        print("              shortened, and proved to pay OUR account)")
    else:
        print("\nEverything that should be converting is converting.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

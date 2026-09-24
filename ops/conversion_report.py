#!/usr/bin/env python3
"""conversion_report.py — "anni perfectga convert chesthunnava ledaa?" answered from DATA.

Run on the server (this is the honest answer to that question):

    python3 ops/conversion_report.py                  # last 24h
    python3 ops/conversion_report.py --hours 72
    python3 ops/conversion_report.py --db <path> --log <path>
    python3 ops/conversion_report.py --json           # machine-readable

It reads the bot's OWN database and log and reports, per monetization route
(Amazon / EarnKaro / HYPD-Meesho), whether links are actually converting:

  CONFIG       what is configured - EarnKaro token claims (which account gets
               paid), convert_option, Amazon policy + tag, HYPD store, Bitly.
               Secrets are never printed (only counts / claims).
  ROUTES       every link the bot produced in the window, classified by route:
               earnkaro / amazon / hypd / affiliate / shortened / PASS-THROUGH
               (a pass-through row is a clean merchant link: it earns NOTHING).
  LOG MARKERS  how often each decision fired lately (EK CONVERT, EK AUTH,
               EK REJECT, HYPD LINK, HYPD MISSING, BITLY unavailable, ...).
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_CANDIDATES = (
    Path(os.getenv("BOT_DB_PATH", "")) if os.getenv("BOT_DB_PATH") else None,
    REPO_ROOT / "bestgaa" / "bestgaa.sqlite3",
    Path("/home/ubuntu/bestgaa-bot/bestgaa-bot/bestgaa.sqlite3"),
)
DEFAULT_LOG_CANDIDATES = (
    Path(os.getenv("BOT_LOG", "")) if os.getenv("BOT_LOG") else None,
    REPO_ROOT / "bestgaa" / "logs" / "bot.log",
    Path("/home/ubuntu/bestgaa-bot/bestgaa-bot/logs/bot.log"),
)
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
    ("EK CONVERT", "EarnKaro: conversion ATTEMPTED and no link returned"),
    ("EK AUTH", "EarnKaro: the TOKEN was refused (regenerate the key!)"),
    ("EK REJECT", "EarnKaro: a link paying somebody else was refused"),
    ("PROVENANCE rejected", "a link we did not produce was refused"),
    ("UNMONETIZED LINK", "a deal posted with a clean merchant link (earns NOTHING)"),
    ("HYPD LINK", "OUR hypd link published (Bitly) - earns on our store"),
    ("HYPD MISSING", "a Meesho/Shopsy product with NO curated hypd link (earns nothing)"),
    ("HYPD WANTED", "the product was added to the curation to-do list"),
    ("BITLY unavailable", "shortener outage: the raw link was kept (never unmonetized)"),
    ("BITLY failed", "Bitly call failed (rate limit/token)"),
    ("SHORTENER fallback=is.gd", "is.gd was used because Bitly was unavailable"),
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
        "amazon_tag": env.get("AMAZON_TAG", ""),
        "hypd_store": env.get("HYPD_STORE_ID", "93944"),
        "hypd_slug": env.get("HYPD_STORE_SLUG", "smartdeals"),
        "hypd_always_bitly": env.get("HYPD_ALWAYS_BITLY", "true"),
        "hypd_merchants": env.get("HYPD_MERCHANT_DOMAINS", "meesho.com,shopsy.in"),
        "bitly_tokens": len(bitly_tokens),
    }


def db_report(db: Path, hours: float) -> dict:
    out: dict = {"db": str(db), "exists": db.exists(), "routes": {}, "passthrough_examples": [],
                 "hypd_learned": [], "hypd_wanted": [], "posted_deals": 0}
    if not db.exists():
        return out
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (datetime.now() - timedelta(hours=hours)).timestamp()

    def table_exists(name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return bool(row)

    if table_exists("link_cache"):
        rows = conn.execute(
            "SELECT affiliate_url, resolved_url, created_at FROM link_cache WHERE created_at>=?",
            (since,)).fetchall()
        for row in rows:
            route = route_of(row["affiliate_url"], row["resolved_url"])
            bucket = out["routes"].setdefault(route, {"count": 0, "examples": []})
            bucket["count"] += 1
            if route == "passthrough" and len(out["passthrough_examples"]) < 8:
                out["passthrough_examples"].append(row["resolved_url"] or row["affiliate_url"])
        out["links_in_window"] = sum(b["count"] for b in out["routes"].values())
    else:
        out["links_in_window"] = 0

    if table_exists("hypd_links"):
        out["hypd_learned"] = [
            {"afflink": row["afflink_url"], "product": row["resolved_url"] or "",
             "created": as_time(row["created_at"])}
            for row in conn.execute(
                "SELECT afflink_url, resolved_url, created_at FROM hypd_links"
                " ORDER BY created_at DESC LIMIT 10").fetchall()]
    if table_exists("hypd_wanted"):
        out["hypd_wanted"] = [
            {"product": row["product_url"], "times": row["times"], "last": as_time(row["last_seen"])}
            for row in conn.execute(
                "SELECT product_url, times, last_seen FROM hypd_wanted"
                " ORDER BY times DESC, last_seen DESC LIMIT 10").fetchall()]
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
        for marker, _what in LOG_MARKERS:
            if marker in line:
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
        no_link = markers.get("EK CONVERT", {}).get("count", 0)
        converted = routes.get("earnkaro", {}).get("count", 0)
        if auth:
            problems.append(f"{auth} EarnKaro AUTH failure(s) in the window - "
                            "regenerate the key (ops/set_earnkaro_key.sh)")
            lines.append(f"EarnKaro : BROKEN - token refused {auth}x (publisher {cfg['earnkaro_publisher']})")
        else:
            lines.append(f"EarnKaro : WORKING - {converted} converted link(s), 0 auth failures"
                         + (f", {no_link} answer(s) of 'no campaign' (normal for some stores)"
                            if no_link else "")
                         + f" (pays {cfg['earnkaro_publisher']})")

    # --- Amazon
    tag = cfg["amazon_tag"] or "(none - tagless!)"
    if not cfg["amazon_tag"]:
        problems.append("AMAZON_TAG is empty - Amazon links would be published tagless")
        lines.append("Amazon   : ATTENTION - no tag configured")
    else:
        lines.append(f"Amazon   : WORKING - tag {tag}; routed via EarnKaro="
                     f"{cfg['amazon_via_earnkaro']} with the native tag as fallback")

    # --- HYPD / Meesho
    learned = db.get("hypd_learned", [])
    wanted = db.get("hypd_wanted", [])
    hypd_posts = markers.get("HYPD LINK", {}).get("count", 0)
    if learned or hypd_posts:
        lines.append(f"HYPD     : WORKING - {len(learned)} link(s) learned, {hypd_posts} post(s) on OUR link"
                     f" (store {cfg['hypd_store']}/{cfg['hypd_slug']})")
    else:
        lines.append("HYPD     : IDLE - nothing learned yet; run ops/hypd_links.py with your links")
    if wanted:
        problems.append(f"{len(wanted)} Meesho/Shopsy product(s) posted UNMONETIZED - "
                        "curate a hypd link and learn it (ops/hypd_links.py)")
        lines.append(f"           {len(wanted)} product(s) WAITING for curation (earn nothing until then)")

    # --- Shortener
    if cfg["bitly_tokens"]:
        lines.append(f"Bitly    : PRESENT - {cfg['bitly_tokens']} token(s); our hypd links are shortened")
    else:
        problems.append("no BITLY_TOKENS on this .env - the is.gd fallback is used instead of Bitly")
        lines.append("Bitly    : MISSING - hypd links fall back to is.gd (still shortened, not Bitly)")
    bitly_fail = markers.get("BITLY failed", {}).get("count", 0)
    if bitly_fail:
        lines.append(f"           {bitly_fail} Bitly call(s) failed in the window (rate limit/token)")

    unmon = markers.get("UNMONETIZED LINK", {}).get("count", 0) or routes.get("passthrough", {}).get("count", 0)
    if unmon:
        problems.append(f"{unmon} deal(s) posted with a clean merchant link "
                        "(no campaign anywhere) - see the pass-through list below")
        lines.append(f"Plain    : {unmon} unmonetized deal(s) in the window")
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

    db = Path(args.db) if args.db else next((p for p in DEFAULT_DB_CANDIDATES if p), None)
    log = Path(args.log) if args.log else next((p for p in DEFAULT_LOG_CANDIDATES if p), None)
    env = load_env(args.env_file)

    cfg = config_report(env)
    db_rep = db_report(db, args.hours) if db else {}
    lg_rep = log_report(log, args.hours) if log else {}
    lines, problems = verdict(cfg, db_rep or {}, lg_rep or {})

    if args.json:
        print(json.dumps({"config": cfg, "db": db_rep, "log": lg_rep,
                          "verdict": lines, "problems": problems}, indent=2))
        return 1 if problems else 0

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
            note = "   <- earns NOTHING (no campaign anywhere)" if route == "passthrough" else ""
            print(f"  {route:<12} {bucket['count']:>6}{note}")
        for url in db_rep.get("passthrough_examples", []):
            print(f"      unmonetized example: {url[:100]}")
        print(f"  posted_deals in window: {db_rep.get('posted_deals', 0)}")
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
    else:
        print("\nEverything that should be converting is converting.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

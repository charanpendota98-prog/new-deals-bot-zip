#!/usr/bin/env python3
"""Link doctor: is this URL valid, and is it OURS?

Written for this report (2026-10-21): *"ilaga invalid url link vasthunnai mana
channels lo"* with two `https://is.gd/xxxx` links. They opened fine, but both
resolved to **untagged** `amazon.in/dp/...` pages - a hop that carries no
publisher, no account and no record of who minted it, so nothing about it is
ours and every click on it earned nobody.

The doctor answers three questions per URL, in this order, because that is the
order the answers matter:

  1. **Does it work?** follows every redirect (up to --max-hops) and reports the
     HTTP status and the page it lands on. A 404, a "Page Not Found" body or a
     dead host is `DEAD`.
  2. **Where does it land?** the final URL, and whether that destination carries
     our Associates tag / EarnKaro publisher / a stored proof.
  3. **Is it ours?** `OURS` only with evidence (native attribution or a durable
     proof row) - never because the hostname looks like a shortener we use.

For anything that is not ours it prints the **exact repaired link**, so the
offending post can be fixed by hand in seconds.

Usage
    python3 ops/link_doctor.py https://is.gd/c3Gl9d https://is.gd/bO79HA
    python3 ops/link_doctor.py --recent 50          # the last 50 published links
    python3 ops/link_doctor.py --text post.txt      # every link in a post
    python3 ops/link_doctor.py --recent 200 --json --strict

`--offline` skips the network entirely and judges the URL shape plus the stored
proofs, which is what the test suite uses (and what a firewalled host can still
answer). `--strict` exits 1 when any link is DEAD or not ours.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

ROOT = Path(__file__).resolve().parent.parent
URL_RE = re.compile(r"https?://[^\s<>\[\](){}\"'`]+", re.I)

# Hosts whose whole job is redirecting. The shape test below catches the rest.
SHORTENER_HOSTS = {
    "bit.ly", "bitly.com", "j.mp", "t.co", "buff.ly", "ow.ly", "tinyurl.com",
    "tiny.cc", "cutt.ly", "rb.gy", "t.ly", "shorturl.at", "spoo.me", "da.gd",
    "surl.li", "shrtco.de", "9qr.de", "cli.re", "v.ht", "shorte.st", "snip.ly",
    "is.gd", "v.gd", "bitli.in", "ekaro.in", "clnk.in", "clnk.app", "ekaro.app",
    "fktr.in", "myntr.it", "ajiio.in", "affiliaters.in", "linkredirect.in",
    "amzn.to", "amzn.in", "amazn.lt", "link.amazon", "fkrt.co", "fkrt.cc",
    "fkrt.in", "dl.flipkart.com", "msho.in", "hypd.store", "hypd.link",
}
# Shorteners that cannot be bound to an account at all: a link on one of these is
# ours only with a stored proof.
TOKENLESS_HOSTS = {
    "is.gd", "v.gd", "tinyurl.com", "tiny.cc", "rb.gy", "cutt.ly", "t.ly",
    "shorturl.at", "spoo.me", "da.gd", "surl.li", "shrtco.de", "9qr.de",
    "cli.re", "v.ht", "shorte.st", "snip.ly",
}
AMAZON_HOST = re.compile(r"^(?:www\.|m\.|smile\.)?amazon\.[a-z.]{2,8}$", re.I)
EK_HOST = re.compile(r"^(?:[^/\s]*\.)?(?:flipkart\.com|myntra\.com|ajio\.com)$", re.I)
# Bodies that mean "this page is not a product", whatever the status code says.
DEAD_MARKERS = (
    "page not found", "sorry! we couldn't find that page",
    "we couldn't find that page", "the page you requested could not be found",
    "this page isn't available", "product not found", "no longer available",
    "invalid url", "error 404", "404 not found",
)
SERVICE_HOSTS = ("wa.me", "whatsapp.com", "t.me", "telegram.me", "youtube.com",
                 "youtu.be", "instagram.com", "facebook.com")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def clean(url: str) -> str:
    return str(url or "").strip().rstrip(".,;:!?)'\"»")


def host_of(url: str) -> str:
    try:
        return (urlparse(clean(url)).hostname or "").lower()
    except ValueError:
        return ""


def in_domains(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def looks_like_hop(url: str) -> bool:
    """A known shortener, or the shape every shortener has: one short slug."""
    host = host_of(url)
    if not host:
        return False
    if in_domains(host, SHORTENER_HOSTS):
        return True
    if in_domains(host, SERVICE_HOSTS):
        return False
    path = (urlparse(clean(url)).path or "").strip("/")
    return bool(path) and "/" not in path and len(path) <= 24


def load_audit():
    """The ownership engine that decides "is this link ours" everywhere else."""
    spec = importlib.util.spec_from_file_location("quality_audit_for_doctor",
                                                  ROOT / "ops" / "quality_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_db(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def minted_links(conn: sqlite3.Connection | None, audit, tag: str, publisher: str) -> set[str]:
    if conn is None:
        return set()
    try:
        return set(audit.our_minted_links(conn, tag, publisher) or ())
    except Exception:  # noqa: BLE001 - a sweep must not die on an old schema
        return set()


def default_tag() -> str:
    import os
    return os.getenv("AMAZON_TAG", "mama086-21")


def default_publisher() -> str:
    import os
    return os.getenv("EARNKARO_PUBLISHER_ID", "5478322")


def repair(url: str, tag: str, publisher: str) -> str:
    """The same page, carrying OUR attribution - the link we should have posted."""
    parsed = urlparse(clean(url))
    host = (parsed.hostname or "").lower()
    pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower() not in ("tag", "affextparam1", "affextparam2", "affid", "subid")]
    if AMAZON_HOST.match(host) and tag:
        pairs.append(("tag", tag))
    elif EK_HOST.match(host) and publisher:
        pairs.append(("affExtParam2", publisher))
    else:
        return ""
    return urlunparse(parsed._replace(query=urlencode(pairs, doseq=True)))


# --------------------------------------------------------------------------- #
# network
# --------------------------------------------------------------------------- #
async def follow(session, url: str, *, max_hops: int = 5, timeout: float = 12.0) -> dict:
    """Follow redirects by hand so every hop is reported, not just the last one."""
    chain: list[dict] = []
    current = clean(url)
    for _ in range(max(1, max_hops)):
        try:
            async with session.get(current, allow_redirects=False,
                                   timeout=timeout) as response:
                location = response.headers.get("Location", "")
                body = ""
                if 200 <= response.status < 300:
                    body = (await response.content.read(200_000)).decode("utf-8", "ignore").lower()
                else:
                    await response.content.read(1)
                chain.append({"url": current, "status": response.status,
                              "location": location})
                if response.status in (301, 302, 303, 307, 308) and location:
                    current = urljoin(str(response.url), location)
                    continue
                return {"chain": chain, "final": str(response.url), "status": response.status,
                        "body": body, "error": ""}
        except Exception as exc:  # noqa: BLE001 - a broken URL is an answer, not a crash
            chain.append({"url": current, "status": 0, "location": ""})
            return {"chain": chain, "final": current, "status": 0, "body": "",
                    "error": f"{type(exc).__name__}: {exc}"[:160]}
    return {"chain": chain, "final": current, "status": 0, "body": "",
            "error": f"more than {max_hops} redirects"}


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #
def classify(url: str, *, audit, minted: set[str], tag: str, publisher: str,
             probe: dict | None = None, offline: bool = False) -> dict:
    url = clean(url)
    host = host_of(url)
    hop = looks_like_hop(url)
    final = clean((probe or {}).get("final") or url)
    status = int((probe or {}).get("status") or 0)
    body = (probe or {}).get("body") or ""
    error = (probe or {}).get("error") or ""

    owned_here = audit.why_not_our_link(url, tag, publisher, minted) is None
    owned_final = audit.why_not_our_link(final, tag, publisher, minted) is None
    final_host = host_of(final)

    verdict, why = "", ""
    if offline:
        if owned_here:
            verdict, why = "OURS", "native attribution or a stored proof"
        elif hop:
            verdict = "UNPROVEN-HOP"
            why = (("a tokenless short link proves nothing about who minted it"
                    if in_domains(host, TOKENLESS_HOSTS) else
                    "a short link with no stored proof that it wraps our destination"))
        elif audit.why_not_our_link(url, tag, publisher, minted):
            verdict, why = "NOT-OURS", str(audit.why_not_our_link(url, tag, publisher, minted))
        else:
            verdict, why = "OURS", ""
        return _finish(url, final, host, final_host, hop, verdict, why, status,
                       error, tag, publisher, offline=True)

    if error and "redirects" in error:
        return _finish(url, final, host, final_host, hop, "LOOP", error, status, error,
                       tag, publisher)
    if error:
        return _finish(url, final, host, final_host, hop, "UNREACHABLE",
                       f"could not fetch: {error}", status, error, tag, publisher)
    if status >= 400:
        return _finish(url, final, host, final_host, hop, "DEAD",
                       f"HTTP {status} at {final}", status, "", tag, publisher)
    if any(marker in body for marker in DEAD_MARKERS):
        return _finish(url, final, host, final_host, hop, "DEAD",
                       "the page itself says it does not exist", status, "", tag, publisher)

    if owned_here:
        return _finish(url, final, host, final_host, hop, "OURS",
                       "native attribution or a stored proof", status, "", tag, publisher)
    if hop and not owned_final:
        why = ("a tokenless short link proves nothing about who minted it"
               if in_domains(host, TOKENLESS_HOSTS)
               else "a short link with no stored proof that it wraps our destination")
        if AMAZON_HOST.match(final_host) and not owned_final:
            why += "; the destination carries no tag of ours, so it earns nothing"
        return _finish(url, final, host, final_host, hop, "UNPROVEN-HOP", why,
                       status, "", tag, publisher)
    if owned_final:
        return _finish(url, final, host, final_host, hop, "OURS",
                       "it lands on a destination that carries our attribution",
                       status, "", tag, publisher)
    reason = audit.why_not_our_link(final, tag, publisher, minted) or "no attribution found"
    return _finish(url, final, host, final_host, hop, "NOT-OURS", str(reason),
                   status, "", tag, publisher)


def _finish(url: str, final: str, host: str, final_host: str, hop: bool, verdict: str,
            why: str, status: int, error: str, tag: str, publisher: str,
            offline: bool = False) -> dict:
    suggestion = "" if verdict == "OURS" else repair(final, tag, publisher)
    return {
        "url": url,
        "host": host,
        "hop": bool(hop),
        "verdict": verdict,
        "why": why,
        "final": final,
        "final_host": final_host,
        "status": status,
        "error": error,
        "repair": suggestion,
        "ours": verdict == "OURS",
        "offline": offline,
    }


async def probe_all(urls: list[str], *, offline: bool, timeout: float,
                    max_hops: int) -> dict[str, dict]:
    if offline:
        return {}
    import aiohttp
    out: dict[str, dict] = {}
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (compatible; BestGAA link doctor)"}) as session:
        sem = asyncio.Semaphore(6)

        async def one(url: str) -> None:
            async with sem:
                out[url] = await follow(session, url, max_hops=max_hops, timeout=timeout)

        await asyncio.gather(*(one(url) for url in urls))
    return out


def recent_links(conn: sqlite3.Connection, limit: int) -> tuple[list[dict], str]:
    """The links the bot actually published, newest first (link_cache + posts)."""
    if conn is None:
        return [], "no database"
    rows: list[dict] = []
    try:
        for row in conn.execute(
                "SELECT affiliate_url, resolved_url, created_at FROM link_cache "
                "ORDER BY created_at DESC LIMIT ?", (limit,)):
            rows.append({"url": clean(row[0]), "source": "link_cache",
                         "resolved": str(row[1] or ""), "at": float(row[2] or 0)})
    except sqlite3.OperationalError:
        pass
    try:
        for row in conn.execute(
                "SELECT id, rendered_text, created_at FROM queue "
                "WHERE rendered_text IS NOT NULL ORDER BY created_at DESC LIMIT ?", (limit,)):
            for url in dict.fromkeys(URL_RE.findall(str(row[1] or ""))):
                rows.append({"url": clean(url), "source": f"post #{row[0]}",
                             "resolved": "", "at": float(row[2] or 0)})
    except sqlite3.OperationalError:
        pass
    seen: set[str] = set()
    unique = []
    for row in sorted(rows, key=lambda r: r["at"], reverse=True):
        if not row["url"] or row["url"] in seen:
            continue
        seen.add(row["url"])
        unique.append(row)
        if len(unique) >= limit:
            break
    return unique, "ok"


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
BAD = {"DEAD", "UNREACHABLE", "LOOP", "UNPROVEN-HOP", "NOT-OURS"}
FIXES = {
    "UNPROVEN-HOP": "do not publish a hop we cannot prove; post the repaired link below",
    "NOT-OURS": "this link pays somebody else or nobody; post the repaired link below",
    "DEAD": "drop the link (the deal text can stay) or re-read the source post",
    "UNREACHABLE": "retry from the server; a blocked host is not a dead deal",
    "LOOP": "the redirect chain never lands; treat the link as dead",
}


def render_human(results: list[dict], *, scanned: int = 0, source: str = "",
                 note: str = "") -> str:
    lines = []
    for item in results:
        chain = item.get("chain") or []
        via = " → ".join(f"{hop['status'] or '??'} {hop['url']}"
                         for hop in chain[:4]) if chain else item["url"]
        lines.append(f"{item['verdict']:<13} {item['url']}")
        if item.get("final") and item["final"] != item["url"]:
            lines.append(f"              lands on  {item['final']}"
                         + (f"  (HTTP {item['status']})" if item.get("status") else ""))
        if chain and len(chain) > 1:
            lines.append(f"              path      {via}")
        if item.get("why"):
            lines.append(f"              why       {item['why']}")
        if item["verdict"] in BAD:
            lines.append(f"              fix       {FIXES.get(item['verdict'], 'repost our link')}")
            if item.get("repair"):
                lines.append(f"              use this  {item['repair']}")
            elif item.get("offline") and item["hop"]:
                lines.append("              use this  re-run without --offline: the doctor follows "
                             "the hop and prints the tagged link")
        lines.append("")
    counts: dict[str, int] = {}
    for item in results:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    bad = sum(counts.get(key, 0) for key in BAD)
    summary = " · ".join(f"{counts[key]} {key.lower()}" for key in sorted(counts, key=str))
    head = f"{len(results)} link(s) checked"
    if scanned:
        head += f" (from {scanned} published link(s), source: {source})"
    lines.append(f"{head}: {summary or 'nothing to check'}")
    lines.append("VERDICT: " + ("every link is ours and answers" if not bad else
                                f"{bad} link(s) need attention - the fix is printed above"))
    if note:
        lines.append(f"note: {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Is this link valid, and is it ours? Prints the repaired link when it is not.")
    parser.add_argument("urls", nargs="*", help="URLs to check")
    parser.add_argument("--recent", type=int, default=0,
                        help="check the N most recent links the bot published")
    parser.add_argument("--text", type=Path, default=None,
                        help="check every link inside this file (a pasted post)")
    parser.add_argument("--db", type=Path, default=None,
                        help="bot database (default: bestgaa/state/bot_state.sqlite3)")
    parser.add_argument("--tag", default=default_tag(), help="our Amazon Associates tag")
    parser.add_argument("--publisher", default=default_publisher(),
                        help="our EarnKaro publisher id")
    parser.add_argument("--offline", action="store_true",
                        help="do not touch the network; judge shape and stored proofs only")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--max-hops", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when any link is dead or not ours")
    args = parser.parse_args(argv)

    audit = load_audit()
    default_db = ROOT / "bestgaa" / "state" / "bot_state.sqlite3"
    db = Path(args.db) if args.db else default_db
    conn = read_db(db)
    minted = minted_links(conn, audit, args.tag, args.publisher)

    urls: list[str] = [clean(u) for u in args.urls if clean(u)]
    scanned, source = 0, "command line"
    if args.recent:
        rows, source = recent_links(conn, args.recent)
        scanned = len(rows)
        urls += [row["url"] for row in rows]
    if args.text:
        try:
            urls += URL_RE.findall(args.text.read_text(encoding="utf-8", errors="ignore"))
        except OSError as exc:
            print(f"cannot read {args.text}: {exc}", file=sys.stderr)
            return 2
        source = str(args.text)
    urls = list(dict.fromkeys(clean(u) for u in urls if clean(u)))
    if conn is not None:
        conn.close()

    if not urls:
        print("nothing to check: give URLs, --recent N or --text FILE", file=sys.stderr)
        return 2

    probes = asyncio.run(probe_all(urls, offline=args.offline, timeout=args.timeout,
                                   max_hops=args.max_hops))
    results = []
    for url in urls:
        item = classify(url, audit=audit, minted=minted, tag=args.tag,
                        publisher=args.publisher, probe=probes.get(url),
                        offline=args.offline)
        item["chain"] = (probes.get(url) or {}).get("chain") or []
        results.append(item)

    if args.json:
        print(json.dumps({"checked_at": time.time(), "offline": args.offline,
                          "db": str(db), "source": source, "results": results},
                         indent=2, ensure_ascii=False))
    else:
        note = ""
        if not db.exists():
            note = (f"no database at {db}: stored proofs are unknown, so every hop is judged "
                    "on shape alone (point --db at the bot's real bestgaa.sqlite3 for the full answer)")
        print(render_human(results, scanned=scanned, source=source, note=note))

    if args.strict and any(item["verdict"] in BAD for item in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

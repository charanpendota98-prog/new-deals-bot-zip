"""Deal forensics: one queue id, every link and every rewrite, with proof.

`trace()` answers "which channels got this post". This module answers the two
questions that decide whether the post was *worth* posting:

  1. **Is every published link OURS?** For each URL in the rendered post: which
     host, which proof we have (`owned_link_proofs`, the conversion cache, our
     HYPD store, our Amazon tag), what it resolves to, and - when it is not
     proven ours - exactly why. A link copied straight out of the source post is
     called out as *the source's own link*, because republishing it earns them,
     not us.
  2. **What did the pipeline do to the source text?** The row keeps
     `source_text` (intake) and `rendered_text` (what went out), so the dossier
     can show the line diff, the link rewrites, the numbers that survived and the
     numbers that did not.

It also builds a single ordered timeline (intake → refusals → send attempts →
delivery verdicts) and a findings list where every finding carries a concrete
fix. Nothing here mutates the database, and anything the database cannot prove is
reported as `unknown` - never assumed to be fine.

Read-only. Pure over a connection, so it is tested against a synthetic database.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

try:  # package import (dashboard/app.py) and script import (tests) both work
    from . import insight
except ImportError:  # pragma: no cover - script-style import
    import insight  # type: ignore

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")
# A rupee amount or any number with a thousands separator: the things a source
# post must never lose ("₹1,299" becoming "₹129" is a real, expensive bug).
NUMBER_RE = re.compile(r"(?:₹|rs\.?\s?)?\d[\d,]*(?:\.\d+)?", re.I)
AMAZON_HOST = re.compile(r"(^|\.)amazon\.[a-z.]{2,}$", re.I)
HYPD_HOSTS = ("hypd.store", "hypd.co", "hypd.shop")
# Shorteners whose links are only "ours" when the conversion cache proves it.
MAX_LINKS = 40
MAX_DIFF_LINES = 60


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _query(url: str) -> dict[str, list[str]]:
    try:
        return parse_qs(urlsplit(url).query)
    except ValueError:
        return {}


def _clean(url: str) -> str:
    return str(url or "").strip().rstrip(".,;:)\u2019\"'")


def _urls(text: str) -> list[str]:
    seen: list[str] = []
    for raw in URL_RE.findall(str(text or "")):
        url = _clean(raw)
        if url and url not in seen:
            seen.append(url)
    return seen


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def owned_index(conn: sqlite3.Connection, tables: set[str]) -> dict:
    """Everything the database can prove about a link, keyed by URL."""
    proofs: dict[str, dict] = {}
    if "owned_link_proofs" in tables:
        cols = _columns(conn, "owned_link_proofs")
        if {"affiliate_url", "destination_url"} <= cols:
            for url, destination, kind, publisher, created in conn.execute(
                    "SELECT affiliate_url, destination_url, "
                    + ("proof_type" if "proof_type" in cols else "''") + ", "
                    + ("publisher_id" if "publisher_id" in cols else "''") + ", "
                    + ("created_at" if "created_at" in cols else "0")
                    + " FROM owned_link_proofs"):
                if url:
                    proofs[str(url)] = {"destination": str(destination or ""),
                                        "kind": str(kind or "proof"),
                                        "publisher": str(publisher or ""),
                                        "at": float(created or 0)}
    cache: dict[str, dict] = {}
    cache_by_source: dict[str, dict] = {}
    if "link_cache" in tables:
        cols = _columns(conn, "link_cache")
        if {"source_url", "affiliate_url"} <= cols:
            resolved_col = "resolved_url" if "resolved_url" in cols else "''"
            key_col = "deal_key" if "deal_key" in cols else "''"
            time_col = "created_at" if "created_at" in cols else "0"
            for source_url, affiliate, resolved, deal_key, created in conn.execute(
                    f"SELECT source_url, affiliate_url, {resolved_col}, {key_col}, "
                    f"{time_col} FROM link_cache"):
                row = {"source": str(source_url or ""), "affiliate": str(affiliate or ""),
                       "resolved": str(resolved or ""), "deal_key": str(deal_key or ""),
                       "at": float(created or 0)}
                if affiliate:
                    cache[str(affiliate)] = row
                if source_url:
                    cache_by_source[str(source_url)] = row
    hypd: set[str] = set()
    if "hypd_links" in tables:
        cols = _columns(conn, "hypd_links")
        if "link" in cols:
            for (link,) in conn.execute("SELECT link FROM hypd_links"):
                if link:
                    hypd.add(str(link))
    return {"proofs": proofs, "cache": cache, "cache_by_source": cache_by_source,
            "hypd": hypd}


def classify_link(url: str, index: dict, *, amazon_tag: str = "",
                  from_source: bool = False) -> dict:
    """Is this published link provably ours, and what is the proof?

    The order matters: an explicit proof beats the cache, the cache beats a tag
    guess, and a link we cannot prove is reported as `unknown` - not as ours.
    """
    url = str(url or "")
    host = _host(url)
    row = {"url": url, "host": host, "ours": False, "kind": "unknown",
           "proof": "", "resolved": "", "from_source": bool(from_source),
           "risk": ""}
    proof = index["proofs"].get(url)
    if proof:
        row.update(ours=True, kind="owned_link_proof",
                   proof=(f"{proof['kind']}"
                          + (f" · publisher {proof['publisher']}" if proof["publisher"] else "")),
                   resolved=proof["destination"])
        return row
    if url in index["hypd"]:
        row.update(ours=True, kind="hypd_store", proof="our HYPD store link")
        return row
    if AMAZON_HOST.search(host):
        tag = (_query(url).get("tag") or [""])[0]
        if tag and amazon_tag and tag == amazon_tag:
            row.update(ours=True, kind="amazon_tag", proof=f"our tag {tag}")
        elif tag and amazon_tag:
            row.update(kind="foreign_tag",
                       risk=f"tag={tag} is not ours ({amazon_tag}) - this pays someone else")
        elif tag:
            row.update(kind="unverified_tag",
                       proof=f"tag={tag}",
                       risk="no AMAZON_TAG in this environment, so a tag cannot be proven ours")
        else:
            row.update(kind="untagged",
                       risk="no ?tag= at all - an untagged Amazon page earns nothing")
        return row
    cached = index["cache"].get(url)
    if cached:
        row.update(ours=True, kind="conversion_cache",
                   proof=("our conversion" + (f" · key {cached['deal_key']}"
                                              if cached["deal_key"] else "")),
                   resolved=cached["resolved"])
        if cached["source"] and cached["source"] != url:
            row["rewrote"] = cached["source"]
        return row
    # Anything copied verbatim from the source post is *their* link unless we can
    # prove otherwise above - that is the exact mistake this dossier exists for.
    row.update(kind="unproven",
               risk=("this is the source's own link, published as-is - it pays them, "
                     "not us" if from_source else
                     "no proof in link_cache, owned_link_proofs or hypd_links"))
    return row


def _numbers(text: str) -> list[str]:
    """Comparable number tokens (₹1,299 / 1299 / Rs. 1299 all become '1299')."""
    out: list[str] = []
    for raw in NUMBER_RE.findall(str(text or "")):
        token = re.sub(r"[^\d.]", "", raw)
        if not token or token in {".", ""}:
            continue
        token = token.rstrip(".")
        if token.count(".") > 1:
            continue
        out.append(token)
    return out


def _diff(source: str, rendered: str) -> dict:
    """What the pipeline kept, dropped and added, line by line."""
    before = [line for line in str(source or "").splitlines()]
    after = [line for line in str(rendered or "").splitlines()]
    removed: list[str] = []
    added: list[str] = []
    for line in difflib.unified_diff(before, after, lineterm="", n=0):
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    return {"removed": removed[:MAX_DIFF_LINES], "added": added[:MAX_DIFF_LINES],
            "removed_count": len(removed), "added_count": len(added),
            "identical": not removed and not added}


def _states(queue_status: str, deliveries: list[dict]) -> dict:
    """The one honest verdict for this post, from the ledger and nothing else."""
    received = [d for d in deliveries if d["received"]]
    withheld = [d for d in deliveries if d["status"] in {"skipped", "held"}]
    failed = [d for d in deliveries if d["status"] == "failed"]
    pending = [d for d in deliveries if d["status"] == "pending"]
    if received:
        state = "delivered"
    elif withheld:
        state = "withheld"
    elif failed:
        state = "failed"
    elif pending:
        state = "waiting"
    elif queue_status in {"pending", "processing"}:
        state = "waiting"
    elif queue_status == "failed":
        state = "failed"
    else:
        # terminal in the queue, but the ledger has no row at all: the bot never
        # recorded an attempt, which is a hole and must never be averaged away.
        state = "not_attempted"
    return {"state": state, "received": [d["target"] for d in received],
            "withheld": [d["target"] for d in withheld],
            "failed": [d["target"] for d in failed],
            "pending": [d["target"] for d in pending]}


def dossier(conn: sqlite3.Connection, queue_id: int, *, now: float | None = None,
            amazon_tag: str = "", snapshot: dict | None = None,
            channels: list | None = None) -> dict:
    """The complete, ordered story of one post: text, links, routing, delivery."""
    now = time.time() if now is None else now
    tables = insight._tables(conn)
    if "queue" not in tables:
        raise KeyError("this database has no queue table yet; start the bot once "
                       "on the merged code")
    cols = _columns(conn, "queue")
    wanted = ["id", "chat_id", "msg_id", "source", "status", "created_at"]
    for optional in ("rendered_text", "source_text", "targets_json", "last_error",
                     "attempts", "quality", "premium_score"):
        if optional in cols:
            wanted.append(optional)
    row = conn.execute(f"SELECT {', '.join(wanted)} FROM queue WHERE id=?",
                       (int(queue_id),)).fetchone()
    if row is None:
        raise LookupError(f"no queue row {queue_id}")
    got = dict(zip(wanted, row))
    rendered = str(got.get("rendered_text") or "")
    source_text = str(got.get("source_text") or "")
    source = str(got.get("source") or "")
    created = float(got.get("created_at") or 0)

    deliveries: list[dict] = []
    if "deliveries" in tables:
        dcols = _columns(conn, "deliveries")
        chunk_col = "chunks_sent" if "chunks_sent" in dcols else "0"
        error_col = "COALESCE(last_error,'')" if "last_error" in dcols else "''"
        for target, status, attempts, chunks, error in conn.execute(
                f"SELECT target, status, {('attempts' if 'attempts' in dcols else '0')}, "
                f"{chunk_col}, {error_col} FROM deliveries WHERE queue_id=? ORDER BY target",
                (int(queue_id),)):
            error = str(error or "")
            deliveries.append({"target": str(target or ""), "status": str(status or ""),
                               "attempts": int(attempts or 0), "chunks": int(chunks or 0),
                               "error": error,
                               "received": status == "sent" and not error})

    attempts: list[dict] = []
    if "send_attempts" in tables:
        for target, index, state, stamp in conn.execute(
                "SELECT target, chunk_index, state, updated_at FROM send_attempts "
                "WHERE queue_id=? ORDER BY updated_at", (int(queue_id),)):
            attempts.append({"target": str(target or ""), "chunk": int(index or 0),
                             "state": str(state or ""), "at": float(stamp or 0),
                             "at_ist": insight.ist(stamp)})

    blocks: list[dict] = []
    if "blocked_content" in tables:
        for stage, target, category, label, matched, reason, version, excerpt, at in conn.execute(
                "SELECT stage, target, category, label, matched, reason, policy_version, "
                "excerpt, created_at FROM blocked_content WHERE queue_id=? "
                "OR (source=? AND msg_id=?) ORDER BY created_at",
                (int(queue_id), source, got.get("msg_id"))):
            blocks.append({"stage": stage, "target": target, "category": category,
                           "label": label, "matched": matched, "reason": reason,
                           "policy_version": version, "excerpt": excerpt,
                           "at": float(at or 0), "at_ist": insight.ist(at)})

    index = owned_index(conn, tables)
    published = []
    for url in _urls(rendered)[:MAX_LINKS]:
        published.append(classify_link(url, index, amazon_tag=amazon_tag,
                                       from_source=url in source_text))
    source_links = []
    published_urls = {row_["url"] for row_ in published}
    rewrites = {row_["rewrote"]: row_["url"] for row_ in published if row_.get("rewrote")}
    for url in _urls(source_text)[:MAX_LINKS]:
        cached = index["cache_by_source"].get(url)
        target = rewrites.get(url) or (cached["affiliate"] if cached else "")
        published_as_present = bool(target) and target in published_urls
        source_links.append({
            "url": url, "host": _host(url),
            "published_as": target,
            # `kept` = the source's own URL went out unchanged; `rewritten` = our
            # conversion of it went out; `dropped` = a conversion exists but was
            # not the link published; `unused` = nothing maps it at all.
            "kept": url in published_urls,
            "rewritten": published_as_present and target != url,
            "dropped": bool(target) and not published_as_present,
            "cached": bool(cached),
        })

    states = _states(str(got.get("status") or ""), deliveries)
    unproven = [row_ for row_ in published if not row_["ours"]]
    source_owned = [row_ for row_ in unproven if row_["from_source"]]

    findings: list[dict] = []

    def finding(severity: str, code: str, detail: str, fix: str) -> None:
        findings.append({"severity": severity, "code": code, "detail": detail, "fix": fix})

    if states["state"] == "not_attempted":
        finding("error", "no_attempt_recorded",
                f"queue #{queue_id} is '{got.get('status')}' but the ledger has no "
                f"delivery row at all - the bot never recorded an attempt",
                "check the log line for this queue id; the rescan loop recovers it, "
                "or re-queue the message id by hand")
    if states["failed"]:
        finding("error", "delivery_failed",
                "delivery failed: " + ", ".join(states["failed"]),
                "read the delivery error below; a transient Telegram/network error is "
                "retried by the queue, a permanent one needs the target or text fixed")
    if unproven and states["received"]:
        # The user's rule: never publish a link we cannot prove is ours. A post
        # that went out with an unproven link is an error, whatever its shape.
        finding("error", "unproven_link",
                f"{len(unproven)} published link(s) are not proven ours: "
                + ", ".join(row_["host"] or row_["url"] for row_ in unproven[:3]),
                "run ops/link_doctor.py on the source post; the converter must mint "
                "our own tag/publisher link before this is posted")
    if source_text and published and all(row_["from_source"] and not row_["ours"]
                                         for row_ in published):
        finding("error", "republished_source_link",
                "every published link is the source's own link, unchanged",
                "the source's link pays the source; convert it to ours or withhold the post")
    if source_text and not published and _urls(source_text):
        finding("warn", "link_free_post",
                "the source carried links and the published post has none",
                "check the LINK REPAIR log line: a dead shortener is posted as complete "
                "text on purpose, an unproven link is withheld instead")
    lost = [token for token in _numbers(source_text) if token not in _numbers(rendered)]
    if source_text and lost:
        finding("error", "numbers_lost",
                f"{len(lost)} number(s) from the source are missing from the post: "
                + ", ".join(lost[:5]),
                "the renderer enforces numeric fidelity; this means the post was edited "
                "after rendering or stored before that rule - re-render from source_text")
    if str(got.get("targets_json") or "").strip():
        planned = []
        try:
            planned = [str(name) for name in json.loads(got["targets_json"]) or []]
        except (ValueError, TypeError):
            planned = []
        missing = [name for name in planned
                   if not any(d["target"].lower() == name.lower() for d in deliveries)]
        if missing:
            finding("warn", "planned_not_attempted",
                    "routed to but never attempted: " + ", ".join(missing[:5]),
                    "the send loop stopped before these targets; check the queue worker log")
        empty = [d["target"] for d in deliveries if d["received"] and d["chunks"] <= 0]
        if empty:
            finding("warn", "zero_chunks",
                    "recorded as delivered with 0 chunks: " + ", ".join(empty[:5]),
                    "a zero-byte send is a silent miss; verify the channel by hand")

    timeline: list[dict] = [{"at": created, "at_ist": insight.ist(created),
                             "kind": "intake", "target": "",
                             "detail": f"taken in from {source or 'an unnamed source'}"}]
    for block in blocks:
        timeline.append({"at": block["at"], "at_ist": block["at_ist"], "kind": "refused",
                         "target": block["target"] or "",
                         "detail": f"{block['stage']}: {block['label'] or block['category']}"
                                   f" - {block['reason'] or 'no reason recorded'}"})
    for attempt in attempts:
        timeline.append({"at": attempt["at"], "at_ist": attempt["at_ist"],
                         "kind": "send_attempt", "target": attempt["target"],
                         "detail": f"chunk {attempt['chunk']}: {attempt['state']}"})
    for delivery in deliveries:
        when = max([a["at"] for a in attempts if a["target"] == delivery["target"]] or [created])
        timeline.append({"at": when, "at_ist": insight.ist(when), "kind": "delivery",
                         "target": delivery["target"],
                         "detail": delivery["status"]
                                   + (f" - {delivery['error']}" if delivery["error"] else "")})
    timeline.sort(key=lambda event: (event["at"] or 0, event["kind"]))

    first_send = min([a["at"] for a in attempts if a["state"] == "sent"], default=0)
    verdict = {
        "delivered": f"posted to {len(states['received'])} channel(s)",
        "withheld": "withheld: " + ", ".join(states["withheld"]),
        "failed": "failed: " + ", ".join(states["failed"]),
        "waiting": "not sent yet",
        "not_attempted": "never recorded as attempted",
    }[states["state"]]
    if published:
        verdict += (" · every link proven ours" if not unproven
                    else f" · {len(unproven)} link(s) NOT proven ours")

    return {
        "generated_at": now,
        "post": {"queue_id": int(got["id"]), "chat_id": got.get("chat_id"),
                 "msg_id": got.get("msg_id"), "source": source,
                 "status": str(got.get("status") or ""),
                 "created_at": created, "created_ist": insight.ist(created),
                 "age": insight.age(now - created) if created else "—",
                 "attempts": int(got.get("attempts") or 0),
                 "error": str(got.get("last_error") or ""),
                 "quality": got.get("quality"), "premium_score": got.get("premium_score"),
                 "rendered": rendered, "source_text": source_text,
                 "targets": _planned(got.get("targets_json"))},
        "words": {"source": len(source_text.split()), "rendered": len(rendered.split())},
        "states": states,
        "verdict": verdict,
        "links": {"published": published, "source": source_links,
                  "proven": sum(1 for row_ in published if row_["ours"]),
                  "unproven": len(unproven),
                  "our_tag": amazon_tag or ""},
        "diff": _diff(source_text, rendered),
        "numbers": {"source": _numbers(source_text), "rendered": _numbers(rendered),
                    "lost": lost},
        "timeline": timeline,
        "findings": findings,
        "latency_seconds": (first_send - created) if first_send and created else None,
        "deliveries": deliveries,
        "blocks": blocks,
        "policy": {"snapshot": bool(snapshot), "channels": bool(channels)},
    }


def _planned(raw) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(name) for name in value]


def newest_refused(conn: sqlite3.Connection, *, now: float | None = None) -> int | None:
    """The queue id of the most recent post that was refused or never attempted.

    This is the one an operator actually wants at 2 a.m.: something was taken in
    and did not come out, most recently.
    """
    tables = insight._tables(conn)
    if "queue" not in tables:
        return None
    try:
        row = conn.execute(
            "SELECT q.id FROM queue q WHERE q.status='failed' "
            "   OR (q.status='done' AND NOT EXISTS "
            "       (SELECT 1 FROM deliveries d WHERE d.queue_id=q.id)) "
            "ORDER BY q.created_at DESC LIMIT 1").fetchone() if "deliveries" in tables else \
            conn.execute("SELECT id FROM queue WHERE status='failed' "
                         "ORDER BY created_at DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row else None

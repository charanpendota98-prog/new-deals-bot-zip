"""Show the "Join …" button row on every Nth post, not on every post.

The operator's request (2026-10-25): *"every time ravoddu … manam every 10th post
ki ilaga, 5th post ki kuda set cheskogalagam"* — do not put the join buttons under
every single post; put them under **every 10th** (or 5th, or any N) — settable
per channel from the dashboard.

Why it needs a counter and not a clock. "Every 10th post" is a property of the
**posts** a channel received, so the answer has to survive restarts, retries and
resumes, and it must count a post **once**:

* the counter lives in the bot's own SQLite database (`button_cadence`), beside
  the delivery ledger, so a restart continues where the channel left off;
* the decision is taken per `(target, queue_id)` and remembered (`last_queue_id`
  + `last_show`). A resumed job - or a settlement retry that re-sends nothing -
  asks the same question again and gets the SAME answer, so a post can never be
  counted twice and a retry can never turn "no buttons" into "buttons";
* the decision is made when the post is actually being delivered to that
  channel, so a skipped target does not consume a slot.

`every <= 1` means every post, which is the old behaviour and needs no state.
The number itself comes from `post_buttons.json` (`every`, global + per channel);
this module only answers "this one, or not?".
"""
from __future__ import annotations

import sqlite3

TABLE = "button_cadence"
DECISIONS = "button_cadence_decisions"
# How many recent decisions per channel are remembered. A retry or a resume
# happens minutes after the send, so a few dozen is already generous; the cap is
# what keeps this from growing with every post forever.
DECISION_ROWS = 200
MAX_EVERY = 100
MIN_EVERY = 1


def clamp(every) -> int:
    """The N from the dashboard, made sane: 1 (every post) .. 100."""
    try:
        value = int(every)
    except (TypeError, ValueError):
        return MIN_EVERY
    return max(MIN_EVERY, min(MAX_EVERY, value))


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ledger if this database predates it (safe to call every pass)."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        " target TEXT PRIMARY KEY,"
        " seen INTEGER NOT NULL DEFAULT 0,"
        " last_queue_id INTEGER NOT NULL DEFAULT 0,"
        " last_show INTEGER NOT NULL DEFAULT 0,"
        " updated_at REAL NOT NULL DEFAULT 0)")
    # One row per (channel, post) that has been decided. `last_queue_id` alone
    # was not enough: a retry of an OLDER post (manual send, a resumed job that
    # did not finish in order) would not match the last id and would be counted
    # again - the schedule would drift every time something was retried.
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {DECISIONS} ("
        " target TEXT NOT NULL,"
        " queue_id INTEGER NOT NULL,"
        " show INTEGER NOT NULL,"
        " decided_at REAL NOT NULL,"
        " PRIMARY KEY (target, queue_id))")


def state(conn: sqlite3.Connection, target: str) -> dict:
    """How many posts this channel has counted, and what the last one did."""
    ensure_schema(conn)
    for row in conn.execute(
            f"SELECT seen,last_queue_id,last_show,updated_at FROM {TABLE} WHERE target=?",
            (str(target),)):
        return {"target": target, "seen": int(row[0]), "last_queue_id": int(row[1]),
                "last_show": bool(row[2]), "updated_at": float(row[3])}
    return {"target": target, "seen": 0, "last_queue_id": 0, "last_show": False,
            "updated_at": 0.0}


def should_show(conn: sqlite3.Connection, target: str, queue_id=None, every=1,
                now: float | None = None) -> bool:
    """True when THIS post is the one that carries the button row.

    `every <= 1` answers True without touching the database. Otherwise the post
    is counted once: the same `(target, queue_id)` always gets the same answer,
    and the Nth, 2Nth, 3Nth … posts carry the row (the FIRST post of a fresh
    channel does too, so a quiet channel still shows the buttons early - the
    schedule is "every Nth", not "after N posts have gone by").
    """
    import time
    every = clamp(every)
    if every <= 1:
        return True
    ensure_schema(conn)
    key = str(target)
    try:
        queue_key = int(queue_id) if queue_id is not None else 0
    except (TypeError, ValueError):
        queue_key = 0
    if queue_key:
        decided = conn.execute(
            f"SELECT show FROM {DECISIONS} WHERE target=? AND queue_id=?",
            (key, queue_key)).fetchone()
        if decided is not None:
            return bool(decided[0])
    row = conn.execute(
        f"SELECT seen,last_queue_id,last_show FROM {TABLE} WHERE target=?", (key,)).fetchone()
    seen = (int(row[0]) if row else 0) + 1
    # 1st, (N+1)th, (2N+1)th … carry the row: a brand-new channel shows it on its
    # first post instead of waiting ten posts for a button nobody has seen.
    show = ((seen - 1) % every) == 0
    stamp = time.time() if now is None else now
    conn.execute(
        f"INSERT INTO {TABLE} (target,seen,last_queue_id,last_show,updated_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(target) DO UPDATE SET seen=excluded.seen,"
        " last_queue_id=excluded.last_queue_id, last_show=excluded.last_show,"
        " updated_at=excluded.updated_at",
        (key, seen, queue_key, 1 if show else 0, stamp))
    if queue_key:
        conn.execute(
            f"INSERT OR REPLACE INTO {DECISIONS} (target,queue_id,show,decided_at)"
            " VALUES (?,?,?,?)", (key, queue_key, 1 if show else 0, stamp))
        # Bounded: keep the newest DECISION_ROWS per channel.
        conn.execute(
            f"DELETE FROM {DECISIONS} WHERE target=? AND queue_id NOT IN ("
            " SELECT queue_id FROM " + DECISIONS + " WHERE target=?"
            " ORDER BY queue_id DESC LIMIT ?)", (key, key, DECISION_ROWS))
    conn.commit()
    return show


def preview(every=1, count: int = 12) -> list[bool]:
    """What the next `count` posts would do - the dashboard shows this."""
    every = clamp(every)
    if every <= 1:
        return [True] * max(1, count)
    return [((index - 1) % every) == 0 for index in range(1, max(1, count) + 1)]


def resume(conn: sqlite3.Connection, target: str, seen: int = 0) -> dict:
    """Set a channel's counter (operator: 'start the count here')."""
    ensure_schema(conn)
    seen = max(0, int(seen or 0))
    conn.execute(
        f"INSERT INTO {TABLE} (target,seen,last_queue_id,last_show,updated_at)"
        " VALUES (?,?,0,0,0) ON CONFLICT(target) DO UPDATE SET seen=excluded.seen",
        (str(target), seen))
    conn.commit()
    return state(conn, target)


def forget(conn: sqlite3.Connection, target: str) -> None:
    ensure_schema(conn)
    conn.execute(f"DELETE FROM {TABLE} WHERE target=?", (str(target),))
    conn.execute(f"DELETE FROM {DECISIONS} WHERE target=?", (str(target),))
    conn.commit()

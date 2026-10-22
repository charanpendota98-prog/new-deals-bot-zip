"""Deal forensics: one queue id, every link and every rewrite, with proof.

The user's standard is "commission asalu miss avvakunda" and "ilaga invalid url
link vasthunnai mana channels lo". A trace tells you which channel received the
post; it does not tell you whether the link that went out pays *us*. This suite
pins the dossier down against a synthetic database, with no Telegram and no
network:

1. a clean post: every published link proven ours (our Amazon tag / our
   conversion cache / an owned-link proof) -> no findings, verdict says so;
2. the expensive failure: the source's own link published unchanged -> an
   `error` finding that names it, and the verdict does not claim it is ours;
3. a forbidden case: a foreign Amazon tag -> reported by tag, never assumed ours;
4. withheld and never-attempted posts get the right verdict instead of "fine";
5. numbers lost between intake and post are an error, not a warning;
6. the diff, the source-link table and the timeline describe what really
   happened - and an old/partial schema answers `unknown` instead of throwing.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("EARNKARO_API_KEY", "k")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))
import main_bot_new as bot  # noqa: E402
from dashboard import forensics  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {extra[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


OUR_TAG = "mama086-21"
OUR_AMAZON = "https://www.amazon.in/dp/B0HNPB6QBS?tag=mama086-21"
OUR_CACHE = "https://ekaro.in/enkr20261022abcd"
SOURCE_LINK = "https://www.amazon.in/dp/B0HNPB6QBS"
OUR_PROOF = "https://bitli.in/ourproved1"
OUR_HYPD = "https://hypd.store/93944/afflink/ours"


def build_db(path: Path) -> "bot.Store":
    store = bot.Store(path)
    now = time.time()
    store.conn.execute(
        "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
        "chat_key,rendered_text,source_text,targets_json,quality,premium_score) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, -1001, 11, "fashionfinds", "done", 1, "", now - 900, "-1001",
         f"Korean Mink Blanket \u20b91,999 \u2192 \u20b9100\n{OUR_AMAZON}\n{OUR_CACHE}\n{OUR_PROOF}",
         f"Korean Mink Blanket \u20b91,999 \u2192 \u20b9100\n{SOURCE_LINK}", '["LootZoneIndia11","PowerLoots1"]', 80, 3))
    store.conn.execute(
        "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
        "chat_key,rendered_text,source_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (2, -1002, 12, "lootzoneindia11", "done", 1, "", now - 600, "-1002",
         f"Safari trolley \u20b9880\n{SOURCE_LINK}",
         f"Safari trolley \u20b9880\n{SOURCE_LINK}", '["LootZoneIndia11"]'))
    store.conn.execute(
        "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
        "chat_key,rendered_text,source_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (3, -1003, 13, "gadgetalerts", "done", 0, "", now - 300, "-1003",
         "Phone \u20b91,299\nhttps://www.amazon.in/dp/B0HNPB6QBS?tag=someoneelse-21",
         "Phone \u20b91,299\nhttps://www.amazon.in/dp/B0HNPB6QBS", '["PowerLoots1"]'))
    store.conn.execute(
        "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
        "chat_key,rendered_text,source_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (4, -1004, 14, "gadgetalerts", "done", 1, "", now - 120, "-1004",
         "Earbuds \u20b9499\n" + OUR_AMAZON, "Earbuds \u20b9499\n" + SOURCE_LINK, '["LootZoneIndia11"]'))
    store.conn.execute(
        "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
        "chat_key,rendered_text,source_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (5, -1005, 15, "gadgetalerts", "done", 0, "", now - 60, "-1005",
         "Watch \u20b9129\n" + OUR_AMAZON, "Watch \u20b91,299\n" + SOURCE_LINK, '["LootZoneIndia11"]'))

    store.conn.execute("INSERT INTO deliveries VALUES(1,'LootZoneIndia11','sent',1,1,'')")
    store.conn.execute("INSERT INTO deliveries VALUES(1,'PowerLoots1','sent',1,1,'')")
    store.conn.execute("INSERT INTO deliveries VALUES(2,'LootZoneIndia11','skipped',1,0,"
                       "'above the price cap for this shelf')")
    store.conn.execute("INSERT INTO deliveries VALUES(3,'PowerLoots1','sent',1,1,'')")
    # queue 4 and 5: no delivery rows at all - the two honest holes
    store.conn.execute("INSERT INTO send_attempts VALUES(1,'LootZoneIndia11',0,'h','sent',?)",
                       (now - 890,))
    store.conn.execute("INSERT INTO send_attempts VALUES(1,'PowerLoots1',0,'h','sent',?)",
                       (now - 880,))
    store.conn.execute("INSERT INTO send_attempts VALUES(3,'PowerLoots1',0,'h','sent',?)",
                       (now - 290,))

    store.conn.execute("INSERT INTO link_cache VALUES(?,?,?,?,?)",
                       (SOURCE_LINK, OUR_CACHE, "https://www.amazon.in/dp/B0HNPB6QBS", 
                        "PID:amazon:ours", now - 950))
    store.conn.execute("INSERT INTO owned_link_proofs VALUES(?,?,?,?,?)",
                       (OUR_PROOF, "https://www.flipkart.com/x/p/itmZ", "earnkaro_api",
                        "5478322", now - 940))
    store.conn.execute("INSERT INTO hypd_links VALUES(?,?,?,?,?,?)",
                       ("93944:ours", "93944", OUR_HYPD, "product", "key", 1))
    store.conn.execute(
        "INSERT INTO blocked_content(stage,source,msg_id,queue_id,target,category,label,"
        "matched,reason,policy_version,excerpt,created_at) "
        "VALUES('presend','gadgetalerts',12,2,'LootZoneIndia11','price_cap','Above price cap',"
        "'880','This shelf only takes deals under the cap.','2026-10-19.3','****',?)",
        (time.time() - 590,))
    store.conn.commit()
    return store


def main() -> None:
    print("== one clean post: every link proven ours ==")
    with tempfile.TemporaryDirectory() as td:
        store = build_db(Path(td) / "forensics.sqlite3")
        clean = forensics.dossier(store.conn, 1, amazon_tag=OUR_TAG)
        links = clean["links"]
        check("every published link is proven ours",
              links["unproven"] == 0 and links["proven"] == 3, json.dumps(links)[:400])
        kinds = {row["kind"] for row in links["published"]}
        check("the proof is named per link (tag, cache, owned proof)",
              kinds == {"amazon_tag", "conversion_cache", "owned_link_proof"}, str(kinds))
        check("the Amazon link is proven by OUR tag, not by the host",
              next(row for row in links["published"] if "amazon.in" in row["url"])["proof"]
              == f"our tag {OUR_TAG}")
        check("the conversion cache shows what the link resolves to",
              next(row for row in links["published"] if row["url"] == OUR_CACHE)["resolved"]
              == "https://www.amazon.in/dp/B0HNPB6QBS")
        check("the source's own link is reported as rewritten, not republished",
              clean["links"]["source"][0]["published_as"] == OUR_CACHE
              and clean["links"]["source"][0]["rewritten"]
              and not clean["links"]["source"][0]["kept"],
              json.dumps(clean["links"]["source"])[:300])
        check("the verdict says the links are ours",
              "every link proven ours" in clean["verdict"], clean["verdict"])
        check("a clean post has no error finding",
              not [f for f in clean["findings"] if f["severity"] == "error"],
              str(clean["findings"]))
        check("both planned targets received it",
              sorted(clean["states"]["received"]) == ["LootZoneIndia11", "PowerLoots1"],
              str(clean["states"]))
        check("first-send latency is measured, not guessed",
              clean["latency_seconds"] is not None and 0 < clean["latency_seconds"] < 60,
              str(clean["latency_seconds"]))
        check("the timeline is ordered",
              [e["at"] for e in clean["timeline"]] == sorted(e["at"] for e in clean["timeline"]))

        print("\n== the expensive failure: the source's own link, unchanged ==")
        copied = forensics.dossier(store.conn, 2, amazon_tag=OUR_TAG)
        finding = next((f for f in copied["findings"] if f["code"] == "republished_source_link"), None)
        check("a republished source link is an error with a concrete fix",
              finding and finding["severity"] == "error"
              and "pays the source" in finding["fix"], str(copied["findings"])[:400])
        check("the link is labelled unproven AND from_source",
              copied["links"]["unproven"] == 1
              and any(row["from_source"] for row in copied["links"]["published"]),
              json.dumps(copied["links"]["published"])[:300])
        check("the verdict never claims that post is ours",
              "NOT proven ours" in copied["verdict"], copied["verdict"])
        check("a withheld delivery is the state and carries its reason",
              copied["states"]["state"] == "withheld"
              and copied["states"]["withheld"] == ["LootZoneIndia11"]
              and any("price cap" in d["error"] for d in copied["deliveries"]),
              str(copied["states"]))
        check("the refusal row is on the timeline with its stage",
              any(e["kind"] == "refused" and "presend" in e["detail"] for e in copied["timeline"]),
              json.dumps(copied["timeline"])[:300])

        print("\n== a foreign tag is named, never assumed ours ==")
        foreign = forensics.dossier(store.conn, 3, amazon_tag=OUR_TAG)
        proof = next(row for row in foreign["links"]["published"] if "amazon.in" in row["url"])
        check("a foreign tag is reported as foreign",
              proof["kind"] == "foreign_tag" and "someoneelse-21" in proof["risk"], str(proof))
        check("that is an error finding, not a quiet warning",
              any(f["code"] == "unproven_link" and f["severity"] == "error"
                  for f in foreign["findings"]), str(foreign["findings"])[:300])
        check("without a configured tag a tag cannot be proven ours",
              forensics.dossier(store.conn, 3)["links"]["published"][0]["kind"] == "unverified_tag",
              forensics.dossier(store.conn, 3)["links"]["published"][0]["kind"])

        print("\n== posts that never reached the ledger are holes, not 'fine' ==")
        hole = forensics.dossier(store.conn, 4, amazon_tag=OUR_TAG)
        check("a done post with no delivery row is 'not_attempted'",
              hole["states"]["state"] == "not_attempted", str(hole["states"]))
        check("it is an error with a concrete fix",
              any(f["code"] == "no_attempt_recorded" and f["severity"] == "error"
                  and f["fix"] for f in hole["findings"]), str(hole["findings"]))
        check("the verdict does not pretend it was sent",
              "never recorded as attempted" in hole["verdict"], hole["verdict"])

        print("\n== a number that vanished is an error ==")
        lost = forensics.dossier(store.conn, 5, amazon_tag=OUR_TAG)
        check("₹1,299 -> ₹129 is caught",
              any(f["code"] == "numbers_lost" and f["severity"] == "error"
                  for f in lost["findings"]), str(lost["findings"]))
        check("the missing token is named",
              "1299" in " ".join(lost["numbers"]["lost"]), str(lost["numbers"]))
        check("the line diff shows what changed",
              lost["diff"]["added_count"] >= 1 and lost["diff"]["removed_count"] >= 1,
              json.dumps(lost["diff"])[:300])

        print("\n== the newest hole is what 'newest refused' opens ==")
        store.conn.execute(
            "INSERT INTO queue(id,chat_id,msg_id,source,status,attempts,last_error,created_at,"
            "chat_key,rendered_text,source_text,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (6, -1006, 16, "gadgetalerts", "done", 0, "", time.time(), "-1006",
             "Charger \u20b9199", "Charger \u20b9199", '[]'))
        store.conn.commit()
        newest = forensics.newest_refused(store.conn)
        check("the newest refused/unattempted post is found", newest is not None, str(newest))
        check("and it is the newest one, not the oldest", newest >= 4, str(newest))

        print("\n== hostile input is data, and an old schema still answers ==")
        weird = forensics.dossier(store.conn, 1, amazon_tag="")
        check("no tag configured -> the tag links are unverified, not 'ours'",
              any(row["kind"] == "unverified_tag" for row in weird["links"]["published"]),
              str(weird["links"]["published"])[:300])
        try:
            forensics.dossier(store.conn, 9999)
        except LookupError as exc:
            check("an unknown queue id is a LookupError the API turns into 404",
                  "9999" in str(exc), str(exc))
        else:
            check("an unknown queue id is a LookupError the API turns into 404", False)

        old_path = Path(td) / "old.sqlite3"
        import sqlite3
        old = sqlite3.connect(old_path)
        old.execute("CREATE TABLE queue(id INTEGER PRIMARY KEY, chat_id INTEGER, msg_id INTEGER,"
                    " source TEXT, status TEXT, created_at REAL)")
        old.execute("INSERT INTO queue VALUES(1,-1,1,'deals','done',?)", (time.time(),))
        old.commit()
        minimal = forensics.dossier(old, 1, amazon_tag=OUR_TAG)
        check("an old database without deliveries/links still renders a dossier",
              minimal["states"]["state"] == "not_attempted"
              and minimal["links"]["published"] == []
              and minimal["timeline"], json.dumps(minimal)[:300])
        old.close()

        empty_path = Path(td) / "empty.sqlite3"
        empty = sqlite3.connect(empty_path)
        empty.execute("CREATE TABLE unrelated(x INTEGER)")
        empty.commit()
        try:
            forensics.dossier(empty, 1)
        except KeyError as exc:
            check("a database with no queue table is a KeyError the API turns into 503",
                  "no queue table" in str(exc), str(exc))
        else:
            check("a database with no queue table is a KeyError the API turns into 503", False)
        empty.close()

        print("\n== HTTP: the dossier endpoint ==")
        asyncio.run(api_checks(Path(td) / "forensics.sqlite3"))
        store.conn.close()


async def api_checks(db: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer
    from dashboard import app as dash
    os.environ["BOT_DB_PATH"] = str(db)
    os.environ["DASHBOARD_TOKEN"] = "dossier-test"
    os.environ["DASHBOARD_RESTART_CMD"] = ""
    os.environ["AMAZON_TAG"] = OUR_TAG
    headers = {"X-Dashboard-Token": "dossier-test"}
    async with TestClient(TestServer(dash.create_app())) as client:
        check("the dossier needs the token",
              (await client.get("/api/forensics?queue_id=1")).status == 401)
        response = await client.get("/api/forensics?queue_id=1", headers=headers)
        body = await response.json()
        check("a real queue id answers 200 with the full dossier",
              response.status == 200 and body["post"]["queue_id"] == 1
              and body["links"]["proven"] == 3, str(body)[:300])
        response = await client.get("/api/forensics?queue_id=9999", headers=headers)
        check("an unknown queue id is 404, not a 500", response.status == 404)
        response = await client.get("/api/forensics?queue_id=abc", headers=headers)
        check("a non-numeric id is 400", response.status == 400)
        response = await client.get("/api/forensics?queue_id=newest", headers=headers)
        check("queue_id=newest opens the newest hole",
              response.status == 200 and (await response.json())["post"]["queue_id"] >= 4)


if __name__ == "__main__":
    main()
    if FAILS:
        print(f"\nDEAL FORENSICS TESTS: FAILURES: {', '.join(FAILS)}")
        sys.exit(1)
    print("\nDEAL FORENSICS TESTS: all checks PASS")

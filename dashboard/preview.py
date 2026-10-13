"""Bounded, side-effect-free category/source simulation over saved dashboard config.

A candidate is NOT send approval. No merchant fetch, bot import, or writes here.
"""
from bestgaa.category_classifier import classify_category, load_rules

MAX_ITEMS = 20
MAX_TOTAL_CHARS = 60000
NOTICE = ("Saved dashboard configuration only; candidate does NOT mean sent or approved. "
          "Bot restart, source access, product identity, affiliate attribution, cooldown, "
          "service-promotion and final delivery checks still apply. No URLs are fetched.")


def validate_item(item, rules):
    if not isinstance(item, dict):
        raise ValueError("each item must be an object")
    text = item.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
        raise ValueError("text must contain 1–12000 characters")
    links = item.get("links", [])
    if (not isinstance(links, list) or len(links) > 20
            or any(not isinstance(url, str) or len(url) > 2000 for url in links)):
        raise ValueError("links must be at most 20 URL strings (2000 characters each)")
    source = item.get("source", "")
    if not isinstance(source, str) or len(source) > 120:
        raise ValueError("source must be a string of at most 120 characters")
    if "expected_category" in item:
        expected = item["expected_category"]
        keys = {c["key"] for c in rules.get("categories", [])}
        if expected is not None and (not isinstance(expected, str) or expected not in keys):
            raise ValueError("expected_category must be a known category key or null (withheld)")
    return len(text) + sum(map(len, links))


def route_checks(category, source, channels, snapshot):
    """Conservative saved-config eligibility, never inferred from a channel name."""
    results = []
    for channel in channels:
        state, reason = "candidate", "category_and_source_match"
        wanted = channel.get("categories", [])
        sources = channel.get("sources", [])
        if not channel.get("enabled", True):
            state, reason = "blocked", "channel_disabled"
        elif wanted and not category:
            state, reason = "blocked", "category_withheld"
        elif wanted and category not in wanted:
            state, reason = "blocked", "category_mismatch"
        elif not source:
            state, reason = "undetermined", "source_required"
        elif sources == ["*"]:
            # Old snapshots lack this list. Never guess that a Tricks source is
            # part of wildcard intake: the live merge intentionally excludes it.
            if not isinstance(snapshot.get("main_sources"), list):
                state, reason = "undetermined", "main_source_snapshot_missing"
            elif source not in snapshot["main_sources"]:
                state, reason = "blocked", "not_a_main_source"
        elif source not in sources:
            state, reason = "blocked", "source_not_selected"
        if not wanted and state == "candidate":
            reason = "unfiltered_channel_source_match"
        results.append({"channel": channel["name"], "state": state, "reason": reason})
    return results


def preview_batch(items, config, snapshot, rules=None):
    rules = load_rules() if rules is None else rules
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError(f"items must contain 1–{MAX_ITEMS} deals")
    # Validate the entire batch first; a bad last item cannot trigger partial work.
    size = sum(validate_item(item, rules) for item in items)
    if size > MAX_TOTAL_CHARS:
        raise ValueError(f"batch text and links exceed {MAX_TOTAL_CHARS} characters")
    results = []
    summary = {"total": len(items), "matched": 0, "withheld": 0,
               "evaluated": 0, "expectation_matches": 0, "expectation_mismatches": 0}
    for index, item in enumerate(items):
        decision = classify_category(item["text"], item.get("links", []), rules)
        summary["matched" if decision["category"] else "withheld"] += 1
        row = {"index": index, "text": item["text"], "source": item.get("source", "").strip(),
               **decision}
        row["channels"] = route_checks(row["category"], row["source"], config["channels"], snapshot)
        if "expected_category" in item:
            row["expected_category"] = item["expected_category"]
            row["expectation_match"] = decision["category"] == item["expected_category"]
            summary["evaluated"] += 1
            summary["expectation_matches" if row["expectation_match"] else "expectation_mismatches"] += 1
        results.append(row)
    return {"scope": "saved_dashboard_config", "notice": NOTICE,
            "rules_version": rules.get("version", 0), "results": results, "summary": summary}

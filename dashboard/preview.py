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


def inspect_product_link(body, config, snapshot):
    """Classify user-supplied URL text, never fetch a page or guess an opaque ID.

    A pasted resolved URL is user-supplied context, not independently verified
    redirect provenance. This tool MUST NOT approve links for live delivery.
    """
    from urllib.parse import urlsplit
    import re

    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")

    def product_url(value, field, optional=False):
        if optional and value == "":
            return ""
        if not isinstance(value, str) or not 1 <= len(value) <= 2000:
            raise ValueError(f"{field} must be a web URL of 1–2000 characters")
        value = value.strip()
        try:
            parsed = urlsplit(value)
            if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                    or "." not in parsed.hostname or parsed.username or parsed.password
                    or any(c.isspace() or ord(c) < 32 for c in value)):
                raise ValueError()
            hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
            if len(hostname) > 253 or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                                          for label in hostname.split(".")):
                raise ValueError()
            # Access .port to reject malformed port syntax as well.
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise ValueError(f"{field} must be an http(s) URL with a valid host and no credentials") from None
        return value

    url = product_url(body.get("url"), "url")
    resolved = product_url(body.get("resolved_url", ""), "resolved_url", optional=True)
    title = body.get("title", "")
    if not isinstance(title, str) or len(title) > 2000:
        raise ValueError("title must be a string of at most 2000 characters")
    item = {"text": title.strip() + "\n" + url, "links": [resolved] if resolved else [],
            "source": body.get("source", "")}
    result = preview_batch([item], config, snapshot)
    decision = result["results"][0]
    if decision["category"]:
        verdict = "suggested"
        next_step = "Review this category, then use it to configure a shelf. This does not verify the product or affiliate link."
    elif decision["reason"] == "mixed_or_conflicting":
        verdict = "withheld"
        next_step = "The supplied title/link evidence conflicts or describes multiple categories. Check the original source; do not guess."
    else:
        verdict = "needs_details"
        next_step = "This link does not reveal enough product details. Paste the original source title or the resolved merchant URL. Short links and opaque IDs are not guessed."
    return {**decision, "verdict": verdict, "next_step": next_step,
            "input_url": url, "fetched": False, "notice": result["notice"],
            "context_note": "URL/title text only. No merchant HTML, redirect resolution or ownership verification.",
            "matching_shelves": [c["name"] for c in config["channels"]
                                 if c.get("enabled", True) and decision["category"]
                                 and decision["category"] in c.get("categories", [])]}

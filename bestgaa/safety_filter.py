"""Content safety for PUBLIC deal channels — one policy, two languages.

WHY THIS EXISTS (USER REQUEST 2026-10-19): "random vasthunnai ... public, ladies
andaru untaru, so avi vaddu". A deal source is somebody else's channel: mixed in
with the offers it also publishes adult content, abuse, betting spam and
"earn money / DM me" junk. Our channels are public and read by everyone, so a
source's junk must never become OUR post.

The policy lives in ONE file, `safety_rules.json`, and is executed by this
module (the bot, Telegram) and by `tg-wa-bridge/safety_filter.js` (the WhatsApp
bridge). Both return the SAME verdict for the same text; `test_safety_filter.py`
proves that on a shared corpus, so the two surfaces can never drift.

Design rules
------------
* Match the NORMALISED text, not the raw one: `S.E.X`, `s e x`, `5ex`, `$3X`
  and `ＳＥＸ` all fold to the same word before any pattern sees them. The
  dodging never survives.
* Never trust a keyword inside a product name by accident: patterns are
  word-bounded, and an allow-list (`unisex`, `XXXL`, `cocktail`, `analog`,
  `massage gun`, ...) is blanked out first, so a real deal is not refused.
* Two severities: `block` stops the post, `review` only records it. The
  intimate-product category is `block` by default (the operator's rule for a
  mixed public audience) and one env switch turns it back on.
* Fail CLOSED on policy errors, OPEN on infrastructure errors: a broken rules
  file must not silently let junk through (the loader raises), but a missing
  rules file at runtime is reported and the caller decides. The bot treats a
  load error as "filter disabled" and says so in the log — it never crashes a
  send path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent / "safety_rules.json"

# ---- operator switches ------------------------------------------------------
def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# Read at CALL time, not at import time: an operator (or a test) can flip a
# switch on a running process or right before a check, and it takes effect.
def safety_enabled() -> bool:
    return not _flag("SAFETY_FILTER_DISABLED", False)


# Intimate products are blocked by default (public, mixed audience). One switch
# restores them for an operator who wants those deals after all.
def allow_intimate() -> bool:
    return _flag("SAFETY_ALLOW_INTIMATE_PRODUCTS", False)


# Suggestive wording is blocked by default too; this records it instead.
def allow_suggestive() -> bool:
    return _flag("SAFETY_ALLOW_SUGGESTIVE", False)


# Treat "review" findings as blocks too (stricter than the default).
def strict_review() -> bool:
    return _flag("SAFETY_STRICT_REVIEW", False)


# Back-compat aliases for anything importing the old constants.
SAFETY_ENABLED = True
ALLOW_INTIMATE = False
ALLOW_SUGGESTIVE = False
STRICT_REVIEW = False

URL_RE = re.compile(r"https?://[^\s<>\[\](){}\"']+", re.I)
# Captions often name a site without a scheme ("1xbet.com", "www.xhamster.com").
# Only blocklisted domains are ever acted on, so this is safe to over-collect.
DOMAIN_RE = re.compile(
    r"(?<![@A-Za-z0-9_.-])(?:[A-Za-z0-9][A-Za-z0-9-]*\.)+[A-Za-z]{2,24}(?![A-Za-z0-9_-])")

# 0->o, 1->i, 3->e, 4->a, 5->s, 7->t, @->a, $->s (the usual chat obfuscation)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s",
                       "7": "t", "@": "a", "$": "s"})

# "s.e.x" / "s e x" / "s-e-x-y" -> "sexy". Only runs of single characters
# separated by punctuation are joined, so "unisex" and "essex" are untouched.
# ASCII-only classes on purpose: JS `\w` is ASCII, and the bridge must agree.
_JOIN_EVASION = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9_][^A-Za-z0-9]+){2,}[A-Za-z0-9_](?![A-Za-z0-9_])")
_ASCII = re.ASCII


def load_rules(path: Path | str | None = None) -> dict:
    """Load and compile the shared policy (raises on a broken file)."""
    rules_path = Path(path or RULES_PATH)
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    if not isinstance(data.get("categories"), list) or not data["categories"]:
        raise ValueError(f"{rules_path}: no categories")
    compiled: list[dict] = []
    for category in data["categories"]:
        compiled.append({
            "key": str(category["key"]),
            "label": str(category.get("label") or category["key"]),
            "severity": str(category.get("severity") or "block"),
            "why": str(category.get("why") or ""),
            "patterns": [re.compile(p, re.ASCII) for p in category.get("patterns") or []],
            "domains": tuple(d.lower().strip(".") for d in category.get("domains") or []),
        })
    return {
        "version": str(data.get("version") or ""),
        "categories": compiled,
        "allow": [re.compile(p, re.ASCII) for p in data.get("allow") or []],
    }


_RULES_CACHE: dict | None = None


def rules() -> dict:
    global _RULES_CACHE
    if _RULES_CACHE is None:
        _RULES_CACHE = load_rules()
    return _RULES_CACHE


def reset_cache() -> None:
    """Forget the compiled policy (tests that swap the rules file use this)."""
    global _RULES_CACHE
    _RULES_CACHE = None


# ---- normalisation (mirrored EXACTLY in safety_filter.js) --------------------
def normalize(text: str) -> list[str]:
    """Every spelling of the text the patterns may match, in a fixed order."""
    raw = unicodedata.normalize("NFKC", text or "").lower()
    folded = raw.translate(_LEET)
    # Join single-letter evasion: "s.e.x" -> "sex", "s e x y" -> "sexy".
    joined = _JOIN_EVASION.sub(
        lambda m: re.sub(r"[^A-Za-z0-9]+", "", m.group(0)), folded)
    variants = [folded]
    if joined != folded:
        variants.append(joined)
    return variants


def _blank(match_text: str) -> str:
    return " " * max(1, len(match_text))


def apply_allow(text: str, allow: list[re.Pattern]) -> str:
    """Blank out the innocent look-alikes before any policy pattern runs."""
    out = text
    for pattern in allow:
        out = pattern.sub(lambda m: _blank(m.group(0)), out)
    return out


def _hosts(urls: list[str]) -> list[str]:
    hosts = []
    for url in urls:
        try:
            body = re.sub(r"^[a-z]+://", "", str(url).strip(), flags=re.I)
            body = body.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
            body = body.rsplit("@", 1)[-1].split(":", 1)[0].strip(".").lower()
            if body:
                hosts.append(body)
        except Exception:  # noqa: BLE001 - a junk URL is not a crash
            continue
    return hosts


def _domain_hit(host: str, domains: tuple[str, ...]) -> str:
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return domain
    return ""


# ---- the verdict -----------------------------------------------------------
def _operator_extra(rules_data: dict) -> tuple[list[re.Pattern], list[str]]:
    extra = [t.strip() for t in os.getenv("SAFETY_EXTRA_BLOCK_TERMS", "").split(",") if t.strip()]
    extra_patterns = []
    for term in extra:
        try:
            extra_patterns.append(re.compile(r"\b" + re.escape(term.lower()) + r"\b", re.ASCII))
        except re.error:
            continue
    allow_extra = [t.strip().lower() for t in os.getenv("SAFETY_ALLOW_TERMS", "").split(",")
                   if t.strip()]
    return extra_patterns, allow_extra


def scan(text: str, urls: list[str] | tuple[str, ...] | None = None) -> dict:
    """The one question: may this text be published on our public channels?

    Returns {blocked, severity, category, label, match, reason, version} with
    severity in {"clean", "review", "block"}. `blocked` is True exactly when a
    block-severity finding exists (or a review finding under
    SAFETY_STRICT_REVIEW). Deterministic and offline.
    """
    verdict = {"blocked": False, "severity": "clean", "category": "", "label": "",
               "matched": "", "reason": "", "version": ""}
    if not safety_enabled():
        verdict["reason"] = "filter disabled (SAFETY_FILTER_DISABLED)"
        return verdict
    data = rules()
    verdict["version"] = data["version"]
    raw_text = text or ""
    if urls is not None:
        links = [str(u) for u in urls]
    else:
        links = URL_RE.findall(raw_text) + DOMAIN_RE.findall(raw_text)
    hosts = _hosts(links)
    variants = [apply_allow(v, data["allow"]) for v in normalize(text or "")]
    extra_patterns, allow_terms = _operator_extra(data)

    def finding(category: dict, match: str, reason: str) -> dict:
        severity = category["severity"]
        if category["key"] == "suggestive" and allow_suggestive():
            severity = "review"
        if severity == "review" and strict_review():
            severity = "block"
        return {"blocked": severity == "block",
                "severity": severity,
                "category": category["key"], "label": category["label"],
                "matched": match[:80], "reason": reason or category["why"],
                "version": data["version"]}

    # 1. An operator's own term (SAFETY_EXTRA_BLOCK_TERMS) is a block, always.
    for pattern in extra_patterns:
        for variant in variants:
            found = pattern.search(variant)
            if found and found.group(0).strip().lower() not in allow_terms:
                return finding({"key": "operator_term", "label": "Operator block term",
                                "severity": "block",
                                "why": "an operator-configured blocked term "
                                       "(SAFETY_EXTRA_BLOCK_TERMS) appears in this post"},
                               found.group(0), "")

    # 2. The policy, in file order: a domain hit first (it is the strongest
    #    evidence), then the wording patterns over every normalised variant.
    for category in data["categories"]:
        if category["key"] == "intimate_product" and allow_intimate():
            continue
        for host in hosts:
            domain = _domain_hit(host, category["domains"])
            if domain:
                return finding(category, host, category["why"])
        for pattern in category["patterns"]:
            for variant in variants:
                found = pattern.search(variant)
                if not found:
                    continue
                term = found.group(0).strip()
                if term.lower() in allow_terms:
                    continue
                return finding(category, term, category["why"])
    return verdict


def is_safe(text: str, urls: list[str] | tuple[str, ...] | None = None) -> bool:
    return not scan(text, urls)["blocked"]


def policy_summary() -> dict:
    """What the dashboard shows: the active policy, without secrets or text."""
    data = rules() if safety_enabled() else {"version": "", "categories": []}
    return {
        "version": data["version"],
        "enabled": safety_enabled(),
        "strict_review": strict_review(),
        "allow_suggestive": allow_suggestive(),
        "allow_intimate": allow_intimate(),
        "categories": [{"key": c["key"], "label": c["label"], "severity": c["severity"],
                        "why": c["why"], "patterns": len(c["patterns"]),
                        "domains": len(c["domains"])}
                       for c in data["categories"]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check text/links against the public-channel safety policy")
    parser.add_argument("--check", metavar="TEXT", action="append", default=[],
                        help="text to judge (repeatable)")
    parser.add_argument("--url", action="append", default=[], help="link to judge")
    parser.add_argument("--policy", action="store_true", help="print the active policy")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.policy:
        print(json.dumps(policy_summary(), indent=2, ensure_ascii=False))
        return 0
    if not args.check:
        parser.print_help()
        return 2
    worst = 0
    for text in args.check:
        verdict = scan(text, urls=args.url or None)
        if args.json:
            print(json.dumps(verdict, ensure_ascii=False))
        else:
            print(f"{verdict['severity'].upper():7s} {verdict['category'] or '-':18s} "
                  f"{text[:80]!r}")
            if verdict["reason"]:
                print(f"        reason: {verdict['reason']}")
        worst = max(worst, 2 if verdict["blocked"] else 0)
    return 1 if worst else 0


if __name__ == "__main__":
    raise SystemExit(main())

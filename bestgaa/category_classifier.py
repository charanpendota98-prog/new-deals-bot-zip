"""Offline, fail-closed shelf classifier. No network, bot startup or credentials.

Scores are evidence weights, NOT probability estimates. Ambiguous/mixed evidence
is withheld from shelves; product identity and affiliate checks still run later.
"""
import html
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

URL_RE = re.compile(r"https?://[^\s<>\[\](){}\"']+", re.I)
OPAQUE_HOSTS = frozenset({"bit.ly", "bitli.in", "bitl.in", "bitly.com", "amzn.to",
    "amzn.in", "a.co", "ekaro.in", "clnk.in", "t.me", "telegram.me", "tinyurl.com",
    "cutt.ly", "fkrt.co", "fkrt.cc", "hypd.store"})

def load_rules():
    try:
        return json.loads(Path(__file__).with_name("category_rules.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"categories": []}

def _normalize_category_phrase(value: str) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
    return re.sub(r"[\W_]+", " ", value, flags=re.UNICODE).strip()


CATEGORY_QUERY_KEYS = frozenset({
    "k", "q", "keyword", "keywords", "search", "searchterm", "query",
    "title", "name", "product", "productname",
})
CATEGORY_URL_STOP_SEGMENTS = frozenset({
    "", "dp", "gp", "aw", "s", "b", "p", "product", "products", "item",
    "items", "detail", "details", "buy", "shop", "offers", "offer", "ref",
})


def category_words_from_url(url: str) -> str:
    """Extract readable product/search words from a URL, never opaque IDs/tokens."""
    try:
        value = html.unescape(str(url or "").strip())
        if "\\" in value or re.search(r"%(?![a-fA-F0-9]{2})", value):
            return ""
        parsed = urlparse(value)
        if not parsed.hostname or parsed.username or parsed.password or "%" in parsed.netloc:
            return ""
        # Invalid encodings and dot-segment normalization differ between URL
        # parsers. Such URLs provide no trustworthy category evidence.
        path = unquote(parsed.path, errors="strict")
        if (any(part in {".", ".."} for part in path.split("/"))
                or any(c.isspace() and c != " " for c in path)):
            return ""
        parsed.port  # reject malformed ports rather than accepting their slugs
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        if any((parsed.hostname or "").lower() == host or
               (parsed.hostname or "").lower().endswith("." + host) for host in OPAQUE_HOSTS):
            return ""
        words: list[str] = []
        previous = ""
        for raw_segment in path.split("/"):
            is_identifier = previous.lower() in {"p", "dp", "product", "item"}
            previous = raw_segment
            if is_identifier:
                continue
            segment = raw_segment.replace("-", " ").replace("_", " ").strip()
            normalized = _normalize_category_phrase(segment)
            if not normalized or normalized in CATEGORY_URL_STOP_SEGMENTS:
                continue
            if re.fullmatch(r"\d{4,}", segment):
                continue
            if re.fullmatch(r"(?i)(?:itm|pid|sku|asin|product)[-_]?[a-z0-9]{5,}", raw_segment):
                continue
            if re.fullmatch(r"(?=[A-Z0-9]{8,}$)(?=.*[A-Z])(?=.*[0-9])[A-Z0-9]+", raw_segment):
                continue
            words.append(normalized)
        for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
            if key.lower() in CATEGORY_QUERY_KEYS:
                words.extend(_normalize_category_phrase(part) for value in values
                             for part in html.unescape(value).splitlines() if part)
        return "\n".join(word for word in words if word)
    except Exception:
        return ""



def classify_category(text, link_context=(), rules=None):
    """Return category, reason, per-category scores and the surviving evidence.

    Longest phrases suppress ONLY overlapping occurrences, never an independent
    product elsewhere. Each alias scores once so repetition cannot manufacture
    confidence. Newlines and URL boundaries cannot form synthetic product names.
    """
    rules = load_rules() if rules is None else rules
    result = {"category": None, "reason": "no_evidence", "scores": {}, "evidence": [],
              "rules_version": rules.get("version", 0)}
    if not isinstance(text, str) or not text.strip():
        return result
    if isinstance(link_context, str):
        link_context = [link_context]
    urls = list(URL_RE.findall(text))
    for value in link_context or ():
        if isinstance(value, str):
            urls.extend(URL_RE.findall(value))
    segments = [("text", part) for line in URL_RE.sub("\n", text).splitlines()
                for part in html.unescape(line).splitlines()]
    # Never invent a product phrase across path components or query fields.
    segments += [("link", part) for url in dict.fromkeys(urls)
                 for part in category_words_from_url(url).splitlines()]
    seen = set()
    for source, segment in segments:
        normalized = " " + _normalize_category_phrase(segment) + " "
        matches = []
        for definition in rules.get("categories", []):
            for alias, weight in definition.get("aliases", []):
                alias = _normalize_category_phrase(alias)
                if not alias:
                    continue
                for match in re.finditer(r"(?<= )" + re.escape(alias) + r"(?= )", normalized):
                    matches.append((match.start(), match.end(), definition["key"], alias, weight))
        for start, end, key, alias, weight in matches:
            if any(a <= start and b >= end and (a < start or b > end) and w >= weight
                   for a, b, _, _, w in matches):
                continue
            if (key, alias) in seen:
                continue
            seen.add((key, alias))
            result["scores"][key] = result["scores"].get(key, 0) + weight
            result["evidence"].append({"category": key, "phrase": alias, "weight": weight, "source": source})
    scores = sorted(result["scores"].items(), key=lambda kv: (-kv[1], kv[0]))
    if not scores:
        return result
    # Even weak evidence for a second category is unsafe on a strict shelf.
    if len(scores) > 1:
        result["reason"] = "mixed_or_conflicting"
        return result
    key, score = scores[0]
    # Broad category words alone (even many synonyms) are not product evidence.
    strong = any(e["weight"] >= rules.get("min_score", 3) for e in result["evidence"])
    if not strong or score < rules.get("min_score", 3):
        result["reason"] = "insufficient_product_evidence"
        return result
    result.update(category=key, reason="matched")
    return result

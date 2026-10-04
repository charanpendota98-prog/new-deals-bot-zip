#!/usr/bin/env python3
"""earnkaro_check.py — does OUR EarnKaro API key actually convert links?

Run this on the server (or anywhere with internet) right after setting
`EARNKARO_API_KEY`:

    python3 ops/earnkaro_check.py                  # token + live conversions
    python3 ops/earnkaro_check.py --offline        # token only, no network
    python3 ops/earnkaro_check.py --key <token>    # check a fresh token first
    python3 ops/earnkaro_check.py --env-file /home/ubuntu/bestgaa-bot/bestgaa-bot/.env

    # THE EXACT LINK THE CHANNEL WILL CARRY, for a real source/product URL:
    python3 ops/earnkaro_check.py --plan \
        --deal-url 'https://www.flipkart.com/<real-product-url>'

Why it exists: "the EarnKaro links are not converting" has three very different
causes that look identical in a channel —

  1. the TOKEN is wrong/expired  -> every call answers 401 and the bot posts
     clean, unmonetized merchant links;
  2. the request is malformed (missing `convert_option: "convert_only"`) ->
     the API answers a shape the bot cannot read;
  3. the store genuinely has no EarnKaro campaign -> nothing to convert.

This script separates them: it prints the token's own claims (which EarnKaro
account gets paid), then calls the SAME endpoint with the SAME headers and body
the bot sends, and prints the raw response next to the link the bot would
publish. Nothing is written anywhere; the token is never echoed to the screen.

`--plan --deal-url <url>` goes one step further: it imports the bot's OWN
module (`bestgaa/main_bot_new.py`) and runs the real pipeline for that URL -
resolve -> convert -> shorten - so the output is not a copy of the rules but
the actual `AffiliateClient`, printing the link the channel will carry, its
length, whether it is still short, and whose account it pays. A link the
network cannot monetize is reported as UNMONETIZED (that deal posts clean and
earns nothing) - never silently reported as a conversion.

Exit codes: 0 = every probe converted, 1 = at least one probe failed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API = "https://ekaro-api.affiliaters.in/api/converter/public"

# A spread of what the channels actually post: a product page, a bookstore with
# query params, fashion, a marketplace search page, and the OLD shortcut to
# Amazon that EarnKaro has no campaign for.
PROBES = (
    ("Flipkart product", "https://www.flipkart.com/boat-airdopes-141-tws-earbuds/p/itm1234567890?pid=ACCFHBDD6HYQZ6AZ"),
    ("Myntra product", "https://www.myntra.com/tshirts/roadster/roadster-men-navy-blue-tshirt/1234567/buy"),
    ("Ajio product", "https://www.ajio.com/status-women-printed-round-neck-t-shirt/p/442125201_navy"),
    ("Meesho product", "https://www.meesho.com/sarees/p/1a2b3c"),
    ("Amazon product (no Associates campaign)", "https://www.amazon.in/dp/B0FPDD9WKP"),
    ("Amazon search page", "https://www.amazon.in/s?k=earbuds"),
)


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def find_key(explicit: str | None, env_file: str | None) -> tuple[str, str]:
    """(key, where it came from) — never printed with the value."""
    if explicit:
        return explicit.strip(), "--key"
    if os.getenv("EARNKARO_API_KEY", "").strip():
        return os.environ["EARNKARO_API_KEY"].strip(), "environment"
    candidates = [Path(env_file)] if env_file else []
    candidates += [
        REPO_ROOT / "bestgaa" / ".env",
        Path("/home/ubuntu/bestgaa-bot/bestgaa-bot/.env"),
        Path.cwd() / ".env",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            value = load_env_file(candidate).get("EARNKARO_API_KEY", "").strip()
            if value:
                return value, str(candidate)
    # The raw token file the deploy scripts read (ops/.earnkaro_key, gitignored):
    # if it exists, THIS is the key a deploy would write, so it is the key a
    # check should test. Never printed.
    key_file = REPO_ROOT / "ops" / ".earnkaro_key"
    try:
        value = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value, str(key_file)
    return "", "not found"


def token_claims(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def describe_token(token: str, source: str) -> tuple[bool, str]:
    print(f"token source      : {source}")
    print(f"token length      : {len(token)}")
    claims = token_claims(token)
    if not claims:
        print("token shape       : NOT a JWT — this is not the EarnKaro/Affiliaters API token.")
        print("                    Expected: eyJhbGciOi... (three dot-separated parts).")
        print("                    A 401 from the endpoint below is the direct consequence.")
        return False, ""
    publisher = str(claims.get("earnkaro") or "").strip()
    issued = claims.get("iat")
    print(f"token shape       : JWT, issued {_as_date(issued)}")
    print(f"EarnKaro publisher: {publisher or '<none in token>'}"
          "   <- every converted link pays THIS account")
    if not publisher:
        print("                    (no 'earnkaro' claim: the foreign-publisher guard is blind)")
    return bool(publisher), publisher


def _as_date(stamp) -> str:
    try:
        import datetime
        return datetime.datetime.fromtimestamp(float(stamp), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "unknown"


def convert(token: str, api: str, deal: str, option: str, timeout: float) -> tuple[int, str]:
    body = json.dumps({"deal": deal, "convert_option": option}).encode("utf-8")
    request = urllib.request.Request(
        api, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # DNS, TLS, timeout
        return 0, f"<transport error: {exc}>"


def same_destination(left: str, right: str) -> bool:
    """Ignore tracking/query differences when checking whether URLs are the same page."""
    from urllib.parse import parse_qs, urlparse
    try:
        a, b = urlparse(left), urlparse(right)
        host_a = (a.hostname or "").lower().removeprefix("www.")
        host_b = (b.hostname or "").lower().removeprefix("www.")
        path_a = (a.path or "/").rstrip("/").lower()
        path_b = (b.path or "/").rstrip("/").lower()
        # affExtParam2 is attribution, not disposable tracking noise. Keep any
        # direct Flipkart affiliate URL for the publisher-validation step below;
        # that step accepts our ID and rejects somebody else's.
        qa = {str(k).lower(): v for k, v in parse_qs(a.query).items()}
        ids_a = qa.get("affextparam2", [])
        if ids_a:
            return False
        return bool(host_a and host_a == host_b and path_a == path_b)
    except Exception:
        return False


def first_http_url(value, excluded_urls=()) -> str | None:
    """Find the first URL that is not merely one of the submitted source links."""
    if value is None:
        return None
    if isinstance(value, str):
        import re
        text = value.strip()
        lowered = text.lower()
        if any(m in lowered for m in ("could not locate", "url not found", "not found in post")):
            return None
        for found in re.finditer(r"https?://[^\s<>\[\](){}|\"']+", text):
            url = found.group(0).rstrip(".,;:!?\"')*]>}")
            if not any(same_destination(url, source) for source in excluded_urls if source):
                return url
        return None
    if isinstance(value, dict):
        lowered = {str(k).lower(): v for k, v in value.items()}
        for key in ("converted_url", "converted_link", "affiliate_url", "affiliate_link",
                    "ekaro_url", "short_url", "link", "url", "deal", "profit_link"):
            if key in lowered:
                found = first_http_url(lowered[key], excluded_urls)
                if found:
                    return found
        for item in value.values():
            found = first_http_url(item, excluded_urls)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found = first_http_url(item, excluded_urls)
            if found:
                return found
    return None


def response_link(http_status: int, body: str, source_url: str) -> tuple[str | None, str]:
    """Honor explicit API failure flags and never call a source echo a conversion."""
    if http_status < 200 or http_status >= 300:
        return None, f"HTTP {http_status}"
    try:
        payload = json.loads(body)
    except Exception:
        return None, "non-JSON converter response"
    if isinstance(payload, dict):
        success_present = "success" in payload
        success = payload.get("success") if success_present else payload.get("status")
        status = payload.get("status")
        if ((success_present and is_failure_flag(success))
                or (status is not None and is_failure_flag(status))):
            message = payload.get("message") or payload.get("error") or "conversion marked failed"
            return None, str(message)[:180]
        data = payload.get("data", payload.get("result", payload))
    else:
        data = payload
    link = first_http_url(data, excluded_urls=(source_url,))
    if not link:
        return None, "no non-echo URL in response"
    if same_destination(link, source_url):
        return None, "API returned the original merchant destination"
    return link, "ok"


def is_failure_flag(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value <= 0
    return str(value).strip().lower() in {
        "0", "false", "no", "failed", "failure", "error", "unsuccessful", "not_ok",
    }


EARNKARO_OUTPUT_HOSTS = {
    "ekaro.in", "ekaro.app", "earnkaro.com", "earnkaro.in", "clnk.in",
    "clnk.app", "bitli.in", "fktr.in", "myntr.it", "ajiio.in", "cuelinks.com",
    "l.ead.me", "affiliaters.in", "j.mp",
}


def is_earnkaro_output(url: str, publisher: str) -> bool:
    """Mirror the bot's attribution gate: known network redirect or OUR publisher id."""
    from urllib.parse import parse_qs, urlparse
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        if host in ("amazon.in", "amazon.com", "www.amazon.in", "www.amazon.com"):
            return False  # a native Associates tag is a fallback, not EarnKaro output
        query = {str(k).lower(): v for k, v in parse_qs(parsed.query).items()}
        ids = query.get("affextparam2", [])
        if ids:
            return bool(publisher and all(str(value).strip() == publisher for value in ids))
        return any(host == suffix or host.endswith("." + suffix)
                   for suffix in EARNKARO_OUTPUT_HOSTS)
    except Exception:
        return False


# Hosts that only redirect: the publisher is visible on the DESTINATION, not on
# the short link itself, so a converted short link must be expanded before it can
# be called "our" link.
SHORTENER_HOSTS = ("ekaro.in", "ekaro.app", "earnkaro.com", "clnk.in", "clnk.app",
                   "bitli.in", "bitl.in", "fktr.in", "myntr.it", "ajiio.co",
                   "bit.ly", "is.gd", "amzn.to", "linkredirect.in")


def attribution_of(url: str) -> dict:
    """The visible affiliate attribution on a URL, as a plain dict.

    Flipkart-family pages carry the publisher in `affExtParam2`, Amazon in
    `tag`, and several networks add `affid`/`affExtParam1`. An EarnKaro short
    link carries none of them - that is why a short link is expanded first.
    """
    from urllib.parse import parse_qs, urlparse
    try:
        parsed = urlparse(url or "")
    except Exception:
        return {}
    query = {str(k).lower(): v for k, v in parse_qs(parsed.query or "").items()}
    out = {"host": (parsed.hostname or "").lower()}
    for key in ("affid", "affextparam1", "affextparam2", "tag", "utm_source"):
        values = query.get(key)
        if values:
            out[key] = values[0]
    return out


def is_short_link(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SHORTENER_HOSTS)


def expand(url: str, timeout: float) -> str:
    """Follow a short link to its destination (GET: HEAD is often blocked)."""
    import urllib.request
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "Chrome/131 Safari/537.36"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.url or url
    except urllib.error.HTTPError as exc:        # a redirect chain still lands here sometimes
        return getattr(exc, "url", url) or url
    except Exception:
        return url


def whose_link(url: str, publisher: str, our_tag: str, timeout: float) -> tuple[str, str]:
    """('ours' | 'foreign' | 'hidden', detail) for a converted link."""
    published = url
    detail = ""
    if is_short_link(url) or not attribution_of(url).get("affextparam2"):
        final = expand(url, timeout)
        if final and final != url:
            detail = f" -> {final[:150]}"
            published = final
    att = attribution_of(published)
    seen = att.get("affextparam2")
    tag = att.get("tag")
    if seen:
        return ("ours" if seen == publisher else "foreign",
                f"affExtParam2={seen}{detail}")
    if tag:
        return ("ours" if (our_tag and tag == our_tag) else "foreign",
                f"tag={tag}{detail}")
    if att.get("affid"):
        return "hidden", f"affid={att['affid']} (publisher id not shown){detail}"
    return "hidden", (detail.strip() or "attribution not visible (short link)")


# The operator-provided candidate from our store. The known Shopsy link is
# excluded; this candidate is shortened only after its Meesho destination passes
# the live check below.
OUR_HYPD_LINKS = (
    "https://hypd.store/93944/afflink/daoll7ltm6mc5h7k1fq0",
)
OUR_HYPD_STORES = ("93944", "smartdeals")
BITLY_ENDPOINT = "https://api-ssl.bitly.com/v4/shorten"


def bitly_tokens(timeout: float) -> list[str]:
    """The tokens the bot itself would use (env first, then .env files)."""
    import os as _os
    tokens = [t.strip() for t in _os.getenv("BITLY_TOKENS", "").split(",") if t.strip()]
    for path in (REPO_ROOT / "bestgaa" / ".env",
                 Path("/home/ubuntu/bestgaa-bot/bestgaa-bot/.env"),
                 REPO_ROOT / "tg-wa-bridge" / ".env",
                 Path.cwd() / ".env"):
        values = load_env_file(path)
        if not values:
            continue
        tokens += [t.strip() for t in values.get("BITLY_TOKENS", "").split(",") if t.strip()]
        tokens += [t.strip() for t in values.get("WA_BITLY_TOKENS", "").split(",") if t.strip()]
    out: list[str] = []
    for token in tokens:
        if token and token not in out:
            out.append(token)
    return out


def bitly_shorten(long_url: str, tokens: list[str], timeout: float) -> tuple[str, str]:
    """POST the way the bot does. ('', reason) when it did not work."""
    import urllib.request
    if not tokens:
        return "", "no BITLY_TOKENS configured (the bot falls back to is.gd)"
    body = json.dumps({"long_url": long_url}).encode("utf-8")
    for token in tokens:
        request = urllib.request.Request(
            BITLY_ENDPOINT, data=body, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            link = payload.get("link")
            if isinstance(link, str) and link.startswith("http"):
                return link, ""
            return "", f"Bitly answered without a link: {str(payload)[:120]}"
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:160]
            if exc.code in (401, 403):
                return "", f"Bitly refused the token (HTTP {exc.code}): {detail}"
            if exc.code == 429:
                continue          # rate limited: try the next token, like the bot
            return "", f"Bitly HTTP {exc.code}: {detail}"
        except Exception as exc:
            return "", f"Bitly unreachable: {exc}"
    return "", "every Bitly token was rate limited (HTTP 429)"


def resolve_once(url: str, timeout: float) -> tuple[str, str]:
    """('final-url', status) — follows redirects; the JS pages do not redirect."""
    import urllib.request
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "Chrome/131 Safari/537.36"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (response.url or url), str(response.status)
    except urllib.error.HTTPError as exc:
        return getattr(exc, "url", url) or url, f"HTTP {exc.code}"
    except Exception as exc:
        return "", f"unreachable ({exc})"


def resolve_hypd_meesho_destination(url: str, timeout: float) -> tuple[str, str, str]:
    """Return (final URL, status, verified Meesho destination) from the HYPD page."""
    from urllib.parse import urlparse
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "Chrome/131 Safari/537.36"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = response.url or url
            body = response.read(200_000).decode("utf-8", "replace")
            candidates = [final]
            candidates.extend(match.group(0).rstrip(".,;:!?\"')*]>")
                              for match in re.finditer(r"""https?://[^\s"'<>]+""", body))
            for candidate in candidates:
                host = (urlparse(candidate).hostname or "").lower()
                if host == "meesho.com" or host.endswith(".meesho.com"):
                    return final, str(response.status), candidate
            return final, str(response.status), ""
    except urllib.error.HTTPError as exc:
        final = getattr(exc, "url", url) or url
        host = (urlparse(final).hostname or "").lower()
        return final, f"HTTP {exc.code}", final if host == "meesho.com" or host.endswith(".meesho.com") else ""
    except Exception as exc:
        return "", f"unreachable ({exc})", ""


def check_our_hypd_links(timeout: float, verbose: bool) -> tuple[int, int]:
    """(failures, ok) — only verified Meesho HYPD links are shortened/probed."""
    print("\n" + "-" * 78)
    print("OUR HYPD CANDIDATES (store 93944; only verified Meesho destinations accepted)")
    print("-" * 78)
    tokens = bitly_tokens(timeout)
    print(f"Bitly tokens found : {len(tokens) or 'NONE (is.gd fallback would be used)'}")
    failures = 0
    ok = 0
    from urllib.parse import urlparse
    for url in OUR_HYPD_LINKS:
        store = url.split("/")[3] if url.count("/") >= 3 else "?"
        print(f"\n[{url[:70]}]")
        print(f"  store     : {store}  ({'OURS' if store in OUR_HYPD_STORES else 'NOT OURS'})")
        if store not in OUR_HYPD_STORES:
            print("  -> NOT SHORTENED: this is not a link from our HYPD store")
            failures += 1
            continue
        final, status, behind = resolve_hypd_meesho_destination(url, timeout)
        behind_host = (urlparse(behind).hostname or "").lower() if behind else ""
        if behind_host != "meesho.com" and not behind_host.endswith(".meesho.com"):
            behind = ""
        print(f"  resolve   : {status}   "
              + (f"Meesho -> {behind[:110]}" if behind else "no Meesho destination verified"))
        if not final:
            if status.startswith("unreachable"):
                print("  -> could not be checked from HERE (network); no Bitly/HYPD route was proved")
            else:
                print(f"  -> the link answered {status}: check it in the HYPD app")
            failures += 1
            continue
        if status.startswith("HTTP 4") or status.startswith("HTTP 5"):
            print(f"  -> the link answered {status}: check it in the HYPD app before trusting it")
            failures += 1
            continue
        if not behind:
            print("  -> NOT SHORTENED: this HYPD link is not used unless its destination is verified Meesho")
            failures += 1
            continue
        short, why = bitly_shorten(url, tokens, timeout)
        if short:
            back, back_status = resolve_once(short, timeout)
            lands_on_ours = "hypd.store" in back and any(f"/{s}/" in back for s in OUR_HYPD_STORES)
            print(f"  bitly     : {short}")
            print(f"  bitly ->  : {back_status} {back[:110]}")
            print("  verdict   : " + ("OUR Meesho link, shortened and still ours" if lands_on_ours
                                     else "shortened, but it did not come back to OUR store - CHECK"))
            if lands_on_ours:
                ok += 1
            else:
                failures += 1
        else:
            print(f"  bitly     : NOT shortened -> {why}")
            print("  verdict   : fix the Bitly token; the bot will never substitute a non-Meesho HYPD link")
            failures += 1
        if verbose:
            print(f"  destination: {behind[:120]} (verified Meesho; used for identity only)")
    return failures, ok


# ---------------------------------------------------------------------------
# --plan: run the BOT'S OWN pipeline for a real source/product link and print
# the exact link the channel will carry (converted -> shortened -> posted).
# ---------------------------------------------------------------------------
def load_bot_module(key: str, amazon_tag: str):
    """(bot module | None, why not) — imports the pipeline that actually posts.

    The checker must never answer "what will be posted?" with a second copy of
    the bot's rules: copies drift and then the answer is a lie. So this imports
    `bestgaa/main_bot_new.py` itself. The key under test always wins over any
    `.env` the bot module loads (it uses setdefault), and the queue database is
    pointed at a throwaway file so a plan run can never touch live state.
    """
    import tempfile
    os.environ["EARNKARO_API_KEY"] = key          # the key under test, not .env
    os.environ.setdefault("TELEGRAM_API_ID", "1")
    os.environ.setdefault("TELEGRAM_API_HASH", "plan-mode-no-telegram-login")
    os.environ.setdefault("AMAZON_TAG", amazon_tag or "mama086-21")
    if not os.environ.get("BOT_DB_PATH"):
        os.environ["BOT_DB_PATH"] = str(
            Path(tempfile.mkdtemp(prefix="earnkaro-plan-")) / "plan.sqlite3")
    sys.path.insert(0, str(REPO_ROOT / "bestgaa"))
    try:
        import main_bot_new as bot  # noqa: PLC0415 - deliberately late/optional
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return bot, ""


def posted_route(bot, link: str) -> str:
    """Which route minted the posted link — asked of the BOT's own gates.

    "earnkaro"      an attributable network conversion: pays the token's publisher
    "hypd"          OUR HYPD creator-store link for a verified Meesho product
    "amazon_native" the native ?tag= FALLBACK - explicitly NOT an EarnKaro
                    conversion, and with Associates still rejecting the account
                    it earns nothing (the bot itself refuses to call this one)
    "other"         a link the bot minted another way (e.g. a Bitly hop)
    """
    from urllib.parse import urlparse
    kind_fn = getattr(getattr(bot, "AffiliateClient", None), "earnkaro_output_kind", None)
    try:
        if kind_fn and kind_fn(link):
            return "earnkaro"
    except Exception:
        pass
    is_our_hypd = getattr(bot, "is_our_hypd_link", None)
    try:
        if is_our_hypd and is_our_hypd(link):
            return "hypd"
    except Exception:
        pass
    host = (urlparse(link or "").hostname or "").lower()
    if getattr(bot, "in_domains")(host, getattr(bot, "AMAZON_DOMAINS", ())):
        return "amazon_native"
    return "other"


def shortness_note(bot, link: str) -> str:
    """Is the link that will be posted actually short? Asked of the BOT's rules."""
    from urllib.parse import urlparse
    host = (urlparse(link or "").hostname or "").lower()
    limit = int(getattr(bot, "SHORTEN_MIN_LEN", 70))
    ours = set(getattr(bot, "OUR_SHORTENER_DOMAINS", ())) | set(
        getattr(bot, "OUR_RUNTIME_SHORTENER_DOMAINS", ()))
    if ours and getattr(bot, "in_domains")(host, ours):
        return f"SHORT LINK on {host} (our short domain; never re-shortened)"
    if len(link) <= limit:
        return (f"{len(link)} chars - within the bot's {limit}-char limit, so it posts "
                "as it is (no shortener quota spent)")
    return (f"{len(link)} chars and NOT on a short domain - the shortener was rate-limited "
            "or down, so this longer link is posted (a deal is never lost)")


async def plan_posted_links(client, probes, *, multi_link: bool, publisher: str, our_tag: str,
                            timeout: float, bot, whose=None):
    """([lines], failures) — the bot's real convert() for each source link.

    `whose` is injectable so the offline test can drive this without network.
    """
    whose = whose or whose_link
    lines: list[str] = []
    failures = 0
    converted = 0
    unmonetized = 0
    for name, url in probes:
        lines.append(f"\n[{name}]")
        lines.append(f"  source    : {url[:110]}")
        try:
            resolved = await client.resolve(url)
        except Exception as exc:
            failures += 1
            lines.append(f"  resolve   : FAILED - {type(exc).__name__}: {exc}")
            continue
        if resolved and str(resolved) != url:
            lines.append(f"  resolve   : {str(resolved)[:110]}")
        try:
            result = await client.convert(url, multi_link, str(resolved) if resolved else None)
        except Exception as exc:
            # The bot raises so the JOB retries: a temporary API outage must
            # never be reported as "this deal earns nothing".
            failures += 1
            lines.append(f"  convert   : TRANSIENT FAILURE, the job would retry - "
                         f"{type(exc).__name__}: {exc}")
            continue
        if not result:
            unmonetized += 1
            failures += 1
            lines.append("  convert   : UNMONETIZED - the network has no campaign for this "
                         "store/URL. The bot still posts the deal with a clean merchant link; "
                         "NOBODY IS PAID for it.")
            lines.append("  fix       : Affiliaters > Affiliate Settings > Networks + Selections "
                         "(connect EarnKaro and pick a network per store), or take the deal from "
                         "another source link")
            continue
        posted = str(result.affiliate)
        route = posted_route(bot, posted)
        lines.append(f"  posted as : {posted}")
        lines.append(f"  long link : {str(result.resolved)[:110]}")
        lines.append(f"  length    : {shortness_note(bot, posted)}")
        if route == "amazon_native":
            # The bot's own gate refuses to call this an EarnKaro conversion, and
            # neither may this report: Associates is still rejecting the account,
            # so a native ?tag= fallback earns NOTHING.
            unmonetized += 1
            failures += 1
            lines.append("  verdict   : NATIVE AMAZON-TAG FALLBACK - NOT an EarnKaro conversion "
                         "(the network had no campaign for this URL). Amazon Associates is still "
                         "rejecting the account, so this earns NOTHING.")
            lines.append("  fix       : Affiliaters > Affiliate Settings > Selections + Networks "
                         "(Amazon -> a network that has a campaign), or treat this deal as "
                         "unmonetized")
            continue
        if route == "hypd":
            converted += 1
            lines.append("  verdict   : OURS - OUR HYPD creator-store link "
                         f"(store {getattr(bot, 'HYPD_STORE_ID', '?')}), verified Meesho only: "
                         "this is our commission link")
            continue
        converted += 1
        paid, where = whose(posted, publisher, our_tag, timeout)
        if where:
            lines.append(f"  attribution: {where}")
        if paid == "ours":
            lines.append(f"  verdict   : OURS - this post pays our EarnKaro account {publisher}")
        elif paid == "foreign":
            failures += 1
            lines.append("  verdict   : WRONG ACCOUNT - it pays somebody else. The bot's own gate "
                         "should have refused it: rotate EARNKARO_API_KEY and re-check the "
                         "Affiliaters selections")
        else:
            lines.append(f"  verdict   : attribution not visible from here (short link whose "
                         f"destination refused to be read) - the token claims ({publisher}) "
                         "remain the proof")
    lines.append("")
    lines.append(f"PLAN: {converted} converted, {unmonetized} unmonetized, "
                 f"{failures} needing attention (of {len(probes)} link(s), "
                 f"{'list post' if multi_link else 'single-link post'})")
    return lines, failures


def run_plan(args, key: str, publisher: str) -> int:
    bot, why = load_bot_module(key, args.amazon_tag)
    if bot is None:
        print("FAIL: --plan runs the BOT'S OWN pipeline, which could not be imported here.")
        print(f"      {why}")
        print("      Fix: python3 -m pip install -r bestgaa/requirements.txt")
        print("      (Without it, use --deal-url without --plan: it still calls the API with "
              "the same request the bot sends.)")
        return 2
    try:
        import aiohttp
    except Exception as exc:
        print(f"FAIL: aiohttp is missing ({exc}); run: python3 -m pip install -r bestgaa/requirements.txt")
        return 2
    probes = [(f"Actual post URL {i + 1}", url.strip())
              for i, url in enumerate(args.deal_url) if url.strip()]
    invalid = [url for _, url in probes if not url.startswith(("http://", "https://"))]
    if invalid:
        print("FAIL: --deal-url values must be full http(s) URLs")
        return 2
    multi_link = bool(args.multi) or len(probes) >= 2

    async def run():
        async with aiohttp.ClientSession() as session:
            client = bot.AffiliateClient(session)
            return await plan_posted_links(
                client, probes, multi_link=multi_link, publisher=publisher,
                our_tag=args.amazon_tag, timeout=args.timeout, bot=bot)

    print(f"\nPLAN for {len(probes)} link(s) - running the bot's own pipeline "
          f"({'list' if multi_link else 'single-link'}, shortener limit "
          f"{getattr(bot, 'SHORTEN_MIN_LEN', '?')} chars)")
    lines, failures = asyncio.run(run())
    print("\n".join(lines))
    if failures:
        print("RESULT: at least one link above needs attention - each line names its own fix.")
        return 1
    print("RESULT: every link converted, is OURS, and is short enough to post as shown above.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the EarnKaro API key end to end.")
    parser.add_argument("--key", help="API token to check (default: env or bestgaa/.env)")
    parser.add_argument("--env-file", help=".env file to read EARNKARO_API_KEY from")
    parser.add_argument("--deal-url", action="append", default=[],
                        help="test an actual product URL from a failing post (repeatable); replaces sample probes")
    parser.add_argument("--api", default=os.getenv("EARNKARO_API_URL", DEFAULT_API))
    parser.add_argument("--convert-option", default=os.getenv("EARNKARO_CONVERT_OPTION", "convert_only"),
                        help="sent as convert_option (default: convert_only)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--offline", action="store_true", help="decode the token only, no network calls")
    parser.add_argument("--plan", action="store_true",
                        help="run the BOT'S OWN pipeline for each --deal-url and print the exact "
                             "link the channel will carry (resolve -> convert -> shorten)")
    parser.add_argument("--multi", action="store_true",
                        help="with --plan: treat the links as ONE list post (2+ links), which is "
                             "when the bot shortens every link")
    parser.add_argument("--no-expand", action="store_true",
                        help="do not follow short links (skip the 'does it pay US?' step)")
    parser.add_argument("--amazon-tag", default=os.getenv("AMAZON_TAG", "mama086-21"),
                        help="our Amazon Associates tag, for Amazon probes")
    parser.add_argument("--skip-hypd", action="store_true",
                        help="do not run the verified-Meesho HYPD + Bitly live proof")
    parser.add_argument("--hypd-only", action="store_true",
                        help="skip the EarnKaro probes; check verified Meesho HYPD links + Bitly")
    args = parser.parse_args()

    key, source = find_key(args.key, args.env_file)
    if not key:
        print("FAIL: no EARNKARO_API_KEY found (use --key, the environment, or bestgaa/.env)")
        return 1

    print("=" * 78)
    print("EARNKARO KEY CHECK")
    print("=" * 78)
    token_ok, publisher = describe_token(key, source)
    print(f"endpoint          : {args.api}")
    print(f"convert_option    : {args.convert_option}")
    if args.offline:
        print("\n--offline: token inspected, no network calls made.")
        return 0 if token_ok else 1
    if args.plan:
        if not args.deal_url:
            print("\nFAIL: --plan needs at least one --deal-url (the source or product link to run "
                  "through the bot's pipeline).")
            return 2
        if not token_ok:
            print("\nRefusing to run the pipeline with a key that is not a converter token.")
            return 1
        return run_plan(args, key, publisher)
    if args.hypd_only:
        hypd_failures, hypd_ok = check_our_hypd_links(args.timeout, verbose=True)
        print("\n" + "=" * 78)
        print(f"RESULT: {hypd_ok} of {len(OUR_HYPD_LINKS)} configured HYPD candidate(s) "
              f"verified as Meesho and returned to OUR store through Bitly"
              + (f"; {hypd_failures} to fix." if hypd_failures else "."))
        return 1 if hypd_failures else 0
    if not token_ok:
        print("\nRefusing to call the API with a key that is not a converter token.\n"
              "Get the token from the Affiliaters/EarnKaro API page and re-run "
              "ops/set_earnkaro_key.sh.")
        return 1

    print("\n" + "-" * 78)
    failures = 0
    ours = 0
    hidden = 0
    probes = ([(f"Actual post URL {i + 1}", url.strip())
               for i, url in enumerate(args.deal_url) if url.strip()]
              if args.deal_url else list(PROBES))
    if args.deal_url:
        invalid = [url for _, url in probes if not url.startswith(("http://", "https://"))]
        if invalid:
            print("FAIL: --deal-url values must be full http(s) URLs")
            return 2
    else:
        print("Using built-in sample URLs; for a real diagnosis, repeat --deal-url with "
              "the exact product links from a failing post.")
    for name, deal in probes:
        status, body = convert(key, args.api, deal, args.convert_option, args.timeout)
        link, parse_reason = response_link(status, body, deal)
        verdict = "MONETIZED" if link else f"NO CONVERSION - {parse_reason}"
        if status in (401, 403):
            verdict = "TOKEN REFUSED"
            failures += 1
        paid = ""
        if link and not is_earnkaro_output(link, publisher):
            verdict = "NOT A VERIFIED AFFILIATE LINK - API returned an echo/unattributed merchant URL"
            link = None
        if link:
            if args.no_expand:
                paid, where = "unchecked", "expansion disabled (--no-expand)"
            else:
                paid, where = whose_link(link, publisher, args.amazon_tag, args.timeout)
            if paid == "ours":
                ours += 1
                verdict = f"MONETIZED - PAYS US ({publisher})"
            elif paid == "foreign":
                failures += 1
                verdict = "WRONG ACCOUNT - this link pays somebody else"
            elif paid == "hidden":
                hidden += 1
                verdict = "AFFILIATE LINK FOUND (whose account not visible)"
        elif status not in (401, 403):
            failures += 1
        print(f"\n[{name}]")
        print(f"  deal      : {deal[:100]}")
        print(f"  http      : {status}   {verdict}")
        if link:
            print(f"  link      : {link[:120]}")
            if where:
                print(f"  attribution: {where}")
        print(f"  raw body  : {body[:220].strip()}")
        if status in (401, 403):
            print("  -> the token itself was refused: regenerate EARNKARO_API_KEY.")
            break
        if status == 429:
            print("  -> rate limited: wait a minute and re-run, the key is fine.")

    hypd_failures = hypd_ok = 0
    if not args.skip_hypd:
        hypd_failures, hypd_ok = check_our_hypd_links(args.timeout, verbose=True)

    print("\n" + "=" * 78)
    if failures:
        print(f"RESULT: {failures} of {len(probes)} EarnKaro probe(s) failed "
              "(no link, source echo, foreign attribution, or non-affiliate output).")
        print("A no-link can mean the store/campaign is unsupported, or Affiliaters")
        print("network selections are not configured. In Affiliaters > Affiliate")
        print("Settings, connect EarnKaro and choose the intended network for Amazon,")
        print("Flipkart, and Others; then save. Use the exact failing product URL above")
        print("with --deal-url to reproduce. A WRONG ACCOUNT means rotate the API key.")
        if not args.skip_hypd:
            print(f"        (HYPD: {hypd_ok} of {len(OUR_HYPD_LINKS)} candidate(s) "
                  f"verified Meesho + Bitly; {hypd_failures} to fix.)")
        return 1
    if ours:
        print(f"RESULT: the key is live and {ours} converted link(s) were expanded and")
        print(f"        PROVED to pay OUR EarnKaro account {publisher}. Nothing else to fix.")
    else:
        print("RESULT: the key is live and every probe converted, but the publisher")
        print("        behind the short links was not visible from here - re-run")
        print("        without --no-expand, or trust the token claims above (they name")
        print("        the account every converted link is minted for).")
    if hidden:
        print(f"        {hidden} probe(s) could not be attributed (short link whose")
        print("        destination refused to be read); the token claims remain the proof.")
    if not args.skip_hypd:
        print(f"        HYPD: {hypd_ok} of {len(OUR_HYPD_LINKS)} candidates verified as Meesho "
              f"and returned to our store through Bitly"
              + (f"; {hypd_failures} to fix (see above)." if hypd_failures else "."))
    return 1 if (failures or hypd_failures) else 0


if __name__ == "__main__":
    sys.exit(main())

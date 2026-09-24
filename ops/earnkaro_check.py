#!/usr/bin/env python3
"""earnkaro_check.py — does OUR EarnKaro API key actually convert links?

Run this on the server (or anywhere with internet) right after setting
`EARNKARO_API_KEY`:

    python3 ops/earnkaro_check.py                  # token + live conversions
    python3 ops/earnkaro_check.py --offline        # token only, no network
    python3 ops/earnkaro_check.py --key <token>    # check a fresh token first
    python3 ops/earnkaro_check.py --env-file /home/ubuntu/bestgaa-bot/bestgaa-bot/.env

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

Exit codes: 0 = every probe converted, 1 = at least one probe failed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
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
    print(f"token shape       : JWT, issued {issued if issued else 'unknown'}"
          f" ({_as_date(issued)})")
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


def first_http_url(value) -> str | None:
    """Same shape-tolerant extraction the bot uses (kept in step with it)."""
    if value is None:
        return None
    if isinstance(value, str):
        import re
        text = value.strip()
        lowered = text.lower()
        if any(m in lowered for m in ("could not locate", "url not found", "not found in post")):
            return None
        found = re.search(r"https?://[^\s<>\[\](){}|\"']+", text)
        return found.group(0).rstrip(".,;:!?\"')*]>}") if found else None
    if isinstance(value, dict):
        lowered = {str(k).lower(): v for k, v in value.items()}
        for key in ("converted_url", "converted_link", "affiliate_url", "affiliate_link",
                    "ekaro_url", "short_url", "link", "url", "deal", "profit_link"):
            if key in lowered:
                found = first_http_url(lowered[key])
                if found:
                    return found
        for item in value.values():
            found = first_http_url(item)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found = first_http_url(item)
            if found:
                return found
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the EarnKaro API key end to end.")
    parser.add_argument("--key", help="API token to check (default: env or bestgaa/.env)")
    parser.add_argument("--env-file", help=".env file to read EARNKARO_API_KEY from")
    parser.add_argument("--api", default=os.getenv("EARNKARO_API_URL", DEFAULT_API))
    parser.add_argument("--convert-option", default=os.getenv("EARNKARO_CONVERT_OPTION", "convert_only"),
                        help="sent as convert_option (default: convert_only)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--offline", action="store_true", help="decode the token only, no network calls")
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
        print("\n--offline: token inspected, no API call made.")
        return 0 if token_ok else 1
    if not token_ok:
        print("\nRefusing to call the API with a key that is not a converter token.\n"
              "Get the token from the Affiliaters/EarnKaro API page and re-run "
              "ops/set_earnkaro_key.sh.")
        return 1

    print("\n" + "-" * 78)
    failures = 0
    for name, deal in PROBES:
        status, body = convert(key, args.api, deal, args.convert_option, args.timeout)
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        link = first_http_url(payload.get("data")) if isinstance(payload, dict) else None
        verdict = "MONETIZED" if link else "no link"
        if status in (401, 403):
            verdict = "TOKEN REFUSED"
        if link:
            if publisher and f"affextparam2={publisher}" not in link.lower() and publisher not in link:
                pass  # EarnKaro short links hide the publisher; not a failure
        else:
            failures += 1
        print(f"\n[{name}]")
        print(f"  deal      : {deal[:100]}")
        print(f"  http      : {status}   {verdict}")
        if link:
            print(f"  link      : {link[:120]}")
        print(f"  raw body  : {body[:220].strip()}")
        if status in (401, 403):
            print("  -> the token itself was refused: regenerate EARNKARO_API_KEY.")
            break
        if status == 429:
            print("  -> rate limited: wait a minute and re-run, the key is fine.")

    print("\n" + "=" * 78)
    if failures:
        print(f"RESULT: {failures} of {len(PROBES)} probes returned no link.")
        print("A store with no EarnKaro campaign is normal (Amazon search pages, some\n"
              "marketplaces). A NO-LINK on the Flipkart/Myntra probes with HTTP 200\n"
              "means the token is valid but the request/response contract changed —\n"
              "the raw body above is what the bot will log as 'EK CONVERT | no link'.")
        return 1
    print("RESULT: the key is live and every probe converted. Nothing else to fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

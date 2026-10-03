#!/usr/bin/env python3
"""hypd_links.py — OUR HYPD share links: see them, learn them, prove them.

Run on the server (or anywhere with internet) from the bot directory:

    python3 ops/hypd_links.py --list
        Every HYPD share link the bot has learned, with the merchant page behind
        it and the product identity it earned.

    python3 ops/hypd_links.py <hypd-link> [<hypd-link> ...]
        Resolve each share link through the LIVE site, print where it goes, and
        remember it - so a bare Meesho link for the same product earns on
        it later. This is the "I curated these products in the HYPD app"
        workflow: paste the links you created, and the bot starts using them.

    python3 ops/hypd_links.py --paste <url> ...      (same as above)
    python3 ops/hypd_links.py --lookup <product-url>
        Which of OUR HYPD links (if any) covers this product page?

    python3 ops/hypd_links.py --wanted
        Products the bot had to post UNMONETIZED because no HYPD link exists for
        them yet (Meesho: use our HYPD store). Create the link
        for one of them in the HYPD app, learn it with this tool, and every later
        post of that product earns on it.

    python3 ops/hypd_links.py --dry-run <hypd-link>
        Resolve and report, change nothing.

WHY THIS EXISTS
  HYPD has no public link-creation API: a share link is minted inside the HYPD
  creator app for a curated product, and that link (hypd.store/<store>/afflink/
  <token>) is the one that carries the attribution. So the bot does the two
  things it CAN do perfectly:
    * treat only verified Meesho share links from our store as OUR monetized
      links (never unwrapped, never stripped, always Bitly-shortened), and
    * remember the Meesho product behind each one, so a later bare Meesho link
      (no EarnKaro campaign) still earns on it.

Exit codes: 0 = ok, 1 = nothing usable (bad link / wrong store / not found).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bestgaa"))


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(REPO_ROOT / "bestgaa" / ".env")

import main_bot_new as bot  # noqa: E402


def describe(url: str) -> str:
    store = bot.hypd_store_of(url)
    ours = "OURS" if bot.is_our_hypd_link(url) else "NOT OURS (another store)"
    return f"store={store or '?'} [{ours}] token={bot.hypd_afflink_token(url) or '?'}"


async def resolve_live(url: str) -> tuple[str, str]:
    """(destination, key) using the bot's own resolver without writing state."""
    async with bot.aiohttp.ClientSession() as session:
        client = bot.AffiliateClient(session)
        if not bot.is_our_hypd_link(url):
            # Not our link: still resolve it so the operator can SEE where it
            # goes (and that it earns for somebody else).
            destination = await bot.AffiliateClient.resolve(client, url)
            return (destination, bot.product_key(destination) if destination else "")
        # Do not call convert(): --dry-run must not mutate link_cache, the HYPD
        # mapping table, or the wanted list as a side effect of verification.
        destination = await client._hypd_destination(url)
        return (destination, bot.product_key(destination) if destination else "")


async def remember(url: str, destination: str, key: str) -> None:
    await bot.store.remember_hypd_link(url, destination, key)


async def main() -> int:
    parser = argparse.ArgumentParser(description="OUR HYPD share links: list, learn, look up.")
    parser.add_argument("links", nargs="*", help="hypd.store share links you created")
    parser.add_argument("--list", action="store_true", help="list what the bot already knows")
    parser.add_argument("--lookup", help="which of OUR links covers this product page?")
    parser.add_argument("--wanted", action="store_true",
                        help="products still missing OUR HYPD link (the curation to-do list)")
    parser.add_argument("--dry-run", action="store_true", help="resolve and report, change nothing")
    args = parser.parse_args()

    print("=" * 78)
    print(f"HYPD store        : {bot.HYPD_STORE_ID} ({bot.HYPD_STORE_SLUG})  domains={sorted(bot.HYPD_DOMAINS)}")
    print(f"always Bitly      : {bot.HYPD_ALWAYS_BITLY}")
    print(f"HYPD merchant     : Meesho only {sorted(bot.HYPD_MERCHANT_DOMAINS)}")
    print(f"database          : {bot.DB_PATH}")
    print("=" * 78)

    if args.wanted:
        rows = bot.store.recent_hypd_wanted(limit=200)
        if not rows:
            print("Every Meesho product seen so far has OUR HYPD link. Nothing pending.")
            return 0
        print(f"{len(rows)} product(s) posted UNMONETIZED - no HYPD link curated yet:\n")
        for row in rows:
            print(f"  {row['product_url']}")
            print(f"    seen {row['times']}x   product key: {row['product_key'] or '(unknown)'}")
        print("\nHow to fix one:")
        print("  1. open the HYPD app, curate the product, copy its share link")
        print("  2. python3 ops/hypd_links.py '<https://hypd.store/93944/afflink/...>'")
        print("  3. that product now earns on OUR link, and it leaves this list.")
        return 0

    if args.list or (not args.links and not args.lookup):
        rows = bot.store.recent_hypd_links(limit=200)
        if not rows:
            print("No HYPD link learned yet. Pass the links you created in the HYPD app:")
            print("  python3 ops/hypd_links.py 'https://hypd.store/93944/afflink/<token>' ...")
            return 0
        print(f"{len(rows)} learned link(s), newest first:\n")
        for afflink, resolved in rows:
            print(f"  {afflink}")
            print(f"    -> {resolved or '(destination unknown)'}")
            print(f"    {describe(afflink)}")
        return 0

    if args.lookup:
        found = await bot.store.hypd_link_for(args.lookup, bot.product_key(args.lookup))
        print(f"product : {args.lookup}")
        print(f"our link: {found or '(none - this product has no HYPD link yet)'}")
        if not found:
            print("\nCreate it in the HYPD app, then run this tool with that link.")
        return 0 if found else 1

    failures = 0
    for url in args.links:
        print(f"\n{url}")
        print(f"  {describe(url)}")
        if not bot.is_hypd_link(url):
            print("  -> not a hypd.store share link (expected /<store>/afflink/<token>)")
            failures += 1
            continue
        destination, key = await resolve_live(url)
        if destination:
            print(f"  destination : {destination[:120]}")
            print(f"  product key : {key or '(unknown)'}")
        else:
            print("  destination : not verified; no merchant is eligible for HYPD until it resolves")
        if not bot.is_our_hypd_link(url):
            print("  -> another creator's store: NEVER used as ours (it would pay them)")
            failures += 1
            continue
        destination_host = (bot.urlparse(bot.clean_url(destination)).hostname or "").lower() if destination else ""
        if not destination or not bot.in_domains(destination_host, bot.HYPD_MERCHANT_DOMAINS):
            print("  -> NOT STORED: our HYPD store is configured for Meesho only; "
                  "destination must be a verified Meesho product URL")
            failures += 1
            continue
        if args.dry_run:
            print("  (dry run: not stored)")
        else:
            stored = await remember(url, destination, key)
            if stored:
                print("  stored: this Meesho product now uses OUR HYPD link")
            else:
                print("  -> NOT STORED: ownership or Meesho destination validation failed")
                failures += 1

    print("\n" + "=" * 78)
    if failures:
        print(f"RESULT: {failures} link(s) could not be learned.")
        return 1
    print("RESULT: ok. Bot usage: only verified Meesho products use our HYPD link (Bitly),")
    print("and the learned product mapping applies to later bare Meesho links.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

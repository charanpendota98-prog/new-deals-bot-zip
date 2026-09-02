#!/usr/bin/env python3
"""Ask the bot's rule directly: are these two posts the SAME product?

Built for the question that started v17.9 - "did we skip a real deal, or post a
repeat?" Run it with two headlines (or two whole posts, or one line to see what
the key is) and it prints the identity each service would use, plus the verdict
and what the skip window would do with it. If a duplicate ever reaches a channel
again, or a deal vanishes, the answer is here in one second instead of in a
journalctl session.

  python3 ops/identity_probe.py "boAt Airdopes 141 TWS Earbuds ₹1,099" \
                                "boAt Airdopes 141 True Wireless Earbuds, 42H Playtime ₹1,099"
  python3 ops/identity_probe.py "Samsung Galaxy S23 FE 5G (128 GB) ₹49,999" \
                                "Samsung Galaxy S23 FE 5G (256 GB) ₹55,999"
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bestgaa"))
for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "EARNKARO_API_KEY", "AMAZON_TAG"):
    os.environ.setdefault(name, "1" if name.endswith("_ID") else "probe")
os.environ.setdefault("TELEGRAM_API_HASH", "probe")

import main_bot_new as bot  # noqa: E402


def whatsapp_identity(text: str) -> str | None:
    """Ask the bridge the same question, when node and the repo copy exist."""
    node = shutil.which("node")
    bridge = ROOT / "tg-wa-bridge" / "bridge.js"
    if not node or not bridge.exists():
        return None
    headline = next((line.strip() for line in text.splitlines()
                     if line.strip() and "http" not in line), "")
    if not headline:
        return None
    env = dict(os.environ, TELEGRAM_BOT_TOKEN="1:dummy", WA_PHONE="910000000000",
               WA_CHANNEL="@probe")
    proc = subprocess.run([node, str(bridge), "--identity-probe"], input=headline,
                          capture_output=True, text=True, env=env, cwd=str(bridge.parent))
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[0]).get("identity")
    except (ValueError, IndexError):
        return None


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print(__doc__)
        return 2
    texts = [argv[0]] + (argv[1:2] or [])
    ids = [bot.product_signature(t) for t in texts]
    readable_ids: list[tuple | None] = []
    for text, key in zip(texts, ids):
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        headline = next((line.strip() for line in text.splitlines()
                         if line.strip() and "http" not in line), "")
        readable = bot._product_identity(headline)
        readable_ids.append(tuple(readable) if readable else None)
        print(f"  headline : {first[:78]}")
        print(f"  identity : {'|'.join(readable) if readable else 'none - this post is never skipped'}")
        print(f"  telegram : {key or 'no identity (never skipped)'}")
        wa = whatsapp_identity(text)
        print(f"  whatsapp : {wa or 'unavailable / no identity'}")
        print()
    if len(ids) == 1:
        return 0
    same = ids[0] is not None and ids[0] == ids[1]
    if same:
        print("VERDICT: SAME product")
        return 0
    if all(x is None for x in ids) and readable_ids[0] is not None and readable_ids[0] == readable_ids[1]:
        # Two bare headlines: the identity RULE says one product, but neither text is a
        # single-product deal post, so the ledger never keys them and nothing is skipped.
        # Saying "DIFFERENT products" here would contradict the identities printed above.
        print("VERDICT: SAME product by identity; neither text is a postable single-product "
              "deal, so no copy would be skipped (paste the whole posts for that answer)")
        return 0
    print("VERDICT: DIFFERENT products")
    if same:
        price_a, disc_a = bot.parse_price(texts[0]), bot.parse_discount(texts[0])
        price_b, disc_b = bot.parse_price(texts[1]), bot.parse_discount(texts[1])
        print(f"  the second copy still posts when it is cheaper or clearly deeper:")
        print(f"  first  = {price_a} / {disc_a}%   second = {price_b} / {disc_b}%"
              f"   (margin {bot.SAME_PRODUCT_DISCOUNT_MARGIN}, window {bot.SAME_PRODUCT_SKIP_SECONDS}s)")
        better = ((price_b and price_a and price_b < price_a)
                  or (disc_b and disc_a and disc_b - disc_a >= bot.SAME_PRODUCT_DISCOUNT_MARGIN))
        print(f"  so this pair would be {'POSTED as news' if better else 'SKIPPED as a repeat'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

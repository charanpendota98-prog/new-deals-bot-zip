#!/usr/bin/env python3
"""Source → Target, from the command line.

    python3 ops/source_to_target.py --source DealSauce \
        --text 'Samsung neo qled 55" @ ₹60k https://link.amazon/B0gWibFuA'

Answers, offline and read-only: what the link becomes, what the price really is,
which shelf it belongs to, whether the content policy allows it, and which of
OUR channels receive it — with the reason for every yes and every no. The same
engine the dashboard's "Source → Target" panel calls (dashboard/source_target.py),
so the CLI and the UI can never disagree.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))

from dashboard.source_target import analyze, default_snapshot, policy  # noqa: E402

ROLE_LABEL = {
    "amazon_short": "Amazon short wrapper",
    "amazon_product": "Amazon product page",
    "amazon_other": "Amazon (not a product page)",
    "ours": "already our link",
    "merchant": "merchant page",
    "wrapper": "short wrapper",
    "unknown_wrapper": "unknown wrapper",
    "telegram": "Telegram link",
    "direct": "plain link",
    "invalid": "invalid",
}


def render(data: dict) -> str:
    out: list[str] = []
    facts = data["facts"]
    money = ("₹" + f"{facts['price']:,}") if facts.get("price") is not None else "— none —"
    out.append(f"SOURCE      {data['input']['source'] or '(not named)'}")
    out.append(f"POST        {data['input']['text'].strip().splitlines()[0][:90]!r}"
               if data["input"]["text"].strip() else "POST        (empty)")
    out.append("")
    out.append(f"PRICE       {money}"
               + (f"   MRP ₹{facts['mrp']:,}" if facts.get("mrp") else "")
               + (f"   {facts['discount']}% off" if facts.get("discount") else ""))
    out.append(f"QUALITY     score {facts['quality_score']}"
               f"   premium-eligible: {'yes' if facts['premium_eligible'] else 'no'}")
    cat = data["category"]
    out.append(f"CATEGORY    {cat['key'] or '— unclassified —'}"
               + (f" ({cat['label']})" if cat.get("label") else "")
               + f"   [{cat['reason']}]")
    for item in cat["evidence"][:5]:
        out.append(f"              · {item['phrase']!r} +{item['weight']} ({item['source']})")
    safe = data["safety"]
    out.append(f"SAFETY      {'ALLOWED' if safe['allowed'] else 'BLOCKED'}"
               + (f" — {safe['category']}: {safe['reason']}" if safe["category"] else ""))
    out.append("")
    out.append("LINKS")
    if not data["links"]:
        out.append("  (none)")
    for link in data["links"]:
        out.append(f"  {link['url']}")
        out.append(f"    {ROLE_LABEL.get(link['role'], link['role'])}"
                   + (f" · product id {link['product_id']}" if link["product_id"] else ""))
        out.append(f"    → {link['why']}")
        if link["never_published_as"]:
            out.append("    → this wrapper itself is never published")
    out.append("")
    out.append("TARGETS")
    for item in data["plan"]["targets"]:
        out.append(f"  ✓ @{item['name']:<21} [{item['phase']}] {item['why']}")
    for item in data["plan"]["excluded"]:
        out.append(f"  ✗ @{item['name']:<21} {item['why']}")
    if not data["plan"]["targets"] and not data["plan"]["excluded"]:
        out.append("  (no routing map available)")
    for note in data["plan"]["notes"]:
        out.append(f"  · {note}")
    if data["warnings"]:
        out.append("")
        out.append("WARNINGS")
        for item in data["warnings"]:
            out.append(f"  ! {item}")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text", default="", help="the source post, exactly as it was written")
    parser.add_argument("--file", help="read the post from a file (one post per run)")
    parser.add_argument("--source", default="", help="the source channel it came from")
    parser.add_argument("--url", action="append", default=[],
                        help="a link to analyse (repeatable; also read from --text)")
    parser.add_argument("--snapshot", help="routing_snapshot.json to use (default: the bot's)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--policy", action="store_true", help="print the content policy only")
    args = parser.parse_args(argv)
    if args.policy:
        print(json.dumps(policy(), indent=2, ensure_ascii=False))
        return 0
    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text and not args.url:
        parser.print_help()
        return 2
    snapshot = (json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
                if args.snapshot else default_snapshot())
    data = analyze(text, args.source, args.url, snapshot=snapshot)
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else render(data))
    return 0 if data["safety"]["allowed"] and data["plan"]["targets"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

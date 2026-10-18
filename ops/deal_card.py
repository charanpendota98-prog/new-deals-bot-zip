#!/usr/bin/env python3
"""Draw a loot-deal card the way a big channel posts it, and say who would get it.

A competitor's post is a PHOTO plus a caption: product image on the left, a red
`-35%` pill, the deal price big, the M.R.P. struck through, a coupon box
("Coupon Discount / Save 4% now / -₹3.12") and a green "Final Price: ₹75" bar.
Our posts are text, so the same deal looks cheaper on the same phone screen -
and when a deal crosses from one channel to another the copy is re-filtered per
destination, so the two versions can even disagree about the price.

This module builds that card from a caption we already have, using the bot's own
parsers (`bestgaa/deal_facts.py` - the same reading that decides routing), and
answers three questions:

  1. what does the card look like?            `card_png()` -> PNG bytes
  2. what caption goes with it, per surface?  `captions()`  -> telegram/whatsapp/premium/text
  3. which destinations would receive it, in which format?  `forward_plan()`

It is a PREVIEW tool. Nothing here is called by the send path: the bot keeps
posting exactly what it posts today, and posting stays off until
`ops/approve_posting.sh --i-approve-posting`. When you do want the card live, the
carriers already exist - `send_file(entity, png, caption=...)` in
`bestgaa/main_bot_new.py` for Telegram and `broadcastMediaItem(sock, job, item,
caption)` in `tg-wa-bridge/bridge.js` for WhatsApp (both already send a photo
with a per-target caption) - so wiring is a one-line change at the send site,
not a new delivery path.

Read-only: it opens the database in `mode=ro` only to check link provenance, and
writes nothing but the PNG you ask for.

    python3 ops/deal_card.py --text-file /tmp/deal.txt --link https://amzn.lt/xyz
    python3 ops/deal_card.py --text "..." --png /tmp/card.png --surface whatsapp
    python3 ops/deal_card.py --text "..." --json          # the studio payload
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BESTGAA = ROOT / "bestgaa"
if str(BESTGAA) not in sys.path:
    sys.path.insert(0, str(BESTGAA))

from deal_facts import deal_facts                        # noqa: E402

SURFACES = ("telegram", "whatsapp", "premium", "text")
CARD_WIDTH = 1080
# Colours read off a real channel's card: white stock, red discount pill, beige
# coupon box, green final-price bar. Nothing here is themeable on purpose - the
# card is meant to look like the format buyers already recognise.
INK = "#141414"
MUTED = "#6d6d6d"
PILL = "#d93025"
COUPON_BG = "#fdf4e3"
COUPON_EDGE = "#eddcb8"
COUPON_SAVE = "#b26a00"
GREEN = "#1e8e3e"
GREEN_INK = "#0f6b2c"
PHOTO_A = "#f7f1e8"
PHOTO_B = "#ece0d0"
EDGE = "#e3e3e3"
FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    "/usr/local/share/fonts",
)


class CardUnavailable(RuntimeError):
    """Pillow (or a usable font) is missing: the card cannot be drawn."""


def pillow():
    """Import Pillow lazily so the dashboard can serve captions without it."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:                      # pragma: no cover - env dependent
        raise CardUnavailable(
            f"Pillow is not installed ({exc}); run: pip install -r bestgaa/requirements.txt"
        ) from exc
    return Image, ImageDraw, ImageFont


def font_paths() -> dict[str, str]:
    """DejaVu Sans regular/bold if this machine has them, else empty strings."""
    found: dict[str, str] = {"regular": "", "bold": ""}
    for directory in FONT_DIRS:
        for kind, name in (("regular", "DejaVuSans.ttf"), ("bold", "DejaVuSans-Bold.ttf")):
            candidate = Path(directory) / name
            if not found[kind] and candidate.is_file():
                found[kind] = str(candidate)
    if not found["regular"]:
        hits = sorted(glob.glob("/usr/share/fonts/**/DejaVuSans.ttf", recursive=True))
        found["regular"] = hits[0] if hits else ""
    if not found["bold"]:
        hits = sorted(glob.glob("/usr/share/fonts/**/DejaVuSans-Bold.ttf", recursive=True))
        found["bold"] = hits[0] if hits else ""
    return found


def rupee_supported(font) -> bool:
    """True when the font really draws ₹ rather than a missing-glyph box.

    Pillow's bundled fallback font has no rupee sign, so a card drawn with it
    would show a box where the price belongs. Compared against a private-use
    codepoint, which no font ships a glyph for: same outline means "not drawn".
    """
    try:
        missing = font.getmask("\ue123").getbbox()
        rupee = font.getmask("₹").getbbox()
    except Exception:                               # pragma: no cover - font quirks
        return False
    return bool(rupee) and rupee != missing


class Fonts:
    """The two sizes the card needs, and whether ₹ can be drawn at all."""

    def __init__(self):
        _, _, image_font = pillow()
        paths = font_paths()
        self.rupee = True
        self._cache: dict[tuple[str, int], object] = {}
        self._paths = paths
        self._loader = image_font
        self.truetype = bool(paths["regular"])
        if self.truetype:
            self.rupee = rupee_supported(self.get("regular", 24))

    def get(self, kind: str, size: int):
        key = (kind, size)
        if key not in self._cache:
            path = self._paths.get(kind) or self._paths.get("regular")
            font = None
            if path:
                with contextlib.suppress(OSError):
                    font = self._loader.truetype(path, size)
            if font is None:
                with contextlib.suppress(TypeError):
                    font = self._loader.load_default(size=size)
                if font is None:
                    font = self._loader.load_default()
            self._cache[key] = font
        return self._cache[key]

    def money(self, amount, paise: bool = False) -> str:
        """₹78 (or Rs.78 when the font cannot draw the sign), with paise if asked."""
        if amount is None:
            return ""
        sign = "₹" if self.rupee else "Rs."
        if paise:
            return f"{sign}{amount:,.2f}"
        return f"{sign}{amount:,.0f}"


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2190-\u2BFF\uFE0E\uFE0F\u200D]+")


def plain_title(title: str) -> str:
    """The title without pictographs: DejaVu Sans has no emoji, so the PNG would
    draw a box where 🔥 belongs. Telegram and WhatsApp render them on the client,
    so captions keep them - only the raster card strips them."""
    cleaned = " ".join(EMOJI_RE.sub(" ", title or "").split())
    return cleaned or " ".join((title or "").split()) or "Deal"


def wrap(draw, text: str, font, width: int, limit: int = 3) -> list[str]:
    """Greedy word wrap measured with the real font, cut to `limit` lines."""
    words = [w for w in re.split(r"\s+", text or "") if w]
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = (lines[-1][: max(0, limit * 12)] + "…").strip()
    return lines or [""]


def coupon_lines(facts: dict, fonts: Fonts) -> list[tuple[str, str, int]]:
    """The coupon box: (text, colour, size). Empty when no coupon is advertised."""
    coupon = facts.get("coupon") or {}
    saving = facts.get("coupon_saving")
    price = facts.get("price")
    out: list[tuple[str, str, int]] = []
    if coupon.get("code"):
        out.append((f"Apply coupon code {coupon['code']}", COUPON_SAVE, 26))
    elif coupon.get("percent"):
        out.append((f"Save {coupon['percent']}% now", COUPON_SAVE, 26))
    elif coupon.get("amount"):
        out.append(("Coupon applied", COUPON_SAVE, 26))
    if not out:
        return []
    head = [("Coupon Discount", INK, 27)]
    tail: list[tuple[str, str, int]] = []
    if saving:
        # A percentage coupon is worth paise on a ₹78 deal, and a loot card
        # shows those paise - that detail is what makes the price believable.
        paise = bool(coupon.get("percent")) and price is not None and price < 1000
        tail.append((f"-{fonts.money(saving if not paise else price * coupon['percent'] / 100, paise=paise)}",
                     GREEN_INK, 34))
    return head + out + tail


def card_png(facts: dict, photo: bytes | None = None, width: int = CARD_WIDTH) -> bytes:
    """Render the deal card and return PNG bytes."""
    image, draw_mod, _ = pillow()
    fonts = Fonts()
    pad = 32
    photo_box = int(width * 0.407)
    right_x = pad + photo_box + pad
    right_w = width - right_x - pad

    body = image.new("RGB", (width, 100), "white")
    draw = draw_mod.Draw(body)

    # --- measure the right column first, so the card height fits the content ---
    title_font = fonts.get("bold", 34)
    title_lines = wrap(draw, plain_title(facts.get("title")), title_font, right_w, limit=2)
    title_h = int(len(title_lines) * title_font.size * 1.28)

    price_font = fonts.get("bold", 74)
    price_h = int(price_font.size * 1.3)
    mrp_font = fonts.get("regular", 27)

    coupon = coupon_lines(facts, fonts)
    coupon_h = 0
    if coupon:
        coupon_h = 26 + sum(int(size * 1.35) for _, _, size in coupon) + 20

    right_h = title_h + 22 + 56 + price_h + (coupon_h + 18 if coupon_h else 0)
    bar_h = 78
    height = pad + max(photo_box, right_h) + 26 + bar_h + pad

    card = image.new("RGB", (width, height), "white")
    draw = draw_mod.Draw(card)
    draw.rounded_rectangle([1, 1, width - 2, height - 2], radius=26, outline=EDGE, width=2)

    # --- photo, or an honest placeholder -------------------------------------
    top = pad
    photo = None if not photo else photo
    pasted = False
    if photo:
        with contextlib.suppress(Exception):
            shot = image.open(io.BytesIO(photo)).convert("RGB")
            side = min(shot.size)
            left = (shot.width - side) // 2
            upper = (shot.height - side) // 2
            shot = shot.crop((left, upper, left + side, upper + side))
            shot = shot.resize((photo_box, photo_box), image.LANCZOS)
            mask = image.new("L", (photo_box, photo_box), 0)
            draw_mod.Draw(mask).rounded_rectangle(
                [0, 0, photo_box - 1, photo_box - 1], radius=20, fill=255)
            card.paste(shot, (pad, top), mask)
            pasted = True
    if not pasted:
        for step in range(photo_box):
            ratio = step / max(1, photo_box - 1)
            red = int(0xf7 + (0xec - 0xf7) * ratio)
            green = int(0xf1 + (0xe0 - 0xf1) * ratio)
            blue = int(0xe8 + (0xd0 - 0xe8) * ratio)
            draw.line([(pad, top + step), (pad + photo_box, top + step)],
                      fill=(red, green, blue))
        draw.rounded_rectangle([pad, top, pad + photo_box, top + photo_box],
                               radius=20, outline=EDGE, width=2)
        note_font = fonts.get("regular", 24)
        note = "no product photo attached"
        box = draw.textbbox((0, 0), note, font=note_font)
        draw.text((pad + (photo_box - (box[2] - box[0])) / 2,
                   top + (photo_box - (box[3] - box[1])) / 2),
                  note, font=note_font, fill="#9a8f80")

    # --- right column --------------------------------------------------------
    y = top
    for line in title_lines:
        draw.text((right_x, y), line, font=title_font, fill=INK)
        y += int(title_font.size * 1.28)
    y += 18

    pill_font = fonts.get("bold", 30)
    discount = facts.get("discount")
    pill_w = 0
    if discount:
        label = f"-{int(discount)}%"
        pill_w = int(draw.textlength(label, font=pill_font)) + 40
        draw.rounded_rectangle([right_x, y, right_x + pill_w, y + 52], radius=12, fill=PILL)
        draw.text((right_x + 20, y + 7), label, font=pill_font, fill="white")

    price = facts.get("final") if facts.get("final") is not None else facts.get("price")
    if price is not None:
        price_text = fonts.money(price)
        px = right_x + (pill_w + 22 if pill_w else 0)
        # the big number sits on the same line as the pill, baseline-aligned
        draw.text((px, y - 16), price_text, font=price_font, fill=INK)
        price_w = int(draw.textlength(price_text, font=price_font))
        mrp = facts.get("mrp")
        if mrp:
            mrp_text = f"M.R.P.: {fonts.money(mrp)}"
            mx = px + price_w + 24
            my = y - 16 + price_font.size - mrp_font.size - 6
            draw.text((mx, my), mrp_text, font=mrp_font, fill=MUTED)
            box = draw.textbbox((mx, my), mrp_text, font=mrp_font)
            strike = (box[1] + box[3]) // 2
            draw.line([(box[0], strike), (box[2], strike)], fill=MUTED, width=2)
    y += price_h + 12

    if coupon:
        box_h = coupon_h
        draw.rounded_rectangle([right_x, y, right_x + right_w, y + box_h],
                               radius=16, fill=COUPON_BG, outline=COUPON_EDGE, width=2)
        cy = y + 16
        for text, colour, size in coupon:
            font = fonts.get("bold" if size >= 30 else "regular", size)
            draw.text((right_x + 22, cy), text, font=font, fill=colour)
            cy += int(size * 1.35)
        y += box_h + 18

    # --- final price bar -----------------------------------------------------
    bar_y = height - pad - bar_h
    draw.rounded_rectangle([pad, bar_y, width - pad, bar_y + bar_h], radius=16, fill=GREEN)
    bar_font = fonts.get("bold", 34)
    label = f"Final Price: {fonts.money(price) if price is not None else 'see caption'}"
    draw.text((pad + 28, bar_y + (bar_h - bar_font.size) / 2 - 2), label,
              font=bar_font, fill="white")
    if facts.get("min_qty"):
        qty_font = fonts.get("regular", 24)
        qty = f"Min Buy Qnty = {facts['min_qty']}"
        qty_w = draw.textlength(qty, font=qty_font)
        draw.text((width - pad - 28 - qty_w, bar_y + (bar_h - qty_font.size) / 2),
                  qty, font=qty_font, fill="#dff3e4")

    buffer = io.BytesIO()
    card.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def load_photo(path_or_url: str) -> bytes | None:
    """Read a local image file for the card's photo slot (never a network fetch)."""
    if not path_or_url:
        return None
    candidate = Path(path_or_url).expanduser()
    if not candidate.is_file():
        return None
    with contextlib.suppress(OSError):
        return candidate.read_bytes()
    return None


def money_text(amount, paise: bool = False) -> str:
    """Caption money: Telegram and WhatsApp both render ₹, so always use it."""
    if amount is None:
        return ""
    return f"₹{amount:,.2f}" if paise else f"₹{amount:,.0f}"


def coupon_phrase(facts: dict) -> str:
    """The one caption line that tells the buyer what to do about the coupon."""
    coupon = facts.get("coupon") or {}
    price = facts.get("price")
    saving = facts.get("coupon_saving")
    if coupon.get("code"):
        detail = f" (saves {money_text(saving)})" if saving else ""
        return f"Apply coupon code {coupon['code']}{detail}"
    if coupon.get("percent"):
        detail = ""
        if price is not None and price < 1000:
            detail = f" (saves {money_text(price * coupon['percent'] / 100, paise=True)})"
        elif saving:
            detail = f" (saves {money_text(saving)})"
        return f"Apply {coupon['percent']}% coupon{detail}"
    if coupon.get("amount"):
        return f"Apply ₹{coupon['amount']} coupon"
    return ""


def captions(facts: dict, link: str = "", link_owned: bool = True,
             link_why: str = "") -> dict[str, str]:
    """One caption per surface. The bot sends with parse_mode=None, so these are
    plain text: no markdown, no HTML, brackets stay brackets."""
    title = plain_title(facts.get("title"))
    price = facts.get("final") if facts.get("final") is not None else facts.get("price")
    coupon = coupon_phrase(facts)
    if link and link_owned:
        link_line = f"🔗 {link}"
    elif link:
        link_line = f"🔗 link withheld — {link_why or 'not proven ours'}"
    else:
        link_line = "🔗 link withheld — no link supplied"

    head = f"{title} @ {money_text(price)}" if price is not None else title
    extras = [line for line in (coupon,) if line]
    if facts.get("mrp") and facts.get("discount"):
        extras.append(f"M.R.P. {money_text(facts['mrp'])} · {facts['discount']}% off")
    if facts.get("min_qty"):
        extras.append(f"Min Buy Qnty = {facts['min_qty']}")

    telegram = "\n".join([f"🔥 {head}", ""] + extras + ["", link_line])
    whatsapp = "\n".join([head] + extras + [link_line.replace("🔗 ", "")])
    premium_lines = [f"🔥 {title}"]
    if price is not None:
        bits = [money_text(price)]
        if facts.get("mrp"):
            bits.append(f"MRP {money_text(facts['mrp'])}")
        if facts.get("discount"):
            bits.append(f"{facts['discount']}% off")
        premium_lines.append(" · ".join(bits))
    if coupon:
        premium_lines.append(coupon)
    premium_lines.append(link_line)
    text = "\n".join([head] + extras + [link_line])
    return {
        "telegram": telegram.strip(),
        "whatsapp": whatsapp.strip(),
        "premium": "\n".join(premium_lines).strip(),
        "text": text.strip(),
    }


def _load_report_module():
    """ops/category_report.py by path: it owns link provenance and channel views."""
    import importlib.util
    path = ROOT / "ops" / "category_report.py"
    spec = importlib.util.spec_from_file_location("card_category_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def link_verdict(link: str, db: str | None = None, our_tags: frozenset | None = None) -> dict:
    """Is this link ours? Read from link_cache/owned_link_proofs, never assumed.

    The card may print a link only when the money it earns is provably ours:
    an Amazon page without our Associates tag pays a stranger, and a shortener
    with no proof row cannot be attributed at all.
    """
    link = (link or "").strip()
    if not link:
        return {"url": "", "owned": False, "bucket": "none", "route": "",
                "why": "no link supplied"}
    report = _load_report_module()
    tags = our_tags
    if tags is None:
        read = report.our_amazon_tags()
        tags = frozenset(read.get("bestgaa") or set()) | frozenset(read.get("bridge") or set())
    links = {"by_affiliate": {}, "by_source": {}, "proofs": {}}
    error = ""
    try:
        conn = report.open_readonly(report.default_db() if db is None else db)
    except Exception as exc:
        conn = None
        error = f"database not readable: {exc}"
    if conn is None:
        error = error or f"database not readable: {db or 'default bot database'}"
    else:
        try:
            links = report.link_index(conn)
        finally:
            conn.close()
    bucket, route = report.url_money(link, links, tags)
    owned = bucket == "owned"
    why = {
        "owned": "our tag / our shortener, proven in the ledger",
        "foreign_tag": "another publisher's Amazon tag would earn, not ours",
        "earns_nothing": "this link earns nothing for us",
        "unattributed": "shortened link with no proof row: destination unknown",
    }.get(bucket, bucket)
    if error:
        why = f"{why} ({error})"
    return {"url": link, "owned": owned, "bucket": bucket, "route": route, "why": why}


def forward_plan(text: str, link: str = "", rules_path: str | None = None,
                 channels_path: str | None = None, env_files: list[str] | None = None,
                 db: str | None = None) -> dict:
    """Every destination this deal could reach, and the format it would get.

    Categories come from the same rules file the bot and the bridge both read;
    acceptance comes from channels.json (`categories`, `enabled`) plus the
    env-declared shelves (`TG_CATEGORY_<KEY>_TARGET`) and WhatsApp channels
    (`WA_CHANNEL_<KEY>`). Nothing is sent and nothing is written.
    """
    report = _load_report_module()
    import category_classifier
    import channels_config

    rules = report.load_rules(Path(rules_path) if rules_path else None)
    data = channels_config.load_channels(Path(channels_path) if channels_path else None)
    env = report.load_env(env_files)
    verdict = link_verdict(link, db=db)
    classified = category_classifier.classify_category(text, link_context=[link] if link else [])
    key = classified.get("category")
    view = report.channels_view(data, rules)
    known = [str(c.get("key", "")).lower() for c in rules.get("categories", [])
             if isinstance(c, dict)]
    declared = report.env_targets(env)

    rows: list[dict] = []
    seen: set[str] = set()

    def add(name: str, surface: str, kind: str, accepts: bool, why: str, enabled: bool = True,
            carrier: str = ""):
        identity = (surface, name)
        if identity in seen:
            return
        seen.add(identity)
        rows.append({
            "destination": name, "surface": surface, "kind": kind, "enabled": enabled,
            "accepts": bool(accepts and enabled), "why": why,
            "format": "card + caption" if (accepts and enabled) else "not sent",
            "carrier": carrier,
        })

    tg_carrier = "send_file(entity, card.png, caption=…) — bestgaa/main_bot_new.py"
    wa_carrier = "broadcastMediaItem(sock, job, {type:'image',buffer}, caption) — tg-wa-bridge/bridge.js"

    for row in view["main"]:
        add(row["name"], "telegram", "main", True,
            "unfiltered target: every category reaches it", row["enabled"], tg_carrier)
    for shelf_key in known:
        for row in view["shelves"].get(shelf_key, []):
            if key and shelf_key != key:
                add(row["name"], "telegram", "shelf", False,
                    f"shelf keeps {shelf_key}; this deal classified as {key}",
                    row["enabled"], tg_carrier)
            else:
                add(row["name"], "telegram", "shelf", True,
                    f"shelf keeps {shelf_key}" + ("" if key else "; deal unclassified"),
                    row["enabled"], tg_carrier)
    for name, value in sorted(env.items()):
        if not name.startswith("TG_CATEGORY_") or not value.strip():
            continue
        env_key = name[len("TG_CATEGORY_"):].split("_TARGET")[0].lower()
        target = report.normalize_target(value)
        if not target:
            continue
        accepts = bool(key) and env_key == key
        add(target, "telegram", "env-shelf", accepts,
            f"TG_CATEGORY_{env_key.upper()}_TARGET" + ("" if accepts else f"; deal is {key or 'unclassified'}"),
            True, tg_carrier)
    surfaces = report.wa_surfaces(env, rules)
    for shelf_key, masked in sorted((surfaces.get("channels") or {}).items()):
        accepts = bool(key) and shelf_key == key
        add(masked, "whatsapp", "channel", accepts,
            f"WA_CHANNEL_{shelf_key.upper()}"
            + ("" if accepts else f"; deal classified as {key or 'nothing'}"), True, wa_carrier)
    for slug, masked in sorted((surfaces.get("core") or {}).items()):
        add(masked, "whatsapp", "core", True,
            f"core WhatsApp destination ({slug}): every deal reaches it", True, wa_carrier)

    if not rows:
        plan_warnings = ["no destinations configured: channels.json has no targets and "
                         "no TG_CATEGORY_*_TARGET / WA_CHANNEL_* is set, so this deal has "
                         "nowhere to go"]
    else:
        plan_warnings = []
    accepted = [row for row in rows if row["accepts"]]
    return {
        "category": key,
        "category_reason": classified.get("reason", ""),
        "evidence": [str(e) for e in (classified.get("evidence") or [])][:6],
        "rules_version": rules.get("version", 0),
        "rows": rows,
        "accepted": len(accepted),
        "card_destinations": len(accepted),
        "link": verdict,
        "link_ok": verdict["owned"],
        "warnings": plan_warnings
        + ([] if verdict["owned"] else [f"link not printable: {verdict['why']}"])
        + ([] if key or not rows else
           ["deal is unclassified: shelves that filter by category will skip it"]),
        "declared_targets": declared,
    }


def studio(text: str, link: str = "", photo_path: str = "", rules_path: str | None = None,
           channels_path: str | None = None, env_files: list[str] | None = None,
           db: str | None = None, width: int = CARD_WIDTH) -> dict:
    """The whole card-studio payload for one deal caption."""
    facts = deal_facts(text)
    plan = forward_plan(text, link=link, rules_path=rules_path, channels_path=channels_path,
                        env_files=env_files, db=db)
    verdict = plan["link"]
    caps = captions(facts, link=link, link_owned=verdict["owned"], link_why=verdict["why"])
    card = {"available": False, "why": "", "width": 0, "height": 0, "bytes": 0}
    try:
        png = card_png(facts, photo=load_photo(photo_path), width=width)
        image, _, _ = pillow()
        with image.open(io.BytesIO(png)) as shot:
            card = {"available": True, "why": "", "width": shot.width, "height": shot.height,
                    "bytes": len(png)}
    except CardUnavailable as exc:
        card["why"] = str(exc)
    try:
        import post_buttons
        buttons = post_buttons.load_buttons()
    except Exception:                               # config problems must not kill preview
        buttons = []
    return {
        "facts": facts,
        "card": card,
        "link": verdict,
        "captions": caps,
        "plan": plan,
        "buttons": buttons,
        "button_note": ("Telegram posts carry this button row; WhatsApp channels have "
                        "no inline buttons, so WhatsApp stays text-only."
                        if buttons else
                        "no buttons configured: posts go out exactly as today"),
        "surfaces": list(SURFACES),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--text", default="", help="the deal caption to render")
    parser.add_argument("--text-file", default="", help="read the caption from a file")
    parser.add_argument("--link", default="", help="the link the card should print")
    parser.add_argument("--photo", default="", help="local image file for the photo slot")
    parser.add_argument("--png", default="", help="write the card PNG here ('-' = stdout)")
    parser.add_argument("--surface", default="", choices=("", *SURFACES),
                        help="print only this surface's caption")
    parser.add_argument("--width", type=int, default=CARD_WIDTH)
    parser.add_argument("--rules", default="", help="category_rules.json to classify against")
    parser.add_argument("--channels", default="", help="channels.json to plan against")
    parser.add_argument("--env-file", action="append", default=[], help="read env from this file")
    parser.add_argument("--db", default="", help="read-only database for link provenance")
    parser.add_argument("--json", action="store_true", help="print the studio payload as JSON")
    args = parser.parse_args(argv)

    text = args.text
    if args.text_file:
        try:
            text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"cannot read --text-file: {exc}", file=sys.stderr)
            return 2
    if not text.strip():
        print("nothing to render: pass --text or --text-file", file=sys.stderr)
        return 2

    payload = studio(text, link=args.link, photo_path=args.photo,
                     rules_path=args.rules or None, channels_path=args.channels or None,
                     env_files=args.env_file or None, db=args.db or None, width=args.width)

    if args.png:
        try:
            png = card_png(payload["facts"], photo=load_photo(args.photo), width=args.width)
        except CardUnavailable as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.png == "-":
            sys.stdout.buffer.write(png)
            sys.stdout.buffer.flush()
            return 0
        Path(args.png).parent.mkdir(parents=True, exist_ok=True)
        Path(args.png).write_bytes(png)
        print(f"card written: {args.png} ({len(png)} bytes, {payload['card']['width']}x"
              f"{payload['card']['height']})", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    facts = payload["facts"]
    print(f"title   : {facts['title']}")
    print(f"price   : {money_text(facts['price']) or 'not read'}"
          + (f"   M.R.P. {money_text(facts['mrp'])}" if facts["mrp"] else "")
          + (f"   {facts['discount']}% off" if facts["discount"] else ""))
    print(f"coupon  : {coupon_phrase(facts) or 'none advertised'}")
    print(f"final   : {money_text(facts['final']) or 'not read'}"
          + (f"   min qty {facts['min_qty']}" if facts["min_qty"] else ""))
    plan = payload["plan"]
    print(f"category: {plan['category'] or 'unclassified'} ({plan['category_reason']}, "
          f"rules v{plan['rules_version']})")
    verdict = payload["link"]
    print(f"link    : {verdict['url'] or 'none'} -> {verdict['bucket']}"
          f"{' (printable)' if verdict['owned'] else ' (NOT printable: ' + verdict['why'] + ')'}")
    print(f"plan    : {plan['accepted']} of {len(plan['rows'])} destinations would receive "
          f"card + caption")
    for row in plan["rows"]:
        mark = "SEND" if row["accepts"] else "skip"
        print(f"  [{mark}] {row['surface']:<9} {row['destination']:<28} {row['format']:<14} {row['why']}")
    for warning in plan["warnings"]:
        print(f"  ! {warning}")
    if args.surface:
        print(f"\n--- caption: {args.surface} ---\n{payload['captions'][args.surface]}")
    else:
        for surface in SURFACES:
            print(f"\n--- caption: {surface} ---\n{payload['captions'][surface]}")
    if not payload["card"]["available"]:
        print(f"\ncard image unavailable: {payload['card']['why']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

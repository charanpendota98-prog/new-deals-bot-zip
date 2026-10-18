"""Deal facts: the one place prices, discounts, MRP and coupons are read.

A loot caption is adversarial text - "MRP Rs.1,999 Deal Rs.899", "Save Rs.500",
"Apply 4% coupon", a model number that looks like a price. The bot's routing has
read those carefully for years; the deal-card renderer and the dashboard's card
studio need the same reading, so it lives here once and main_bot_new imports it
instead of carrying a second copy that could drift.

Pure and side-effect free: no network, no database, no Telegram. The coupon and
final-price maths are presentation only - they never feed routing or money
decisions, which keep using parse_price / parse_discount.
"""
from __future__ import annotations

import contextlib
import re

__all__ = [
    "parse_price", "parse_discount", "has_lowest_price_claim", "parse_mrp",
    "parse_coupon", "parse_min_qty", "deal_title", "coupon_saving",
    "final_price", "deal_facts",
]


def parse_price(text: str) -> int | None:
    """Prefer explicit deal/effective price; avoid MRP/coupon/bank discount values.

    Deep-audit fixes (2026-09-04): "MRP ₹1,999 Deal ₹899" used to return the
    MRP (routing a ₹899 deal as if it cost ₹1,999); "INR 799", "Price: 349/-"
    and "349/-" (the Indian price suffix) were not read at all. MRP-labelled
    amounts are masked before the generic currency scan, so the generic match
    can only ever see the DEAL price. Bare numbers with no currency marker are
    still ignored on purpose - a model number is not a price."""
    value = text or ""
    # Mask "MRP ₹1,999" style amounts so no later pattern can mistake the MRP
    # for the deal price. The deal-price labels below never say MRP.
    masked = re.sub(r"(?i)\b(?:mrp|m\.r\.p\.?|list\s*price|regular\s*price)\s*[:@-]?\s*(?:rs\.?|₹|inr)?\s*[\d,]+",
                    " ", value)
    # A DISCOUNT is not a price. "Save Rs.500", "Rs.99 off", "Flat 200 off" and
    # "Rs.50 cashback" all name money the buyer does NOT pay; reading one as the
    # price prints a figure the source never charged. Masked before the scan so
    # no pattern below can reach them.
    masked = re.sub(r"(?i)\b(?:save|flat|upto|up\s*to|extra|discount|off|cashback)\s*"
                    r"(?:rs\.?|₹|inr)?\s*[\d,]+", " ", masked)
    # Only the amount IMMEDIATELY before the word, with no space swallowed from
    # a preceding price: "Rs.1,999 Discount: 66%" must keep the 1,999. So the
    # currency mark is required, or the number must hug the word.
    masked = re.sub(r"(?i)(?:rs\.?|₹|inr)\s*[\d,]+\s*(?:off|cashback)\b", " ", masked)
    masked = re.sub(r"(?i)\b[\d,]+\s*%?\s*(?:off|cashback)\b", " ", masked)
    patterns = [
        r"(?:deal|effective|offer|final)\s*price\s*[:@-]?\s*(?:rs\.?|₹|inr)?\s*([\d,]+)",
        r"(?:deal|offer|now|today)\s*[:@-]?\s*(?:rs\.?|₹|inr)\s*\.?\s*([\d,]+)",
        r"(?:only|at|@)\s*(?:rs\.?|₹|inr)?\s*([\d,]+)",
        # \b anchors the END of the number: without it "Rs.99 off" backtracked to
        # "9" (the lookahead only had to fail for the SHORTER match) and printed
        # a price of 9 rupees.
        r"(?:rs\.?|₹|inr)\s*\.?\s*([\d,]+)\b(?!\s*(?:off|coupon|cashback))",
        # Indian "/-" price suffix: "Price: 349/-", "349/-" is explicit money.
        r"(?:price\s*[:@-]?\s*)?([\d,]{2,})\s*/-",
        # Suffix currency: "749 rs" / "249Rs." / "1,299 INR" - money named AFTER
        # the number is still explicit money.
        r"([\d,]{2,})\s*(?:rs\.?|inr)\b",
        r"^\s*([\d,]{2,})\s+(?:https?://|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, masked, re.I | re.M)
        if match:
            price = int(match.group(1).replace(",", ""))
            if 1 <= price <= 1_000_000:
                return price
    # "899 (MRP 2990)" - the deal price written as a BARE number, with the MRP
    # right after it. Nothing above matches (no currency mark, no @/only/at),
    # so the MRP fallback below used to answer 2990: the post advertised a
    # price the buyer does not pay, and price drives routing and dedup.
    # A bare number immediately followed by an MRP is the deal price.
    bare_before_mrp = re.search(
        r"(?<![\d,.])([\d,]{2,})\s*[({\[]?\s*"
        r"(?:mrp|m\.r\.p\.?|list\s*price|regular\s*price)\b",
        value, re.I)
    if bare_before_mrp:
        price = int(bare_before_mrp.group(1).replace(",", ""))
        if 1 <= price <= 1_000_000:
            return price
    # No deal price anywhere: an MRP-only line ("MRP: ₹270") is still the only
    # money on the post, so it is the best available answer (old behaviour).
    # The masking above only stops MRP from SHADOWING a real deal price.
    mrp = re.search(r"(?i)\b(?:mrp|m\.r\.p\.?|list\s*price|regular\s*price)\s*[:@-]?\s*(?:rs\.?|₹|inr)?\s*([\d,]+)", value)
    if mrp:
        price = int(mrp.group(1).replace(",", ""))
        if 1 <= price <= 1_000_000:
            return price
    return None

def parse_discount(text: str, price: int | None = None) -> int | None:
    """Return the strongest explicit/derived product discount percentage."""
    value = text or ""
    found: list[int] = []
    patterns = (
        r"(?:up\s*to|upto|flat|save|get)?\s*([1-9]\d?|100)\s*%\s*(?:off|discount)",
        # "save 25 %" / "get 40 %" with a space before the sign and no
        # off/discount word after it — the label BEFORE the number is what
        # makes it a discount, so plain "25 %" alone still never matches.
        r"(?:save|get|upto|up\s*to|flat)\s+([1-9]\d?|100)\s*%",
        # "Discount: 26%" is how a loot channel actually writes it - the colon after
        # the label used to lose the whole percentage, which matters because the
        # discount decides which price-tier channel a deal is routed to.
        r"(?:off|discount|savings?)\s*[:=-]?\s*(?:up\s*to|upto|flat)?\s*[:=-]?\s*([1-9]\d?|100)\s*(?:%|percent\b)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, value, re.I):
            percent = int(match.group(1))
            if 1 <= percent <= 100:
                found.append(percent)

    mrp_matches = re.findall(r"\bMRP\s*[:@-]?\s*(?:rs\.?|₹)?\s*([\d,]+)", value, re.I)
    deal_price = price if price is not None else parse_price(value)
    if mrp_matches and deal_price:
        with contextlib.suppress(ValueError):
            mrp = max(int(raw.replace(",", "")) for raw in mrp_matches)
            if mrp > deal_price > 0:
                found.append(round((mrp - deal_price) * 100 / mrp))
    return max(found) if found else None

def has_lowest_price_claim(text: str) -> bool:
    return bool(re.search(
        r"\b(?:lowest(?:\s+ever)?\s+price|all[ -]?time\s+low|price\s+drop|"
        r"lower\s+than\s+market|historical\s+low|best\s+price)\b",
        text or "", re.I,
    ))


MRP_RE = re.compile(
    r"\b(?:mrp|m\.r\.p\.?|list\s*price|regular\s*price)\s*[:@-]?\s*"
    r"(?:rs\.?|₹|inr)?\s*([\d,]+)", re.I)
COUPON_PERCENT_RES = (
    re.compile(r"([1-9]\d?)\s*%\s*(?:off\s+)?coupon\b", re.I),
    re.compile(r"\bcoupon\s*(?:of|worth|for)?\s*([1-9]\d?)\s*%", re.I),
)
COUPON_AMOUNT_RES = (
    re.compile(r"\bcoupon\s*(?:discount\s*)?(?:of|worth|for)?\s*(?:rs\.?|₹|inr)?\s*([\d,]+)", re.I),
    re.compile(r"(?:rs\.?|₹|inr)\s*([\d,]+)\s*(?:off\s+)?coupon\b", re.I),
)
COUPON_CODE_RE = re.compile(
    r"\b(?:code|coupon\s*code|use\s*code)\s*[:\-]?\s*([A-Z][A-Z0-9]{3,14})\b", re.I)
MIN_QTY_RE = re.compile(
    r"\bmin(?:imum)?\s*(?:buy|order)?\s*(?:qty|quantity|qnty|qt)\s*[:=]?\s*(\d{1,3})\b", re.I)
URLISH_RE = re.compile(r"^\s*(?:https?://|www\.)")
LABEL_RE = re.compile(
    r"^\s*(?:mrp|price|deal\s*price|offer|discount|coupon|save|final|buy|stock|limited|"
    r"min\s*buy|use\s*code|apply|join|link)\b", re.I)


def parse_mrp(text: str) -> int | None:
    """The struck-through list price, when the caption names one."""
    values = []
    for raw in MRP_RE.findall(text or ""):
        with contextlib.suppress(ValueError):
            value = int(raw.replace(",", ""))
            if 1 <= value <= 1_000_000:
                values.append(value)
    return max(values) if values else None


def parse_coupon(text: str) -> dict:
    """An extra coupon the caption advertises: percent, rupee amount, or a code."""
    value = text or ""
    percent = None
    for pattern in COUPON_PERCENT_RES:
        match = pattern.search(value)
        if match and 1 <= int(match.group(1)) <= 99:
            percent = int(match.group(1))
            break
    amount = None
    for pattern in COUPON_AMOUNT_RES:
        match = pattern.search(value)
        if match:
            with contextlib.suppress(ValueError):
                candidate = int(match.group(1).replace(",", ""))
                if 1 <= candidate <= 100_000:
                    amount = candidate
                    break
    code = None
    match = COUPON_CODE_RE.search(value)
    if match:
        code = match.group(1).upper()
    if percent is None and amount is None and code is None:
        return {}
    return {"percent": percent, "amount": amount, "code": code}


def parse_min_qty(text: str) -> int | None:
    """'Min Buy Qnty = 2': a buying condition worth printing next to the price."""
    match = MIN_QTY_RE.search(text or "")
    if not match:
        return None
    qty = int(match.group(1))
    return qty if 2 <= qty <= 99 else None


def deal_title(text: str, limit: int = 96) -> str:
    """The product-name line: the first line that is not a link, label or price."""
    for line in (text or "").splitlines():
        candidate = " ".join(line.split())
        if len(candidate) < 8:
            continue
        if URLISH_RE.match(candidate) or LABEL_RE.match(candidate):
            continue
        if candidate.count("₹") + candidate.lower().count("rs.") > 1:
            continue
        return candidate[:limit]
    first = " ".join((text or "").split())
    return first[:limit] or "Deal"


def coupon_saving(price: int | None, coupon: dict) -> int | None:
    """The coupon's rupee value: its amount, or its percent of the deal price."""
    if price is None or not coupon:
        return None
    if coupon.get("amount"):
        return int(coupon["amount"])
    if coupon.get("percent"):
        return round(price * int(coupon["percent"]) / 100)
    return None


def final_price(price: int | None, coupon: dict) -> dict | None:
    """What the buyer pays after the advertised coupon, floored at Rs.1.

    A coupon bigger than the price is a caption typo, not a free product: the
    card then shows the deal price and keeps the coupon as information.
    """
    if price is None:
        return None
    saving = coupon_saving(price, coupon)
    if saving is None or saving >= price:
        return price
    return max(1, price - saving)


def deal_facts(text: str, price: int | None = None) -> dict:
    """Everything the card and the captions print, from one reading of the post."""
    deal_price = price if price is not None else parse_price(text)
    discount = parse_discount(text, deal_price)
    mrp = parse_mrp(text)
    if mrp is not None and deal_price is not None and mrp <= deal_price:
        mrp = None          # an "MRP" at or below the deal price is not an MRP
    coupon = parse_coupon(text)
    return {
        "title": deal_title(text),
        "price": deal_price,
        "mrp": mrp,
        "discount": discount,
        "coupon": coupon,
        "coupon_saving": coupon_saving(deal_price, coupon),
        "final": final_price(deal_price, coupon),
        "min_qty": parse_min_qty(text),
        "lowest_price_claim": has_lowest_price_claim(text),
    }

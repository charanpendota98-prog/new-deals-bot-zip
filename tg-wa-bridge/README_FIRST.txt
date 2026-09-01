BestGAA Telegram -> WhatsApp Rotational Deal Lists (unofficial)
================================================================

SELECTED ROTATION
1. @Under99Deals11: preferred batch 5 unique verified products.
2. @under499loots: preferred batch 5 curated verified products.
3. @LootZoneIndia11: preferred batch 10 curated verified products.
4. Repeat from Under ₹99.

To avoid long Channel silence, an older queue can flush 2+ deals after 30 minutes (Under99), 35 minutes (Under499), or 45 minutes (LootZone). A single verified deal can flush after 90 minutes. Items remain durably queued until eligible. Every list gets a random 20-90 second settle. Cross-channel duplicates are removed globally.

PRIORITY 1 — SPECIAL OFFERS PREEMPT THE ROTATION
- 80-100% OFF, credit/debit card, bank offer, PNB/HDFC/ICICI/SBI/Axis/Kotak/IDFC/AU Bank.
- Caption text and the first image are both checked.
- Free local Tesseract OCR can detect visible 90% OFF/card text even when caption omits it.
- Special post uses the first available photo/video + compact complete details + verified links.
- After the special post, normal safety pacing applies and rotation resumes where it stopped.
- OCR is best-effort; tiny/stylized/regional-language image text may not always be recognized.

PRIORITY 2 — LARGE MULTI-LINK POSTS
- Any Telegram post with 4+ distinct links becomes a separate MEGA DEAL LIST after its random 30-90 second settle; it does not wait for 5/5/10 thresholds.
- A text-only "Up to 83% Off" multi-link post remains a mega list. If a multi-link post also has a special-offer photo, the photo card is sent first, then the full mega list continues after the safety gap—no category links are lost.
- Original title/category labels and every verified affiliate link are retained neatly with arrow formatting.
- If it exceeds 3,800 characters, it is split safely into numbered sequential Channel updates; links are never truncated.
- Each part is checkpointed in the durable job, and normal safety pacing applies between parts. The rotation remains paused and resumes after the mega list finishes.

PRIORITY 3 — ROTATIONAL LIST FORMATS
UNDER ₹99:
🔥 DEALS OF THE DAY
💥 UNDER ₹99
✅ Verified deals • Enjoy (Grab fast)
Exactly 5 numbered unique deal cards.

UNDER ₹499:
🔥 DEALS OF THE DAY
💥 UNDER ₹499
✅ Verified deals • Enjoy (Grab fast)
Exactly 5 numbered unique deal cards.

LOOT ZONE:
🔥 LOOT ZONE — India
DEALS OF THE DAY
✅ Verified deals • Enjoy (Grab fast)
Exactly 10 numbered unique deal cards.

STRICT SOURCE-ONLY WHATSAPP MODE
- Output format is strict source-only: cleaned source photo/video, useful title/price/coupon/variant labels, and exact generated affiliate links.
- Every valid shopping candidate is retained; smart curation changes dispatch priority rather than silently dropping ordinary deals. Useful Under99/Under499, Shopsy, clothing, footwear, electronics, home/kitchen, fitness, toys, personal-care/daily-essential deals and strong discounts are sent first.
- Dispatch priority is recalculated at send time, so a newly arrived stronger deal can move ahead while the safety delay is running.
- Exact dispatch order: Under₹99/₹199/₹499 first; then photo/video; 4+ product lists; 2+ links; highest discounts; women-focused products; Shopsy/home/electronics/clothing/footwear; finally remaining deals with ageing bonus.
- No bot-added Deals-of-the-Day/Special/Mega headings, slogans, numbering, or promotional footer.
- Listener covers Under99, Under499, LootZone, Secret, Power and Premium owned targets; global URL/headline-content dedup prevents the same deal copied through several targets.
- Unnecessary #merchant, Deal Time, cashback-forward text and source branding are removed. Price/MRP lines are normalized.

SMART BREAKS
- Base warm-up/random gaps remain.
- Normal active-period deal gap is randomized between 60–120 seconds (warm-up days 1–2 stay slower at 120–180 seconds).
- Multi-photo parts and long-text follow-ups wait a random 65–90 seconds instead of bursting.
- During hours with enough posts, take one irregular 5–6 minute break per IST hour.
- Sparse hours do not create dummy posts.

SAFETY / RELIABILITY
- Separate BotFather listener and separate tg-wa-bridge.service.
- bestgaa.service stays untouched and independent.
- Durable queue, restart recovery, global dedup, persisted rotation and retries.
- WhatsApp jobs older than 12 hours expire automatically, so stale/expired deals are not reposted later and cannot fill disk; state backups remain bounded.
- Same campaign headline is suppressed for 24 hours even when regenerated short URLs differ.
- Hybrid 02:00–06:00 mode allows only major 80%+, special/card, 4+ link, or strong 70%+ photo deals.
- Max 3,800 characters; descriptions compact safely, affiliate URLs are never truncated.
- Amazon tag and visible EarnKaro publisher ID validation.
- Every outgoing URL must also exactly exist as an authenticated generated affiliate_url in BestGAA's SQLite link_cache; manually posted/foreign links are blocked even if their hostname looks acceptable.
- Foreign source shorteners and explicit broken Flipkart repair pages blocked.
- Posting schedule is 24/7. Quiet/hybrid windows are disabled (`QUIET_START=QUIET_END=00:00`, `HYBRID_QUIET=false`).
- The normal 60–120 second random gap, hourly/day caps, dedup and one 5–6 minute random break/hour still apply at night.
- Warm-up pacing, hourly/day caps, random longer pauses and session breaks.
- Queue growth never increases sending speed.

IMPORTANT REALITY
- Software/OCR are free and self-hosted on Oracle.
- This is NOT Meta's official WhatsApp Business API.
- It is not unlimited, ban-proof, or guaranteed to keep working.
- Random pacing is conservative load control, not a guarantee against enforcement.
- Use only a dedicated number and Channel you control. No unsolicited private messaging.

BEFORE INSTALL
1. Create a new bot with @BotFather.
2. Add it as admin in @Under99Deals11, @under499loots and @LootZoneIndia11.
3. Keep token private; enter it only in SSH installer prompt.
4. Dedicated WhatsApp number must own/admin target WhatsApp Channel.
5. Keep public WhatsApp Channel invite link ready.

WINDOWS UPLOAD
scp -i "C:\Users\Charan\Downloads\ssh-key-2026-08-01.key" "C:\Users\Charan\Downloads\tg_wa_bridge_bundle.zip" ubuntu@129.159.229.134:~/

ORACLE INSTALL
ssh -i "C:\Users\Charan\Downloads\ssh-key-2026-08-01.key" ubuntu@129.159.229.134
mkdir -p ~/tg-wa-bridge
cd ~/tg-wa-bridge
unzip -o ~/tg_wa_bridge_bundle.zip
chmod +x install_bridge.sh
./install_bridge.sh

STATUS
sudo systemctl is-active tg-wa-bridge
sudo systemctl is-active bestgaa
journalctl -u tg-wa-bridge -n 100 --no-pager -l

LIVE LOGS
journalctl -u tg-wa-bridge -f --no-pager

STOP ONLY WHATSAPP BRIDGE
sudo systemctl stop tg-wa-bridge

SWITCH TO ANOTHER WHATSAPP NUMBER, SAME CHANNEL
1. First add the new dedicated number as owner/admin of the same WhatsApp Channel.
2. Run:
cd ~/tg-wa-bridge
./switch_whatsapp_number.sh
3. Enter the new number and pairing code locally in SSH.

The switch script stops only tg-wa-bridge, backs up old auth/.env, changes only WA_PHONE, preserves bridge-state.json/backup queue, pairs the new number, starts the bridge and verifies both services. WA_CHANNEL remains unchanged. If pairing fails, the bridge stays stopped and old auth backup remains available. bestgaa.service is never stopped or edited.

PAIRING FALLBACK
- Phone-number code: npm run pair
- QR fallback: rm -rf auth && mkdir -m 700 auth && npm run pair:qr
- Scan only the newest QR immediately from WhatsApp > Linked devices > Link a device. Pairing codes/QRs expire and must never be pasted into chat.

FAILURE ISOLATION
- bestgaa.service (Telegram affiliate bot) and tg-wa-bridge.service are separate processes/services.
- WhatsApp logout/ban/disconnect does not stop Telegram monitoring, conversion or Telegram target posting.
- While WhatsApp is unavailable, the bridge continues collecting Telegram channel posts into its durable queue as long as the collector process remains running.
- Primary queue state uses atomic replacement plus a one-generation backup. Number switching also makes a manual queue backup.
- A new admin number can resume the same pending queue and same Channel after pairing.

SECURITY
Never upload/paste .env, BotFather token, pairing code or auth/. Back up .env and auth/ securely.

WhatsApp channel policies (v17.4). Each configured channel is fed by its own rule,
set in .env:
  WA_CHANNEL            main channel - every deal that passes the pipeline
  WA_CHANNEL_UNDER99    deals up to Rs99, card/bank offers, and every product list
  WA_CHANNEL_UNDER499   same, with a Rs499 band - picks from all sources
  WA_CHANNEL_BEST_OF    only the single best deal available at that moment; a deal
                        that is not a best pick is skipped here (never delayed and
                        dumped). WA_BEST_OF_COOLDOWN_SECONDS spaces the picks out.
WA_CHANNEL_ALL_POSTS=true overrides all of this and mirrors every post to every
channel. Channel IDs may be an invite link or a ...@newsletter JID; a channel that
fails to resolve once is retried every 10 minutes, and the main channel keeps
posting meanwhile.

v17.5 formatting rule: a WhatsApp post is the SOURCE post. The bridge no longer
adds anything of its own - no bold headline hoisted out of the text, no "💰 price /
🔥 discount" badge line, no "LOOT ZONE — India / SPECIAL OFFER / Verified • Enjoy
(Grab fast)" wrapper on specials, no "MEGA DEAL LIST" banner on big lists, no
"DEALS OF THE DAY" header on digests, no "➜" bullets and no "Latest deal" filler.
What is still removed is only junk (another channel's branding, referral /
app-install farming, CTA filler, markdown debris, glued tokens) and what is changed
is only the link: every merchant link becomes OUR monetized link, one per line.
If a source opens with its own "🔥🔥 TOP DEAL OF THE DAY 🔥🔥" line, that line is
published exactly as the source wrote it; set WA_STRIP_CAMPAIGN_BANNERS=true only if
you want those hype lines gone.

v17.6 intake rule (same on the Telegram bot): the queue always publishes the BEST
copy of a product. When a second source posts the same merchant product id while the
first copy is still waiting to be delivered, the waiting job is re-pointed at the
stronger deal ("best copy: queued job re-pointed") instead of the newer post being
dropped as a duplicate - so the price/discount our channels show is the best one
that existed at that moment, not a race result. Safety rules that never change:
one job per product (this is a swap, not a second job), the swap only matches EXACT
product ids (a shortener-only post is never matched by name), a list or a
photo-special already in the queue is never rewritten, a job that already started
delivering is left alone, and a list that merely contains an already-queued product
is still posted in full (dropping it would lose the other items).

v17.8 (round 10) - nothing is cut to make a message fit, and a product is not sent
twice:
  * A DIGEST ITEM IS THE SOURCE POST, numbered. It used to print a 90-character
    name plus a badge WE invented ("₹1,099 • 78% OFF") and then cut the item to
    220/160/100/60 characters so that ten deals would fit one message - that is
    where the user's "price appears twice in one WhatsApp post" and "text is
    missing on WhatsApp" both came from. formatDigestItem now renders the cleaned
    source body with our links, invents nothing, and buildBucketDigest returns
    {digest, used}: if the bucket holds more complete posts than fit, the digest
    carries FEWER of them and the rest stay queued for the next list. Truncating a
    source line to squeeze more items in is not an option any more.
  * A PHOTO CAPTION TOO LONG FOR WHATSAPP IS SPLIT, NOT CUT. formatSpecialCaption
    no longer ends with an ellipsis; splitCaptionForMedia puts whole lines on the
    photo and the remainder is sent as a message right behind it.
    compactLargeLine no longer shortens list lines either (max defaults to 0).
  * CLEANING MAY NOT DELETE A FACT. The CTA clause patterns ended with
    "[^.\n|]*$", which deleted the rest of the line - a coupon code after "More
    offers:" went away, and a line like "More deals here: <link>" lost the link,
    which is how a whole post once disappeared. CTA_TAIL may only swallow plain
    words: a price, a digit, a percentage, a code-like token or a link ends the
    clause. If a cleanup did take an amount/percentage/code away, stripInlineCta
    undoes itself for that line (dealPayloadOf / payloadLacks), exactly like the
    bot's strip_inline_cta.
  * SAME PRODUCT, ONE CHANNEL, ONE TIME. nameOnlyKey() identifies a product by its
    own words plus size/capacity tokens (prices and MRP clauses masked, because
    those change between copies of one deal) and duplicateProductReason() skips a
    product the channel already carried inside WA_SAME_PRODUCT_HOURS (default 48) -
    unless the new copy is strictly better: cheaper, or at least
    WA_SAME_PRODUCT_MARGIN (default 5) points deeper discounted, because a 1-point
    difference is measurement noise and must not re-post the same item. A roundup
    (3+ merchant links), a campaign banner and a bare category phrase never get an
    identity. state.sentNames is the ledger; markProductSent() writes it.
    The v17.6 swap rule is unchanged and stays exact-id only: this key may SKIP a
    repeat, it never re-points or rewrites a queued job.
  * ONE CEILING ON PACING. scheduleNext() could multiply an already-clamped gap by
    the morning stretch, the near-cap-hour stretch and the quiet-hours factor and
    then add the hourly break, so the WhatsApp channels could be parked for more
    than an hour by accident (and the self-test ceiling was wrong about it, which
    made the deploy gate fail roughly one run in six). The scheduled gap is now
    clamped to WA_MAX_MESSAGE_GAP_SECONDS (default 3600) and the self-test asserts
    that number.

  v17.9 (round 11) - the same-product rule, fixed at the root:
  * IDENTITY IS A TOKEN SET, NOT A PHRASE. The name-only key used to be the first
    eight words of the headline in order, which got BOTH directions wrong: it
    dropped digit-only model numbers (so "Airdopes 141" and "Airdopes 131" were
    "the same product" and a real deal got skipped), and it treated any extra
    adjective as a new product (so the same AC written "1.5 Ton 5 Star Inverter Split
    AC" by one channel and "…with 4 Way Swing" by another was posted twice).
    productNameIdentity() now keys brand + model numbers (141, s23, wh-ch720n,
    pro4, model2600) + variant qualifiers (pro/fe/max - S23 and S23 FE stay apart)
    + the numbers that change the product (128GB, 1.5 ton, 5 star, 55 inch, 5
    burner), and ignores specs channels type inconsistently (42H, 5000mAh, 1080p,
    4K, 5G) and a launch year such as "(2023)". No model number at all (a shirt, a
    handbag) means every product word must agree, which is the conservative answer:
    lose a duplicate, never a deal.
  * THE THREE SERVICES MUST AGREE, AND A TEST ENFORCES IT. The rule lives in
    main_bot_new.py; ops/sync_identity.py GENERATES the auditor's copy
    (`python3 ops/sync_identity.py`, checked by `--check`); and this bridge answers
    `--identity-probe` on stdin (one headline per line, identity per line as JSON)
    so test_line_fidelity.py compares JavaScript against Python on the same corpus.
    If you change one copy, that test goes red - do not "fix" it by loosening it.
  * A BROKEN LINK COSTS MONEY. splitCaptionForMedia() used to cut an over-long
    caption at the 1024th character, which could leave "https://www.amazon.in/dp/B0AB"
    at the end of the caption and the rest of it in the follow-up text: a dead link,
    an unpaid sale, and a customer who saw a half a URL. captionCutAt() breaks at a
    space OUTSIDE any URL instead (or right after the URL when the URL alone is too
    long), and the self-test fails if a link is ever bisected or printed twice.

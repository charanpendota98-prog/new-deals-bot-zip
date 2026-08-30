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

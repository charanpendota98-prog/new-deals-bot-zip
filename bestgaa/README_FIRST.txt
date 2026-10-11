BESTGAA ONE-CLICK DEPLOY

1) Upload bestgaa_final_bundle.zip to:
   /home/ubuntu/bestgaa-bot/bestgaa-bot/

2) On Oracle server run:

   cd ~/bestgaa-bot/bestgaa-bot
   unzip -o bestgaa_final_bundle.zip
   chmod +x deploy_bestgaa.sh
   ./deploy_bestgaa.sh

The deployer automatically:
- extracts credentials locally from the existing working main_bot.py
- creates .env with chmod 600 without printing secrets
- backs up the old working bot
- compiles the new bot
- installs dependencies
- starts/restarts bestgaa.service
- validates startup
- automatically rolls back if deployment fails

CATEGORY-WISE CHANNELS (optional, configured after channel creation)

The bot now supports six additive category mirrors. A deal still appears on the
Main feed; a high-confidence single category adds one matching channel. Mixed or
unclear posts remain on Main rather than being guessed into the wrong shelf.

1) Create the Telegram channels you want (Footwear, Fashion, Electronics,
   Home & Kitchen, Beauty, Grocery) and grant the Telethon session account
   administrator/posting rights.
2) In bestgaa/.env, set the matching variables to each public Telegram
   username (with or without @):
   TG_CATEGORY_FOOTWEAR_TARGET=
   TG_CATEGORY_FASHION_TARGET=
   TG_CATEGORY_ELECTRONICS_TARGET=
   TG_CATEGORY_HOME_KITCHEN_TARGET=
   TG_CATEGORY_BEAUTY_TARGET=
   TG_CATEGORY_GROCERY_TARGET=
3) For Amazon links, declare each new channel in Associates Central BEFORE
   adding its username to AMAZON_TAG_CATEGORY_TARGETS. `AMAZON_TAG_TARGETS=all`
   alone deliberately does not enable tags on newly created category channels.
4) Set the matching WA_CHANNEL_* variables in the bridge .env after creating
   WhatsApp Channels and making the bridge number an admin. See
   tg-wa-bridge/.env.example for invite-link/JID format. Amazon tags on these
   WhatsApp channels remain off until their slugs are declared and listed in
   WA_AMAZON_TAG_CATEGORY_TARGETS.
5) Restart bestgaa and tg-wa-bridge. Category routing/classification rules are
   versioned in category_rules.json alongside each service bundle.

Expected final output:
   DEPLOY SUCCESS

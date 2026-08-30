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

Expected final output:
   DEPLOY SUCCESS

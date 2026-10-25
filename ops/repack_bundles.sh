#!/usr/bin/env bash
# Regenerate the deploy bundles (bestgaa_final_bundle.zip / tg_wa_bridge_bundle.zip)
# from the tracked source directories, with the exact same file lists as the
# originally shipped bundles. Output lands next to apply_dual_hotfix.sh so the
# dual-hotfix deployer can run straight from a fresh clone:
#
#   cd ops && ./repack_bundles.sh && ./apply_dual_hotfix.sh
#
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

command -v zip >/dev/null 2>&1 || { echo "ERROR: zip is required (apt-get install zip)" >&2; exit 1; }

echo "[1/3] Packing bestgaa_final_bundle.zip ..."
cd "$REPO_ROOT/bestgaa"
zip -X -q "$HERE/bestgaa_final_bundle.zip" \
  main_bot_new.py deal_facts.py safety_filter.py safety_rules.json category_classifier.py channels_config.py channel_directory.py post_buttons.py live_channels.py hypd_policy.py category_rules.json source_catalog.json deploy_bestgaa.sh migrate_legacy_env.py bot_paths.py \
  requirements.txt bestgaa.service README_FIRST.txt .env.example \
  -j "$HERE/coverage_audit.py" -j "$HERE/quality_audit.py"

# Both of these are HARD imports of main_bot_new.py (channel_directory for the
# source picker, post_buttons for the send path) and both were missing from this
# list until 2026-10-23: every repack silently shipped a bundle that could not
# boot a clean host, while deploy_bestgaa.sh happily required them.
for required in channel_directory.py post_buttons.py; do
  unzip -l "$HERE/bestgaa_final_bundle.zip" | grep -q " $required$" || {
    echo "ERROR: $required did not make it into the bundle" >&2; exit 1; }
done

echo "[2/3] Packing tg_wa_bridge_bundle.zip ..."
cd "$REPO_ROOT/tg-wa-bridge"
zip -X -q "$HERE/tg_wa_bridge_bundle.zip" \
  bridge.js send_guard.js safety_filter.js category_classifier.js hypd_policy.py category_rules.json package.json tg-wa-bridge.service \
  install_bridge.sh switch_whatsapp_number.sh README_FIRST.txt .env.example

# The bridge is deployed to its own directory (~/tg-wa-bridge) while the policy
# lives with the bot, so the shared JSON must travel INSIDE this bundle too.
echo "[2/3] Packing the shared safety policy into the bridge bundle ..."
zip -X -q "$HERE/tg_wa_bridge_bundle.zip" -j "$REPO_ROOT/bestgaa/safety_rules.json"

echo
echo "Bundles written to $HERE:"
ls -l "$HERE"/*.zip
echo
echo "Contents:"
unzip -l "$HERE/bestgaa_final_bundle.zip" | tail -n +4 | head -n -2
unzip -l "$HERE/tg_wa_bridge_bundle.zip" | tail -n +4 | head -n -2
echo
echo "REPACK SUCCESS — now run ./apply_dual_hotfix.sh from this directory (server)."

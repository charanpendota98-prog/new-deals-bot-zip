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

echo "[1/2] Packing bestgaa_final_bundle.zip ..."
cd "$REPO_ROOT/bestgaa"
zip -X -q "$HERE/bestgaa_final_bundle.zip" \
  main_bot_new.py deploy_bestgaa.sh migrate_legacy_env.py \
  requirements.txt bestgaa.service README_FIRST.txt .env.example \
  -j "$HERE/coverage_audit.py" -j "$HERE/quality_audit.py"

echo "[2/2] Packing tg_wa_bridge_bundle.zip ..."
cd "$REPO_ROOT/tg-wa-bridge"
zip -X -q "$HERE/tg_wa_bridge_bundle.zip" \
  bridge.js package.json tg-wa-bridge.service \
  install_bridge.sh switch_whatsapp_number.sh README_FIRST.txt .env.example

echo
echo "Bundles written to $HERE:"
ls -l "$HERE"/*.zip
echo
echo "Contents:"
unzip -l "$HERE/bestgaa_final_bundle.zip" | tail -n +4 | head -n -2
unzip -l "$HERE/tg_wa_bridge_bundle.zip" | tail -n +4 | head -n -2
echo
echo "REPACK SUCCESS — now run ./apply_dual_hotfix.sh from this directory (server)."

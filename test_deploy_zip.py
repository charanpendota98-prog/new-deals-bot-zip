#!/usr/bin/env python3
"""The deploy bundle must contain the code that is in the tree — always.

`bestgaa_deploy.zip` is what the operator actually uploads, so it is the one
artifact where "merged" and "shipped" can silently disagree. It did: the tracked
zip was built 2026-09-06 and carried none of `deal_facts.py`, `post_buttons.py`,
`safety_filter.py`, `safety_rules.json`, `category_classifier.py`,
`channels_config.py`, `live_channels.py`, `hypd_policy.py`,
`source_catalog.json` — so `bash deploy_bestgaa.sh` from that zip aborted at
step [1/9] on a server that had nothing wrong with it.

These checks make that failure mode impossible to merge:
  * the tracked zip passes `ops/build_deploy_zip.py --check` (byte-identical
    files, MANIFEST.sha256 honest, no secrets, no missing hard imports);
  * every Python file in it compiles and the bot imports from the *unpacked*
    bundle (not from the repo) — a missing helper shows up here, not on the server;
  * every JS file in it is syntactically valid and the bridge safety filter
    loads the policy out of the bundle;
  * the shared policy is the same file on both sides of the zip;
  * the real post still prices and routes correctly when read from the bundle.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO = os.path.dirname(os.path.abspath(__file__))
ZIP = os.path.join(REPO, "bestgaa_deploy.zip")
BUILDER = os.path.join(REPO, "ops", "build_deploy_zip.py")
PYTHON = sys.executable

checks = 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    print(("  ok   " if ok else "  FAIL ") + name + ((" — " + detail) if (detail and not ok) else ""))
    if not ok:
        raise SystemExit(1)


print("== deploy bundle ==")
check("bundle is tracked in the repository",
      subprocess.run(["git", "-C", REPO, "ls-files", "--error-unmatch", "bestgaa_deploy.zip"],
                     capture_output=True).returncode == 0,
      "git add bestgaa_deploy.zip")

result = subprocess.run([PYTHON, BUILDER, "--check"], capture_output=True, text=True)
check("ops/build_deploy_zip.py --check passes", result.returncode == 0,
      (result.stdout + result.stderr).strip()[:600])

with zipfile.ZipFile(ZIP) as archive:
    names = set(archive.namelist())
    # The exact helpers deploy_bestgaa.sh refuses to start without.
    for required in ["main_bot_new.py", "deal_facts.py", "post_buttons.py",
                     "safety_filter.py", "safety_rules.json", "category_classifier.py",
                     "category_rules.json", "channels_config.py", "live_channels.py",
                     "hypd_policy.py", "source_catalog.json", "deploy_bestgaa.sh",
                     "migrate_legacy_env.py", "requirements.txt", "bestgaa.service",
                     "tg-wa-bridge/bridge.js", "tg-wa-bridge/install_bridge.sh",
                     "tg-wa-bridge/package.json", "tg-wa-bridge/safety_rules.json"]:
        check("bundle ships " + required, required in names)
    check("no .env / database / session in the bundle",
          not any(os.path.basename(n) in (".env",) or n.endswith((".sqlite3", ".session", ".log"))
                  for n in names))

work = tempfile.mkdtemp(prefix="deploybundle-")
try:
    with zipfile.ZipFile(ZIP) as archive:
        archive.extractall(work)
    subprocess.run(["sha256sum", "-c", "--quiet", "MANIFEST.sha256"], cwd=work, check=True)
    check("MANIFEST.sha256 verifies after unpacking", True)

    pys = sorted(n for n in names if n.endswith(".py"))
    compile_result = subprocess.run([PYTHON, "-m", "py_compile", *pys], cwd=work,
                                    capture_output=True, text=True)
    check(f"all {len(pys)} Python files compile", compile_result.returncode == 0,
          compile_result.stderr[:400])

    for js in sorted(n for n in names if n.endswith(".js")):
        node = subprocess.run(["node", "--check", js], cwd=work, capture_output=True, text=True)
        check("node --check " + js, node.returncode == 0, node.stderr[:300])

    # Import from the UNPACKED bundle, with the repo not on the path: this is the
    # only way a missing helper is caught before the server boots into ImportError.
    env = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x",
           "EARNKARO_API_KEY": "k", "EARNKARO_PUBLISHER_ID": "5478322",
           "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
    probe = (
        "import json, deal_facts, safety_filter, category_classifier, post_buttons\n"
        "import main_bot_new as bot\n"
        "text = 'Samsung neo qled 55\\u201d @ \\u20b960k\\nhttps://link.amazon/B0gWibFuA'\n"
        "print(json.dumps({\n"
        "  'price': deal_facts.deal_facts(text).get('price'),\n"
        "  'category': bot.classify_deal_category(text),\n"
        "  'policy': json.load(open('safety_rules.json')).get('version'),\n"
        "  'bridge_policy_same': json.load(open('tg-wa-bridge/safety_rules.json'))\n"
        "                         == json.load(open('safety_rules.json')),\n"
        "}))\n"
    )
    probe_result = subprocess.run([PYTHON, "-c", probe], cwd=work, capture_output=True,
                                  text=True, env=env)
    check("the bot and every helper import from the unpacked bundle",
          probe_result.returncode == 0, probe_result.stderr.strip()[-600:])
    facts = json.loads(probe_result.stdout.strip().splitlines()[-1]) if probe_result.returncode == 0 else {}
    check("the bundle prices the reported post as ₹60,000", facts.get("price") == 60000,
          str(facts.get("price")))
    check("the bundle routes it to the electronics shelf", facts.get("category") == "electronics",
          str(facts.get("category")))
    check("the bundle carries the live safety policy", facts.get("policy") == "2026-10-19.4",
          str(facts.get("policy")))
    check("the bridge bundle reads the same policy file", facts.get("bridge_policy_same") is True)

    bridge_probe = subprocess.run(
        ["node", "-e",
         "const s=require('./tg-wa-bridge/safety_filter.js');"
         "const v=s.scan('S.E.X video leaked, join fast');"
         "console.log(JSON.stringify({blocked: !!(v&&(v.blocked||v.severity==='block')),"
         "version:(s.policySummary&&s.policySummary().version)||s.RULES_PATH.split('/').pop()}));"],
        cwd=work, capture_output=True, text=True)
    check("the bridge gate loads its policy from the bundle and blocks",
          bridge_probe.returncode == 0 and '"blocked":true' in bridge_probe.stdout,
          (bridge_probe.stdout + bridge_probe.stderr).strip()[:400])
finally:
    shutil.rmtree(work, ignore_errors=True)

print(f"\ndeploy bundle PASS ({checks} checks): the tracked zip is the tree.")

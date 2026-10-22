#!/usr/bin/env python3
"""Build (or verify) the single-file deploy bundle: bestgaa_deploy.zip.

Why this exists
---------------
Until now `bestgaa_deploy.zip` was packed by hand on some laptop and committed.
It drifted: the tracked copy was built 2026-09-06 and did not contain
`deal_facts.py`, `post_buttons.py`, `safety_filter.py`, `safety_rules.json`,
`category_classifier.py`, `channels_config.py`, `live_channels.py`,
`hypd_policy.py`, `source_catalog.json` or the new `deploy_bestgaa.sh` — so a
server that unzipped it and ran `bash deploy_bestgaa.sh` aborted at step [1/9]
with "deal_facts.py missing". The bundle that is supposed to carry *everything*
was the one thing carrying the old code.

This builder reads the files straight out of the repository, so the zip can
only ever contain the code that is in the tree. `--check` proves it: every
shipped file must be byte-identical to its repo file, and every file the bot
hard-imports must be present.

Usage
-----
    python3 ops/build_deploy_zip.py            # rebuild bestgaa_deploy.zip
    python3 ops/build_deploy_zip.py --check     # verify the tracked zip

The zip layout is the layout the deploy expects: bot files at the archive root
(unzip -o into /home/ubuntu/bestgaa-bot/bestgaa-bot) and the WhatsApp bridge
under tg-wa-bridge/.
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP_NAME = "bestgaa_deploy.zip"
ZIP_PATH = os.path.join(REPO_ROOT, ZIP_NAME)

# Bot files that land at the archive root (the app directory on the server).
BOT_FILES = [
    "main_bot_new.py",
    "deal_facts.py",
    "safety_filter.py",
    "safety_rules.json",
    "category_classifier.py",
    "channels_config.py",
    "channel_directory.py",
    "live_channels.py",
    "hypd_policy.py",
    "category_rules.json",
    "source_catalog.json",
    "post_buttons.py",
    "deploy_bestgaa.sh",
    "migrate_legacy_env.py",
    "requirements.txt",
    "bestgaa.service",
    "README_FIRST.txt",
    ".env.example",
]

# Bridge files, copied to ~/tg-wa-bridge on the server.
BRIDGE_FILES = [
    "bridge.js",
    "send_guard.js",
    "safety_filter.js",
    "category_classifier.js",
    "hypd_policy.py",
    "category_rules.json",
    "package.json",
    "tg-wa-bridge.service",
    "install_bridge.sh",
    "switch_whatsapp_number.sh",
    "README_FIRST.txt",
    ".env.example",
    # The bridge is deployed to its own directory (~/tg-wa-bridge) and reads the
    # policy from its own folder first (safety_filter.js: HERE/safety_rules.json),
    # so the one shared policy has to travel INSIDE this bundle too.
    "safety_rules.json",
]

# Files the bot cannot boot or cannot send without (mirrors the check in
# deploy_bestgaa.sh step [1/9]). A bundle missing any of these is not deployable.
REQUIRED = {
    "main_bot_new.py",
    "deal_facts.py",
    "post_buttons.py",
    "safety_filter.py",
    "safety_rules.json",
    "category_classifier.py",
    "category_rules.json",
    "channels_config.py",
    "live_channels.py",
    "hypd_policy.py",
    "source_catalog.json",
    "deploy_bestgaa.sh",
    "migrate_legacy_env.py",
    "requirements.txt",
    "bestgaa.service",
    "tg-wa-bridge/bridge.js",
    "tg-wa-bridge/install_bridge.sh",
    "tg-wa-bridge/package.json",
    "tg-wa-bridge/safety_rules.json",
}

# Written by this builder itself, so they cannot be listed inside themselves.
GENERATED = {"MANIFEST.sha256", "BUILD_INFO.txt", "DEPLOY_README.txt"}

# Secrets never belong in an artifact that gets copied between machines.
FORBIDDEN_SUFFIXES = (".env", ".sqlite3", ".session", ".log")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(rel: str) -> bytes:
    with open(os.path.join(REPO_ROOT, rel), "rb") as handle:
        return handle.read()


def iter_files():
    """Yield (archive name, repo path) for everything the bundle ships."""
    for name in BOT_FILES:
        yield name, os.path.join("bestgaa", name)
    for name in BRIDGE_FILES:
        repo_path = os.path.join("tg-wa-bridge", name)
        if name == "safety_rules.json":
            # single source of truth: the bot's copy, shipped into the bridge
            repo_path = os.path.join("bestgaa", name)
        yield "tg-wa-bridge/" + name, repo_path


def _deterministic_zip(entries) -> bytes:
    """Build the archive in memory with fixed timestamps (reproducible bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buf.getvalue()


def build(verbose: bool = True) -> bytes:
    entries = []
    for archive_name, repo_path in iter_files():
        if os.path.basename(repo_path) == ".env" or archive_name.endswith(FORBIDDEN_SUFFIXES):
            raise SystemExit(f"refusing to ship a secret: {repo_path}")
        data = _read(repo_path)
        entries.append((archive_name, data))

    manifest = ["# sha256  file", ""]
    sizes = []
    for name, data in sorted(entries):
        sizes.append(f"{len(data):>8}  {name}")
        manifest.append(f"{_sha256(data)}  {name}")
    manifest_blob = ("\n".join(manifest) + "\n").encode()

    build_info = [
        "BESTGAA DEPLOY BUNDLE",
        "=====================",
        f"files: {len(entries)} + MANIFEST.sha256 + BUILD_INFO.txt",
        "source: this repository's tracked files (ops/build_deploy_zip.py)",
        "",
        "Bot files sit at the archive root; the WhatsApp bridge is under tg-wa-bridge/.",
        "Every file listed in MANIFEST.sha256 is byte-identical to the repo:",
        "    sha256sum -c MANIFEST.sha256        (after unzipping)",
        "",
        "WHAT IS IN HERE",
        "---------------",
        "  main_bot_new.py        the bot (safety gate, our-link conversion, routing)",
        "  deal_facts.py          price/quality reader imported by the bot",
        "  safety_filter.py       public-channel safety gate (python)",
        "  safety_rules.json      the policy both the bot and the bridge enforce",
        "  category_classifier.py + category_rules.json   shelf routing",
        "  channels_config.py, live_channels.py, hypd_policy.py, source_catalog.json",
        "  post_buttons.py        join buttons on sends",
        "  deploy_bestgaa.sh      verified deploy (tag guard, backup, rollback)",
        "  tg-wa-bridge/*         WhatsApp sender + its own safety policy copy",
        "",
        "The dashboard is NOT in this zip: it runs from the repo checkout, so",
        "`git pull` + `sudo systemctl restart bestgaa-dashboard` updates it.",
        "",
        "SIZES",
        "-----",
        *sizes,
        "",
    ]
    deploy_readme = """BESTGAA DEAL BOT + WHATSAPP BRIDGE — DEPLOY BUNDLE
=================================================

This one file carries everything the services need. Sizes and hashes are in
BUILD_INFO.txt and MANIFEST.sha256; nothing here was hand-copied.

BOT (3 commands on the server)
------------------------------
  cd /home/ubuntu/bestgaa-bot/bestgaa-bot
  unzip -o ~/bestgaa_deploy.zip
  bash deploy_bestgaa.sh

The script refuses a foreign AMAZON_TAG, backs up the running bot, compiles it,
rolls back on failure, verifies the live file by sha256, then restarts.

WHATSAPP BRIDGE (only if you want it updated too)
-------------------------------------------------
  cd /home/ubuntu/tg-wa-bridge
  unzip -o ~/bestgaa_deploy.zip 'tg-wa-bridge/*' -j -d .
  bash install_bridge.sh

tg-wa-bridge/safety_rules.json is the SAME policy file as the bot's, shipped
inside the bridge directory because the bridge reads the policy from its own
folder first.

VERIFY BEFORE YOU DEPLOY ANYTHING
---------------------------------
  unzip -o ~/bestgaa_deploy.zip -d /tmp/bundle_check
  cd /tmp/bundle_check && sha256sum -c MANIFEST.sha256
  python3 -c "import main_bot_new"      # imports deal_facts, safety_filter, ...

DASHBOARD
---------
Not in this zip: it runs from the repo checkout, so
  cd /home/ubuntu/bestgaa-bot/bestgaa-bot && git pull
  sudo systemctl restart bestgaa-dashboard

REBUILD THIS ZIP
----------------
  python3 ops/build_deploy_zip.py            # writes bestgaa_deploy.zip
  python3 ops/build_deploy_zip.py --check     # proves it matches the tree
  python3 test_deploy_zip.py                  # unpacks it and runs the bot offline
"""
    entries.append(("DEPLOY_README.txt", deploy_readme.encode()))
    entries.append(("MANIFEST.sha256", manifest_blob))
    entries.append(("BUILD_INFO.txt", ("\n".join(build_info)).encode()))

    blob = _deterministic_zip(entries)
    if verbose:
        print(f"wrote {ZIP_NAME}: {len(blob)} bytes, {len(entries)} entries")
        for name, data in entries:
            print(f"  {len(data):>8}  {name}")
    return blob


def check(zip_path: str | None = None) -> int:
    """Verify a bundle against the tree. Returns a process exit code."""
    path = zip_path or ZIP_PATH
    try:
        blob = open(path, "rb").read()
    except FileNotFoundError:
        print(f"FAIL: {os.path.basename(path)} is missing; run python3 ops/build_deploy_zip.py")
        return 1
    problems = []
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = set(archive.namelist())
        for required in sorted(REQUIRED):
            if required not in names:
                problems.append(f"missing from the bundle: {required}")
        for archive_name, repo_path in iter_files():
            if archive_name not in names:
                # already reported above when it is a required file
                if archive_name not in REQUIRED:
                    problems.append(f"missing from the bundle: {archive_name} (from {repo_path})")
                continue
            if _sha256(archive.read(archive_name)) != _sha256(_read(repo_path)):
                problems.append(
                    f"stale in the bundle: {archive_name} differs from {repo_path} "
                    "(rebuild: python3 ops/build_deploy_zip.py)")
            # (repo_path is already repo-relative, e.g. bestgaa/category_rules.json)
        for name in sorted(names):
            base = os.path.basename(name)
            if base == ".env" or name.endswith(FORBIDDEN_SUFFIXES):
                problems.append(f"secret/data file in the bundle: {name}")
        manifest = {}
        if "MANIFEST.sha256" in names:
            for line in archive.read("MANIFEST.sha256").decode().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                digest, _, filename = line.partition("  ")
                manifest[filename] = digest
        for name in sorted(names):
            if name in GENERATED:
                continue
            if name not in manifest:
                problems.append(f"not listed in MANIFEST.sha256: {name}")
            elif manifest[name] != _sha256(archive.read(name)):
                problems.append(f"MANIFEST.sha256 disagrees for: {name}")
    problems = list(dict.fromkeys(problems))
    if problems:
        print("DEPLOY BUNDLE CHECK FAILED")
        for problem in problems:
            print("  -", problem)
        return 1
    print(f"deploy bundle OK: {len(names)} entries, every file matches the tree")
    return 0


def main(argv) -> int:
    if "--check" in argv:
        rest = [a for a in argv if a != "--check"]
        if rest and rest[0] == "--zip":
            return check(rest[1])
        if rest:
            raise SystemExit("usage: build_deploy_zip.py [--check [--zip PATH]]")
        return check()
    blob = build()
    with open(ZIP_PATH, "wb") as handle:
        handle.write(blob)
    return check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

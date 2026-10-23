"""A deploy may not claim success without proving it put the new code there.

On 2026-10-23 `bash bestgaa/deploy_bestgaa.sh` printed

    [8/9] Verifying installed code and startup marker...
           live main_bot.py sha256 = cd99cd37...
           bundled main_bot_new.py sha256 = cd99cd37...
    [9/9] DEPLOY SUCCESS (verified: live file == shipped code)

while the checkout held a different build (3df906ef...) - because the script
copied the APP DIRECTORY's own file onto itself and then compared the app
directory with itself. The operator believed the multi-key failover and the
"never join a channel by itself" fix were live; the log still printed
`JOIN failed https://t.me/+...`, which is the exact line that fix removed.

This suite pins the three ways out of that hole:

  1. run from a checkout, the script INSTALLS that checkout and verifies the
     live file against the checkout's bytes (and against git HEAD when clean);
  2. the deploy bundle carries every helper the bot hard-imports - two of them
     (channel_directory.py, post_buttons.py) were missing from the zip list;
  3. the hotfix no longer rewrites keys the dashboard's API-keys panel owns, and
     the long-lived Bitly token is not committed anywhere.
"""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEPLOY = ROOT / "bestgaa" / "deploy_bestgaa.sh"
REPACK = ROOT / "ops" / "repack_bundles.sh"
HOTFIX = ROOT / "ops" / "apply_dual_hotfix.sh"
FAILS = []


def check(name, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_app(prefix):
    """A throwaway app directory that looks like the server's."""
    app = Path(tempfile.mkdtemp(prefix=prefix)) / "app"
    (app / "logs").mkdir(parents=True)
    (app / ".env").write_text(
        "TELEGRAM_API_ID=1234567\n"
        f"TELEGRAM_API_HASH={'b' * 32}\n"
        "EARNKARO_API_KEY=test\n"
        "AMAZON_TAG=mama086-21\n", encoding="utf-8")
    (app / "requirements.txt").write_text("aiohttp\n", encoding="utf-8")
    return app


def run_deploy(app):
    env = dict(os.environ)
    env.update(BESTGAA_APP_DIR=str(app), BESTGAA_DRY_RUN="1", BESTGAA_KEEP_OFF="0")
    env.pop("BESTGAA_SOURCE_DIR", None)
    return subprocess.run(["bash", str(DEPLOY)], capture_output=True, text=True, env=env)


def test_installs_from_the_checkout():
    print("\n== run from a checkout, the checkout is what gets installed ==")
    app = make_app("deploy-stale-")
    (app / "main_bot_new.py").write_text("# the four-hour-old build\n", encoding="utf-8")
    (app / "main_bot.py").write_text("# the four-hour-old build\n", encoding="utf-8")
    proc = run_deploy(app)
    out = proc.stdout + proc.stderr
    check("it says which checkout it is installing from",
          "Installing from the checkout this script was run from" in out, out[:400])
    check("the stale app-directory copy is replaced by the checkout's file",
          sha(app / "main_bot.py") == sha(ROOT / "bestgaa" / "main_bot_new.py"),
          f"live={sha(app / 'main_bot.py')[:16]} repo={sha(ROOT / 'bestgaa/main_bot_new.py')[:16]}")
    check("every hard-imported helper travelled with it",
          all((app / name).exists() for name in (
              "channel_directory.py", "post_buttons.py", "deal_facts.py",
              "channels_config.py", "category_classifier.py", "live_channels.py")),
          str(sorted(p.name for p in app.glob("*.py"))))
    check("the operator's .env is never overwritten by the install",
          "EARNKARO_API_KEY=test" in (app / ".env").read_text(), "")
    check("a deploy that installed the new code says so", proc.returncode == 0, out[-300:])


def test_a_stale_install_cannot_report_success():
    print("\n== 'success' requires the live file to be the checkout's bytes ==")
    text = DEPLOY.read_text(encoding="utf-8")
    check("the script compares the live file with the CHECKOUT, not only with itself",
          'cmp -s "$LIVE_FILE" "$SOURCE_DIR/main_bot_new.py"' in text, "")
    check("it also compares against git HEAD when the checkout is clean",
          'HEAD:bestgaa/main_bot_new.py' in text, "")
    check("a dirty working tree is announced, never silently shipped",
          "UNCOMMITTED changes" in text, "")
    check("the old self-comparison alone can no longer print SUCCESS",
          text.count("cmp -s \"$LIVE_FILE\" \"$NEW_FILE\"") == 1
          and "the install did not take" in text, "")

    # (a) A checkout that is not a git repository has no HEAD to compare with,
    #     so the checkout-vs-live comparison is the one that has to hold.
    # (b) A checkout with uncommitted edits is announced, and the bytes that
    #     were installed are the checkout's, not HEAD's.
    app = make_app("deploy-dirty-")
    fake_repo = Path(tempfile.mkdtemp(prefix="fake-checkout-")) / "bestgaa"
    shutil.copytree(ROOT / "bestgaa", fake_repo,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for cmd in (["git", "init", "-q"], ["git", "add", "bestgaa"],):
        subprocess.run(cmd, cwd=fake_repo.parent, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "committed build"], cwd=fake_repo.parent, check=True)
    edited = (ROOT / "bestgaa" / "main_bot_new.py").read_text(encoding="utf-8") + "\n# edited\n"
    (fake_repo / "main_bot_new.py").write_text(edited, encoding="utf-8")
    env = dict(os.environ)
    env.update(BESTGAA_APP_DIR=str(app), BESTGAA_DRY_RUN="1",
               BESTGAA_SOURCE_DIR=str(fake_repo))
    proc = subprocess.run(["bash", str(DEPLOY)], capture_output=True, text=True, env=env)
    out = proc.stdout + proc.stderr
    check("a checkout with uncommitted edits is announced loudly",
          "UNCOMMITTED changes" in out, out[-400:])
    check("and what it installed is the checkout's bytes, not the commit's",
          sha(app / "main_bot.py") == sha(fake_repo / "main_bot_new.py"), "")

    # (c) An install that cannot land must not reach DEPLOY SUCCESS. cp onto a
    #     directory is the cheapest way to make the copy itself fail.
    app = make_app("deploy-blocked-")
    (app / "main_bot.py").mkdir()
    env = dict(os.environ)
    env.update(BESTGAA_APP_DIR=str(app), BESTGAA_DRY_RUN="1",
               BESTGAA_SOURCE_DIR=str(ROOT / "bestgaa"))
    proc = subprocess.run(["bash", str(DEPLOY)], capture_output=True, text=True, env=env)
    out = proc.stdout + proc.stderr
    check("an install that did not take fails the run",
          proc.returncode != 0 and ("ERROR" in out or "DEPLOY FAILED" in out),
          f"rc={proc.returncode} {out[-200:]}")
    check("and it never claims SUCCESS", "DEPLOY SUCCESS" not in out, "")

    # (d) A checkout missing one helper is refused BEFORE anything is copied,
    #     so the live bot is never left half replaced.
    app = make_app("deploy-incomplete-")
    stale = "# the build that is currently running\n"
    (app / "main_bot.py").write_text(stale, encoding="utf-8")
    (app / "main_bot_new.py").write_text(stale, encoding="utf-8")
    broken = Path(tempfile.mkdtemp(prefix="broken-checkout-")) / "bestgaa"
    shutil.copytree(ROOT / "bestgaa", broken,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (broken / "post_buttons.py").unlink()
    env = dict(os.environ)
    env.update(BESTGAA_APP_DIR=str(app), BESTGAA_DRY_RUN="1", BESTGAA_SOURCE_DIR=str(broken))
    proc = subprocess.run(["bash", str(DEPLOY)], capture_output=True, text=True, env=env)
    out = proc.stdout + proc.stderr
    check("a checkout missing a hard-imported helper is refused",
          proc.returncode != 0 and "refusing a half-install" in out, out[-300:])
    check("and the running bot is left exactly as it was",
          (app / "main_bot.py").read_text() == stale
          and (app / "main_bot_new.py").read_text() == stale, "")


def test_the_bundle_carries_every_helper():
    print("\n== the deploy bundle carries what the bot hard-imports ==")
    repack = REPACK.read_text(encoding="utf-8")
    deploy = DEPLOY.read_text(encoding="utf-8")
    for helper in ("channel_directory.py", "post_buttons.py"):
        check(f"{helper} is in the bundle's file list",
              re.search(r"^\s+.*\b" + re.escape(helper), repack, re.M) is not None, "")
        check(f"{helper} is required by the deploy before it installs", helper in deploy, "")
        compile_loop = next((line for line in deploy.splitlines()
                             if line.startswith("for helper in ")), "")
        check(f"{helper} is compiled before anything is moved",
              helper in compile_loop and "py_compile" in deploy, compile_loop)
    check("the repack refuses to ship a bundle that lost one",
          "did not make it into the bundle" in repack, "")
    check("the tracked bundle builder ships them too",
          all(h in (ROOT / "ops" / "build_deploy_zip.py").read_text() for h in
              ("channel_directory.py", "post_buttons.py")), "")


def test_the_hotfix_never_edits_vault_keys():
    print("\n== a deploy cannot trim the keys the dashboard owns ==")
    hotfix = HOTFIX.read_text(encoding="utf-8")
    check("the hotfix names the keys the vault owns", "VAULT_OWNED" in hotfix, "")
    check("a present value for a vault key is kept", "out.append(line); seen.add(key)" in hotfix, "")
    check("a vault key is never appended over an existing one",
          "key not in VAULT_OWNED" in hotfix, "")
    check("a multi-key EarnKaro list is not replaced by one token",
          "multi-key failover" in hotfix, "")
    check("the bridge's shortener token is seeded from the bot, not hardcoded",
          "WA_BITLY_TOKENS" in hotfix and "seeded WA_BITLY_TOKENS" in hotfix, "")

    # The token that sat in this file until 2026-10-23 must not come back.
    # Assembled from pieces on purpose: written out whole, this test would be
    # the one place the dead token still lives.
    token = "0cb6a376" + "353a7a5ecd93ee07bf0149e2d3f3aa22"
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "node_modules" in path.parts:
            continue
        if path.suffix in {".zip", ".png", ".jpg", ".sqlite3", ".pyc"}:
            continue
        try:
            if token in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(path.relative_to(ROOT)))
        except OSError:
            continue
    check("the old Bitly token is not committed anywhere in the tree",
          not offenders, str(offenders))


def main():
    test_installs_from_the_checkout()
    test_a_stale_install_cannot_report_success()
    test_the_bundle_carries_every_helper()
    test_the_hotfix_never_edits_vault_keys()
    print("\n" + ("DEPLOY TRUTH TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_deploy_truth: all checks PASS"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())

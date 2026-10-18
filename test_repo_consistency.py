"""Repo self-consistency: the gate must cover every test, and README must not lie.

Why this suite exists (2026-10-18). The offline gate was fully green — every
suite passing — while three real drifts sat in the same tree:

  * README's "Current repo source" SHA-256 table had 14 of 18 hashes that no
    longer matched any file, and the `ops/deploy_and_verify.sh` row documented a
    63-character digest that can never match anything. The table exists so an
    operator can prove which build they are reading about; it was proving a build
    that does not exist.
  * README said "GitHub Actions runs this same command on every push and pull
    request" and the repository had no `.github/workflows/` at all.
  * `test_dashboard_ui.cjs`, `test_dashboard_setup.cjs` and
    `test_dashboard_sources.cjs` passed when run by hand but were referenced by
    nothing, so a regression in the dashboard JavaScript the operator actually
    clicks could land with the gate green.

A green gate that does not cover the code and documentation that contradicts the
tree cause the same operator error: trusting a build that is not there. Both are
checked here, and `ops/doc_freshness.py --write` repairs the documentation side
in one command.

Offline and read-only on this repository: the only writes happen inside a
private temp directory. No Telegram, WhatsApp, affiliate or database access.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "ops"))

import doc_freshness as docs  # noqa: E402

GATE = ROOT / "ops" / "test_all.sh"
README = ROOT / "README.md"

CHECKS = 0
PY = sys.executable


def ok(message: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  ok  {message}")


def gate_text() -> str:
    return GATE.read_text(encoding="utf-8")


def tracked() -> list[str]:
    return docs.tracked_files(ROOT)


# ------------------------------------------------------- 1. gate coverage ----
# The failure this prevents: someone adds test_something.py, runs it once by
# hand, and it never enters the gate or CI again.

files = tracked()
test_files = [name for name in files if Path(name).name.startswith("test_")]
assert test_files, "discovery found no test files; tracked_files() is broken"
gate = gate_text()
unwired = [name for name in test_files if Path(name).name not in gate]
assert not unwired, f"test files the gate never runs: {unwired}"
ok(f"all {len(test_files)} test files are wired into ops/test_all.sh")

# Python suites must also be linted, not just executed: the gate's pyflakes step
# uses a glob, so a new ops tool is covered automatically. Assert the globs stay.
assert "ops/*.py" in gate, "the gate stopped syntax-checking ops/*.py"
assert "test_*.py" in gate, "the gate stopped linting test_*.py"
ok("gate keeps the ops/*.py and test_*.py globs (new tools cannot escape lint)")

# The three jsdom suites need NODE_PATH to reach jsdom and PYTHON to reach the
# venv interpreter that serves the dashboard HTML.
assert "export PYTHON" in gate, "the gate must export PYTHON for the DOM suites"
assert "NODE_PATH" in gate, "the gate must point NODE_PATH at a jsdom install"
for name in ("test_dashboard_ui.cjs", "test_dashboard_setup.cjs", "test_dashboard_sources.cjs"):
    assert name in gate, f"the gate lost the DOM suite {name}"
ok("dashboard DOM suites run when jsdom is present, and say so when it is not")
assert "SKIPPED" in gate, "a jsdom-less run must report the skip loudly, not silently"
ok("a jsdom-less run reports the skip instead of passing quietly")

# Shell scripts are deployed to a server and executed as root's neighbour; a
# syntax error there is a failed deploy, so the gate parses every one.
assert "bash -n" in gate, "the gate stopped syntax-checking shell scripts"
ok("the gate syntax-checks every shell script")

# Documentation is part of the gate: hashes, version label and per-suite counts
# are verified against a real run, after the run completes.
assert "doc_freshness.py --check --gate-log" in gate, \
    "the gate no longer verifies README against its own completed log"
assert "_BESTGAA_GATE_INNER" in gate, "the gate lost its self-logging wrapper"
ok("the gate verifies README against its own completed log (no hand-copied counts)")


# ------------------------------------------------- 2. shell scripts parse ----

shell_scripts = [name for name in files if name.endswith(".sh")]
assert shell_scripts, "discovery found no shell scripts"
for name in shell_scripts:
    result = subprocess.run(["bash", "-n", str(ROOT / name)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"{name} does not parse: {result.stderr.strip()}"
ok(f"all {len(shell_scripts)} shell scripts parse cleanly (bash -n)")


# ----------------------------------------------------- 3. CI really exists ----
# README promises GitHub Actions on every push and pull request. GitHub only
# reads .github/workflows/, and that path cannot be pushed by a GitHub App
# installation token without the `workflows` permission (GitHub rejects the whole
# push). So the workflow ships as verified source in ops/ci/ plus an installer,
# and both halves are checked here: a promise with no file behind it is how the
# README came to claim CI that did not exist.

WORKFLOW_SRC = ROOT / "ops" / "ci" / "offline-gate.yml"
WORKFLOW_DEST = ROOT / ".github" / "workflows" / "offline-gate.yml"
INSTALLER = ROOT / "ops" / "install_ci_workflow.sh"

assert WORKFLOW_SRC.exists(), f"missing the shipped CI workflow at {WORKFLOW_SRC}"
workflow = WORKFLOW_SRC.read_text(encoding="utf-8")
for needle, why in [
    ("push", "CI must run on push"),
    ("pull_request", "CI must run on pull requests"),
    ("ops/test_all.sh", "CI must run the same gate an operator runs"),
    ("jsdom", "CI must install jsdom so the DOM suites never skip"),
    ("bestgaa/requirements.txt", "CI must install the Python dependencies"),
    ("tg-wa-bridge", "CI must install the bridge dependencies"),
    ("pyflakes", "CI must turn on the gate's lint step"),
    ("git status --porcelain", "CI must assert the gate left the checkout clean"),
]:
    assert needle in workflow, f"{why} ({needle!r} absent from the workflow)"
ok("the shipped workflow runs the same gate on push and pull_request, with jsdom")
assert "secrets." not in workflow, "the offline gate needs no credentials; CI must not read any"
ok("CI reads no secrets: the gate is offline by contract")

assert INSTALLER.exists(), f"missing {INSTALLER}"
installer = INSTALLER.read_text(encoding="utf-8")
assert ".github/workflows" in installer and "ops/ci/offline-gate.yml" in installer, \
    "the installer must move the shipped workflow to the path GitHub reads"
assert "workflows" in installer and "remote rejected" in installer, \
    "the installer must explain the App-permission rejection it works around"
assert "git push" not in installer.replace("git push        #", ""), \
    "the installer must never push anything itself"
ok("the installer explains the permission wall and never pushes on its own")

# --print is the web-UI path: it must be the file, byte for byte.
printed = subprocess.run(["bash", str(INSTALLER), "--print"], capture_output=True,
                         text=True, check=False)
assert printed.returncode == 0 and printed.stdout == workflow, \
    "--print must emit the shipped workflow verbatim for pasting into the web UI"
ok("installer --print emits the workflow verbatim (the web-UI publish path)")

# --check must agree with reality, whichever way round it is.
checked = subprocess.run(["bash", str(INSTALLER), "--check"], capture_output=True,
                         text=True, check=False)
expected_rc = 0 if WORKFLOW_DEST.exists() else 1
assert checked.returncode == expected_rc, (
    f"installer --check exited {checked.returncode}, expected {expected_rc} "
    f"(installed={WORKFLOW_DEST.exists()})"
)
ok(f"installer --check agrees with the tree (installed={WORKFLOW_DEST.exists()})")

# An installed copy that drifts from the shipped source is the worst case: CI
# would run something README does not describe, and no hash would catch it.
if WORKFLOW_DEST.exists():
    assert WORKFLOW_DEST.read_bytes() == WORKFLOW_SRC.read_bytes(), (
        "installed .github/workflows/offline-gate.yml differs from ops/ci/"
        "offline-gate.yml; re-run: ./ops/install_ci_workflow.sh --force"
    )
    ok("the installed workflow is byte-identical to the shipped, hash-verified source")
else:
    ok("no installed workflow to drift (CI activates when the installer output is pushed)")

# Full installer round trip on a private copy: the repo must not gain a
# .github/ directory as a side effect of being tested.
ci_tmp = Path(tempfile.mkdtemp(prefix="bestgaa-ci-"))
try:
    (ci_tmp / "ops" / "ci").mkdir(parents=True)
    shutil.copy2(INSTALLER, ci_tmp / "ops" / "install_ci_workflow.sh")
    shutil.copy2(WORKFLOW_SRC, ci_tmp / "ops" / "ci" / "offline-gate.yml")
    dest = ci_tmp / ".github" / "workflows" / "offline-gate.yml"

    first = subprocess.run(["bash", str(ci_tmp / "ops" / "install_ci_workflow.sh")],
                           capture_output=True, text=True, check=False)
    assert first.returncode == 0 and dest.exists(), first.stdout + first.stderr
    assert dest.read_bytes() == WORKFLOW_SRC.read_bytes()
    assert "git add .github/workflows/offline-gate.yml" in first.stdout, \
        "the installer must print the exact publish commands"
    ok("installer publishes the workflow and prints the exact publish commands")

    again = subprocess.run(["bash", str(ci_tmp / "ops" / "install_ci_workflow.sh")],
                           capture_output=True, text=True, check=False)
    assert again.returncode == 0 and "already installed" in again.stdout
    ok("re-installing is idempotent")

    dest.write_text("name: something else entirely\n", encoding="utf-8")
    drifted = subprocess.run(["bash", str(ci_tmp / "ops" / "install_ci_workflow.sh"), "--check"],
                             capture_output=True, text=True, check=False)
    assert drifted.returncode == 1, "a drifted installed workflow must fail --check"
    refused = subprocess.run(["bash", str(ci_tmp / "ops" / "install_ci_workflow.sh")],
                             capture_output=True, text=True, check=False)
    assert refused.returncode == 1 and "--force" in refused.stderr, \
        "install must refuse to clobber a modified workflow without --force"
    forced = subprocess.run(["bash", str(ci_tmp / "ops" / "install_ci_workflow.sh"), "--force"],
                            capture_output=True, text=True, check=False)
    assert forced.returncode == 0 and dest.read_bytes() == WORKFLOW_SRC.read_bytes()
    ok("a drifted installed workflow is detected, refused without --force, then repaired")

    bogus = subprocess.run(["bash", str(ci_tmp / "ops" / "install_ci_workflow.sh"), "--nope"],
                           capture_output=True, text=True, check=False)
    assert bogus.returncode == 2, "an unknown installer flag must exit 2, not guess"
    ok("the installer rejects an unknown flag instead of guessing")
finally:
    shutil.rmtree(ci_tmp, ignore_errors=True)

# --------------------------------------- 4. README is true about this tree ----

report = docs.check(ROOT, README, None)
drift = [f for f in report["findings"] if f["severity"] == "error"]
assert report["ok"], (
    "README no longer describes this tree; run: python3 ops/doc_freshness.py --write\n"
    + "\n".join(f"    [{f['check']}] {f.get('path', '')}: {f['detail']}" for f in drift)
)
ok(f"README proof table matches the tree ({report['hashes_verified']} hashes verified)")
assert report["files_tracked"] >= 50, \
    f"only {report['files_tracked']} files tracked; discovery is under-reporting"
ok(f"the proof table covers the whole code+gate surface ({report['files_tracked']} files)")
skipped = [f for f in report["findings"] if f["severity"] == "skip"]
assert skipped and skipped[0]["check"] == "suite_totals", \
    "without a gate log the suite-count check must report 'skip', never a silent pass"
ok("suite counts report SKIPPED without a gate log (never a silent pass)")


# ------------------------------- 5. doc_freshness round trip (private temp) ----
# The repair path must actually work, or the check above is a dead end that
# blocks a deploy with no way forward.

MINI_README = """# synthetic mini repo

Matches what ran on the Oracle server after a deploy.

| Artifact | SHA-256 |
|---|---|
| Hotfix zip (`archive/mini_hotfix.zip`) | `{zip_hash}` |
| `bestgaa/main_bot_new.py` (= server `main_bot.py`) | `deadbeef` |

Current **repo source** on this branch (v9.9 - synthetic).
Hashes below are refreshed for this source snapshot:

| File | SHA-256 |
|---|---|
| `bestgaa/main_bot_new.py` (v9.9) | `{bot_hash}` |

<!-- gate-suite-totals:begin -->
<!-- generated by ops/doc_freshness.py --write from a real gate run;
     do not edit by hand -->
| Suite | Checks passed | Failed |
|---|---|---|
| render and routing | 1 | 0 |
<!-- gate-suite-totals:end -->
"""

MINI_BOT = '"""BestGAA Production Bot v9.9\n\nSynthetic module for the doc round trip.\n"""\n'
MINI_LOG = """
===== render and routing =====
  ok  something
RESULT: 232 passed, 0 failed

===== pipeline fixes =====
RESULT: 238 passed, 0 failed
"""

tmp = Path(tempfile.mkdtemp(prefix="bestgaa-docfresh-"))
try:
    mini = tmp / "mini"
    (mini / "bestgaa").mkdir(parents=True)
    (mini / "ops").mkdir(parents=True)
    (mini / "archive").mkdir(parents=True)
    (mini / "bestgaa" / "main_bot_new.py").write_text(MINI_BOT, encoding="utf-8")
    (mini / "ops" / "tool.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    (mini / "test_demo.py").write_text("print('demo PASS')\n", encoding="utf-8")
    (mini / "archive" / "mini_hotfix.zip").write_bytes(b"PK\x03\x04synthetic")

    zip_hash = docs.sha256_of(mini / "archive" / "mini_hotfix.zip") or ""
    bot_hash = "0" * 64  # deliberately wrong: --write must fix it
    (mini / "README.md").write_text(
        MINI_README.format(zip_hash=zip_hash, bot_hash=bot_hash), encoding="utf-8"
    )

    before = docs.check(mini, mini / "README.md", None)
    assert not before["ok"], "a wrong hash must fail the check"
    assert any(f.get("path") == "bestgaa/main_bot_new.py" for f in before["findings"]), \
        "the finding must name the drifted file"
    ok("a wrong documented hash fails the check and names the file")

    # --write repairs it, and discovers files the table never mentioned.
    changes = docs.rewrite(mini, mini / "README.md", None)
    assert changes, "--write reported no changes on a drifted README"
    after = docs.check(mini, mini / "README.md", None)
    assert after["ok"], f"--write did not repair the README: {after['findings']}"
    text = (mini / "README.md").read_text(encoding="utf-8")
    for name in ("ops/tool.sh", "test_demo.py"):
        assert name in text, f"--write dropped an undocumented file: {name}"
    ok(f"--write repaired the hash and added {len(changes)} generation(s), incl. new files")

    # A new source file the table does not know about is drift, not a shrug.
    (mini / "bestgaa" / "fresh_module.py").write_text("X = 1\n", encoding="utf-8")
    drifted = docs.check(mini, mini / "README.md", None)
    assert not drifted["ok"], "a new file must fail the check until --write adds it"
    assert any(f.get("path") == "bestgaa/fresh_module.py" for f in drifted["findings"])
    docs.rewrite(mini, mini / "README.md", None)
    assert docs.check(mini, mini / "README.md", None)["ok"]
    ok("a newly added module is drift until --write documents it")

    # Editing a byte of a tracked file changes its hash: the table must notice.
    (mini / "test_demo.py").write_text("print('demo PASS v2')\n", encoding="utf-8")
    edited = docs.check(mini, mini / "README.md", None)
    assert not edited["ok"], "editing a tracked file must invalidate its documented hash"
    docs.rewrite(mini, mini / "README.md", None)
    assert docs.check(mini, mini / "README.md", None)["ok"]
    ok("editing one byte of a tracked file invalidates its documented hash")

    # The malformed-digest case actually shipped in README.
    broken = (mini / "README.md").read_text(encoding="utf-8").replace(
        docs.sha256_of(mini / "test_demo.py") or "@", "abc123", 1
    )
    (mini / "README.md").write_text(broken, encoding="utf-8")
    malformed = docs.check(mini, mini / "README.md", None)
    assert not malformed["ok"], "a malformed digest must fail"
    assert any("not a SHA-256" in f["detail"] for f in malformed["findings"]), \
        "a short digest must be reported as malformed, not merely 'missing'"
    ok("a 63-char digest is reported as malformed (the bug README actually shipped)")

    # Version label must follow the bot, not the other way round.
    docs.rewrite(mini, mini / "README.md", None)
    (mini / "bestgaa" / "main_bot_new.py").write_text(
        MINI_BOT.replace("v9.9", "v10.0"), encoding="utf-8"
    )
    versioned = docs.check(mini, mini / "README.md", None)
    assert any(f["check"] == "bot_version" for f in versioned["findings"]), \
        "a version bump in the bot must invalidate README's version label"
    docs.rewrite(mini, mini / "README.md", None)
    assert "v10.0" in (mini / "README.md").read_text(encoding="utf-8")
    assert docs.check(mini, mini / "README.md", None)["ok"]
    ok("README's version label follows the version the bot prints at startup")

    # Suite counts come from a real gate log, and are verified against it.
    log = tmp / "gate.log"
    log.write_text(MINI_LOG, encoding="utf-8")
    suites = docs.parse_gate_log(log)
    assert suites == {"render and routing": {"passed": 232, "failed": 0},
                      "pipeline fixes": {"passed": 238, "failed": 0}}, suites
    docs.rewrite(mini, mini / "README.md", log)
    counted = docs.check(mini, mini / "README.md", log)
    assert counted["ok"], f"suite counts did not verify: {counted['findings']}"
    ok("--write regenerates per-suite counts from a real gate log")

    tampered = (mini / "README.md").read_text(encoding="utf-8").replace("| 232 |", "| 211 |", 1)
    (mini / "README.md").write_text(tampered, encoding="utf-8")
    stale_count = docs.check(mini, mini / "README.md", log)
    assert not stale_count["ok"], "a hand-edited suite count must fail against the log"
    assert any(f["check"] == "suite_totals" for f in stale_count["findings"])
    ok("a hand-copied suite count fails against the log that produced it (the '211' bug)")

    failing_log = tmp / "failing.log"
    failing_log.write_text("===== broken suite =====\nRESULT: 4 passed, 2 failed\n",
                           encoding="utf-8")
    failed_run = docs.check(mini, mini / "README.md", failing_log)
    assert not failed_run["ok"], "a log containing failed checks must not verify as fresh"
    assert any("failed checks" in f["detail"] for f in failed_run["findings"])
    ok("a gate log with failures can never be laundered into fresh documentation")

    # CLI contract: --check exits 1, --write exits 0, --json is machine-readable.
    docs.rewrite(mini, mini / "README.md", log)
    env = dict(os.environ)
    cli_check = subprocess.run(
        [PY, str(ROOT / "ops" / "doc_freshness.py"), "--repo", str(mini),
         "--readme", str(mini / "README.md"), "--gate-log", str(log), "--check"],
        capture_output=True, text=True, check=False, env=env, cwd=str(tmp),
    )
    assert cli_check.returncode == 0, cli_check.stdout + cli_check.stderr
    ok("CLI --check exits 0 on a fresh README")

    (mini / "test_demo.py").write_text("print('demo PASS v3')\n", encoding="utf-8")
    cli_drift = subprocess.run(
        [PY, str(ROOT / "ops" / "doc_freshness.py"), "--repo", str(mini),
         "--readme", str(mini / "README.md"), "--check"],
        capture_output=True, text=True, check=False, env=env, cwd=str(tmp),
    )
    assert cli_drift.returncode == 1, "CLI --check must exit 1 on drift"
    assert "doc_freshness.py --write" in cli_drift.stdout, \
        "the failure must print the one-command fix"
    ok("CLI --check exits 1 on drift and prints the one-command fix")

    cli_json = subprocess.run(
        [PY, str(ROOT / "ops" / "doc_freshness.py"), "--repo", str(mini),
         "--readme", str(mini / "README.md"), "--check", "--json"],
        capture_output=True, text=True, check=False, env=env, cwd=str(tmp),
    )
    import json

    payload = json.loads(cli_json.stdout)
    assert payload["ok"] is False and payload["findings"], payload
    ok("CLI --json is machine-readable for CI annotations")

    cli_write = subprocess.run(
        [PY, str(ROOT / "ops" / "doc_freshness.py"), "--repo", str(mini),
         "--readme", str(mini / "README.md"), "--write"],
        capture_output=True, text=True, check=False, env=env, cwd=str(tmp),
    )
    assert cli_write.returncode == 0, cli_write.stdout + cli_write.stderr
    assert docs.check(mini, mini / "README.md", None)["ok"]
    ok("CLI --write repairs the README and re-verifies it in the same call")

    # --write must never touch anything but the README.
    (mini / "bestgaa" / "main_bot_new.py").write_text(MINI_BOT, encoding="utf-8")
    bot_before = docs.sha256_of(mini / "bestgaa" / "main_bot_new.py")
    docs.rewrite(mini, mini / "README.md", None)
    assert docs.sha256_of(mini / "bestgaa" / "main_bot_new.py") == bot_before
    ok("--write leaves source bytes untouched (README is the only file it may change)")

    # A README with no proof table is an error, not a pass.
    bare = tmp / "bare" / "README.md"
    bare.parent.mkdir(parents=True)
    bare.write_text("# nothing to verify here\n", encoding="utf-8")
    bare_report = docs.check(mini, bare, None)
    assert not bare_report["ok"], "a README without the proof table must fail"
    ok("a README with no proof table fails instead of passing vacuously")
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------- 6. every tracked file is described ----
# Not "does its name appear somewhere": the generated proof table lists every
# tracked path, so that question answers itself. prose_only() hides the generated
# blocks, which makes this a real documentation-completeness check — an operator
# reading README can find out what every script, module and suite is for.

prose = docs.prose_only(README)
undescribed = [name for name in files if Path(name).name not in prose]
assert not undescribed, (
    "tracked files README never describes (add them to the layout, the ops "
    f"inventory, or the suite table): {undescribed}"
)
ok(f"all {len(files)} tracked code/gate files are described in README prose")

ops_files = [name for name in files if name.startswith("ops/")]
assert len(ops_files) >= 20, f"only {len(ops_files)} ops files discovered"
ok(f"the ops inventory covers all {len(ops_files)} operator scripts, tools and notes")


# --------------------------- 7. the deploy path proves itself the same way ----
# A deploy used to run a hardcoded list of 8 suites while the gate had 32
# sections, so most of the regression coverage never ran before shipping. These
# assertions keep the two paths welded together.

deploy = (ROOT / "ops" / "deploy_and_verify.sh").read_text(encoding="utf-8")
assert "ops/test_all.sh" in deploy, "the deploy stopped running the full offline gate"
assert "GATE_DOC_MODE=warn" in deploy, \
    "a deploy must report stale docs, never be blocked by them (GATE_DOC_MODE=warn)"
assert "GATE_RAN" in deploy, "the deploy lost its full-gate-then-fallback structure"
ok("a deploy proves itself with the same gate CI runs, then falls back if it cannot")
assert "DOCUMENTATION DRIFT" in deploy, \
    "the deploy must surface README drift to the operator instead of burying it in a log"
ok("the deploy surfaces README drift rather than burying it in /tmp")

modes = gate_text()
for mode in ("GATE_DOC_MODE", "check|warn|off"):
    assert mode in modes, f"the gate lost its documentation-drift policy ({mode})"
assert 'GATE_DOC_MODE must be one of' in modes, \
    "a typo'd GATE_DOC_MODE must fail loudly, not silently downgrade the check"
ok("GATE_DOC_MODE accepts check/warn/off and rejects a typo instead of ignoring it")


# ------------------------------------------------- 8. secret hygiene ----------
# The gate runs in CI on a public-ish runner; a committed credential would be
# published by the very workflow meant to protect the build.

ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
for pattern in (".env", "auth/", "*.session", "ops/.earnkaro_key", "node_modules/"):
    assert pattern in ignore, f".gitignore lost {pattern}"
ok(".gitignore still excludes .env, auth/, sessions and the EarnKaro token file")

leaks = []
for name in files:
    base = Path(name).name
    if base == ".env" or base.endswith(".session") or base == ".earnkaro_key":
        leaks.append(name)
assert not leaks, f"credential files are tracked: {leaks}"
ok("no credential file is tracked on the code+gate surface")

# The one JWT in the tree is a documented fake (claims only, bogus signature).
fixture = (ROOT / "test_earnkaro_conversion.py").read_text(encoding="utf-8")
assert "not-a-real-signature-only-the-claims-matter" in fixture, \
    "the EarnKaro test token must stay the obvious fake, never a real one"
ok("the only JWT in the tree is the deliberate fake (no live token committed)")


print(f"\ntest_repo_consistency PASS: {CHECKS} checks - the gate covers every test file, "
      "every shell script parses, CI exists and runs the same gate, and README's "
      "proof tables match the bytes on disk.")

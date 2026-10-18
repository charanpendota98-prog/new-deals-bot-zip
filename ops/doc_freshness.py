#!/usr/bin/env python3
"""Keep README's proof tables true, or fail loudly.

The README publishes SHA-256 hashes so an operator can answer "is the build I
read about the build I have?". Until now nothing checked them, and 14 of the 18
documented hashes had silently drifted from the working tree (found 2026-10-18):
the document that exists to prove the source was describing bytes that no longer
exist anywhere. A stale proof table is worse than none, because it is trusted.

This tool makes those tables generated facts instead of hand-copied claims.

    # verify (exit 1 on any drift) - the offline gate runs this
    python3 ops/doc_freshness.py --check

    # also verify the per-suite check counts against a real gate run
    python3 ops/doc_freshness.py --check --gate-log /tmp/gate.log

    # refresh README in place after any source change (one command, no editing)
    python3 ops/doc_freshness.py --write

    # machine-readable, for CI annotations / the dashboard
    python3 ops/doc_freshness.py --check --json

What it verifies
  1. repo source table  every code/test file in the tree appears in the
     "Current **repo source**" `| File | SHA-256 |` table with its real hash.
     The file list is discovered from the tree (git when available), so a new
     module cannot be left undocumented.
  2. bot version        the "(vNN.N)" in that paragraph equals the version the
     bot itself prints when it starts.
  3. deployed zip       the committed `archive/` hotfix-zip row still matches
     the committed zip. The two "(= server ...)" rows are deliberately left
     alone: they describe an older deployed build, not this tree.
  4. suite totals       the managed `gate-suite-totals` block equals the
     `RESULT: N passed` counts parsed from a real gate log (only with
     --gate-log; without one this check is reported as skipped, not passed).

Read-only by default. --write rewrites README.md and nothing else; it never
touches source, config, a database, or a service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = OPS_DIR.parent

# ---------------------------------------------------------------- discovery --

# Groups in table order. Anything matching a pattern is tracked; the patterns
# are deliberately the code + gate surface, not prose or runtime state.
TRACKED_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # *.sh is included on purpose: these are the deploy/install/number-switch
    # scripts that run on the production host, so their exact bytes belong in the
    # proof table alongside the services they install.
    ("bestgaa", ("bestgaa/*.py", "bestgaa/*.json", "bestgaa/*.sh")),
    ("tg-wa-bridge", ("tg-wa-bridge/*.py", "tg-wa-bridge/*.js",
                      "tg-wa-bridge/*.json", "tg-wa-bridge/*.sh")),
    ("dashboard", ("dashboard/*.py",)),
    # ops/ci/*.yml is the CI workflow as SHIPPED. The installed copy under
    # .github/workflows/ is deliberately not listed: a GitHub App token cannot
    # push it, so whether it exists differs between checkouts, and a proof table
    # that changes shape with the machine it is read on proves nothing. The
    # consistency suite instead requires any installed copy to be byte-identical
    # to this one.
    ("ops", ("ops/*.py", "ops/*.sh", "ops/ci/*.yml")),
    ("tests", ("test_*.py", "test_*.mjs", "test_*.cjs")),
)

# Files that are copied verbatim between the two services; the gate already
# proves they are byte-identical with cmp, so hashing both is noise-free but
# listing only one would hide a half-applied edit. Keep both.
EXCLUDE_NAMES: frozenset[str] = frozenset()


def _git_tracked(repo: Path) -> list[str] | None:
    """Paths git knows about, or None when git/the repo is unavailable.

    --others --exclude-standard matters: a brand-new suite or tool is untracked
    until someone commits it, and "the gate must cover every test" is exactly the
    check that has to see it *before* it is committed. Ignored build output stays
    out.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.split("\0") if p]


def tracked_files(repo: Path) -> list[str]:
    """Every code/gate file in the tree, in stable table order.

    Uses git when this is a checkout (so ignored build output never sneaks in)
    and falls back to the filesystem for a plain zip/tarball copy.
    """
    git = _git_tracked(repo)
    rows: list[tuple[int, str]] = []
    for group_index, (_group, patterns) in enumerate(TRACKED_GROUPS):
        if git is not None:
            import fnmatch

            names = [
                path for path in git
                if any(fnmatch.fnmatch(path, pat) for pat in patterns)
            ]
        else:
            found: set[str] = set()
            for pat in patterns:
                for path in repo.glob(pat):
                    if path.is_file():
                        found.add(str(path.relative_to(repo)))
            names = sorted(found)
        for name in names:
            if Path(name).name in EXCLUDE_NAMES:
                continue
            rows.append((group_index, name))
    rows.sort(key=lambda row: (row[0], row[1]))
    # de-duplicate while keeping order (a file can match two patterns)
    seen: set[str] = set()
    unique: list[str] = []
    for _group_index, name in rows:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def bot_version(repo: Path) -> str | None:
    """The version the bot prints at startup, e.g. 'v19.0'."""
    source = repo / "bestgaa" / "main_bot_new.py"
    try:
        head = source.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    match = re.search(r"BestGAA Production Bot (v\d+(?:\.\d+)*)", head)
    return match.group(1) if match else None


# ------------------------------------------------------------------- readme --

REPO_SOURCE_ANCHOR = "Current **repo source**"
FILE_TABLE_HEADER = "| File | SHA-256 |"
ARTIFACT_TABLE_HEADER = "| Artifact | SHA-256 |"
TOTALS_BEGIN = "<!-- gate-suite-totals:begin -->"
TOTALS_END = "<!-- gate-suite-totals:end -->"
# The version sits in a long parenthetical ("(v19.0 - optional category-wise
# ... )"), so only the opening paren is anchored; the closing one is prose.
VERSION_RE = re.compile(
    r"(Current \*\*repo source\*\* on this branch \()(v\d+(?:\.\d+)*)"
)


def _path_from_cell(cell: str) -> str | None:
    """`bestgaa/main_bot_new.py (v19.0)` -> bestgaa/main_bot_new.py."""
    for span in re.findall(r"`([^`]+)`", cell):
        candidate = re.sub(r"\s*\([^)]*\)\s*$", "", span).strip()
        if "/" in candidate or candidate.endswith(
            (".py", ".sh", ".js", ".mjs", ".cjs", ".json", ".yml", ".zip")
        ):
            return candidate
    return None


def _hash_from_cell(cell: str) -> str | None:
    match = re.search(r"`([0-9a-fA-F]{64})`", cell)
    return match.group(1).lower() if match else None


def _table_rows(lines: list[str], header: str, start: int = 0) -> tuple[int, int] | None:
    """Return (header_index, first_index_after_table) for `header` at/after start."""
    try:
        head = next(i for i in range(start, len(lines)) if lines[i].strip() == header)
    except StopIteration:
        return None
    end = head + 1
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    return head, end


def read_readme(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def prose_only(readme: Path) -> str:
    """README with the machine-generated blocks blanked out.

    The generated proof table lists every tracked file by path, so "is this file
    documented anywhere?" is trivially true once --write has run. Anything that
    wants to know whether a file is actually *described* must ask against this
    text instead.
    """
    lines = read_readme(readme)
    hidden: set[int] = set()
    span = parse_repo_source_table(lines)
    if span is not None:
        hidden.update(range(span[0], span[1]))
    totals = parse_totals_block(lines)
    if totals is not None:
        hidden.update(range(totals[0], totals[1] + 1))
    return "\n".join("" if i in hidden else line for i, line in enumerate(lines))



def parse_repo_source_table(
    lines: list[str],
) -> tuple[int, int, dict[str, str], list[tuple[str, str]]] | None:
    """(header_index, end_index, {path: hash}, [(path, bad_hash)]) for the table.

    The fourth element catches rows whose documented digest is not a SHA-256 at
    all. README shipped one of these for `ops/deploy_and_verify.sh` (63 hex
    chars, found 2026-10-18): a hash that can never match anything, so the row
    looked like proof while being impossible to check.
    """
    try:
        anchor = next(
            i for i, line in enumerate(lines) if REPO_SOURCE_ANCHOR in line
        )
    except StopIteration:
        return None
    span = _table_rows(lines, FILE_TABLE_HEADER, anchor)
    if span is None:
        return None
    head, end = span
    documented: dict[str, str] = {}
    malformed: list[tuple[str, str]] = []
    for line in lines[head + 2:end]:  # skip header + |---|---|
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        path = _path_from_cell(cells[0])
        if not path:
            continue
        digest = _hash_from_cell(cells[1])
        if digest:
            documented[path] = digest
        else:
            malformed.append((path, cells[1].strip("` ")))
    return head, end, documented, malformed


def parse_archive_row(lines: list[str]) -> tuple[int, str, str] | None:
    """(line_index, path, documented_hash) for the committed hotfix zip row."""
    span = _table_rows(lines, ARTIFACT_TABLE_HEADER)
    if span is None:
        return None
    head, end = span
    for index in range(head + 2, end):
        cells = [c.strip() for c in lines[index].strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        path = _path_from_cell(cells[0])
        digest = _hash_from_cell(cells[1])
        if path and digest and path.startswith("archive/") and "= server" not in cells[0]:
            return index, path, digest
    return None


def parse_totals_block(lines: list[str]) -> tuple[int, int, dict[str, int]] | None:
    try:
        begin = next(i for i, line in enumerate(lines) if TOTALS_BEGIN in line)
        end = next(i for i, line in enumerate(lines) if TOTALS_END in line)
    except StopIteration:
        return None
    if end <= begin:
        return None
    counts: dict[str, int] = {}
    for line in lines[begin + 1:end]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[1].isdigit():
            continue
        counts[cells[0].strip("`")] = int(cells[1])
    return begin, end, counts


# --------------------------------------------------------------- gate log --

SUITE_HEADER_RE = re.compile(r"^===== (.+?) =====\s*$")
RESULT_RE = re.compile(r"^RESULT:\s*(\d+)\s+passed,\s*(\d+)\s+failed")


def parse_gate_log(path: Path) -> dict[str, dict[str, int]]:
    """{suite: {'passed': N, 'failed': M}} for suites that print a RESULT line."""
    suites: dict[str, dict[str, int]] = {}
    current: str | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        header = SUITE_HEADER_RE.match(raw)
        if header:
            current = header.group(1).strip()
            continue
        result = RESULT_RE.match(raw)
        if result and current:
            suites[current] = {
                "passed": int(result.group(1)),
                "failed": int(result.group(2)),
            }
    return suites


# ------------------------------------------------------------------ writers --

def render_source_table(repo: Path, files: list[str]) -> list[str]:
    version = bot_version(repo)
    lines = [FILE_TABLE_HEADER, "|---|---|"]
    for name in files:
        digest = sha256_of(repo / name) or "MISSING-FILE"
        label = f"`{name}`"
        if name == "bestgaa/main_bot_new.py" and version:
            label = f"`{name}` ({version})"
        lines.append(f"| {label} | `{digest}` |")
    return lines


def render_totals_block(suites: dict[str, dict[str, int]]) -> list[str]:
    lines = [
        TOTALS_BEGIN,
        "<!-- generated by ops/doc_freshness.py --write from a real gate run;",
        "     do not edit by hand -->",
        "| Suite | Checks passed | Failed |",
        "|---|---|---|",
    ]
    for name in sorted(suites):
        lines.append(f"| {name} | {suites[name]['passed']} | {suites[name]['failed']} |")
    lines.append(TOTALS_END)
    return lines


def rewrite(repo: Path, readme: Path, gate_log: Path | None) -> list[str]:
    """Apply every generated block to README.md; return the change list."""
    lines = read_readme(readme)
    changes: list[str] = []
    files = tracked_files(repo)

    span = parse_repo_source_table(lines)
    if span is None:
        raise SystemExit(
            f"ERROR: {readme} has no '{REPO_SOURCE_ANCHOR}' + '{FILE_TABLE_HEADER}' "
            "table to refresh."
        )
    head, end, _documented, _malformed = span
    generated = render_source_table(repo, files)
    if lines[head:end] != generated:
        lines[head:end] = generated
        changes.append(f"repo source table regenerated ({len(files)} files)")

    version = bot_version(repo)
    if version:
        joined = "\n".join(lines)
        replaced, count = VERSION_RE.subn(rf"\g<1>{version}", joined, count=1)
        if count and replaced != joined:
            lines = replaced.splitlines()
            changes.append(f"branch version label -> {version}")

    archive = parse_archive_row(lines)
    if archive:
        index, path, digest = archive
        actual = sha256_of(repo / path)
        if actual and actual != digest:
            lines[index] = lines[index].replace(digest, actual)
            changes.append(f"archive zip hash refreshed ({path})")

    if gate_log is not None:
        suites = parse_gate_log(gate_log)
        if not suites:
            raise SystemExit(
                f"ERROR: no 'RESULT: N passed' suite lines found in {gate_log}; "
                "is it a complete ops/test_all.sh log?"
            )
        block = render_totals_block(suites)
        found = parse_totals_block(lines)
        if found is None:
            raise SystemExit(
                f"ERROR: {readme} has no {TOTALS_BEGIN} block to refresh; "
                "add the managed block first (see the tool docstring)."
            )
        begin, stop, _counts = found
        if lines[begin:stop + 1] != block:
            lines[begin:stop + 1] = block
            changes.append(f"suite totals regenerated from {gate_log.name}")

    if changes:
        readme.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changes


# ------------------------------------------------------------------- checks --

def check(repo: Path, readme: Path, gate_log: Path | None) -> dict:
    """Return {'ok': bool, 'findings': [...], 'counts': {...}}."""
    lines = read_readme(readme)
    findings: list[dict] = []
    files = tracked_files(repo)

    span = parse_repo_source_table(lines)
    if span is None:
        findings.append({
            "check": "repo_source_table", "severity": "error",
            "detail": f"no '{REPO_SOURCE_ANCHOR}' + '{FILE_TABLE_HEADER}' table found",
        })
        documented: dict[str, str] = {}
        malformed: list[tuple[str, str]] = []
    else:
        documented = span[2]
        malformed = span[3]

    for path, raw in malformed:
        findings.append({
            "check": "repo_source_table", "severity": "error", "path": path,
            "detail": (
                f"documented digest is not a SHA-256 ({len(raw)} chars: '{raw[:20]}...'); "
                "it can never match any file"
            ),
        })

    bad_paths = {path for path, _raw in malformed}
    missing = [n for n in files if n not in documented and n not in bad_paths]
    extra = [name for name in documented if name not in files]
    stale: list[dict] = []
    for name in files:
        if name not in documented:
            continue
        actual = sha256_of(repo / name)
        if actual is None:
            stale.append({"path": name, "documented": documented[name], "actual": "MISSING"})
        elif actual != documented[name]:
            stale.append({"path": name, "documented": documented[name], "actual": actual})

    for name in missing:
        findings.append({
            "check": "repo_source_table", "severity": "error", "path": name,
            "detail": "tracked in the tree but absent from the README source table",
        })
    for name in extra:
        findings.append({
            "check": "repo_source_table", "severity": "error", "path": name,
            "detail": "listed in the README source table but no longer in the tree",
        })
    for row in stale:
        findings.append({
            "check": "repo_source_table", "severity": "error", "path": row["path"],
            "documented": row["documented"][:12], "actual": row["actual"][:12],
            "detail": "documented SHA-256 does not match the file on disk",
        })

    version = bot_version(repo)
    match = VERSION_RE.search("\n".join(lines))
    if version is None:
        findings.append({
            "check": "bot_version", "severity": "error",
            "detail": "could not read a version from bestgaa/main_bot_new.py",
        })
    elif match is None:
        findings.append({
            "check": "bot_version", "severity": "error",
            "detail": f"README does not state a branch version (expected {version})",
        })
    elif match.group(2) != version:
        findings.append({
            "check": "bot_version", "severity": "error",
            "documented": match.group(2), "actual": version,
            "detail": "README branch version != the version the bot prints at startup",
        })

    archive = parse_archive_row(lines)
    if archive is None:
        findings.append({
            "check": "archive_zip", "severity": "warn",
            "detail": "no committed archive/ zip row found to verify",
        })
    else:
        _index, path, digest = archive
        actual = sha256_of(repo / path)
        if actual is None:
            findings.append({
                "check": "archive_zip", "severity": "error", "path": path,
                "detail": "README documents an archive zip that is not in the tree",
            })
        elif actual != digest:
            findings.append({
                "check": "archive_zip", "severity": "error", "path": path,
                "documented": digest[:12], "actual": actual[:12],
                "detail": "committed hotfix zip hash does not match the README row",
            })

    counts: dict[str, dict[str, int]] = {}
    if gate_log is None:
        findings.append({
            "check": "suite_totals", "severity": "skip",
            "detail": "no --gate-log supplied; per-suite counts not verified",
        })
    else:
        counts = parse_gate_log(gate_log)
        if not counts:
            findings.append({
                "check": "suite_totals", "severity": "error", "path": str(gate_log),
                "detail": "log has no 'RESULT: N passed' suite lines",
            })
        else:
            found = parse_totals_block(lines)
            if found is None:
                findings.append({
                    "check": "suite_totals", "severity": "error",
                    "detail": f"README has no {TOTALS_BEGIN} managed block",
                })
            else:
                documented_counts = found[2]
                expected = {name: counts[name]["passed"] for name in counts}
                if documented_counts != expected:
                    for name in sorted(set(expected) | set(documented_counts)):
                        if documented_counts.get(name) != expected.get(name):
                            findings.append({
                                "check": "suite_totals", "severity": "error", "suite": name,
                                "documented": documented_counts.get(name),
                                "actual": expected.get(name),
                                "detail": "README suite count != the count this gate run printed",
                            })
                failed = {n: c["failed"] for n, c in counts.items() if c["failed"]}
                if failed:
                    findings.append({
                        "check": "suite_totals", "severity": "error",
                        "detail": f"gate log reports failed checks: {failed}",
                    })

    errors = [f for f in findings if f["severity"] == "error"]
    return {
        "ok": not errors,
        "readme": str(readme),
        "files_tracked": len(files),
        "hashes_verified": len(files) - len(missing),
        "findings": findings,
        "suite_counts": counts,
    }


def print_human(report: dict) -> None:
    print(
        "DOC FRESHNESS | readme={} tracked-files={} hashes-verified={}".format(
            report["readme"], report["files_tracked"], report["hashes_verified"],
        )
    )
    for suite, counts in sorted(report["suite_counts"].items()):
        print(f"  SUITE {suite}: {counts['passed']} passed, {counts['failed']} failed")
    if not report["findings"]:
        print("  every documented hash, version label and suite count matches this tree")
        return
    for finding in report["findings"]:
        tag = finding["severity"].upper()
        where = finding.get("path") or finding.get("suite") or finding["check"]
        print(f"  {tag:5} [{finding['check']}] {where}: {finding['detail']}")
        if "documented" in finding:
            print(f"          documented={finding['documented']} actual={finding['actual']}")
    if not report["ok"]:
        print(
            "\n  FIX: python3 ops/doc_freshness.py --write"
            "   (add --gate-log <log> right after a full ops/test_all.sh run"
            " to refresh the suite counts too)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                        help="repository root (default: the checkout this file lives in)")
    parser.add_argument("--readme", type=Path, default=None,
                        help="README to verify (default: <repo>/README.md)")
    parser.add_argument("--gate-log", type=Path, default=None,
                        help="a complete ops/test_all.sh log; verifies per-suite counts")
    parser.add_argument("--write", action="store_true",
                        help="refresh README.md in place (never touches source or services)")
    parser.add_argument("--check", action="store_true",
                        help="verify only, exit 1 on drift (default when --write is absent)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    readme = (args.readme or repo / "README.md").resolve()
    if not readme.exists():
        print(f"ERROR: {readme} not found", file=sys.stderr)
        return 2

    if args.write:
        changes = rewrite(repo, readme, args.gate_log)
        if args.json:
            print(json.dumps({"written": str(readme), "changes": changes}, indent=2))
        elif changes:
            print(f"DOC FRESHNESS | refreshed {readme}")
            for change in changes:
                print(f"  - {change}")
        else:
            print(f"DOC FRESHNESS | {readme} already matches this tree")
        report = check(repo, readme, args.gate_log)
        if not report["ok"]:
            print_human(report)
            return 1
        return 0

    report = check(repo, readme, args.gate_log)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
# ============================================================================
# Install the offline-gate CI workflow: ops/ci/offline-gate.yml -> .github/workflows/
#
#   ./ops/install_ci_workflow.sh            # install (or verify) the workflow
#   ./ops/install_ci_workflow.sh --print     # print it, for the GitHub web UI
#   ./ops/install_ci_workflow.sh --force     # overwrite a modified installed copy
#   ./ops/install_ci_workflow.sh --check     # exit 1 unless installed & identical
#
# WHY THIS IS TWO STEPS
#   The source of truth is `ops/ci/offline-gate.yml`, which is hash-verified in
#   README's proof table like every other file. Installing it writes
#   `.github/workflows/offline-gate.yml`, and *that* path cannot be pushed by a
#   GitHub App installation token without the `workflows` permission - GitHub
#   refuses with:
#
#     ! [remote rejected] (refusing to allow a GitHub App to create or update
#        workflow `.github/workflows/offline-gate.yml` without `workflows`
#        permission)
#
#   So an agent/automation checkout can prepare everything but a human (or a
#   token with `workflows`) has to publish it. Two ways:
#
#     A) after this script:  git add .github/workflows/offline-gate.yml
#                            git commit -m "ci: run the offline gate"
#                            git push        # with your own credentials
#     B) GitHub web UI:      ./ops/install_ci_workflow.sh --print  -> copy the
#                            output into a new file at
#                            .github/workflows/offline-gate.yml
#
#   Until one of those happens CI does not run, and README must not claim it
#   does - test_repo_consistency.py checks the claim against the shipped source.
#
# This script never pushes, never touches a server, and writes exactly one file
# inside this checkout.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/ops/ci/offline-gate.yml"
DEST_DIR="$ROOT/.github/workflows"
DEST="$DEST_DIR/offline-gate.yml"

MODE="install"
for arg in "$@"; do
  case "$arg" in
    --print) MODE="print" ;;
    --check) MODE="check" ;;
    --force) MODE="force" ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$SRC" ]]; then
  echo "ERROR: $SRC is missing; nothing to install." >&2
  exit 2
fi

if [[ "$MODE" == "print" ]]; then
  cat "$SRC"
  exit 0
fi

if [[ -f "$DEST" ]] && cmp -s "$SRC" "$DEST"; then
  echo "OK  already installed and byte-identical: ${DEST#"$ROOT"/}"
  echo "    CI runs ops/test_all.sh on every push and pull request."
  exit 0
fi

if [[ -f "$DEST" && "$MODE" == "check" ]]; then
  echo "PROBLEM  ${DEST#"$ROOT"/} exists but differs from ${SRC#"$ROOT"/}." >&2
  echo "         Re-install with: ./ops/install_ci_workflow.sh --force" >&2
  exit 1
fi
if [[ "$MODE" == "check" ]]; then
  echo "PROBLEM  CI is not installed: ${DEST#"$ROOT"/} does not exist." >&2
  echo "         Install with: ./ops/install_ci_workflow.sh   (then commit and push it)" >&2
  exit 1
fi

if [[ -f "$DEST" && "$MODE" != "force" ]]; then
  echo "PROBLEM  ${DEST#"$ROOT"/} already exists and differs from the source copy." >&2
  echo "         Review it, then overwrite with: ./ops/install_ci_workflow.sh --force" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST"
echo "OK  installed ${DEST#"$ROOT"/}  (from ${SRC#"$ROOT"/})"
echo ""
echo "Publish it - one of these two:"
echo "  A) git add .github/workflows/offline-gate.yml"
echo "     git commit -m 'ci: run the offline gate on push and pull request'"
echo "     git push        # needs a credential with the 'workflows' permission"
echo "  B) GitHub web UI: create .github/workflows/offline-gate.yml and paste"
echo "     ./ops/install_ci_workflow.sh --print"
echo ""
echo "Until it is pushed, GitHub Actions does NOT run; the local gate is unchanged:"
echo "  PYTHON=.venv/bin/python ./ops/test_all.sh"

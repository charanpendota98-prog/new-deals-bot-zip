#!/usr/bin/env python3
"""Extract hardcoded v13/v14 credentials locally and write a secure .env.

The script never prints secret values. Run it inside the Oracle app directory.
It scans Python files for the old API_ID/API_HASH/EK_KEY/etc. assignments.
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
import time
from pathlib import Path

APP_DIR = Path.cwd()
ENV_PATH = APP_DIR / ".env"


def assignments(path: Path) -> dict[str, object]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except Exception:
        return {}
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        try:
            value = ast.literal_eval(value_node)
        except Exception:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = value
    return values


def valid(values: dict[str, object]) -> bool:
    return all(values.get(key) for key in ("API_ID", "API_HASH", "EK_KEY", "OUR_TAG"))


def find_source() -> tuple[Path, dict[str, object]]:
    explicit = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    if explicit:
        candidates = [explicit]
    else:
        candidates = sorted(
            (p for p in APP_DIR.glob("*.py") if p.name != Path(__file__).name),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    for candidate in candidates:
        values = assignments(candidate)
        if valid(values):
            return candidate, values
    raise SystemExit(
        "ERROR: No legacy Python file containing API_ID/API_HASH/EK_KEY/OUR_TAG was found.\n"
        "Usage: python3 migrate_legacy_env.py /path/to/old_working_main_bot.py"
    )


def text(value: object, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def main() -> None:
    source, values = find_source()
    tokens = values.get("BITLY_TOKENS")
    if isinstance(tokens, (list, tuple)):
        bitly_tokens = ",".join(text(token) for token in tokens if text(token))
    else:
        bitly_tokens = text(values.get("BITLY_TOKEN"))

    required = {
        "TELEGRAM_API_ID": text(values.get("API_ID")),
        "TELEGRAM_API_HASH": text(values.get("API_HASH")),
        "TELEGRAM_SESSION": text(values.get("SESSION"), "bestgaa_fresh"),
        "EARNKARO_API_KEY": text(values.get("EK_KEY")),
        "EARNKARO_API_URL": text(
            values.get("EK_API"), "https://ekaro-api.affiliaters.in/api/converter/public"
        ),
        "EARNKARO_PUBLISHER_ID": text(values.get("OUR_EK_ID")),
        "AMAZON_TAG": text(values.get("OUR_TAG")),
        "BITLY_TOKENS": bitly_tokens,
        "BOT_DB_PATH": str(APP_DIR / "bestgaa.sqlite3"),
        "PRODUCT_DEDUP_SECONDS": "36000",
        "PRICE_DEDUP_SECONDS": "3600",
        "QUEUE_WORKERS": "6",
        "EK_MAX_CONCURRENCY": "6",
        "POST_RETRIES": "3",
    }
    missing = [key for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "EARNKARO_API_KEY", "AMAZON_TAG") if not required[key]]
    if missing:
        raise SystemExit("ERROR: Missing values in legacy code: " + ", ".join(missing))

    if ENV_PATH.exists():
        backup = ENV_PATH.with_name(f".env.backup.{int(time.time())}")
        shutil.copy2(ENV_PATH, backup)
        print(f"Existing .env backed up to: {backup.name}")

    temp = ENV_PATH.with_suffix(".tmp")
    temp.write_text("\n".join(f"{key}={value}" for key, value in required.items()) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, ENV_PATH)
    os.chmod(ENV_PATH, 0o600)

    print(f"SUCCESS: .env created from {source.name}")
    print("Secret values were not printed.")
    print("Next: python3 -m py_compile main_bot.py && sudo systemctl restart bestgaa")


if __name__ == "__main__":
    main()

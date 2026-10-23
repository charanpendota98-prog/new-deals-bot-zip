"""Opening the dashboard: the server link, the tunnel, and the token.

The dashboard is bound to 127.0.0.1 on purpose, so "what do I open?" is a real
question with a real answer that must not be a stale wiki page. These checks pin
`ops/dashboard_link.sh` (the answer) and the served page's one-click token.

Covered:
  * the script's human output names the tunnel, the local link and the pipeline
    proof command, and never prints the token in full;
  * a missing service is exit 1 with the exact command that starts it;
  * --json is valid JSON, carries the same facts, and carries no secret;
  * --nginx is a reverse-proxy config in front of 127.0.0.1 with its own auth;
  * --help works and an unknown flag is refused;
  * the served page can take ?token= once, store it, and strip it from the URL,
    and shows the operator how to reach it.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bestgaa"))
sys.path.insert(0, str(ROOT))
from dashboard import app as dash  # noqa: E402

FAILS: list[str] = []
SCRIPT = ROOT / "ops" / "dashboard_link.sh"
TOKEN = "s3cret-dashboard-token-abcd1234"


def check(name: str, condition: bool, extra: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"  <- {str(extra)[:300]}" if extra and not condition else ""))
    if not condition:
        FAILS.append(name)


def run(*args, env_file: Path | None = None, token: str | None = TOKEN):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("DASHBOARD_", "BOT_DB_PATH"))}
    if env_file is not None:
        env["DASHBOARD_ENV_FILE"] = str(env_file)
    if token:
        env["DASHBOARD_TOKEN"] = token
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                          text=True, env=env)


def test_script():
    print("\n== ops/dashboard_link.sh: the link, the tunnel, the token ==")
    with tempfile.TemporaryDirectory() as td:
        env_file = Path(td) / "dashboard.env"
        env_file.write_text(f"DASHBOARD_TOKEN={TOKEN}\n", encoding="utf-8")
        env_file.chmod(0o644)

        proc = run("--server", "203.0.113.9", "--port", "8080", env_file=env_file)
        out = proc.stdout
        check("the tunnel command is exact and copy-pasteable",
              "ssh -N -L 8080:127.0.0.1:8080" in out and "203.0.113.9" in out, out)
        check("the local link is printed with the anchor and the token placeholder",
              "http://127.0.0.1:8080/?token=<DASHBOARD_TOKEN>#pipeline" in out, out)
        check("the token is shown masked, never in full",
              TOKEN not in out + proc.stderr and "…1234" in out, out)
        check("the command that reads the token is shown (server-side only)",
              "DASHBOARD_TOKEN=//p" in out and str(env_file) in out, out)
        check("the proof command for 'nothing dropped' is included",
              "ops/pipeline_check.py --hours 24" in out, out)
        check("it says which checkout is SERVING and where the bot's keys and DB are",
              "runs from:" in out and "bot keys :" in out
              and str(env_file.parent / "nothing.sqlite3") not in out, out)
        check("those paths never include the token",
              TOKEN not in out and "…1234" in out, out)
        check("a dashboard that is not running is exit 1, with the fix",
              proc.returncode == 1 and "systemctl start" in out, (proc.returncode, out))

        proc = run("--json", "--server", "203.0.113.9", env_file=env_file)
        data = json.loads(proc.stdout)
        check("--json is valid JSON with the same facts",
              data["tunnel"].startswith("ssh -N -L") and data["port"] == 8080
              and data["token"] == "present", data)
        check("--json carries no secret",
              TOKEN not in proc.stdout and "…" not in json.dumps(data), data)
        check("--json reports the service state instead of pretending",
              data["state"] in {"active", "inactive", "unknown", "not-installed", "failed"},
              data.get("state"))
        check("--json names the checkout, the bot env and the bot db",
              all(key in data for key in ("checkout", "bot_env", "bot_db"))
              and data["checkout"].startswith("/"), data)
        check("--json says nothing about being on the wrong server when the service exists",
              data.get("wrong_host") in ("", None)
              or data["state"] != "not-installed", data.get("wrong_host"))
        check("--json marks the token missing when there is no env file",
              json.loads(run("--json", env_file=Path(td) / "nope.env",
                             token=None).stdout)["token"] == "missing")

        proc = run("--nginx", env_file=env_file)
        check("--nginx proxies to 127.0.0.1 and adds its own password",
              "proxy_pass http://127.0.0.1:8080;" in proc.stdout
              and "auth_basic" in proc.stdout and "ssl_certificate" in proc.stdout,
              proc.stdout[:400])
        check("--nginx warns that this is optional and privileged",
              "OPTIONAL public link" in proc.stdout
              and "restart the bot" in proc.stdout, proc.stdout[:300])

        proc = run("--paste", env_file=env_file)
        check("--paste prints exactly one line for the laptop",
              len(proc.stdout.strip().splitlines()) == 1
              and proc.stdout.strip().startswith("ssh -N -L"), proc.stdout)

        check("--help documents the modes",
              run("--help").returncode == 0 and "tunnel" in run("--help").stdout)
        check("an unknown flag is refused, not ignored",
              run("--nonsense").returncode == 2)


def test_page():
    print("\n== the served page: one-click token + how to connect ==")
    html = dash.UI_HTML
    check("the page can take ?token= once", "searchParams.get('token')" in html)
    check("it stores the token for the tab and strips it from the address bar",
          "sessionStorage.setItem('dash_token', given)" in html
          and "replaceState" in html and "searchParams.delete('token')" in html, "")
    check("the how-to card names the tunnel and the server-side token command",
          'id="howtoconnect"' in html and "ssh -N -L 8080:127.0.0.1:8080" in html
          and "dashboard.env" in html)
    check("the how-to card points at the integrity panel",
          "#pipeline" in html and "dashboard_link.sh" in html)


VERIFY = ROOT / "ops" / "deploy_and_verify.sh"


def _section_7() -> str:
    """The deploy report's dashboard block, lifted out so it can be run alone."""
    text = VERIFY.read_text(encoding="utf-8")
    start = text.index('echo "==== 7. DASHBOARD')
    end = text.index('echo ""\nif ((${#FAILED[@]})); then')
    body = text[start:end].replace('echo ""\n', "", 1)
    return ("set -Eeuo pipefail\nREPO='" + str(ROOT) + "'\nFAILED=()\n"
            "ok(){ printf '  OK    %s\\n' \"$*\"; }\n"
            "warn(){ printf '  CHECK %s\\n' \"$*\"; FAILED+=(\"$*\"); }\n"
            "info(){ printf '  ----  %s\\n' \"$*\"; }\n" + body)


def _fake_server(payload: str, port: int):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's name
            body = payload.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_deploy_report_proves_the_dashboard():
    print("\n== the deploy report proves the dashboard too, and never leaks the token ==")
    check("the deploy report has a dashboard section", "==== 7. DASHBOARD" in
          VERIFY.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as td:
        env_file = Path(td) / "dashboard.env"
        env_file.write_text(f"DASHBOARD_TOKEN={TOKEN}\n", encoding="utf-8")
        bin_dir = Path(td) / "bin"
        bin_dir.mkdir()
        (bin_dir / "systemctl").write_text(
            "#!/bin/bash\n"
            'case "$1 $2" in\n'
            "  cat*) exit 0 ;;\n"
            '  "is-active"*) echo active ;;\n'
            '  "show -p"*) echo "$FAKE_CWD" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n", encoding="utf-8")
        (bin_dir / "systemctl").chmod(0o755)

        ready = json.dumps({"readiness": {"state": "ready", "headline": "Everything the bot needs is set."},
                            "bot_has_it": True, "leaks": {"state": "clean"}})
        server = _fake_server(ready, 8199)
        try:
            env = dict(os.environ)
            env.update(PATH=f"{bin_dir}:{env.get('PATH', '')}",
                       FAKE_CWD=str(ROOT), DASHBOARD_PORT="8199",
                       DASHBOARD_ENV_FILE=str(env_file))
            script = Path(td) / "section7.sh"
            script.write_text(_section_7() + '\nprintf "problems: %s\\n" "${#FAILED[@]}"\n',
                              encoding="utf-8")
            good = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
            out = good.stdout + good.stderr
            check("a healthy dashboard is reported as the checkout that is serving",
                  "serves the checkout that was just deployed" in out, out[:400])
            check("the page, the keys panel and the health panel are each proven",
                  "GET /api/status -> 200" in out and "GET /api/vault -> 200" in out
                  and "GET /api/health -> 200" in out, out[:400])
            check("the API-keys verdict is printed with its second line",
                  "API keys: ready" in out and "bot_has_it=True leaks=clean" in out, out[-300:])
            check("the report never prints the token",
                  TOKEN not in out and "…1234" not in out, out[-200:])
            check("a healthy run reports no problem", "problems: 0" in out, out[-100:])

            env["FAKE_CWD"] = "/home/ubuntu/bestgaa-latest"
            wrong = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
            out2 = wrong.stdout + wrong.stderr
            check("a dashboard serving another checkout is a CHECK, not a silent OK",
                  "not " in out2 and "the dashboard serves /home/ubuntu/bestgaa-latest" in out2,
                  out2[:400])
        finally:
            server.shutdown()


def test_gate_wiring():
    print("\n== gate wiring ==")
    gate = (ROOT / "ops" / "test_all.sh").read_text(encoding="utf-8")
    check("the new suites run in the offline gate",
          "test_dashboard_link.py" in gate and "test_dashboard_link.cjs" in gate, "")
    check("the script is executable", os.access(SCRIPT, os.X_OK), oct(SCRIPT.stat().st_mode))


if __name__ == "__main__":
    test_script()
    test_page()
    test_deploy_report_proves_the_dashboard()
    test_gate_wiring()
    print("\n" + ("DASHBOARD ACCESS TESTS: FAILURES: " + ", ".join(FAILS) if FAILS
                  else "test_dashboard_link: all checks PASS"))
    sys.exit(1 if FAILS else 0)

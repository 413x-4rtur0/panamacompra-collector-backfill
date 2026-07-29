#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import datetime
import hmac
import os
import shlex
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common as pc_common

BASE = pc_common.APP_ROOT
LOG = pc_common.LOG_DIR / "webhook_listener.log"
REQUEST_FLAG = pc_common.QUEUE_DIR / "run_all_requested.flag"
SETTINGS_FILE = pc_common.DATA_CONFIG_DIR / "monitor_settings.env"

# Two independent routes, one per changedetection.io watch: the original
# Abiertas/Programadas watch (priority 1, full pipeline) and a second watch
# scoped to the Closed tab (priority 2, new-closures only — see
# 070-run-collector-closed-new.sh). Different path, different token file,
# different runner script, so the two can be toggled/rotated independently
# and neither trigger chain can affect the other.
ROUTES = {
    "panamacompra": {
        "token_file": BASE / ".webhook_token",
        "runner": str(Path(__file__).resolve().parent / "060-run-collector.sh"),
        "auto_run_setting": "PC_WEBHOOK_AUTO_RUN",
    },
    "panamacompra-closed": {
        "token_file": BASE / ".webhook_token_closed",
        "runner": str(Path(__file__).resolve().parent / "070-run-collector-closed-new.sh"),
        "auto_run_setting": "PC_CLOSED_WEBHOOK_AUTO_RUN",
    },
    # Third route: the dedicated historical backfill watch (137-create-closed-
    # backfill-watch.py). Its runner (080) imports the changedetection
    # snapshot via 016-import-closed-backfill-snapshot.py, then drains full
    # details (037b) and cotizaciones (038) — see that script for the
    # priority/lock ordering shared with routes 1 and 2 above.
    "panamacompra-closed-backfill": {
        "token_file": BASE / ".webhook_token_closed_backfill",
        "runner": str(Path(__file__).resolve().parent / "080-run-collector-closed-backfill.sh"),
        "auto_run_setting": "PC_CLOSED_BACKFILL_WEBHOOK_AUTO_RUN",
    },
}

# Bind address is configurable. The default 0.0.0.0 is required when
# changedetection.io runs in Docker and reaches the host via
# host.docker.internal. Set PC_WEBHOOK_HOST=127.0.0.1 to restrict to localhost.
HOST = os.environ.get("PC_WEBHOOK_HOST", "0.0.0.0")
PORT = int(os.environ.get("PC_WEBHOOK_PORT", "8765"))

# Enqueue-only mode: when running inside the docker-compose `webhook` container,
# the listener cannot launch the host browser pipeline, so it only records the
# run request into the shared data/queue volume. A host-side runner
# (src/10_webhook/050-watch-queue-flag.sh or the systemd webhook unit) performs the real
# collection. Unset/0 keeps the original behaviour (run 060-run-collector.sh).
ENQUEUE_ONLY = os.environ.get("PC_WEBHOOK_ENQUEUE_ONLY", "0").strip().lower() in {"1", "true", "yes", "on"}


def automatic_runs_enabled(setting_name: str) -> bool:
    """Read the given monitor-settings flag fresh on every request (not
    cached at startup), so toggling it in the monitor Settings tab takes
    effect immediately without restarting this listener. Defaults to
    enabled, matching the previous (always-trigger) behavior."""
    env_override = os.environ.get(setting_name)
    if env_override is not None:
        return env_override.strip().lower() not in {"0", "false", "no", "off"}
    if not SETTINGS_FILE.exists():
        return True
    try:
        for line in SETTINGS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw_value = line.partition("=")
            if key.strip() != setting_name:
                continue
            parts = shlex.split(raw_value)
            value = parts[0] if parts else ""
            return value.strip().lower() not in {"0", "false", "no", "off"}
    except OSError:
        pass
    return True


def load_required_token(token_path: Path) -> str:
    try:
        token = token_path.read_text().strip()
    except FileNotFoundError:
        sys.exit(
            f"ERROR: webhook token file not found: {token_path}\n"
            f"Create it with: printf 'YOUR_SECRET_TOKEN' > {token_path}"
        )
    if not token:
        sys.exit(f"ERROR: webhook token file is empty: {token_path}")
    return token


def load_optional_token(token_path: Path) -> str:
    """Same as load_required_token, but a missing/empty file just disables
    this route instead of taking down the whole listener — the primary
    Abiertas/Programadas route must keep working even if the Closed watch
    was never set up (or its token was deleted)."""
    try:
        token = token_path.read_text().strip()
    except FileNotFoundError:
        return ""
    return token


# Populated in __main__ before the server starts: {route_name: token or ""}.
TOKENS: dict = {}

class Handler(BaseHTTPRequestHandler):
    def log_line(self, message):
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} | {message}\n")

    def do_GET(self):
        self.handle_trigger(0)

    def do_POST(self):
        # changedetection can send a large rendered-page JSON body and commonly
        # uses a short read timeout. We do not need the payload; acknowledge first
        # and trigger work asynchronously so the notifier never waits for disk, git,
        # browser, or queue operations. The connection is HTTP/1.0/close, so the
        # unread request body is discarded when the response closes.
        length = int(self.headers.get("Content-Length", "0") or 0)
        self.handle_trigger(length)

    def send_plain(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def caller_tag(self) -> str:
        """Source IP + User-Agent, so webhook_listener.log/collector_triggered.log
        show who/what actually hit the trigger URL (changedetection, curl, a stray
        script) instead of just "a request arrived"."""
        ip = self.client_address[0] if self.client_address else "unknown"
        agent = self.headers.get("User-Agent", "-")
        return f"{ip} | {agent}"

    def trigger_async(self, route_name: str, body_length: int, caller: str) -> None:
        route = ROUTES[route_name]
        try:
            if not automatic_runs_enabled(route["auto_run_setting"]):
                # Manual mode: acknowledge the webhook (already done in
                # handle_trigger, so changedetection does not retry-storm) but
                # do not start a collector run. Toggle back on from the monitor
                # Settings tab, or restart the listener with the route's
                # auto-run env var set to force it regardless of the setting.
                self.log_line(f"[{route_name}] Webhook trigger ignored ({body_length} byte body) from {caller}: automatic runs are disabled (manual mode)")
                return

            if ENQUEUE_ONLY:
                if route_name == "panamacompra":
                    # Record the request into the shared queue volume; the host
                    # runner picks it up and performs the actual collection.
                    REQUEST_FLAG.parent.mkdir(parents=True, exist_ok=True)
                    REQUEST_FLAG.touch()
                    self.log_line(f"[{route_name}] Run request enqueued by webhook ({body_length} byte body) from {caller}; waiting for host runner")
                else:
                    # No enqueue-only path exists yet for the Closed route
                    # (it only runs where this listener also has host access).
                    self.log_line(f"[{route_name}] Webhook trigger ignored ({body_length} byte body) from {caller}: enqueue-only mode is not supported for this route")
                return

            subprocess.Popen(
                ["bash", route["runner"]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.log_line(f"[{route_name}] Collector triggered by webhook ({body_length} byte body) from {caller}")
        except Exception as exc:  # noqa: BLE001 - response was already sent; log failure
            self.log_line(f"[{route_name}] ERROR: failed to process accepted webhook from {caller}: {exc}")

    def handle_trigger(self, body_length):
        request_path = self.path.split("?")[0]
        matched_route = None
        for route_name, route in ROUTES.items():
            token = TOKENS.get(route_name, "")
            if not token:
                continue  # route not configured (no token file) — never matches
            if hmac.compare_digest(request_path, f"/{route_name}/{token}"):
                matched_route = route_name
                break

        if matched_route is None:
            self.send_plain(403, b"Forbidden\n")
            self.log_line(f"Rejected path from {self.caller_tag()}: {self.path}")
            return

        body = b"Collector run request enqueued\n" if ENQUEUE_ONLY else b"Collector trigger accepted\n"
        self.send_plain(202, body)
        caller = self.caller_tag()
        threading.Thread(target=self.trigger_async, args=(matched_route, body_length, caller), daemon=True).start()

    def log_message(self, format, *args):
        return

if __name__ == "__main__":
    TOKENS["panamacompra"] = load_required_token(ROUTES["panamacompra"]["token_file"])
    TOKENS["panamacompra-closed"] = load_optional_token(ROUTES["panamacompra-closed"]["token_file"])
    if not TOKENS["panamacompra-closed"]:
        print("NOTE: no .webhook_token_closed found — the Closed new-closures "
              "webhook route is disabled until one is created.")
    TOKENS["panamacompra-closed-backfill"] = load_optional_token(ROUTES["panamacompra-closed-backfill"]["token_file"])
    if not TOKENS["panamacompra-closed-backfill"]:
        print("NOTE: no .webhook_token_closed_backfill found — the Closed "
              "backfill webhook route is disabled until one is created.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"PanamaCompra webhook listener running on {HOST}:{PORT}")
    server.serve_forever()

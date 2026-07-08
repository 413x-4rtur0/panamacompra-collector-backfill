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
RUNNER = str(Path(__file__).resolve().parent / "060-run-collector.sh")
LOG = pc_common.LOG_DIR / "webhook_listener.log"
REQUEST_FLAG = pc_common.QUEUE_DIR / "run_all_requested.flag"
SETTINGS_FILE = pc_common.DATA_CONFIG_DIR / "monitor_settings.env"

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


def automatic_runs_enabled() -> bool:
    """Read PC_WEBHOOK_AUTO_RUN fresh on every request (not cached at startup),
    so toggling the monitor's "Automatic runs from changedetection" checkbox
    takes effect immediately without restarting this listener. Defaults to
    enabled, matching the previous (always-trigger) behavior."""
    env_override = os.environ.get("PC_WEBHOOK_AUTO_RUN")
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
            if key.strip() != "PC_WEBHOOK_AUTO_RUN":
                continue
            parts = shlex.split(raw_value)
            value = parts[0] if parts else ""
            return value.strip().lower() not in {"0", "false", "no", "off"}
    except OSError:
        pass
    return True


def load_token():
    token_path = BASE / ".webhook_token"
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


# Populated in __main__ before the server starts.
TOKEN = ""

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

    def trigger_async(self, body_length: int) -> None:
        try:
            if not automatic_runs_enabled():
                # Manual mode: acknowledge the webhook (already done in
                # handle_trigger, so changedetection does not retry-storm) but
                # do not start a collector run. Toggle back on from the monitor
                # Settings tab, or restart the listener with
                # PC_WEBHOOK_AUTO_RUN=1 to force it regardless of the setting.
                self.log_line(f"Webhook trigger ignored ({body_length} byte body): automatic runs are disabled (manual mode)")
                return

            if ENQUEUE_ONLY:
                # Record the request into the shared queue volume; the host runner
                # picks it up and performs the actual collection.
                REQUEST_FLAG.parent.mkdir(parents=True, exist_ok=True)
                REQUEST_FLAG.touch()
                self.log_line(f"Run request enqueued by webhook ({body_length} byte body); waiting for host runner")
                return

            subprocess.Popen(
                ["bash", RUNNER],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.log_line(f"Collector triggered by webhook ({body_length} byte body)")
        except Exception as exc:  # noqa: BLE001 - response was already sent; log failure
            self.log_line(f"ERROR: failed to process accepted webhook: {exc}")

    def handle_trigger(self, body_length):
        expected_path = f"/panamacompra/{TOKEN}"

        if not hmac.compare_digest(self.path.split("?")[0], expected_path):
            self.send_plain(403, b"Forbidden\n")
            self.log_line(f"Rejected path: {self.path}")
            return

        body = b"Collector run request enqueued\n" if ENQUEUE_ONLY else b"Collector trigger accepted\n"
        self.send_plain(202, body)
        threading.Thread(target=self.trigger_async, args=(body_length,), daemon=True).start()

    def log_message(self, format, *args):
        return

if __name__ == "__main__":
    TOKEN = load_token()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"PanamaCompra webhook listener running on {HOST}:{PORT}")
    server.serve_forever()

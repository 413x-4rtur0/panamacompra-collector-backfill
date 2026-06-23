#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import datetime
import hmac
import os
import subprocess
import sys

BASE = Path(__file__).resolve().parent
RUNNER = str(BASE / "run_collector.sh")
LOG = BASE / "data" / "logs" / "webhook_listener.log"
REQUEST_FLAG = BASE / "data" / "queue" / "run_all_requested.flag"

# Bind address is configurable. The default 0.0.0.0 is required when
# changedetection.io runs in Docker and reaches the host via
# host.docker.internal. Set PC_WEBHOOK_HOST=127.0.0.1 to restrict to localhost.
HOST = os.environ.get("PC_WEBHOOK_HOST", "0.0.0.0")
PORT = int(os.environ.get("PC_WEBHOOK_PORT", "8765"))

# Enqueue-only mode: when running inside the docker-compose `webhook` container,
# the listener cannot launch the host browser pipeline, so it only records the
# run request into the shared data/queue volume. A host-side runner
# (pc_run_all_flag_watcher.sh or the systemd webhook unit) performs the real
# collection. Unset/0 keeps the original behaviour (run run_collector.sh).
ENQUEUE_ONLY = os.environ.get("PC_WEBHOOK_ENQUEUE_ONLY", "0").strip().lower() in {"1", "true", "yes", "on"}


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
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        self.handle_trigger(length)

    def handle_trigger(self, body_length):
        expected_path = f"/panamacompra/{TOKEN}"

        if not hmac.compare_digest(self.path.split("?")[0], expected_path):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden\n")
            self.log_line(f"Rejected path: {self.path}")
            return

        if ENQUEUE_ONLY:
            # Record the request into the shared queue volume; the host runner
            # picks it up and performs the actual collection.
            REQUEST_FLAG.parent.mkdir(parents=True, exist_ok=True)
            REQUEST_FLAG.touch()
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b"Collector run request enqueued\n")
            self.log_line(f"Run request enqueued by webhook ({body_length} byte body); waiting for host runner")
            return

        subprocess.Popen(
            ["bash", RUNNER],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"Collector triggered\n")
        self.log_line(f"Collector triggered by webhook ({body_length} byte body)")

    def log_message(self, format, *args):
        return

if __name__ == "__main__":
    TOKEN = load_token()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"PanamaCompra webhook listener running on {HOST}:{PORT}")
    server.serve_forever()

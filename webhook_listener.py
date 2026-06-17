#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import datetime

BASE = Path.home() / "Apps" / "panamacompra-collector"
TOKEN = (BASE / ".webhook_token").read_text().strip()
RUNNER = str(BASE / "run_collector.sh")
LOG = BASE / "data" / "logs" / "webhook_listener.log"

class Handler(BaseHTTPRequestHandler):
    def log_line(self, message):
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} | {message}\n")

    def do_GET(self):
        self.handle_trigger()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        self.handle_trigger()

    def handle_trigger(self):
        expected_path = f"/panamacompra/{TOKEN}"

        if self.path.split("?")[0] != expected_path:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden\n")
            self.log_line(f"Rejected path: {self.path}")
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
        self.log_line("Collector triggered by webhook")

    def log_message(self, format, *args):
        return

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
    print("PanamaCompra webhook listener running on port 8765")
    server.serve_forever()

"""
server.py — servidor HTTP que serve o dashboard e aceita comandos.
"""
from __future__ import annotations
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DASHBOARD_HTML  = Path("dashboard.html")
DATA_DIR        = Path("/data") if Path("/data").exists() else Path(".")
DASHBOARD_DATA  = DATA_DIR / "dashboard_data.json"
DASHBOARD_WHALE = DATA_DIR / "dashboard_data_whale.json"
PAUSE_FILE      = DATA_DIR / "PAUSE"
STARTED_FILE    = DATA_DIR / "STARTED"
PORT = 8080


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/dashboard.html"):
            self._serve_file(DASHBOARD_HTML, "text/html")
        elif path == "/dashboard_data.json":
            self._serve_file(DASHBOARD_DATA, "application/json")
        elif path == "/dashboard_data_whale.json":
            self._serve_file(DASHBOARD_WHALE, "application/json")
        elif path == "/status":
            self._json({"paused": PAUSE_FILE.exists(), "started": STARTED_FILE.exists()})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/pause":
            PAUSE_FILE.write_text("paused")
            log.info("[DASHBOARD] Bot pausado")
            self._json({"paused": True, "started": STARTED_FILE.exists()})
        elif path == "/resume":
            if PAUSE_FILE.exists():
                PAUSE_FILE.unlink()
            log.info("[DASHBOARD] Bot retomado")
            self._json({"paused": False, "started": STARTED_FILE.exists()})
        elif path == "/start":
            STARTED_FILE.write_text("started")
            if PAUSE_FILE.exists():
                PAUSE_FILE.unlink()
            log.info("[DASHBOARD] Bot iniciado via dashboard")
            self._json({"paused": False, "started": True})
        elif path == "/stop":
            if STARTED_FILE.exists():
                STARTED_FILE.unlink()
            log.info("[DASHBOARD] Bot parado via dashboard")
            self._json({"paused": False, "started": False})
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        content = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(content))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, data: dict):
        content = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(content))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)


def start_server():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Dashboard disponível em http://0.0.0.0:{PORT}")
    return server


def is_paused() -> bool:
    return PAUSE_FILE.exists()

def is_started() -> bool:
    return STARTED_FILE.exists()

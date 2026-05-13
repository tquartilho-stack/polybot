"""
server.py — servidor HTTP que serve o dashboard e aceita comandos.
Corre em background numa thread separada.
"""
from __future__ import annotations
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DASHBOARD_HTML = Path("dashboard.html")
DASHBOARD_DATA = Path("dashboard_data.json")
DASHBOARD_WHALE = Path("dashboard_data_whale.json")
PAUSE_FILE = Path("PAUSE")
PORT = 8080


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silencia logs HTTP

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/dashboard.html":
            self._serve_file(DASHBOARD_HTML, "text/html")
        elif path == "/dashboard_data.json":
            self._serve_file(DASHBOARD_DATA, "application/json")
        elif path == "/dashboard_data_whale.json":
            self._serve_file(DASHBOARD_WHALE, "application/json")
        elif path == "/status":
            paused = PAUSE_FILE.exists()
            self._json({"paused": paused})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/pause":
            PAUSE_FILE.write_text("paused")
            log.info("[DASHBOARD] Bot pausado via dashboard")
            self._json({"paused": True})
        elif path == "/resume":
            if PAUSE_FILE.exists():
                PAUSE_FILE.unlink()
            log.info("[DASHBOARD] Bot retomado via dashboard")
            self._json({"paused": False})
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
    """Inicia o servidor HTTP numa thread de background."""
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Dashboard disponível em http://0.0.0.0:{PORT}")
    return server


def is_paused() -> bool:
    return PAUSE_FILE.exists()

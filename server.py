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
PAUSE_FILE_WHALE   = DATA_DIR / "PAUSE_WHALE"
STARTED_FILE_WHALE = DATA_DIR / "STARTED_WHALE"
PORT = 8080

# Registry de portfolios em memória — populado pelo main_combined
_portfolios: dict = {}

def register_portfolio(name: str, portfolio) -> None:
    _portfolios[name] = portfolio


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
        elif path == "/download-portfolio":
            self._serve_file(DATA_DIR / "portfolio_state.json", "application/json")
        elif path == "/download-portfolio-whale":
            self._serve_file(DATA_DIR / "portfolio_state_whale.json", "application/json")
        elif path == "/status":
            self._json({
                "paused":        PAUSE_FILE.exists(),
                "started":       STARTED_FILE.exists(),
                "paused_whale":  PAUSE_FILE_WHALE.exists(),
                "started_whale": STARTED_FILE_WHALE.exists(),
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/clean-history":
            BAD_IDS = {"sync_0x3dbf1d_YE", "sync_0x7382a5_YE", "sync_0xc6ddb1_YE", "sync_0x69f9e1_YE", "sync_0x4f60e4_YE"}
            BAD_QUESTIONS = {"GamerLegion vs Natus Vincere"}
            results = {}
            for fname in ("portfolio_state.json", "portfolio_state_whale.json"):
                f = DATA_DIR / fname
                if not f.exists():
                    results[fname] = "not found"
                    continue
                try:
                    data = json.loads(f.read_text())
                    before = len(data.get("history", []))
                    data["history"] = [t for t in data.get("history", []) if t["trade_id"] not in BAD_IDS and not any(q in t.get("market_question", "") for q in BAD_QUESTIONS)]
                    after = len(data["history"])
                    f.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
                    results[fname] = f"removidas {before - after} entradas ({before} → {after})"
                    log.info(f"[CLEAN] {fname}: {before} → {after} trades")
                except Exception as e:
                    results[fname] = f"erro: {e}"
            self._json({"ok": True, "results": results})

        elif path == "/upload-portfolio":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                target = DATA_DIR / "portfolio_state.json"
                target.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
                log.info("[UPLOAD] portfolio_state.json actualizado")
                self._json({"ok": True, "positions": len(data.get("positions", []))})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif path == "/upload-portfolio-whale":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                target = DATA_DIR / "portfolio_state_whale.json"
                target.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
                log.info("[UPLOAD] portfolio_state_whale.json actualizado")
                self._json({"ok": True, "positions": len(data.get("positions", []))})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif path == "/pause":
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
        elif path == "/start-whale":
            STARTED_FILE_WHALE.write_text("started")
            if PAUSE_FILE_WHALE.exists():
                PAUSE_FILE_WHALE.unlink()
            log.info("[DASHBOARD] Whale iniciado via dashboard")
            self._json({"started_whale": True, "paused_whale": False})
        elif path == "/pause-whale":
            PAUSE_FILE_WHALE.write_text("paused")
            log.info("[DASHBOARD] Whale pausado")
            self._json({"started_whale": True, "paused_whale": True})
        elif path == "/resume-whale":
            if PAUSE_FILE_WHALE.exists():
                PAUSE_FILE_WHALE.unlink()
            log.info("[DASHBOARD] Whale retomado")
            self._json({"started_whale": True, "paused_whale": False})
        elif path == "/stop-whale":
            if STARTED_FILE_WHALE.exists():
                STARTED_FILE_WHALE.unlink()
            log.info("[DASHBOARD] Whale parado via dashboard")
            self._json({"started_whale": False, "paused_whale": False})
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

def is_paused_whale() -> bool:
    return PAUSE_FILE_WHALE.exists()

def is_started_whale() -> bool:
    return STARTED_FILE_WHALE.exists()

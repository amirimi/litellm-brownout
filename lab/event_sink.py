import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("event_sink")

PORT = 8080
MAX_BODY_BYTES = 1_000_000

_lock = threading.Lock()
_events: list[dict] = []


class EventSink(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
        if length:
            raw = self.rfile.read(length)
        else:
            raw = b"{}"
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            event = {"event": "unparsable", "raw": raw.decode("utf-8", "replace")}
        with _lock:
            _events.append(event)
        logger.info("recorded %s", json.dumps(event, sort_keys=True))
        self._respond(200, {"recorded": True})

    def do_GET(self) -> None:
        with _lock:
            snapshot = list(_events)
        self._respond(200, {"events": snapshot})

    def do_DELETE(self) -> None:
        with _lock:
            _events.clear()
        self._respond(200, {"cleared": True})

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


if __name__ == "__main__":
    logger.info("Event sink listening on port %s", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), EventSink).serve_forever()

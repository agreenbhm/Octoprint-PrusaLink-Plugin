"""
Tiny PrusaLink v1 API simulator for development and tests.

    python tests/mock_prusalink.py --port 8081 --api-key secret

Files are kept in memory under storage "usb" with Buddy-style short names (SFN) plus a
display name, so the SFN/LFN mapping is exercised. A started job advances progress by
``--step`` percent per status request.
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


class PrinterSim:
    def __init__(self, step=10.0):
        self.lock = threading.Lock()
        self.step = step
        self.state = "IDLE"
        self.job = None  # dict(id, file, progress)
        self.next_job_id = 100
        self.files = {}  # sfn -> dict(display, data, m_timestamp)
        self.requests = []  # (method, path)

    def _sfn(self, display):
        base, _, ext = display.rpartition(".")
        base = "".join(c for c in base.upper() if c.isalnum())[:6] or "FILE"
        ext = ext.upper()[:3]
        n = 1
        while True:
            sfn = f"{base}~{n}.{ext}"
            existing = self.files.get(sfn)
            if existing is None or existing["display"] == display:
                return sfn
            n += 1

    def add_file(self, display, data):
        with self.lock:
            for sfn, f in self.files.items():
                if f["display"] == display:
                    f["data"] = data
                    return sfn
            sfn = self._sfn(display)
            self.files[sfn] = {"display": display, "data": data, "m_timestamp": int(time.time())}
            return sfn

    def find(self, name):
        for sfn, f in self.files.items():
            if name in (sfn, f["display"]):
                return sfn
        return None

    def start(self, sfn):
        with self.lock:
            if self.job is not None and self.state in ("PRINTING", "PAUSED"):
                return False
            self.job = {"id": self.next_job_id, "file": sfn, "progress": 0.0}
            self.next_job_id += 1
            self.state = "PRINTING"
            return True

    def tick(self):
        with self.lock:
            if self.job and self.state == "PRINTING":
                self.job["progress"] = min(100.0, self.job["progress"] + self.step)
                if self.job["progress"] >= 100.0:
                    self.state = "FINISHED"

    def status(self):
        self.tick()
        with self.lock:
            s = {
                "printer": {
                    "state": self.state,
                    "temp_nozzle": 215.1 if self.state == "PRINTING" else 24.0,
                    "target_nozzle": 215.0 if self.state == "PRINTING" else 0.0,
                    "temp_bed": 60.2 if self.state == "PRINTING" else 23.5,
                    "target_bed": 60.0 if self.state == "PRINTING" else 0.0,
                    "axis_z": 1.2,
                    "axis_x": 10.0,
                    "axis_y": 20.0,
                },
                "storage": {"name": "usb", "path": "/usb/", "read_only": False},
            }
            if self.job:
                s["job"] = {
                    "id": self.job["id"],
                    "progress": self.job["progress"],
                    "time_remaining": int((100 - self.job["progress"]) * 6),
                    "time_printing": int(self.job["progress"] * 6),
                }
            return s

    def job_info(self):
        with self.lock:
            if not self.job:
                return None
            f = self.files.get(self.job["file"], {"display": self.job["file"], "data": b""})
            return {
                "id": self.job["id"],
                "state": self.state,
                "progress": self.job["progress"],
                "time_printing": 0,
                "file": {
                    "name": self.job["file"],
                    "display_name": f["display"],
                    "path": "/usb",
                    "size": len(f["data"]),
                    "m_timestamp": 0,
                },
            }


def make_handler(sim, api_key=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _auth(self):
            if api_key and self.headers.get("X-Api-Key") != api_key:
                self._send(401, {"title": "Unauthorized"})
                return False
            return True

        def _send(self, code, body=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _route(self, method):
            path = unquote(urlparse(self.path).path)
            sim.requests.append((method, path))
            if not self._auth():
                return
            body = b""
            if "Content-Length" in self.headers:
                body = self.rfile.read(int(self.headers["Content-Length"]))

            if method == "GET" and path == "/api/version":
                return self._send(200, {"api": "2.0.0", "version": "2.1.2", "printer": "1.3.4",
                                        "text": "PrusaLink", "firmware": "6.1.3+8254", "server": "2.1.2",
                                        "capabilities": {"upload-by-put": True}})
            if method == "GET" and path == "/api/v1/info":
                return self._send(200, {"name": "MockMK4", "hostname": "mock"})
            if method == "GET" and path == "/api/v1/status":
                return self._send(200, sim.status())
            if method == "GET" and path == "/api/v1/storage":
                return self._send(200, {"storage_list": [
                    {"name": "usb", "type": "USB", "path": "/usb", "available": True, "read_only": False}]})
            if method == "GET" and path == "/api/v1/job":
                j = sim.job_info()
                return self._send(200, j) if j else self._send(204)

            if path.startswith("/api/v1/job/"):
                parts = path.split("/")
                job_id = int(parts[4])
                action = parts[5] if len(parts) > 5 else None
                with sim.lock:
                    if not sim.job or sim.job["id"] != job_id:
                        return self._send(404, {"title": "Not Found"})
                    if method == "DELETE":
                        sim.state = "STOPPED"
                    elif action == "pause" and sim.state == "PRINTING":
                        sim.state = "PAUSED"
                    elif action == "resume" and sim.state == "PAUSED":
                        sim.state = "PRINTING"
                    else:
                        return self._send(409, {"title": "Conflict"})
                return self._send(204)

            if path.startswith("/api/v1/files/usb"):
                rel = path[len("/api/v1/files/usb"):].strip("/")
                if method == "GET" and rel == "":
                    children = [{"name": sfn, "display_name": f["display"], "type": "PRINT_FILE",
                                 "size": len(f["data"]), "m_timestamp": f["m_timestamp"], "read_only": False}
                                for sfn, f in sim.files.items()]
                    return self._send(200, {"name": "usb", "type": "FOLDER", "read_only": False,
                                            "m_timestamp": 0, "children": children})
                if method == "PUT":
                    sfn = sim.add_file(rel, body)
                    if self.headers.get("Print-After-Upload") == "?1":
                        sim.start(sfn)
                    return self._send(201)
                sfn = sim.find(rel)
                if sfn is None:
                    return self._send(404, {"title": "Not Found"})
                if method == "POST":
                    return self._send(204) if sim.start(sfn) else self._send(409, {"title": "Conflict"})
                if method == "DELETE":
                    with sim.lock:
                        sim.files.pop(sfn, None)
                    return self._send(204)
                if method == "GET":
                    f = sim.files[sfn]
                    return self._send(200, {"name": sfn, "display_name": f["display"], "type": "PRINT_FILE",
                                            "size": len(f["data"]), "m_timestamp": f["m_timestamp"]})

            self._send(404, {"title": "Not Found"})

        def do_GET(self):
            self._route("GET")

        def do_PUT(self):
            self._route("PUT")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


def serve(port=0, api_key=None, step=10.0):
    sim = PrinterSim(step=step)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(sim, api_key))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, sim


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--step", type=float, default=2.0)
    a = ap.parse_args()
    server, sim = serve(a.port, a.api_key, a.step)
    print(f"Mock PrusaLink on http://127.0.0.1:{server.server_address[1]}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()

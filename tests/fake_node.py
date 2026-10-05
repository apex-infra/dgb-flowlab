"""Tiny in-process fake DigiByte JSON-RPC server for tests."""
import json, threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeNode:
    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.wallets = ["flab_source", "flab_a"]
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])),
                                  parse_float=Decimal)
                srv.calls.append((self.path, body["method"], body["params"],
                                  self.headers.get("Authorization")))
                fn = srv.handlers.get(body["method"])
                if fn is None:
                    out = {"result": None, "error": {"code": -32601, "message": "nope"}}
                else:
                    out = {"result": fn(*body["params"]), "error": None}
                data = json.dumps(out, default=str).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(data)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()

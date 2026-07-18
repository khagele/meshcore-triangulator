from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import os
import socket
import sys


PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPSTREAMS = {
    "/proxy/mc-radar/search": {
        "url": "https://mc-radar.woodwar.com/api/node-inspector/search",
        "method": "POST",
        "content_type": "application/json",
    },
    "/proxy/mc-radar/connected/": {
        "prefix": "https://mc-radar.woodwar.com/api/node-inspector/connected/",
        "method": "GET",
    },
    "/proxy/meshcore/nodes": {
        "url": "https://map.meshcore.io/api/v1/nodes?binary=1&short=1",
        "method": "GET",
    },
    "/proxy/pdok/ahn": {
        "prefix": "https://service.pdok.nl/rws/actueel-hoogtebestand-nederland/wms/v1_0",
        "method": "GET",
    },
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/proxy/mc-radar/connected/"):
            self._proxy_dynamic("/proxy/mc-radar/connected/")
            return
        if self.path == "/proxy/meshcore/nodes":
            self._proxy_static("/proxy/meshcore/nodes")
            return
        if self.path.startswith("/proxy/pdok/ahn"):
            self._proxy_query_passthrough("/proxy/pdok/ahn")
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/proxy/mc-radar/search":
            self._proxy_static("/proxy/mc-radar/search")
            return
        self.send_error(404, "Unknown endpoint")

    def _proxy_static(self, key):
        config = UPSTREAMS[key]
        body = self._read_body() if config["method"] == "POST" else None
        self._forward(config["url"], method=config["method"], body=body, content_type=config.get("content_type"))

    def _proxy_dynamic(self, key):
        config = UPSTREAMS[key]
        suffix = self.path[len(key):]
        self._forward(f"{config['prefix']}{suffix}", method=config["method"])

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length > 0 else b""

    def _proxy_query_passthrough(self, key):
        config = UPSTREAMS[key]
        query = ""
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
        url = config["prefix"]
        if query:
            url = f"{url}?{query}"
        self._forward(url, method=config["method"])

    def _forward(self, url, method="GET", body=None, content_type=None):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }
        if content_type:
            headers["Content-Type"] = content_type

        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                data = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/octet-stream"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
        except HTTPError as error:
            data = error.read()
            self.send_response(error.code)
            self.send_header("Content-Type", error.headers.get("Content-Type", "application/json"))
            self.end_headers()
            self.wfile.write(data)
        except URLError as error:
            message = str(error.reason).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message)
        except socket.timeout:
            message = b"PDOK request timed out"
            self.send_response(504)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message)
        except Exception as error:
            message = str(error).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message)


def main():
    port = PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    # Bind to localhost by default; set HOST=0.0.0.0 to accept connections from
    # other machines (put a reverse proxy with TLS/access control in front — see README).
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving {BASE_DIR} on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

from __future__ import annotations

import socketserver
from http.server import ThreadingHTTPServer


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.socket.getsockname()[1]


def make_server(handler, port=0):
    server = LoopbackHTTPServer(("127.0.0.1", port), handler)
    server.timeout = 1
    return server

"""Real ddgs/primp interoperability, using only explicit local fixture targets.

Run with: python tools/test_core.py --loopback proxy_loopback
This module is deliberately excluded from ordinary test_*.py discovery. Python
socket guards are not an OS sandbox for native primp; every request below names
a loopback origin or a loopback proxy. SOCKS remote DNS uses only localhost.
No provider searches, registry detection, public DNS or paid APIs are called.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import os
from pathlib import Path
import select
import socket
import socketserver
import ssl
import struct
import threading
import unittest
import urllib.error
from urllib.parse import urlsplit
from unittest.mock import patch

if os.environ.get("AIPET_TEST_LOOPBACK") != "1" or not os.environ.get("AIPET_TEST_WORK"):
    raise RuntimeError("Use tools/test_core.py --loopback proxy_loopback; never run against an installation")

import primp
from ddgs.ddgs import DDGS
from ddgs.engines import ENGINES
import ddgs.ddgs as ddgs_module
import ddgs.http_client as ddgs_http
import proxy as P


TLS = Path(__file__).parent / "fixtures" / "proxy_tls"
PROXY_ENV = tuple(name for base in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "DDGS_PROXY", "NO_PROXY")
                  for name in (base, base.lower()))


def read_exact(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        part = stream.recv(count - len(chunks))
        if not part:
            raise EOFError("Fixture connection closed")
        chunks.extend(part)
    return bytes(chunks)


def tunnel(left, right):
    """Relay bytes unchanged, including the real client/server TLS handshake."""
    peers = {left: right, right: left}
    while True:
        ready = [peer for peer in peers if isinstance(peer, ssl.SSLSocket) and peer.pending()]
        if not ready:
            ready, _, _ = select.select(list(peers), [], [], 3)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            peers[source].sendall(data)


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def server_bind(self):
        # HTTPServer normally reverse-resolves its name; fixtures need no DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "loopback"
        self.server_port = self.server_address[1]

    def record(self, **event):
        with self.events_lock:
            self.events.append(event)

    def destination(self, host, port):
        if host not in ("127.0.0.1", "::1", "localhost") or port not in self.allowed_ports:
            raise ValueError("Fixture proxy refuses nonlocal or unregistered destinations")
        return "127.0.0.1", port


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


class Origin(QuietHandler):
    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.request_headers.append({key.lower(): value for key, value in self.headers.items()})
        self.server.record(method=self.command, path=self.path, payload=payload)
        status = int(self.path.split("/", 2)[2]) if self.path.startswith("/status/") else 200
        body = b"" if status == 204 or self.path == "/status/200" else ("<html><title>Loopback fixture</title><body>LOCAL_ONLY "
                                         + self.path + "</body></html>").encode()
        self.reply(status, body, {"Content-Type": "text/html; charset=utf-8"})


class HttpProxy(QuietHandler):
    def reject(self):
        mode = self.server.mode
        if mode == "malformed":
            self.connection.sendall(b"THIS IS NOT HTTP\r\n\r\n")
            self.close_connection = True
            return True
        if isinstance(mode, int):
            self.reply(mode, b"local proxy rejection", {"Proxy-Authenticate": 'Basic realm="fixture"'})
            return True
        return False

    def do_CONNECT(self):
        self.server.record(method="CONNECT", target=self.path)
        if self.reject():
            return
        parsed = urlsplit("//" + self.path)
        target = self.server.destination(parsed.hostname, parsed.port)
        try:
            with socket.create_connection(target, timeout=3) as remote:
                self.send_response(200, "Connection Established")
                self.end_headers()
                self.wfile.flush()
                tunnel(self.connection, remote)
        except (OSError, EOFError):
            pass  # An intentionally untrusted TLS client closes the tunnel.
        self.close_connection = True

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        self.server.record(method=self.command, target=self.path)
        if self.reject():
            return
        parsed = urlsplit(self.path)
        if parsed.scheme != "http":
            self.reply(400, b"absolute HTTP target required")
            return
        target = self.server.destination(parsed.hostname, parsed.port or 80)
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in ("proxy-authorization", "proxy-connection", "connection")}
        headers["Connection"] = "close"
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection = http.client.HTTPConnection(*target, timeout=3)
        try:
            connection.request(self.command, path, body=data or None, headers=headers)
            response = connection.getresponse()
            body = response.read()
            self.reply(response.status, body, {"Content-Type": response.getheader("Content-Type", "text/plain")})
        finally:
            connection.close()


class SocksProxy(socketserver.BaseRequestHandler):
    def handle(self):
        stream = self.request
        stream.settimeout(3)
        try:
            version, count = read_exact(stream, 2)
            methods = read_exact(stream, count)
            expected = self.server.credentials
            method = 2 if expected is not None else 0
            if version != 5 or method not in methods:
                stream.sendall(b"\x05\xff")
                return
            stream.sendall(bytes((5, method)))
            if expected is not None:
                auth_version, size = read_exact(stream, 2)
                user = read_exact(stream, size).decode()
                password = read_exact(stream, read_exact(stream, 1)[0]).decode()
                accepted = auth_version == 1 and (user, password) == expected
                self.server.record(kind="auth", user=user, accepted=accepted)
                stream.sendall(bytes((1, 0 if accepted else 1)))
                if not accepted:
                    return
            version, command, reserved, kind = read_exact(stream, 4)
            if kind == 1:
                host = socket.inet_ntop(socket.AF_INET, read_exact(stream, 4))
            elif kind == 3:
                host = read_exact(stream, read_exact(stream, 1)[0]).decode("ascii")
            elif kind == 4:
                host = socket.inet_ntop(socket.AF_INET6, read_exact(stream, 16))
            else:
                return
            port = struct.unpack("!H", read_exact(stream, 2))[0]
            self.server.record(kind="connect", address_type=kind, host=host, port=port)
            if (version, command, reserved) != (5, 1, 0):
                return
            target = self.server.destination(host, port)
            with socket.create_connection(target, timeout=3) as remote:
                stream.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                tunnel(stream, remote)
        except (OSError, EOFError):
            pass


class ProxyLoopback(unittest.TestCase):
    def setUp(self):
        self.assertTrue(getattr(socket.socket.connect, "_aipet_loopback_guard", False),
                        "Loopback runner guard must be installed before any fixture starts")
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in PROXY_ENV:
            os.environ.pop(name, None)
        self.http = self.server(Origin)
        self.https = self.server(Origin, tls=True)
        self.allowed_ports = {self.http.server_port, self.https.server_port}
        self.forward = self.server(HttpProxy, mode="forward")
        self.poison = self.server(HttpProxy, mode=407)
        self.http_url = f"http://127.0.0.1:{self.http.server_port}"
        self.https_url = f"https://127.0.0.1:{self.https.server_port}"
        self.proxy_url = f"http://127.0.0.1:{self.forward.server_port}"
        self.poison_url = f"http://127.0.0.1:{self.poison.server_port}"

    def server(self, handler, *, tls=False, **settings):
        server = FixtureServer(("127.0.0.1", 0), handler)
        server.events, server.events_lock, server.request_headers = [], threading.Lock(), []
        server.allowed_ports = getattr(self, "allowed_ports", set())
        for name, value in settings.items():
            setattr(server, name, value)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(TLS / "server.crt", TLS / "server-key.fixture")
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "Fixture listener did not stop")
        self.addCleanup(close)
        return server

    def polluted(self, no_proxy="*"):
        return {name: (no_proxy if name.lower() == "no_proxy" else self.poison_url) for name in PROXY_ENV}

    def tls_client(self, route):
        return P.make_client(route, timeout=3, verify=True, ca_cert_file=str(TLS / "ca.crt"))

    def globals(self):
        return (socket.socket, socket.socket.connect, socket.socket.connect_ex,
                socket.create_connection, socket.getaddrinfo, dict(os.environ),
                ddgs_http.HttpClient, ddgs_http.primp.Client, ddgs_module.HttpClient)

    def test_pinned_libraries_and_loopback_python_guard(self):
        self.assertEqual(importlib.metadata.version("ddgs"), "9.16.0")
        self.assertEqual(importlib.metadata.version("primp"), "2.0.1")
        with self.assertRaises(RuntimeError):
            socket.create_connection(("198.51.100.1", 80), timeout=.1)
        with self.assertRaises(RuntimeError):
            socket.getaddrinfo("network-test.invalid", 80)

    def test_real_http_forwarding_preserves_method_body_and_absolute_target(self):
        response = P.request(self.http_url + "/forward?query=fixture", proxy_url=self.proxy_url, timeout=3)
        self.assertIsInstance(response, primp.Response)
        self.assertEqual(response.status_code, 200)
        self.assertIn("LOCAL_ONLY /forward?query=fixture", response.text)
        P.request(self.http_url + "/post", proxy_url=self.proxy_url, timeout=3,
                  method="POST", content=b"only local test bytes")
        self.assertEqual(self.forward.events[0]["target"], self.http_url + "/forward?query=fixture")
        self.assertEqual(self.http.events[-1], {"method": "POST", "path": "/post", "payload": b"only local test bytes"})

    def test_real_connect_tls_requires_trusted_fixture_ca(self):
        with patch.dict(os.environ, self.polluted()):
            with self.assertRaises(primp.PrimpError):
                P.make_client(self.proxy_url, timeout=3, verify=True).get(self.https_url + "/untrusted")
            response = self.tls_client(self.proxy_url).get(self.https_url + "/connect")
        self.assertEqual(response.status_code, 200)
        self.assertIn("LOCAL_ONLY /connect", response.text)
        self.assertTrue(all(e["method"] == "CONNECT" for e in self.forward.events))
        self.assertEqual([e["path"] for e in self.https.events], ["/connect"])
        self.assertEqual(self.poison.events, [])

    def test_real_socks5_transports_tls(self):
        socks = self.server(SocksProxy, credentials=None)
        route = f"socks5://127.0.0.1:{socks.server_port}"
        with self.assertRaises(primp.PrimpError):
            P.make_client(route, timeout=3, verify=True).get(self.https_url + "/untrusted-socks")
        response = self.tls_client(route).get(self.https_url + "/socks")
        self.assertEqual(response.status_code, 200)
        self.assertIn("LOCAL_ONLY /socks", response.text)
        self.assertEqual(socks.events, [{"kind": "connect", "address_type": 1,
                                      "host": "127.0.0.1", "port": self.https.server_port}] * 2)
        self.assertEqual([e["path"] for e in self.https.events], ["/socks"])

    def test_real_https_proxy_carries_connect_to_tls_origin(self):
        encrypted_proxy = self.server(HttpProxy, tls=True, mode="forward")
        route = f"https://127.0.0.1:{encrypted_proxy.server_port}"
        with patch.dict(os.environ, self.polluted()):
            with self.assertRaises(primp.PrimpError):
                P.make_client(route, timeout=3, verify=True).get(self.https_url + "/untrusted-proxy")
            self.assertEqual(encrypted_proxy.events, [])
            response = self.tls_client(route).get(self.https_url + "/tls-through-tls-proxy")
        self.assertEqual(response.status_code, 200)
        self.assertIn("LOCAL_ONLY /tls-through-tls-proxy", response.text)
        self.assertEqual(encrypted_proxy.events, [{"method": "CONNECT",
                         "target": f"127.0.0.1:{self.https.server_port}"}])
        self.assertEqual([e["path"] for e in self.https.events], ["/tls-through-tls-proxy"])
        self.assertEqual(self.poison.events, [])

    def test_proxy_setter_preserves_headers_cookies_timeout_and_certificate_validation(self):
        for route in (None, self.proxy_url):
            with self.subTest(route=route):
                client = P.make_client(route, timeout=3, verify=True,
                                       ca_cert_file=str(TLS / "ca.crt"),
                                       headers={"X-Fixture-Header": "preserved"},
                                       cookies={"fixture_cookie": "preserved"})
                self.assertEqual(client.timeout, 3)
                self.assertEqual(client.get(self.https_url + "/settings").status_code, 200)
                headers = self.https.request_headers[-1]
                self.assertEqual(headers.get("x-fixture-header"), "preserved")
                self.assertIn("fixture_cookie=preserved", headers.get("cookie", ""))
                with self.assertRaises(primp.PrimpError):
                    P.make_client(route, timeout=3, verify=True).get(self.https_url + "/untrusted")

    def test_real_socks_authentication_and_remote_dns_keep_tls_verification(self):
        socks = self.server(SocksProxy, credentials=("fixture@user", "fixture:p@ss"))
        route = f"socks5h://fixture%40user:fixture%3Ap%40ss@127.0.0.1:{socks.server_port}"
        # localhost is intrinsically local even if a client regresses to local DNS;
        # ATYP=3 proves the tested implementation sends its name to the proxy.
        target = f"https://localhost:{self.https.server_port}/remote-dns"
        response = self.tls_client(route).get(target)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(socks.events[0], {"kind": "auth", "user": "fixture@user", "accepted": True})
        self.assertEqual(socks.events[1], {"kind": "connect", "address_type": 3,
                                         "host": "localhost", "port": self.https.server_port})
        wrong = f"socks5h://fixture%40user:wrong@127.0.0.1:{socks.server_port}"
        with self.assertRaises(primp.PrimpError):
            self.tls_client(wrong).get(target)
        self.assertEqual(socks.events[-1]["accepted"], False)
        self.assertEqual(len(self.https.events), 1)

    def test_verify_requires_success_and_rejects_407_server_errors_and_bad_http(self):
        for status in (200, 204, 302, 407, 502, 503):
            with self.subTest(status=status), patch.object(P, "CHECK_URL", self.http_url + f"/status/{status}"):
                ok, message = P.verify(self.proxy_url, timeout=3)
                self.assertEqual(ok, status in (200, 204), message)
        with patch.object(P, "CHECK_URL", self.http_url + "/probe"):
            self.assertFalse(P.verify(self.proxy_url, timeout=3)[0], "HTTP 200 login/body must not count as a probe")
            self.assertFalse(P.verify(self.poison_url, timeout=3)[0])
            malformed = self.server(HttpProxy, mode="malformed")
            self.assertFalse(P.verify(f"http://127.0.0.1:{malformed.server_port}", timeout=3)[0])
        with self.assertRaises(urllib.error.HTTPError) as result:
            P.request(self.http_url + "/status/503", proxy_url=None, timeout=3)
        self.assertEqual(result.exception.code, 503)

    def test_none_is_direct_despite_all_proxy_variables_and_explicit_proxy_ignores_no_proxy(self):
        for bypass in ("*", "127.0.0.1,localhost,::1", ""):
            with self.subTest(no_proxy=bypass), patch.dict(os.environ, self.polluted(bypass)):
                before = self.globals()
                with patch.object(P, "detect", side_effect=AssertionError("Explicit routes must not autodetect")):
                    direct = P.make_client(None, timeout=3)
                    self.assertIsNone(direct.proxy)
                    self.assertEqual(direct.get(self.http_url + "/direct").status_code, 200)
                    self.assertEqual(self.tls_client(None).get(self.https_url + "/direct-tls").status_code, 200)
                    self.assertEqual(P.request(self.http_url + "/explicit", proxy_url=self.proxy_url, timeout=3).status_code, 200)
                self.assertEqual(self.globals(), before)
        self.assertEqual(len(self.forward.events), 3)
        self.assertEqual(self.poison.events, [])

    def test_automatic_request_selection_uses_the_selected_local_route_once(self):
        with patch.object(P, "detect", return_value=self.proxy_url) as choose:
            self.assertEqual(P.request(self.http_url + "/auto", timeout=3).status_code, 200)
        choose.assert_called_once_with()
        self.assertEqual(len(self.forward.events), 1)

    def test_real_ddgs_engines_and_extract_keep_per_instance_routes(self):
        with patch.dict(os.environ, self.polluted()):
            before = self.globals()
            ordinary = DDGS(proxy=self.poison_url, timeout=3)
            for route, label in ((None, "ddgs-direct"), (self.proxy_url, "ddgs-proxy")):
                client = P.ddgs_client(route, timeout=3)
                self.assertIsInstance(client, DDGS)
                self.assertIsNot(type(client), DDGS)
                self.assertEqual(client._proxy, route)
                for category in ENGINES:
                    engines = client._get_engines(category, "all")
                    self.assertTrue(engines)
                    for engine in engines:
                        self.assertEqual(engine.http_client.client.proxy, route)
                result = client.extract(self.http_url + "/" + label, fmt="text")
                self.assertIn("LOCAL_ONLY /" + label, result["content"])
            self.assertEqual(ordinary._proxy, self.poison_url)
            self.assertEqual(self.globals(), before)
        self.assertEqual(len(self.forward.events), 1)
        self.assertEqual(self.poison.events, [])
        self.assertEqual(len(self.http.events), 2)

    def test_concurrent_proxy_direct_and_unrelated_client_do_not_change_global_state(self):
        with patch.dict(os.environ, self.polluted()):
            unrelated = primp.Client(timeout=3)
            unrelated.proxy = self.poison_url
            unrelated_proxy = unrelated.proxy
            before = self.globals()
            barrier = threading.Barrier(6)

            def run(number):
                barrier.wait(timeout=5)
                route = self.proxy_url if number % 2 else None
                return P.request(self.http_url + f"/concurrent-{number}", proxy_url=route, timeout=3).status_code

            with ThreadPoolExecutor(max_workers=6) as pool:
                self.assertEqual(list(pool.map(run, range(6))), [200] * 6)
            self.assertEqual(self.globals(), before)
            self.assertEqual(unrelated.proxy, unrelated_proxy)
            self.assertEqual(unrelated.get(self.http_url + "/unrelated").status_code, 407)
        self.assertEqual(len(self.forward.events), 3)
        self.assertEqual(len(self.http.events), 6)
        self.assertEqual(len(self.poison.events), 1)

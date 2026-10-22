import importlib.util
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / "internal/pipeline/assets/pts_pipeline.py"
if not PATH.exists():
    PATH = Path(__file__).resolve().parents[1] / "scripts/pts_pipeline.py"
spec = importlib.util.spec_from_file_location("pts_upload_transport", PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class UploadTransportTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"UPLOAD_ORIGIN_IP": "", "UPLOAD_CA_PEM": "", "UPLOAD_WORKERS": "8"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        runner.TOKEN = "unit-test-token"

    def origin_uploader(self):
        context = Mock()
        with patch.dict(os.environ, {"UPLOAD_ORIGIN_IP": "203.0.113.10", "UPLOAD_CA_PEM": "test-ca"}), patch.object(runner.ssl, "create_default_context", return_value=context) as factory:
            up = runner.Uploader("https://example.test/api/v1")
        factory.assert_called_once_with(cadata="test-ca")
        return up, context

    def good_connection(self):
        connection = Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"received":3}'
        return connection

    def test_worker_count_is_bounded(self):
        for raw, expected in [("1", 1), ("8", 8), ("16", 16), ("32", 32), ("64", 32), ("-1", 1), ("bad", 8)]:
            with self.subTest(raw=raw), patch.dict(os.environ, {"UPLOAD_WORKERS": raw}):
                self.assertEqual(runner.upload_worker_count(), expected)

    def test_default_uses_public_verified_https(self):
        up = runner.Uploader("https://example.test/api/v1")
        connection = self.good_connection()
        with patch.object(runner.http.client, "HTTPSConnection", return_value=connection) as factory:
            up.post("seq=1", b"abc")
        factory.assert_called_once_with("example.test", None, timeout=120)
        self.assertEqual(connection.request.call_args.args[:2], ("POST", "/api/v1/worker/upload?seq=1"))

    def test_direct_upload_keeps_hostname_and_bearer_auth(self):
        up, context = self.origin_uploader()
        connection = self.good_connection()
        with patch.object(runner, "VerifiedOriginConnection", return_value=connection) as factory:
            up.post("seq=1", b"abc")
            up.post("seq=2", b"abc")
        factory.assert_called_once_with("example.test", None, "203.0.113.10", context)
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Authorization"], "Bearer unit-test-token")

    def test_direct_connection_verifies_original_tls_hostname(self):
        context, raw, tls = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = tls
        with patch.object(runner.socket, "create_connection", return_value=raw) as factory:
            connection = runner.VerifiedOriginConnection("example.test", None, "203.0.113.10", context)
            connection.connect()
        factory.assert_called_once_with(("203.0.113.10", 443), timeout=15)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="example.test")
        tls.settimeout.assert_called_once_with(120)

    def test_failed_tls_handshake_closes_raw_socket(self):
        context, raw = Mock(), Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("untrusted origin")
        with patch.object(runner.socket, "create_connection", return_value=raw):
            connection = runner.VerifiedOriginConnection("example.test", None, "203.0.113.10", context)
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.connect()
        raw.close.assert_called_once()

    def test_certificate_failure_falls_back_without_disabling_tls(self):
        up, _ = self.origin_uploader()
        bad, good = Mock(), self.good_connection()
        bad.request.side_effect = ssl.SSLCertVerificationError("untrusted origin")
        with patch.object(runner, "VerifiedOriginConnection", return_value=bad), patch.object(runner.http.client, "HTTPSConnection", return_value=good) as fallback:
            with self.assertRaises(ssl.SSLCertVerificationError):
                up.post("seq=1", b"abc")
            self.assertEqual(up.origin_ip, "")
            up.post("seq=1", b"abc")
        fallback.assert_called_once_with("example.test", None, timeout=120)

    def test_two_network_failures_trigger_public_route(self):
        up, _ = self.origin_uploader()
        bad, good = Mock(), self.good_connection()
        bad.request.side_effect = socket.timeout("origin timeout")
        with patch.object(runner, "VerifiedOriginConnection", return_value=bad), patch.object(runner.http.client, "HTTPSConnection", return_value=good):
            for _ in range(2):
                with self.assertRaises(socket.timeout):
                    up.post("seq=1", b"abc")
            up.post("seq=1", b"abc")
        self.assertEqual(up.origin_ip, "")

    def test_http_authorization_error_is_not_a_route_failure(self):
        up, _ = self.origin_uploader()
        connection = self.good_connection()
        connection.getresponse.return_value.status = 401
        with patch.object(runner, "VerifiedOriginConnection", return_value=connection):
            with self.assertRaises(runner.ChunkError):
                up.post("seq=1", b"abc")
        self.assertEqual(up.origin_ip, "203.0.113.10")
        self.assertEqual(up.direct_failures, 0)

    def test_missing_ca_never_disables_certificate_checks(self):
        with patch.dict(os.environ, {"UPLOAD_ORIGIN_IP": "203.0.113.10"}):
            up = runner.Uploader("https://example.test/api/v1")
        self.assertEqual(up.origin_ip, "")

    def test_direct_upload_rejects_cleartext_and_non_ip_targets(self):
        with patch.dict(os.environ, {"UPLOAD_ORIGIN_IP": "203.0.113.10"}):
            with self.assertRaises(ValueError):
                runner.Uploader("http://example.test/api/v1")
        with patch.dict(os.environ, {"UPLOAD_ORIGIN_IP": "unexpected.example"}):
            with self.assertRaises(ValueError):
                runner.Uploader("https://example.test/api/v1")

    def test_parallel_upload_is_byte_exact_and_final_part_is_last(self):
        state = {"active": 0, "maximum": 0, "parts": {}, "before_last": None}
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                seq = int(query["seq"][0])
                data = self.rfile.read(int(self.headers["Content-Length"]))
                with lock:
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                    if query["last"] == ["1"]:
                        state["before_last"] = len(state["parts"])
                time.sleep(0.03)
                with lock:
                    state["parts"][seq] = data
                    state["active"] -= 1
                body = json.dumps({"received": len(data)}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "test.bin"
                data = os.urandom(11 * 65536 + 200)
                path.write_bytes(data)
                with patch.dict(os.environ, {"UPLOAD_WORKERS": "4"}), patch.object(runner, "CHUNK", 65536), patch.object(runner, "CALLBACK", "http://127.0.0.1:%d/api/v1" % server.server_port, create=True), patch.object(runner, "JOB_ID", "JOB-TEST", create=True), patch.object(runner, "ATTEMPT", "attempt-test", create=True), patch.object(runner, "report"):
                    runner.upload(path)
                self.assertEqual(b"".join(state["parts"][i] for i in sorted(state["parts"])), data)
                self.assertEqual(state["before_last"], len(state["parts"]) - 1)
                self.assertGreaterEqual(state["maximum"], 2)
                self.assertLessEqual(state["maximum"], 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()

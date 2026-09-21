"""Offline transport regressions against the actual client source methods.

Run directly (no Odoo installation/database needed):
    python3 tests/test_download_security.py -v

Only Odoo's model base/decorators and environment are replaced. Method bodies,
constants, and pure helpers are compiled from the real source. Requests uses its
real Session/redirect/Response implementation with an in-memory adapter; a socket
tripwire forbids network access. No global ``odoo`` modules are installed, even
when this file is imported by test discovery.
"""

import ast
import base64
import json
from pathlib import Path
import socket
import traceback
import types
import unittest
from unittest.mock import patch

import requests
from requests.adapters import BaseAdapter


ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://allowed.invalid/api"
SIGNED_URL = "https://allowed.invalid/storage/v1/object/sign/image?token=synthetic-signature"
REDIRECT_URL = "http://outside.invalid/private?token=synthetic-redirect"
API_KEY = "synthetic-api-credential"
GROUP = "photostudio_connector.group_photostudio_user"
IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1sAAAAASUVORK5CYII="
)


class UserError(Exception):
    """Local stand-in, never registered as an Odoo module."""


class AccessError(UserError):
    pass


def load_client():
    """AST-load all exact real method bodies, including new private helpers."""
    namespace = {"requests": requests, "json": json, "base64": base64,
                 "UserError": UserError, "AccessError": AccessError}
    for filename in ("download_errors.py", "idempotency.py", "url_allowlist.py"):
        path = ROOT / "utils" / filename
        helpers = {}
        exec(compile(path.read_text(), str(path), "exec"), helpers)
        namespace.update({key: value for key, value in helpers.items()
                          if not key.startswith("__")})
    path = ROOT / "services" / "photostudio_client.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "PhotostudioClient")
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef)]
    for method in methods:
        method.decorator_list = []
    cls.bases = []
    cls.keywords = []
    cls.decorator_list = []
    body: list[ast.stmt] = [node for node in tree.body if isinstance(node, ast.Assign)]
    body.append(cls)
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["PhotostudioClient"], namespace


class Params:
    def __init__(self):
        self.values = {
            "photostudio_connector.api_url": API_URL,
            "photostudio_connector.api_key": API_KEY,
            "photostudio_connector.timeout": "30",
        }
        self.reads = 0

    def sudo(self):
        return self

    def get_param(self, key):
        self.reads += 1
        return self.values.get(key)


class Environment(dict):
    def __init__(self, params, authorized=True, superuser=False):
        super().__init__({"ir.config_parameter": params})
        self.su = superuser
        self.checked_groups = []

        def has_group(group):
            self.checked_groups.append(group)
            # An ordinary internal user deliberately has base.group_user only.
            return group == "base.group_user" or (authorized and group == GROUP)

        self.user = types.SimpleNamespace(has_group=has_group)


class MemoryRaw:
    def __init__(self, chunks, interruption=None, close_error=None):
        self.chunks = chunks
        self.interruption = interruption
        self.close_error = close_error
        self.closed = False

    def stream(self, chunk_size, decode_content=True):
        yield from self.chunks
        if self.interruption is not None:
            raise self.interruption

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def release_conn(self):
        self.close()


class MemoryAdapter(BaseAdapter):
    def __init__(self):
        self.seen = []
        self.responses = []
        self.responder = lambda request: self.response()

    def response(self, status=200, content_type="image/png", chunks=None, headers=None,
                 interruption=None, close_error=None):
        response = requests.Response()
        response.status_code = status
        response.headers["Content-Type"] = content_type
        response.headers.update(headers or {})
        response.raw = MemoryRaw([IMAGE] if chunks is None else chunks,
                                 interruption, close_error)
        self.responses.append(response)
        return response

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self.seen.append((request, {"stream": stream, "timeout": timeout}))
        response = self.responder(request)
        response.request = request
        assert request.url is not None
        response.url = request.url
        return response

    def close(self):
        pass


class TestDownloadSecurity(unittest.TestCase):
    def setUp(self):
        client_class, self.namespace = load_client()
        self.DownloadError = self.namespace["PhotostudioDownloadError"]
        self.client = client_class()
        self.params = Params()
        self.client.env = Environment(self.params)
        self.adapter = MemoryAdapter()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.mount("http://", self.adapter)
        self.session.mount("https://", self.adapter)
        self.addCleanup(self.session.close)
        for target, replacement in (("get", self.session.get), ("request", self.session.request)):
            patcher = patch.object(requests, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        tripwire = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        tripwire.start()
        self.addCleanup(tripwire.stop)

    def assert_sanitized(self, error):
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        for forbidden in (SIGNED_URL, REDIRECT_URL, API_KEY, "synthetic-signature",
                          "synthetic-redirect", "Bearer "):
            self.assertNotIn(forbidden, str(error))
            self.assertNotIn(forbidden, rendered)
        self.assertIsNone(error.__cause__)

    def test_download_302_rejected_without_following(self):
        def respond(request):
            if len(self.adapter.seen) == 1:
                return self.adapter.response(302, headers={"Location": REDIRECT_URL}, chunks=[])
            return self.adapter.response()
        self.adapter.responder = respond
        with self.assertRaises(self.DownloadError) as caught:
            self.client._download_output(SIGNED_URL)
        self.assertEqual(caught.exception.status_code, 302)
        self.assertEqual(len(self.adapter.seen), 1)
        self.assertTrue(self.adapter.responses[0].raw.closed)
        self.assert_sanitized(caught.exception)

    def test_download_all_3xx_rejected_even_without_location(self):
        for status in (300, 301, 302, 303, 304, 305, 307, 308, 399):
            with self.subTest(status=status):
                self.adapter.responder = lambda request: self.adapter.response(status)
                with self.assertRaises(self.DownloadError) as caught:
                    self.client._download_output(SIGNED_URL)
                self.assertEqual(caught.exception.status_code, status)

    def test_interrupted_chunk_is_wrapped_and_sanitized(self):
        self.adapter.responder = lambda request: self.adapter.response(
            chunks=[b"partial"],
            interruption=requests.exceptions.ChunkedEncodingError(SIGNED_URL))
        with self.assertRaises(self.DownloadError) as caught:
            self.client._download_output(SIGNED_URL)
        self.assertEqual(caught.exception.status_code, 200)
        self.assertTrue(self.adapter.responses[0].raw.closed)
        self.assert_sanitized(caught.exception)

    def test_cleanup_failure_is_wrapped_and_sanitized(self):
        self.adapter.responder = lambda request: self.adapter.response(
            close_error=OSError(SIGNED_URL))
        with self.assertRaises(self.DownloadError) as caught:
            self.client._download_output(SIGNED_URL)
        self.assertEqual(caught.exception.status_code, 200)
        self.assert_sanitized(caught.exception)

    def test_initial_transport_error_is_sanitized(self):
        def fail(request):
            raise requests.exceptions.ConnectTimeout(SIGNED_URL)
        self.adapter.responder = fail
        with self.assertRaises(self.DownloadError) as caught:
            self.client._download_output(SIGNED_URL)
        self.assertIsNone(caught.exception.status_code)
        self.assert_sanitized(caught.exception)

    def test_transport_exception_response_status_is_preserved(self):
        def fail(request):
            raise requests.exceptions.HTTPError(
                SIGNED_URL, response=self.adapter.response(403, chunks=[]))
        self.adapter.responder = fail
        with self.assertRaises(self.DownloadError) as caught:
            self.client._download_output(SIGNED_URL)
        self.assertEqual(caught.exception.status_code, 403)
        self.assert_sanitized(caught.exception)

    def test_403_status_preserved(self):
        self.adapter.responder = lambda request: self.adapter.response(403, chunks=[])
        with self.assertRaises(self.DownloadError) as caught:
            self.client._download_output(SIGNED_URL)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertTrue(self.adapter.responses[0].raw.closed)
        self.assert_sanitized(caught.exception)

    def test_normal_signed_storage_image(self):
        self.adapter.responder = lambda request: self.adapter.response(
            content_type="image/png; charset=binary", chunks=[IMAGE[:10], IMAGE[10:]])
        self.assertEqual(self.client._download_output(SIGNED_URL), IMAGE)
        self.assertEqual(len(self.adapter.seen), 1)
        request, options = self.adapter.seen[0]
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(options["timeout"], 30)
        self.assertTrue(options["stream"])
        self.assertTrue(self.adapter.responses[0].raw.closed)

    def test_content_length_limit(self):
        self.adapter.responder = lambda request: self.adapter.response(
            headers={"Content-Length": str(30 * 1024 * 1024 + 1)})
        with self.assertRaisesRegex(self.DownloadError, "30 MB"):
            self.client._download_output(SIGNED_URL)
        self.assertTrue(self.adapter.responses[0].raw.closed)

    def test_stream_limit_without_trusted_content_length(self):
        block = b"x" * (1024 * 1024)
        self.adapter.responder = lambda request: self.adapter.response(
            headers={"Content-Length": "invalid"}, chunks=[block] * 30 + [b"x"])
        with self.assertRaisesRegex(self.DownloadError, "30 MB"):
            self.client._download_output(SIGNED_URL)
        self.assertTrue(self.adapter.responses[0].raw.closed)

    def test_exact_size_limit_accepted(self):
        block = b"x" * (1024 * 1024)
        self.adapter.responder = lambda request: self.adapter.response(chunks=[block] * 30)
        self.assertEqual(len(self.client._download_output(SIGNED_URL)), 30 * 1024 * 1024)

    def test_non_image_rejected(self):
        self.adapter.responder = lambda request: self.adapter.response(content_type="text/html")
        with self.assertRaisesRegex(self.DownloadError, "not an image"):
            self.client._download_output(SIGNED_URL)

    def test_other_origin_rejected_before_network(self):
        with self.assertRaises(self.DownloadError) as caught:
            self.client._download_output(REDIRECT_URL)
        self.assertEqual(len(self.adapter.seen), 0)
        self.assert_sanitized(caught.exception)

    def test_timeout_configuration_and_batch_override_preserved(self):
        for configured, expected in (("0", 1), ("999", 120), ("bad", 30), ("17", 17)):
            with self.subTest(configured=configured):
                self.params.values["photostudio_connector.timeout"] = configured
                self.client._download_output(SIGNED_URL)
                self.assertEqual(self.adapter.seen[-1][1]["timeout"], expected)
        self.adapter.responder = lambda request: self.adapter.response(
            content_type="application/json", chunks=[b'{"ok":true}'])
        self.assertEqual(self.client._create_job_batch({"items": []}, "test-key"), {"ok": True})
        request, options = self.adapter.seen[-1]
        self.assertEqual(options["timeout"], 120)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Authorization"], "Bearer " + API_KEY)
        self.assertEqual(request.headers["Idempotency-Key"], "test-key")
        self.assertEqual(json.loads(request.body), {"items": []})

    def test_credential_request_redirects_rejected_without_following(self):
        for status in (300, 301, 302, 303, 304, 307, 308, 399):
            with self.subTest(status=status):
                self.adapter.seen.clear()
                def respond(request):
                    if len(self.adapter.seen) == 1:
                        return self.adapter.response(status, content_type="text/html", chunks=[],
                                                     headers={"Location": REDIRECT_URL})
                    return self.adapter.response(content_type="application/json", chunks=[b'{}'])
                self.adapter.responder = respond
                with self.assertRaisesRegex(UserError, str(status)) as caught:
                    self.client._create_job_batch({"items": []}, "test-key")
                self.assertEqual(len(self.adapter.seen), 1)
                self.assertTrue(self.adapter.responses[-1].raw.closed)
                self.assert_sanitized(caught.exception)

    def test_credential_transport_error_sanitized(self):
        def fail(request):
            raise requests.exceptions.ConnectionError(SIGNED_URL + " Bearer " + API_KEY)
        self.adapter.responder = fail
        with self.assertRaises(UserError) as caught:
            self.client._get_job("test-job")
        self.assert_sanitized(caught.exception)

    def test_api_error_never_echoes_remote_message_or_reason(self):
        for body in ({"code": "FORBIDDEN", "message": SIGNED_URL},
                     {"code": "PRIORITY_NOT_ALLOWED", "error": API_KEY},
                     {"code": "UNKNOWN", "error": SIGNED_URL}, [SIGNED_URL]):
            with self.subTest(body_type=type(body).__name__):
                def respond(request):
                    response = self.adapter.response(403, content_type="application/json",
                                                     chunks=[json.dumps(body).encode()])
                    response.reason = REDIRECT_URL
                    return response
                self.adapter.responder = respond
                with self.assertRaisesRegex(UserError, "403") as caught:
                    self.client._get_job("test-job")
                self.assert_sanitized(caught.exception)

    def test_non_json_response_error_sanitized(self):
        self.adapter.responder = lambda request: self.adapter.response(
            content_type="text/html", chunks=[SIGNED_URL.encode()])
        with self.assertRaisesRegex(UserError, "non-JSON") as caught:
            self.client._get_batch("test-batch")
        self.assert_sanitized(caught.exception)

    def test_cancel_expected_conflict_preserved(self):
        body = {"code": "JOB_NOT_CANCELLABLE", "job": {"status": "processing"}}
        self.adapter.responder = lambda request: self.adapter.response(
            409, content_type="application/json", chunks=[json.dumps(body).encode()])
        self.assertEqual(self.client._cancel_job("test-job"), body)

    def test_unauthorized_public_entrypoints_rejected_before_config_or_network(self):
        self.client.env = Environment(self.params, authorized=False)
        calls = (
            ("_create_job_batch", ({"items": []}, "test-key")),
            ("_get_job", ("test-job",)),
            ("_get_batch", ("test-batch",)),
            ("_cancel_job", ("test-job",)),
            ("_download_output", (SIGNED_URL,)),
            ("_deterministic_key", ("part",)),
        )
        self.adapter.responder = lambda request: (
            self.adapter.response() if "/storage/" in request.url else
            self.adapter.response(content_type="application/json", chunks=[b'{}']))
        for method, arguments in calls:
            with self.subTest(method=method):
                with self.assertRaises(AccessError):
                    getattr(self.client, method)(*arguments)
                self.assertEqual(len(self.adapter.seen), 0)
                self.assertEqual(self.params.reads, 0)
        self.assertEqual(set(self.client.env.checked_groups), {GROUP})

    def test_superuser_does_not_need_dedicated_group(self):
        self.client.env = Environment(self.params, authorized=False, superuser=True)
        self.assertEqual(self.client._download_output(SIGNED_URL), IMAGE)
        self.adapter.responder = lambda request: self.adapter.response(
            content_type="application/json", chunks=[b'{}'])
        self.assertEqual(self.client._get_job("test-job"), {})
        self.assertEqual(self.client.env.checked_groups, [])

    def test_group_user_can_use_deterministic_key(self):
        result = self.client._deterministic_key("part", 1)
        self.assertEqual(result, self.namespace["deterministic_key"]("part", 1))


if __name__ == "__main__":
    unittest.main()

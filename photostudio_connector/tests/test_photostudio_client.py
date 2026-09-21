import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

# Load the exact client methods without adding fake Odoo modules to sys.modules.
spec = importlib.util.spec_from_file_location(
    "transport_support", Path(__file__).with_name("test_download_security.py")
)
assert spec and spec.loader
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
Client, namespace = support.load_client()
UserError = support.UserError
client_module = types.SimpleNamespace(PhotostudioClient=Client, **{
    name: namespace[name] for name in ("requests", "PhotostudioDownloadError")
})


class Environment(dict):
    su = True


class ConfigParameters:
    def sudo(self):
        return self

    def get_param(self, key):
        return {
            "photostudio_connector.api_url": "https://jobs.example.test",
            "photostudio_connector.api_key": "secret",
            "photostudio_connector.timeout": "30",
        }.get(key)


class TestPhotostudioClient(unittest.TestCase):
    def setUp(self):
        self.client = client_module.PhotostudioClient()
        self.client.env = Environment({"ir.config_parameter": ConfigParameters()})

    @patch.object(client_module.requests, "request")
    def test_cancel_accepts_not_cancellable_response(self, request):
        response = Mock(status_code=409, reason="Conflict")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.json.return_value = {
            "success": False,
            "code": "JOB_NOT_CANCELLABLE",
            "job": {"job_id": "job-1", "status": "processing"},
        }
        request.return_value = response

        payload = self.client._cancel_job("job-1")

        self.assertEqual(payload["job"]["status"], "processing")

    @patch.object(client_module.requests, "get")
    def test_download_raises_typed_error_with_status(self, get):
        response = Mock(status_code=403, reason="Forbidden")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        get.return_value = response

        with self.assertRaises(client_module.PhotostudioDownloadError) as ctx:
            self.client._download_output("https://jobs.example.test/storage/v1/object/sign/x")

        self.assertEqual(ctx.exception.status_code, 403)

    @patch.object(client_module.requests, "request")
    def test_cancel_still_raises_for_other_conflicts(self, request):
        response = Mock(status_code=409, reason="Conflict")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.json.return_value = {
            "success": False,
            "code": "JOB_TERMINAL",
            "error": "Job is already terminal.",
        }
        request.return_value = response

        with self.assertRaisesRegex(UserError, "JOB is already terminal|already terminal"):
            self.client._cancel_job("job-1")

    def test_missing_api_key_raises_configuration_error(self):
        class EmptyKeyParams:
            def sudo(self):
                return self

            def get_param(self, key):
                values = {
                    "photostudio_connector.api_url": "https://jobs.example.test",
                    "photostudio_connector.api_key": "",
                    "photostudio_connector.timeout": "30",
                }
                return values.get(key)

        self.client.env = Environment({"ir.config_parameter": EmptyKeyParams()})

        with self.assertRaisesRegex(
            UserError, "Configure a Photostudio API key before generating images"
        ):
            self.client._create_job_batch({"items": []}, "test-key")


if __name__ == "__main__":
    unittest.main()

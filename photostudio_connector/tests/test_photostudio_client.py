import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class UserError(Exception):
    pass


odoo = types.ModuleType("odoo")
odoo.api = types.SimpleNamespace(model=lambda method: method)
odoo.models = types.SimpleNamespace(AbstractModel=object)
odoo_exceptions = types.ModuleType("odoo.exceptions")
odoo_exceptions.UserError = UserError
sys.modules.setdefault("odoo", odoo)
sys.modules.setdefault("odoo.exceptions", odoo_exceptions)

connector_root = Path(__file__).resolve().parents[1]
utils_root = connector_root / "utils"

for module_name, filename in (
    ("photostudio_connector.utils.idempotency", "idempotency.py"),
    ("photostudio_connector.utils.url_allowlist", "url_allowlist.py"),
    ("photostudio_connector.utils.download_errors", "download_errors.py"),
):
    module_path = utils_root / filename
    module_spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    module_spec.loader.exec_module(module)

sys.modules.setdefault("photostudio_connector", types.ModuleType("photostudio_connector"))
sys.modules.setdefault("photostudio_connector.utils", types.ModuleType("photostudio_connector.utils"))
sys.modules.setdefault(
    "photostudio_connector.services", types.ModuleType("photostudio_connector.services")
)

module_path = connector_root / "services" / "photostudio_client.py"
spec = importlib.util.spec_from_file_location(
    "photostudio_connector.services.photostudio_client", module_path
)
client_module = importlib.util.module_from_spec(spec)
sys.modules["photostudio_connector.services.photostudio_client"] = client_module
spec.loader.exec_module(client_module)


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
        self.client.env = {"ir.config_parameter": ConfigParameters()}

    @patch.object(client_module.requests, "request")
    def test_cancel_accepts_not_cancellable_response(self, request):
        response = Mock(status_code=409, reason="Conflict")
        response.json.return_value = {
            "success": False,
            "code": "JOB_NOT_CANCELLABLE",
            "job": {"job_id": "job-1", "status": "processing"},
        }
        request.return_value = response

        payload = self.client.cancel_job("job-1")

        self.assertEqual(payload["job"]["status"], "processing")

    @patch.object(client_module.requests, "get")
    def test_download_raises_typed_error_with_status(self, get):
        response = Mock(status_code=403, reason="Forbidden")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        get.return_value = response

        with self.assertRaises(client_module.PhotostudioDownloadError) as ctx:
            self.client.download_output("https://jobs.example.test/storage/v1/object/sign/x")

        self.assertEqual(ctx.exception.status_code, 403)

    @patch.object(client_module.requests, "request")
    def test_cancel_still_raises_for_other_conflicts(self, request):
        response = Mock(status_code=409, reason="Conflict")
        response.json.return_value = {
            "success": False,
            "code": "JOB_TERMINAL",
            "error": "Job is already terminal.",
        }
        request.return_value = response

        with self.assertRaisesRegex(UserError, "JOB is already terminal|already terminal"):
            self.client.cancel_job("job-1")


if __name__ == "__main__":
    unittest.main()

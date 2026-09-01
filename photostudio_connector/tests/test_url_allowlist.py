import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utils"))

from url_allowlist import is_allowed_output_url, parse_origin  # noqa: E402


class TestUrlAllowlist(unittest.TestCase):
    def test_parse_origin(self):
        self.assertEqual(
            parse_origin("https://abc.supabase.co/functions/v1/api-jobs"),
            "https://abc.supabase.co",
        )

    def test_accepts_storage_sign_on_api_host(self):
        api_url = "https://abc.supabase.co/functions/v1/api-jobs"
        output = "https://abc.supabase.co/storage/v1/object/sign/bucket/path?token=x"
        self.assertTrue(is_allowed_output_url(output, api_url))

    def test_rejects_other_hosts(self):
        api_url = "https://abc.supabase.co/functions/v1/api-jobs"
        output = "https://evil.example/storage/v1/object/sign/bucket/path"
        self.assertFalse(is_allowed_output_url(output, api_url))


if __name__ == "__main__":
    unittest.main()

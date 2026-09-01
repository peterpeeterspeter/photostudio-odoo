import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utils"))

from idempotency import build_batch_idempotency_key  # noqa: E402
from mime_sniff import sniff_image_mime  # noqa: E402

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


def _sniffed_body(body):
    item = body["items"][0]
    job_input = dict(item["input"])
    job_input["mime"] = sniff_image_mime(job_input["base64"], job_input.get("mime") or "image/jpeg")
    return {
        **body,
        "items": [{**item, "input": job_input}],
    }


class TestMimeSniff(unittest.TestCase):
    def test_sniff_png_from_bytes(self):
        self.assertEqual(sniff_image_mime(PNG_B64, "image/jpeg"), "image/png")

    def test_sniff_changes_key_when_declared_jpeg(self):
        body_declared = {
            "config": {"integration": "odoo", "priority": "normal"},
            "metadata": {},
            "items": [
                {
                    "operation": "product_image.ghost",
                    "input": {"base64": PNG_B64, "mime": "image/jpeg"},
                    "metadata": {"odoo_model": "product.template", "odoo_record_id": 1},
                }
            ],
        }
        key_declared = build_batch_idempotency_key(
            "db-uuid", "add_gallery", False, body_declared
        )
        sniffed_body = _sniffed_body(body_declared)
        key_sniffed = build_batch_idempotency_key(
            "db-uuid", "add_gallery", False, sniffed_body
        )
        self.assertNotEqual(key_declared, key_sniffed)
        self.assertEqual(sniffed_body["items"][0]["input"]["mime"], "image/png")

    def test_enqueue_order_key_matches_sniffed_body(self):
        body = {
            "config": {"integration": "odoo", "priority": "normal"},
            "metadata": {},
            "items": [
                {
                    "operation": "product_image.ghost",
                    "input": {"base64": PNG_B64, "mime": "image/jpeg"},
                    "metadata": {"odoo_model": "product.template", "odoo_record_id": 1},
                }
            ],
        }
        sniffed = _sniffed_body(body)
        key = build_batch_idempotency_key("db-uuid", "add_gallery", False, sniffed)
        self.assertEqual(
            key,
            build_batch_idempotency_key("db-uuid", "add_gallery", False, sniffed),
        )
        self.assertNotEqual(
            key,
            build_batch_idempotency_key("db-uuid", "add_gallery", False, body),
        )


if __name__ == "__main__":
    unittest.main()

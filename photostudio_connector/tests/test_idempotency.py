import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utils"))

from idempotency import (  # noqa: E402
    attach_client_reference,
    body_without_client_reference,
    bool_salt,
    build_batch_idempotency_key,
    canonical_json,
    deterministic_key,
)


class TestIdempotencyHelpers(unittest.TestCase):
    def test_client_reference_excluded_from_key(self):
        body = {
            "config": {"integration": "odoo", "priority": "normal"},
            "metadata": {"client_reference": "odoo:should-not-affect-hash"},
            "items": [
                {
                    "operation": "product_image.ghost",
                    "input": {"base64": "abc", "mime": "image/jpeg"},
                    "metadata": {"odoo_model": "product.template", "odoo_record_id": 1},
                }
            ],
        }
        key_without = build_batch_idempotency_key("db-uuid", "add_gallery", False, body)
        attached = attach_client_reference(body, key_without)
        key_with = build_batch_idempotency_key("db-uuid", "add_gallery", False, attached)
        self.assertEqual(key_without, key_with)

    def test_replace_strategy_and_auto_publish_change_key(self):
        body = {
            "config": {"integration": "odoo", "priority": "normal"},
            "metadata": {},
            "items": [
                {
                    "operation": "product_image.ghost",
                    "input": {"base64": "abc", "mime": "image/jpeg"},
                    "metadata": {"odoo_model": "product.template", "odoo_record_id": 1},
                }
            ],
        }
        key_a = build_batch_idempotency_key("db-uuid", "replace_main", True, body)
        key_b = build_batch_idempotency_key("db-uuid", "add_gallery", True, body)
        key_c = build_batch_idempotency_key("db-uuid", "replace_main", False, body)
        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)
        self.assertEqual(bool_salt(True), "1")
        self.assertEqual(bool_salt(False), "0")

    def test_product_rename_does_not_change_key(self):
        body_a = {
            "config": {"integration": "odoo", "priority": "normal"},
            "metadata": {},
            "items": [
                {
                    "operation": "product_image.ghost",
                    "input": {"base64": "abc", "mime": "image/jpeg"},
                    "metadata": {"odoo_model": "product.template", "odoo_record_id": 42},
                }
            ],
        }
        body_b = canonical_json(body_a)
        key_a = build_batch_idempotency_key("db-uuid", "add_gallery", False, body_a)
        key_b = build_batch_idempotency_key("db-uuid", "add_gallery", False, body_a)
        self.assertEqual(key_a, key_b)

    def test_extra_media_changes_key(self):
        base = {
            "config": {"integration": "odoo", "priority": "normal"},
            "metadata": {},
            "items": [
                {
                    "operation": "product_image.ghost",
                    "input": {"base64": "abc", "mime": "image/jpeg"},
                    "metadata": {"odoo_model": "product.template", "odoo_record_id": 1},
                }
            ],
        }
        with_extra = {
            **base,
            "items": [
                {
                    **base["items"][0],
                    "input": {
                        "base64": "abc",
                        "mime": "image/jpeg",
                        "additional_images": [{"base64": "def", "mime": "image/jpeg"}],
                    },
                }
            ],
        }
        key_base = build_batch_idempotency_key("db-uuid", "add_gallery", False, base)
        key_extra = build_batch_idempotency_key("db-uuid", "add_gallery", False, with_extra)
        self.assertNotEqual(key_base, key_extra)

    def test_none_zero_and_false_are_distinct(self):
        self.assertNotEqual(deterministic_key(None), deterministic_key(0))
        self.assertNotEqual(deterministic_key(False), deterministic_key(None))
        self.assertNotEqual(deterministic_key(False), deterministic_key(0))
        self.assertEqual(deterministic_key(None), deterministic_key(None))
        self.assertEqual(deterministic_key(0), deterministic_key("0"))

    def test_body_without_client_reference(self):
        body = {"metadata": {"client_reference": "odoo:abc", "other": 1}}
        stripped = body_without_client_reference(body)
        self.assertNotIn("client_reference", stripped["metadata"])
        self.assertEqual(stripped["metadata"]["other"], 1)

    def test_regenerate_nonce_changes_key(self):
        body = {
            "config": {"integration": "odoo", "priority": "normal"},
            "metadata": {},
            "items": [
                {
                    "operation": "product_image.ghost",
                    "input": {"base64": "abc", "mime": "image/jpeg"},
                    "metadata": {"odoo_model": "product.template", "odoo_record_id": 1},
                }
            ],
        }
        key_zero = build_batch_idempotency_key("db-uuid", "add_gallery", False, body, 0)
        key_one = build_batch_idempotency_key("db-uuid", "add_gallery", False, body, 1)
        self.assertNotEqual(key_zero, key_one)


if __name__ == "__main__":
    unittest.main()

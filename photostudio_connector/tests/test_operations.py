import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utils"))

from operations import (  # noqa: E402
    ALL_OPERATIONS,
    OPERATION_LABELS,
    RESERVED_OPERATIONS,
    SUPPORTED_OPERATIONS,
    WIZARD_PSEUDO_OPERATIONS,
    expand_requested_operations,
    extract_batch_job_payloads,
    job_operation_selection,
    wizard_operation_selection,
)


class TestPhotostudioOperations(unittest.TestCase):
    def test_core_set_expands_to_three_ops_not_all_supported(self):
        expanded = expand_requested_operations("all")
        self.assertEqual(expanded, list(ALL_OPERATIONS))
        if len(SUPPORTED_OPERATIONS) > len(ALL_OPERATIONS):
            self.assertNotEqual(expanded, list(SUPPORTED_OPERATIONS))

    def test_all_operations_subset_of_supported(self):
        self.assertLessEqual(set(ALL_OPERATIONS), set(SUPPORTED_OPERATIONS))

    def test_flatlay_supported_but_not_in_core_set(self):
        self.assertIn("product_image.flatlay", SUPPORTED_OPERATIONS)
        self.assertNotIn("product_image.flatlay", ALL_OPERATIONS)

    def test_photoshoot_supported_but_not_in_core_set(self):
        self.assertIn("product_image.photoshoot", SUPPORTED_OPERATIONS)
        self.assertNotIn("product_image.photoshoot", ALL_OPERATIONS)

    def test_detail_supported_but_not_in_core_set(self):
        self.assertIn("product_image.detail", SUPPORTED_OPERATIONS)
        self.assertNotIn("product_image.detail", ALL_OPERATIONS)

    def test_campaign_supported_but_not_in_core_set(self):
        self.assertIn("product_image.campaign", SUPPORTED_OPERATIONS)
        self.assertNotIn("product_image.campaign", ALL_OPERATIONS)

    def test_single_operation_is_preserved(self):
        self.assertEqual(
            expand_requested_operations("product_image.ghost"),
            ["product_image.ghost"],
        )

    def test_marketplace_is_supported(self):
        self.assertEqual(OPERATION_LABELS["product_image.campaign"], "Marketplace Export")
        self.assertIn("product_image.campaign", SUPPORTED_OPERATIONS)

    def test_core_set_label_is_honest(self):
        self.assertIn("Ghost", OPERATION_LABELS["all"])
        self.assertNotEqual(OPERATION_LABELS["all"], "All")

    def test_every_supported_operation_has_label(self):
        for operation in SUPPORTED_OPERATIONS:
            self.assertIn(operation, OPERATION_LABELS)

    def test_reserved_not_in_wizard_selection(self):
        if not RESERVED_OPERATIONS:
            return
        wizard_keys = {key for key, _label in wizard_operation_selection()}
        for operation in RESERVED_OPERATIONS:
            self.assertNotIn(operation, wizard_keys)

    def test_all_only_in_wizard_selection(self):
        wizard_keys = {key for key, _label in wizard_operation_selection()}
        job_keys = {key for key, _label in job_operation_selection()}
        self.assertIn("all", wizard_keys)
        self.assertNotIn("all", job_keys)

    def test_campaign_on_job_selection(self):
        job_keys = {key for key, _label in job_operation_selection()}
        self.assertIn("product_image.campaign", job_keys)

    def test_extract_batch_jobs_from_creation_items(self):
        self.assertEqual(
            extract_batch_job_payloads(
                {
                    "items": [
                        {"index": 0, "job": {"job_id": "job-1"}},
                        {"index": 1, "error": {"code": "FAILED"}},
                    ]
                }
            ),
            [{"job_id": "job-1"}],
        )

    def test_extract_batch_jobs_from_status_response(self):
        self.assertEqual(
            extract_batch_job_payloads(
                {
                    "batch_id": "batch-1",
                    "jobs": [
                        {"job_id": "job-1", "status": "completed"},
                        {"job_id": "job-2", "status": "processing"},
                    ],
                }
            ),
            [
                {"job_id": "job-1", "status": "completed"},
                {"job_id": "job-2", "status": "processing"},
            ],
        )


if __name__ == "__main__":
    unittest.main()

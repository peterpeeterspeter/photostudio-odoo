import base64
from datetime import datetime
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .test_connector_transaction import PNG_B64


@tagged("post_install", "-at_install", "photostudio_connector")
class TestReplaySafety(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env["photostudio.connector.service"]
        cls.a, cls.b = cls.env["product.template"].create([
            {"name": "Replay A"}, {"name": "Replay B"},
        ])

    def candidates(self):
        return [{"odoo_model": "product.template", "odoo_record_id": p.id,
                 "product_tmpl_id": p, "product_id": False,
                 "operation": "product_image.ghost"} for p in (self.a, self.b)]

    def payload(self, product=None):
        return {"job_id": "replay-safety-job", "operation": "product_image.ghost",
                "status": "completed", "metadata": {
                    "odoo_model": "product.template",
                    "odoo_record_id": (product or self.b).id,
                }, "outputs": [{"type": "image", "state": "ready", "image_id": "output-1",
                                "download_url": "https://output.example.test/image.png"}]}

    def reconcile(self, payload, index=0):
        return self.service._reconcile_submission_response(
            {"batch_id": "replay-batch", "items": [{"index": index, "job": payload}]},
            self.candidates(), "add_gallery", False, "normal")

    def test_compacted_replay_index_never_selects_failed_product(self):
        job = self.reconcile(self.payload())
        self.assertEqual(job.product_tmpl_id, self.b)

    def test_unidentified_payload_is_not_assigned_by_position(self):
        payload = self.payload()
        payload.pop("metadata")
        self.assertFalse(self.reconcile(payload))

    def test_foreign_metadata_is_not_assigned_by_position(self):
        payload = self.payload()
        payload["metadata"]["odoo_record_id"] = 2147483647
        self.assertFalse(self.reconcile(payload))

    def test_invalid_error_indices_are_ignored(self):
        for index in (-1, True, False, 2, "0"):
            self.service._reconcile_submission_response(
                {"items": [{"index": index, "error": {"code": "FAILED", "message": "failed"}}]},
                self.candidates(), "add_gallery", False, "normal")
        self.assertFalse(self.env["photostudio.job"].search([
            ("product_tmpl_id", "in", [self.a.id, self.b.id])]))

    def test_replay_preserves_local_writeback_and_intent(self):
        payload = self.payload()
        job = self.reconcile(payload, 1)
        job.write({"writeback_state": "attached", "main_output_attached": True,
                   "download_attempts": 2, "replace_strategy": "replace_main",
                   "auto_publish": True, "metadata": {**job.metadata, "attached_output_ids": ["output-1"]}})
        replay = self.reconcile(payload)
        self.assertEqual(replay, job)
        self.assertEqual(job.product_tmpl_id, self.b)
        self.assertEqual(job.writeback_state, "attached")
        self.assertEqual(job.metadata["attached_output_ids"], ["output-1"])
        self.assertTrue(job.main_output_attached)
        self.assertEqual(job.download_attempts, 2)
        self.assertEqual(job.replace_strategy, "replace_main")
        self.assertTrue(job.auto_publish)

    def test_job_id_cannot_be_reassigned(self):
        job = self.reconcile(self.payload(), 1)
        with self.assertRaises(UserError):
            self.service._create_or_update_job(self.a, self.payload(self.a),
                "product_image.ghost", "add_gallery", False, "normal", "replay-batch")
        self.assertEqual(job.product_tmpl_id, self.b)

    def test_remote_metadata_cannot_erase_attachment_ledger(self):
        job = self.reconcile(self.payload(), 1)
        job.write({"writeback_state": "attached", "metadata": {"attached_output_ids": ["output-1"]}})
        payload = self.payload()
        payload["metadata"]["attached_output_ids"] = []
        self.service._apply_job_payload(job, payload)
        self.assertEqual(job.metadata["attached_output_ids"], ["output-1"])

    def test_replay_then_poll_does_not_write_twice(self):
        job = self.reconcile(self.payload(), 1)
        client = type(self.env["photostudio.client"])
        images = type(self.env["photostudio.image.service"])
        with patch.object(client, "_download_output", return_value=base64.b64decode(PNG_B64)), \
                patch.object(images, "append_gallery") as append:
            self.service._apply_job_payload(job, self.payload())
            self.reconcile(self.payload(), 1)
            self.service._apply_job_payload(job, self.payload())
            self.assertEqual(append.call_count, 1)

    def test_terminal_remote_state_not_regressed_by_stale_replay(self):
        job = self.reconcile(self.payload(), 1)
        job.write({"writeback_state": "attached"})
        payload = self.payload()
        payload["status"] = "queued"
        self.reconcile(payload, 1)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.writeback_state, "attached")

    def test_failed_replay_finishes_pending_writeback(self):
        job = self.reconcile(self.payload(), 1)
        for local_status in ("queued", "processing"):
            for remote_status in ("failed", "cancelled"):
                with self.subTest(local_status=local_status, remote_status=remote_status):
                    job.write({"status": local_status, "progress": local_status,
                               "writeback_state": "pending"})
                    payload = {**self.payload(), "status": remote_status, "outputs": []}
                    self.assertEqual(self.reconcile(payload, 1), job)
                    self.assertEqual(job.status, remote_status)
                    self.assertEqual(job.progress, remote_status)
                    self.assertEqual(job.writeback_state, "failed")

    def test_replay_preserves_terminal_writeback_states(self):
        job = self.reconcile(self.payload(), 1)
        for writeback_state in ("attached", "expired", "failed"):
            for remote_status in ("failed", "cancelled"):
                with self.subTest(writeback_state=writeback_state, remote_status=remote_status):
                    job.write({"status": "queued", "writeback_state": writeback_state})
                    self.reconcile({**self.payload(), "status": remote_status}, 1)
                    self.assertEqual(job.status, remote_status)
                    self.assertEqual(job.writeback_state, writeback_state)

    def test_replay_preserves_terminal_status_before_writeback_transition(self):
        job = self.reconcile(self.payload(), 1)
        for status in ("completed", "failed", "cancelled"):
            for remote_status in ("queued", "failed", "cancelled"):
                with self.subTest(status=status, remote_status=remote_status):
                    job.write({"status": status, "progress": "terminal detail",
                               "writeback_state": "pending"})
                    self.reconcile({**self.payload(), "status": remote_status}, 1)
                    self.assertEqual(job.status, status)
                    self.assertEqual(job.progress, "terminal detail")
                    self.assertEqual(job.writeback_state,
                                     "pending" if status == "completed" else "failed")

    def test_empty_replay_errors_preserve_writeback_diagnostics(self):
        job = self.reconcile(self.payload(), 1)
        local_error = {"code": "DOWNLOAD_FAILED", "message": "Local download failed."}
        for state in ("expired", "failed"):
            for remote_errors in ({}, {"errors": []}, {"errors": None}):
                for errors in ([], [local_error]):
                    with self.subTest(state=state, remote_errors=remote_errors, errors=errors):
                        job.write({"writeback_state": state, "errors": errors,
                                   "error_message": local_error["message"]})
                        self.reconcile({**self.payload(), **remote_errors}, 1)
                        self.assertEqual(job.writeback_state, state)
                        self.assertEqual(job.error_message, local_error["message"])
                        # Odoo reloads empty JSON fields as False.
                        self.assertEqual(job.errors or [], errors)

    def test_empty_poll_errors_preserve_writeback_diagnostics(self):
        job = self.reconcile(self.payload(), 1)
        errors = [{"code": "DOWNLOAD_FAILED", "message": "Local download failed."}]
        for state in ("expired", "failed"):
            for remote_errors in ({}, {"errors": []}, {"errors": None}):
                with self.subTest(state=state, remote_errors=remote_errors):
                    job.write({"writeback_state": state, "errors": errors,
                               "error_message": errors[0]["message"]})
                    self.service._apply_job_payload(job, {**self.payload(), **remote_errors})
                    self.assertEqual(job.writeback_state, state)
                    self.assertEqual(job.error_message, errors[0]["message"])
                    self.assertEqual(job.errors, errors)

    def test_replay_accepts_nonempty_remote_errors(self):
        payload = {**self.payload(), "status": "queued", "outputs": []}
        job = self.reconcile(payload, 1)
        errors = [{"code": "GENERATION_FAILED", "message": "Remote generation failed."}]
        self.reconcile({**payload, "status": "failed", "errors": errors}, 1)
        self.assertEqual(job.errors, errors)
        self.assertEqual(job.error_message, errors[0]["message"])

    def test_stale_poll_preserves_terminal_progress(self):
        job = self.reconcile(self.payload(), 1)
        for status in ("completed", "failed", "cancelled"):
            for progress in ({}, {"progress": "waiting for worker"}):
                with self.subTest(status=status, progress=progress):
                    job.write({"status": status, "progress": "terminal detail",
                               "writeback_state": "attached"})
                    self.service._apply_job_payload(
                        job, {**self.payload(), "status": "queued", **progress})
                    self.assertEqual(job.status, status)
                    self.assertEqual(job.progress, "terminal detail")
                    self.assertEqual(job.writeback_state, "attached")

    def test_poll_sets_completed_at_only_once(self):
        payload = {**self.payload(), "status": "queued", "outputs": []}
        job = self.reconcile(payload, 1)
        first_completion = datetime(2026, 1, 1, 12)
        later_poll = datetime(2026, 1, 2, 12)
        self.assertFalse(job.completed_at)
        with patch.object(fields.Datetime, "now", return_value=first_completion):
            self.service._apply_job_payload(job, {**payload, "status": "completed"})
        self.assertEqual(job.completed_at, first_completion)
        for status in ("queued", "completed"):
            with self.subTest(status=status):
                with patch.object(fields.Datetime, "now", return_value=later_poll):
                    self.service._apply_job_payload(job, {**payload, "status": status})
                self.assertEqual(job.completed_at, first_completion)
                self.assertEqual(self.b.last_generation, first_completion)

    def test_job_id_cannot_change_operation(self):
        job = self.reconcile(self.payload(), 1)
        payload = {**self.payload(), "operation": "product_image.lifestyle"}
        with self.assertRaises(UserError):
            self.service._create_or_update_job(self.b, payload,
                "product_image.lifestyle", "add_gallery", False, "normal", "replay-batch")
        self.assertEqual(job.operation, "product_image.ghost")

    def test_template_job_id_cannot_gain_variant(self):
        job = self.reconcile(self.payload(), 1)
        variant = self.b.product_variant_id
        payload = {**self.payload(), "metadata": {
            "odoo_model": "product.product", "odoo_record_id": variant.id}}
        with self.assertRaises(UserError):
            self.service._create_or_update_job(self.b, payload,
                "product_image.ghost", "add_gallery", False, "normal", "replay-batch",
                product_id=variant.id)
        self.assertEqual(job.product_tmpl_id, self.b)
        self.assertFalse(job.product_id)

    def test_variant_job_id_cannot_lose_variant(self):
        variant = self.b.product_variant_id
        payload = {**self.payload(), "metadata": {
            "odoo_model": "product.product", "odoo_record_id": variant.id}}
        job = self.service._create_or_update_job(self.b, payload,
            "product_image.ghost", "add_gallery", False, "normal", "replay-batch",
            product_id=variant.id)
        with self.assertRaises(UserError):
            self.reconcile(self.payload(), 1)
        self.assertEqual(job.product_tmpl_id, self.b)
        self.assertEqual(job.product_id, variant)

    def test_variant_attachment_fallback_keeps_target(self):
        variant = self.b.product_variant_id
        service = self.env["photostudio.image.service"]
        with patch.object(type(service), "_has_product_image_model", return_value=False):
            service.append_gallery(variant, base64.b64decode(PNG_B64), "variant-only")
        attachment = self.env["ir.attachment"].search([("name", "=", "variant-only")])
        self.assertEqual(attachment.res_model, "product.product")
        self.assertEqual(attachment.res_id, variant.id)

    def test_variant_gallery_keeps_parent_gallery_separate(self):
        if "product.image" not in self.env.registry:
            self.skipTest("website_sale not installed")
        variant = self.b.product_variant_id
        service = self.env["photostudio.image.service"]
        service.append_gallery(self.b, base64.b64decode(PNG_B64), "template-only")
        service.append_gallery(variant, base64.b64decode(PNG_B64), "variant-only")
        self.assertEqual(self.b.product_template_image_ids.mapped("name"), ["template-only"])
        self.assertEqual(variant.product_variant_image_ids.mapped("name"), ["variant-only"])
        self.assertFalse(variant.product_variant_image_ids.product_tmpl_id)
        self.assertEqual(len(service.extract(variant)), 2)
        self.assertEqual(len(service.extract(self.b)), 1)

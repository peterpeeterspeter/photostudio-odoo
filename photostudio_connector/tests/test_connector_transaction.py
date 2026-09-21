import base64
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.photostudio_connector.utils.download_errors import PhotostudioDownloadError

PNG_B64 = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
).decode("ascii")


@tagged("post_install", "-at_install", "photostudio_connector")
class TestPhotostudioConnectorTransaction(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(
            "photostudio_connector.api_url",
            "https://test.supabase.co/functions/v1/api-jobs",
        )
        cls.product = cls.env["product.template"].create(
            {
                "name": "Photostudio Test Product",
                "image_1920": PNG_B64,
            }
        )
        cls.internal_user = cls.env["res.users"].create(
            {
                "name": "Photostudio Internal",
                "login": "photostudio_internal_user",
                "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])],
            }
        )

    def _selection_map(self, model, field_name):
        field = model._fields[field_name]
        selection = field.selection
        if callable(selection):
            selection = selection(model)
        elif isinstance(selection, str):
            selection = getattr(model, selection)()
        return dict(selection)

    def _completed_job(self, job_id="remote-job-1", batch_id="batch-1", **extra):
        values = {
            "job_id": job_id,
            "batch_id": batch_id,
            "product_tmpl_id": self.product.id,
            "operation": "product_image.ghost",
            "status": "completed",
            "progress": "completed",
            "writeback_state": "pending",
            "replace_strategy": "add_gallery",
            "auto_publish": False,
            "priority": "normal",
            "outputs": [
                {
                    "type": "image",
                    "state": "ready",
                    "download_url": "https://test.supabase.co/storage/v1/object/sign/x",
                }
            ],
        }
        values.update(extra)
        return self.env["photostudio.job"].create(values)

    def _mock_image_download(self, mock_get):
        mock_response = mock_get.return_value
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "image/png",
            "Content-Length": str(len(base64.b64decode(PNG_B64))),
        }
        mock_response.iter_content.return_value = [base64.b64decode(PNG_B64)]
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False
        return mock_response

    @patch("odoo.addons.photostudio_connector.services.photostudio_client.requests.get")
    def test_authorized_operator_poll_attaches_without_system_access(self, mock_get):
        self.internal_user.write({"group_ids": [
            (4, self.env.ref("photostudio_connector.group_photostudio_user").id),
            (4, self.env.ref("product.group_product_manager").id),
        ]})
        self.assertFalse(self.internal_user.has_group("base.group_system"))
        self._mock_image_download(mock_get)

        job = self._completed_job()
        payload = {
            "job_id": job.job_id,
            "status": "completed",
            "outputs": job.outputs,
            "metadata": {
                "odoo_model": "product.template",
                "odoo_record_id": self.product.id,
            },
        }
        service = self.env["photostudio.connector.service"].with_user(self.internal_user)
        service._apply_job_payload(job, payload)
        job.invalidate_recordset()
        self.assertEqual(job.writeback_state, "attached")
        if "product.image" in self.env.registry:
            self.assertTrue(
                self.env["product.image"].sudo().search_count(
                    [("product_tmpl_id", "=", self.product.id)]
                )
            )
        else:
            self.assertTrue(
                self.env["ir.attachment"].sudo().search_count(
                    [
                        ("res_model", "=", "product.template"),
                        ("res_id", "=", self.product.id),
                    ]
                )
            )

    def test_unique_job_id_race_creates_one_row(self):
        service = self.env["photostudio.connector.service"]
        payload = {
            "job_id": "race-job-1",
            "operation": "product_image.ghost",
            "status": "queued",
            "metadata": {
                "odoo_model": "product.template",
                "odoo_record_id": self.product.id,
            },
        }
        first = service._create_or_update_job(
            self.product,
            payload,
            "product_image.ghost",
            "add_gallery",
            False,
            "normal",
            "batch-race",
        )
        second = service._create_or_update_job(
            self.product,
            payload,
            "product_image.ghost",
            "add_gallery",
            False,
            "normal",
            "batch-race",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            self.env["photostudio.job"].search_count([("job_id", "=", "race-job-1")]),
            1,
        )

    def test_high_priority_job_renders(self):
        job = self.env["photostudio.job"].create(
            {
                "job_id": "historical-high-priority",
                "product_tmpl_id": self.product.id,
                "operation": "product_image.ghost",
                "status": "failed",
                "priority": "high",
            }
        )
        fields_to_read = job.read(["priority", "job_id"])[0]
        self.assertEqual(fields_to_read["priority"], "high")

    def test_wizard_replay_returns_notification(self):
        self.env["photostudio.job"].create(
            {
                "job_id": "existing-job",
                "batch_id": "existing-batch",
                "product_tmpl_id": self.product.id,
                "operation": "product_image.ghost",
                "status": "queued",
            }
        )
        wizard = self.env["photostudio.generate.wizard"].create(
            {
                "product_ids": [(6, 0, [self.product.id])],
                "operation": "product_image.ghost",
                "confirm_external_processing": True,
            }
        )

        def fake_enqueue(*args, **kwargs):
            return {"jobs": self.env["photostudio.job"], "replay": True}

        with patch.object(
            type(self.env["photostudio.connector.service"]),
            "enqueue_generation",
            fake_enqueue,
        ):
            action = wizard.action_confirm()
        self.assertEqual(action.get("tag"), "display_notification")
        self.assertIn("Identical request", action["params"]["message"])

    def test_wizard_requires_external_processing_consent(self):
        wizard = self.env["photostudio.generate.wizard"].create(
            {
                "product_ids": [(6, 0, [self.product.id])],
                "operation": "product_image.ghost",
            }
        )
        with self.assertRaises(UserError) as error:
            wizard.action_confirm()
        self.assertIn("Photostudio", str(error.exception))

    def test_wizard_selections_include_campaign_exclude_high(self):
        wizard_model = self.env["photostudio.generate.wizard"]
        operation_values = self._selection_map(wizard_model, "operation")
        priority_values = self._selection_map(wizard_model, "priority")
        self.assertIn("product_image.campaign", operation_values)
        self.assertNotIn("high", priority_values)

    def test_settings_priority_selection_excludes_high(self):
        settings_model = self.env["res.config.settings"]
        priority_values = self._selection_map(settings_model, "photostudio_priority")
        self.assertNotIn("high", priority_values)

    @patch("odoo.addons.photostudio_connector.services.photostudio_client.requests.request")
    def test_replay_notice_only_when_local_batch_exists(self, mock_request):
        mock_request.return_value.json.return_value = {
            "batch_id": "fresh-batch",
            "items": [
                {
                    "index": 0,
                    "job": {
                        "job_id": "fresh-job",
                        "operation": "product_image.ghost",
                        "status": "queued",
                        "metadata": {
                            "odoo_model": "product.template",
                            "odoo_record_id": self.product.id,
                        },
                    },
                }
            ],
        }
        mock_request.return_value.status_code = 202

        self.env["ir.config_parameter"].sudo().set_param(
            "photostudio_connector.api_key", "ps_test_key"
        )
        result = self.env["photostudio.connector.service"].enqueue_generation(
            self.product,
            "product_image.ghost",
            "add_gallery",
            "normal",
            False,
            False,
        )
        self.assertFalse(result["replay"])

        self.env["photostudio.job"].create(
            {
                "job_id": "old-job",
                "batch_id": "fresh-batch",
                "product_tmpl_id": self.product.id,
                "operation": "product_image.ghost",
                "status": "queued",
            }
        )
        mock_request.return_value.json.return_value = {
            "batch_id": "fresh-batch",
            "items": [],
        }
        result_replay = self.env["photostudio.connector.service"].enqueue_generation(
            self.product,
            "product_image.ghost",
            "add_gallery",
            "normal",
            False,
            False,
        )
        self.assertTrue(result_replay["replay"])

    @patch(
        "odoo.addons.photostudio_connector.services.photostudio_client.PhotostudioClient._download_output"
    )
    def test_download_failure_increments_attempts_without_rollback(self, mock_download):
        mock_download.side_effect = PhotostudioDownloadError(
            "Photostudio output download failed (403).",
            status_code=403,
        )
        job = self._completed_job(job_id="poison-job")
        service = self.env["photostudio.connector.service"]
        service._attach_ready_outputs(job)
        job.invalidate_recordset()
        self.assertEqual(job.download_attempts, 1)
        self.assertEqual(job.writeback_state, "pending")
        self.assertEqual(job.status, "completed")

    @patch(
        "odoo.addons.photostudio_connector.services.photostudio_client.PhotostudioClient._download_output"
    )
    def test_max_download_failures_mark_expired(self, mock_download):
        mock_download.side_effect = PhotostudioDownloadError(
            "Photostudio output download failed (403).",
            status_code=403,
        )
        service = self.env["photostudio.connector.service"]
        job = self._completed_job(
            job_id="expired-job",
            download_attempts=service.MAX_DOWNLOAD_ATTEMPTS - 1,
        )
        service._attach_ready_outputs(job)
        job.invalidate_recordset()
        self.assertEqual(job.download_attempts, service.MAX_DOWNLOAD_ATTEMPTS)
        self.assertEqual(job.writeback_state, "expired")

    def test_writeback_cron_selects_completed_pending(self):
        pending = self._completed_job(job_id="writeback-pending")
        attached = self._completed_job(job_id="writeback-attached")
        attached.write({"writeback_state": "attached"})
        expired = self._completed_job(job_id="writeback-expired")
        expired.write({"writeback_state": "expired"})

        domain = [
            ("status", "=", "completed"),
            ("writeback_state", "=", "pending"),
            ("download_attempts", "<", self.env["photostudio.connector.service"].MAX_DOWNLOAD_ATTEMPTS),
        ]
        selected = self.env["photostudio.job"].search(domain)
        self.assertIn(pending, selected)
        self.assertNotIn(attached, selected)
        self.assertNotIn(expired, selected)

    @patch("odoo.addons.photostudio_connector.services.photostudio_client.requests.get")
    def test_replace_main_only_once_after_retry(self, mock_get):
        self._mock_image_download(mock_get)

        job = self._completed_job(
            job_id="replace-main-job",
            replace_strategy="replace_main",
            outputs=[
                {
                    "type": "image",
                    "state": "ready",
                    "download_url": "https://test.supabase.co/storage/v1/object/sign/a",
                    "image_id": "img-a",
                },
                {
                    "type": "image",
                    "state": "ready",
                    "download_url": "https://test.supabase.co/storage/v1/object/sign/b",
                    "image_id": "img-b",
                },
            ],
        )
        service = self.env["photostudio.connector.service"]
        image_model = type(self.env["photostudio.image.service"])
        with patch.object(image_model, "replace_main") as replace_main, patch.object(
            image_model, "append_gallery"
        ) as append_gallery:
            service._attach_ready_outputs(job)
            self.assertEqual(replace_main.call_count, 1)
            self.assertEqual(append_gallery.call_count, 1)

            job.invalidate_recordset()
            service._attach_ready_outputs(job)
            self.assertEqual(replace_main.call_count, 1)
            self.assertEqual(append_gallery.call_count, 1)
            self.assertTrue(job.main_output_attached)

    def test_product_sync_uses_latest_batch_only(self):
        old_batch = self.env["photostudio.job"].create(
            {
                "job_id": "old-batch-job",
                "batch_id": "old-batch",
                "product_tmpl_id": self.product.id,
                "operation": "product_image.ghost",
                "status": "failed",
                "writeback_state": "failed",
            }
        )
        new_batch = self.env["photostudio.job"].create(
            {
                "job_id": "new-batch-job",
                "batch_id": "new-batch",
                "product_tmpl_id": self.product.id,
                "operation": "product_image.ghost",
                "status": "completed",
                "writeback_state": "pending",
                "completed_at": fields.Datetime.now(),
            }
        )
        self.env["photostudio.connector.service"]._refresh_product_state(self.product)
        self.product.invalidate_recordset()
        self.assertEqual(self.product.photostudio_sync_state, "attach_pending")
        self.assertEqual(self.product.last_job_id, new_batch.job_id)
        old_batch.unlink()

    def test_cron_sets_last_poll_at_after_sweep(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("photostudio_connector.api_key", "ps_test_key")
        params.set_param("photostudio_connector.poll_interval", "30")
        params.set_param("photostudio_connector.last_poll_at", False)
        self.env["photostudio.job"].create(
            {
                "job_id": "queued-job",
                "product_tmpl_id": self.product.id,
                "operation": "product_image.ghost",
                "status": "queued",
            }
        )
        before = fields.Datetime.now()
        with patch.object(
            type(self.env["photostudio.connector.service"]),
            "poll_job_isolated",
            lambda self, job: None,
        ):
            self.env["photostudio.job"]._cron_poll_active_jobs()
        last_poll_at = fields.Datetime.to_datetime(
            params.get_param("photostudio_connector.last_poll_at")
        )
        self.assertTrue(last_poll_at)
        self.assertGreaterEqual(last_poll_at, before)

    @patch(
        "odoo.addons.photostudio_connector.services.photostudio_client.PhotostudioClient._download_output"
    )
    def test_attach_isolated_refreshes_product_state(self, mock_download):
        mock_download.side_effect = PhotostudioDownloadError(
            "Photostudio output download failed (403).",
            status_code=403,
        )
        job = self._completed_job(job_id="attach-isolated-job")
        self.product.write({"photostudio_sync_state": "completed"})
        service = self.env["photostudio.connector.service"]
        service.attach_isolated(job)
        job.invalidate_recordset()
        self.product.invalidate_recordset()
        self.assertEqual(job.download_attempts, 1)
        self.assertEqual(self.product.photostudio_sync_state, "attach_pending")

    @patch(
        "odoo.addons.photostudio_connector.services.photostudio_client.PhotostudioClient._download_output"
    )
    def test_writeback_cron_uses_attach_isolated(self, mock_download):
        mock_download.side_effect = PhotostudioDownloadError(
            "Photostudio output download failed (403).",
            status_code=403,
        )
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("photostudio_connector.api_key", "ps_test_key")
        params.set_param("photostudio_connector.poll_interval", "30")
        params.set_param("photostudio_connector.last_poll_at", False)
        self._completed_job(job_id="writeback-cron-job")
        with patch.object(
            type(self.env["photostudio.connector.service"]),
            "poll_batch_isolated",
        ) as mock_poll_batch, patch.object(
            type(self.env["photostudio.connector.service"]),
            "poll_job_isolated",
        ) as mock_poll_job:
            self.env["photostudio.job"]._cron_poll_active_jobs()
        mock_poll_batch.assert_not_called()
        mock_poll_job.assert_not_called()

    def test_wizard_core_set_label(self):
        wizard_model = self.env["photostudio.generate.wizard"]
        operation_values = self._selection_map(wizard_model, "operation")
        self.assertIn("all", operation_values)
        self.assertIn("Ghost", operation_values["all"])
        self.assertNotEqual(operation_values["all"], "All")

    def test_wizard_hardcoded_options_present(self):
        wizard_model = self.env["photostudio.generate.wizard"]
        model_values = self._selection_map(wizard_model, "model_preset")
        pose_values = self._selection_map(wizard_model, "pose")
        scene_values = self._selection_map(wizard_model, "scene_preset")
        self.assertEqual(len(model_values), 4)
        self.assertEqual(len(pose_values), 4)
        self.assertEqual(len(scene_values), 8)
        self.assertNotIn("detail", pose_values)

import base64
import io
from unittest.mock import patch

from PIL import Image
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


def png(color):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


@tagged("post_install", "-at_install", "photostudio_connector")
class TestPhotostudioSecurity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a, cls.company_b = cls.env["res.company"].create([
            {"name": "Security A", "logo": False},
            {"name": "Security B", "logo": False},
        ])
        cls.employee = cls.env["res.users"].create({
            "name": "Security employee", "login": "security_employee",
            "company_id": cls.company_a.id,
            "company_ids": [(6, 0, cls.company_a.ids)],
            "group_ids": [(6, 0, cls.env.ref("base.group_user").ids)],
        })
        cls.product = cls.env["product.template"].create({
            "name": "Restricted product", "company_id": cls.company_b.id,
            "image_1920": base64.b64encode(png("red")),
        })

    def job_values(self):
        return {
            "job_id": "security-forged-job", "product_tmpl_id": self.product.id,
            "operation": "product_image.ghost", "status": "completed",
            "writeback_state": "pending", "replace_strategy": "replace_main",
            "outputs": [{"type": "image", "state": "ready", "image_id": "security-image",
                         "download_url": "https://allowed.invalid/image.png"}],
        }

    def test_employee_cannot_forge_completed_cross_company_job(self):
        with self.assertRaises(AccessError):
            self.product.with_user(self.employee).write({"image_1920": base64.b64encode(png("blue"))})
        with self.assertRaises(AccessError):
            self.env["photostudio.job"].with_user(self.employee).create(self.job_values())

    def test_employee_cannot_mutate_job(self):
        job = self.env["photostudio.job"].create(self.job_values())
        with self.assertRaises(AccessError):
            job.with_user(self.employee).write({"outputs": [], "status": "completed"})

    def test_employee_cannot_enqueue_before_paid_call(self):
        product = self.env["product.template"].create({"name": "Shared", "image_1920": base64.b64encode(png("red"))})
        service = self.env["photostudio.connector.service"].with_user(self.employee)
        with patch.object(type(self.env["photostudio.client"]), "_create_job_batch", return_value={}) as remote:
            with self.assertRaises(AccessError):
                service.enqueue_generation(product, "product_image.ghost", "replace_main", "normal", False)
            remote.assert_not_called()

    def test_employee_cannot_use_indirect_attachment(self):
        job = self.env["photostudio.job"].create(self.job_values())
        service = self.env["photostudio.connector.service"].with_user(self.employee)
        before = self.product.image_1920
        with patch.object(type(self.env["photostudio.client"]), "_download_output", return_value=png("blue")) as remote:
            with self.assertRaises(AccessError):
                service._attach_ready_outputs(job)
            remote.assert_not_called()
        self.assertEqual(self.product.image_1920, before)

    def operator(self):
        self.employee.write({"group_ids": [(4, self.env.ref("photostudio_connector.group_photostudio_user").id),
                                           (4, self.env.ref("product.group_product_manager").id)]})
        return self.employee

    def submitted_job(self):
        user = self.operator()
        product = self.env["product.template"].create({"name": "Authorized target", "company_id": self.company_a.id,
                                                       "image_1920": base64.b64encode(png("red"))})
        service = self.env["photostudio.connector.service"].with_user(user).with_context(allowed_company_ids=self.company_a.ids)
        payload = {"job_id": "authorized-job", "operation": "product_image.ghost", "status": "completed",
                   "outputs": self.job_values()["outputs"],
                   "metadata": {"odoo_model": "product.template", "odoo_record_id": product.id}}
        with patch.object(type(self.env["photostudio.client"]), "_create_job_batch",
                          return_value={"batch_id": "authorized-batch", "jobs": [payload]}) as remote:
            result = service.enqueue_generation(product, "product_image.ghost", "replace_main", "normal", False)
            remote.assert_called_once()
        return product, result["jobs"]

    def test_authorized_submission_and_cron_attachment(self):
        product, job = self.submitted_job()
        self.assertEqual(job.requesting_user_id, self.employee)
        self.assertEqual(job.company_id, self.company_a)
        self.assertFalse(job.env.su)
        with self.assertRaises(AccessError):
            job.write({"outputs": []})
        before = product.image_1920
        self.run_cron()
        self.assertNotEqual(product.image_1920, before)
        self.assertEqual(job.writeback_state, "attached")

    def run_cron(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("photostudio_connector.api_key", "synthetic-not-a-live-key")
        params.set_param("photostudio_connector.last_poll_at", "")
        with patch.object(type(self.env["photostudio.client"]), "_download_output", return_value=png("blue")) as remote:
            self.env["photostudio.job"]._cron_poll_active_jobs()
        return remote

    def test_cron_rechecks_revoked_product_write_rights(self):
        product, job = self.submitted_job()
        before = product.image_1920
        self.employee.write({"group_ids": [(3, self.env.ref("product.group_product_manager").id)]})
        self.run_cron().assert_not_called()
        self.assertEqual(product.image_1920, before)
        self.assertEqual(job.writeback_state, "pending")

    def test_cron_rechecks_revoked_connector_role(self):
        product, job = self.submitted_job()
        self.employee.write({"group_ids": [(3, self.env.ref("photostudio_connector.group_photostudio_user").id)]})
        self.run_cron().assert_not_called()
        self.assertEqual(job.writeback_state, "pending")

    def test_untrusted_legacy_job_fails_closed_in_cron(self):
        values = {**self.job_values(), "requesting_user_id": False}
        job = self.env["photostudio.job"].create(values)
        before = self.product.image_1920
        with self.assertRaises(AccessError):
            self.env["photostudio.connector.service"]._attach_ready_outputs(job)
        self.run_cron().assert_not_called()
        self.assertEqual(self.product.image_1920, before)

    def test_operator_cannot_access_another_company_even_with_sudo_target(self):
        user = self.operator()
        service = self.env["photostudio.connector.service"].with_user(user).with_context(allowed_company_ids=self.company_a.ids)
        job = self.env["photostudio.job"].create({**self.job_values(), "batch_id": "private-batch"})
        with patch.object(type(self.env["photostudio.client"]), "_create_job_batch") as remote:
            with self.assertRaises(AccessError):
                service.enqueue_generation(self.product.sudo(), "product_image.ghost", "replace_main", "normal", False)
            remote.assert_not_called()
        for method, argument, remote_name in [
            ("poll_job", job, "_get_job"), ("poll_job_isolated", job, "_get_job"),
            ("_cancel_job", job, "_cancel_job"), ("poll_batch", "private-batch", "_get_batch"),
            ("poll_batch_isolated", "private-batch", "_get_batch"), ("attach_isolated", job, "_download_output"),
        ]:
            with self.subTest(method=method), patch.object(type(self.env["photostudio.client"]), remote_name) as remote:
                with self.assertRaises(AccessError):
                    getattr(service, method)(argument)
                remote.assert_not_called()
        with self.assertRaises(AccessError):
            job.with_user(user).read(["outputs"])

    def test_target_identity_is_immutable_even_in_server_write(self):
        product, job = self.submitted_job()
        for values in [{"product_tmpl_id": self.product.id}, {"requesting_user_id": self.env.uid},
                       {"company_id": self.company_b.id}, {"job_id": "replacement"}]:
            with self.subTest(values=values), self.assertRaises(AccessError):
                job.sudo().write(values)
        with self.assertRaises(AccessError):
            self.env["photostudio.job"].create({**self.job_values(), "product_id": product.product_variant_id.id})

    def test_moved_product_never_attaches(self):
        product, job = self.submitted_job()
        product.company_id = self.company_b
        self.run_cron().assert_not_called()
        self.assertEqual(job.writeback_state, "pending")

    def test_role_without_product_write_rights_cannot_submit(self):
        self.employee.write({"group_ids": [(4, self.env.ref("photostudio_connector.group_photostudio_user").id)]})
        shared = self.env["product.template"].create({"name": "Read-only target", "image_1920": base64.b64encode(png("red"))})
        with patch.object(type(self.env["photostudio.client"]), "_create_job_batch") as remote:
            with self.assertRaises(AccessError):
                self.env["photostudio.connector.service"].with_user(self.employee).enqueue_generation(
                    shared, "product_image.ghost", "replace_main", "normal", False)
            remote.assert_not_called()

    def test_cron_rechecks_disabled_requester(self):
        product, job = self.submitted_job()
        self.employee.active = False
        self.run_cron().assert_not_called()
        self.assertEqual(job.sudo().writeback_state, "pending")

    def test_cron_rechecks_revoked_company_membership(self):
        product, job = self.submitted_job()
        self.employee.write({"company_id": self.company_b.id, "company_ids": [(6, 0, self.company_b.ids)]})
        self.run_cron().assert_not_called()
        self.assertEqual(job.sudo().writeback_state, "pending")

    def test_raw_transport_is_rejected_by_rpc_for_connector_only_user(self):
        from odoo.service.model import call_kw
        self.employee.write({"group_ids": [(4, self.env.ref("photostudio_connector.group_photostudio_user").id)]})
        client = self.env["photostudio.client"].with_user(self.employee)
        for name in ("create_job_batch", "get_job", "get_batch", "cancel_job", "download_output", "deterministic_key"):
            self.assertFalse(hasattr(client, name))
            with self.subTest(name=name), self.assertRaises(AttributeError):
                call_kw(client, name, [], {})
            with self.subTest(name="_" + name), self.assertRaises(AccessError):
                call_kw(client, "_" + name, [], {})

    def test_untrusted_old_page_does_not_starve_new_job(self):
        self.env["photostudio.job"].sudo().create([
            {**self.job_values(), "job_id": "untrusted-old-%s" % n, "status": "queued",
             "requesting_user_id": False} for n in range(105)
        ])
        product, job = self.submitted_job()
        job.sudo().write({"status": "queued", "batch_id": False})
        with patch.object(type(self.env["photostudio.client"]), "_get_job",
                          return_value={"job_id": job.job_id, "status": "completed", "outputs": job.outputs}) as get:
            self.run_cron()
        get.assert_called_once_with(job.job_id)
        self.assertEqual(job.writeback_state, "attached")

    def test_revoked_job_stays_blocked_until_explicit_revalidation(self):
        product, job = self.submitted_job()
        self.employee.write({"group_ids": [(3, self.env.ref("product.group_product_manager").id)]})
        self.run_cron().assert_not_called()
        self.assertTrue(job.sudo().authorization_blocked)
        self.assertEqual(job.download_attempts, 0)
        with patch.object(type(self.env["photostudio.connector.service"]), "_authorize_job") as authorize:
            self.run_cron().assert_not_called()
        authorize.assert_not_called()
        self.operator()
        self.run_cron().assert_not_called()
        with patch.object(type(self.env["photostudio.client"]), "_get_batch") as batch:
            with self.assertRaises(AccessError):
                self.env["photostudio.connector.service"].poll_batch_isolated(job.batch_id)
            batch.assert_not_called()
        with patch.object(type(self.env["photostudio.client"]), "_get_job",
                          return_value={"job_id": job.job_id, "status": "completed", "outputs": job.outputs}), \
                patch.object(type(self.env["photostudio.client"]), "_download_output", return_value=png("blue")):
            job.action_poll_status()
        self.assertFalse(job.authorization_blocked)
        self.assertFalse(job.error_message)
        self.assertEqual(job.writeback_state, "attached")

    def test_denied_batch_peer_does_not_block_authorized_peer(self):
        product, good = self.submitted_job()
        good.sudo().write({"status": "queued"})
        denied = self.env["photostudio.job"].sudo().create({
            **self.job_values(), "job_id": "denied-batch-peer", "batch_id": good.batch_id,
            "status": "queued", "requesting_user_id": self.employee.id,
        })
        service = self.env["photostudio.connector.service"]
        with patch.object(type(self.env["photostudio.client"]), "_get_batch") as batch, \
                patch.object(type(self.env["photostudio.client"]), "_get_job",
                             return_value={"job_id": good.job_id, "status": "completed", "outputs": good.outputs}) as get:
            with self.assertRaises(AccessError):
                service.poll_batch_isolated(good.batch_id)
            self.run_cron()
        batch.assert_not_called()
        get.assert_called_once_with(good.job_id)
        self.assertTrue(denied.authorization_blocked)
        self.assertEqual(good.writeback_state, "attached")

    def test_duplicate_output_in_single_payload_attaches_once(self):
        product, job = self.submitted_job()
        job.sudo().write({"outputs": job.outputs * 2, "replace_strategy": "add_gallery"})
        with patch.object(type(self.env["photostudio.image.service"]), "append_gallery") as append:
            remote = self.run_cron()
        remote.assert_called_once()
        append.assert_called_once()
        self.assertEqual(len(job.metadata["attached_output_ids"]), 1)

    def test_system_group_implies_connector_role_but_cannot_forge_jobs(self):
        self.employee.write({"group_ids": [(4, self.env.ref("base.group_system").id)]})
        self.assertTrue(self.employee.has_group("photostudio_connector.group_photostudio_user"))
        with self.assertRaises(AccessError):
            self.env["photostudio.job"].with_user(self.employee).create(self.job_values())

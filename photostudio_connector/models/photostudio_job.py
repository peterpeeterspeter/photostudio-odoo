from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from ..utils.operations import job_operation_selection


class PhotostudioJob(models.Model):
    _name = "photostudio.job"
    _description = "Photostudio Job"
    _order = "create_date desc, id desc"
    _rec_name = "job_id"
    _photostudio_job_job_id_unique = models.Constraint(
        "unique(job_id)",
        "Photostudio job ID must be unique.",
    )

    # No field default: pre-upgrade rows must remain untrusted until reviewed.
    requesting_user_id = fields.Many2one("res.users", readonly=True, copy=False, ondelete="restrict")
    company_id = fields.Many2one("res.company", readonly=True, copy=False, index=True, ondelete="restrict")
    authorization_blocked = fields.Boolean(default=False, readonly=True, copy=False, index=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError("Photostudio jobs can only be created by the connector.")
        prepared = []
        for vals in vals_list:
            vals = dict(vals)
            product = self.env["product.template"].browse(vals.get("product_tmpl_id"))
            vals.setdefault("requesting_user_id", self.env.uid)
            vals.setdefault("company_id", product.company_id.id or self.env.company.id)
            prepared.append(vals)
        jobs = super().create(prepared)
        jobs._check_target_consistency()
        return jobs

    def write(self, vals):
        if not self.env.su:
            raise AccessError("Photostudio jobs are server-owned and read-only.")
        immutable = {"job_id", "product_tmpl_id", "product_id", "company_id", "requesting_user_id", "operation"}
        for job in self:
            for name in immutable.intersection(vals):
                current = job[name]
                current = current.id if job._fields[name].type == "many2one" else current
                if (current or False) != (vals[name] or False):
                    raise AccessError("A Photostudio job's target and requester cannot be changed.")
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError("Photostudio jobs are server-owned and read-only.")
        return super().unlink()

    def _check_target_consistency(self):
        for job in self:
            if not job.product_tmpl_id.exists() or (
                job.product_id and job.product_id.product_tmpl_id != job.product_tmpl_id
            ) or (job.product_tmpl_id.company_id and job.product_tmpl_id.company_id != job.company_id):
                raise AccessError("Photostudio job target no longer matches its authorized product and company.")

    job_id = fields.Char(required=True, copy=False, index=True)
    batch_id = fields.Char(copy=False, index=True)
    product_tmpl_id = fields.Many2one(
        "product.template",
        string="Product",
        required=True,
        ondelete="cascade",
        index=True,
    )
    product_id = fields.Many2one(
        "product.product",
        string="Variant",
        ondelete="cascade",
        index=True,
    )
    job_type = fields.Selection(
        [("generation", "Generation")],
        default="generation",
        required=True,
        readonly=True,
    )
    operation = fields.Selection(
        selection="_selection_operation",
        required=True,
        index=True,
    )
    status = fields.Selection(
        [
            ("queued", "Queued"),
            ("processing", "Processing"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        default="queued",
        required=True,
        index=True,
    )
    writeback_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("attached", "Attached"),
            ("expired", "Expired"),
            ("failed", "Failed"),
        ],
        string="Writeback",
        default="pending",
        required=True,
        index=True,
    )
    download_attempts = fields.Integer(default=0, readonly=True)
    main_output_attached = fields.Boolean(default=False, readonly=True)
    progress = fields.Char(default="queued")
    created_at = fields.Datetime(readonly=True)
    updated_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(string="Completed")
    credits_used = fields.Float(readonly=True)
    error_message = fields.Text(readonly=True)
    processing_time = fields.Float(string="Processing Time", readonly=True)
    replace_strategy = fields.Selection(
        [
            ("replace_main", "Replace Main Image"),
            ("add_gallery", "Add to Gallery"),
        ],
        default="add_gallery",
        required=True,
    )
    auto_publish = fields.Boolean(default=False)
    priority = fields.Selection(
        [("low", "Low"), ("normal", "Normal"), ("high", "High")],
        default="normal",
        required=True,
    )
    outputs = fields.Json(default=list)
    errors = fields.Json(default=list)
    links = fields.Json(default=dict)
    metadata = fields.Json(default=dict)

    output_count = fields.Integer(compute="_compute_output_count", store=False)
    duration_display = fields.Char(compute="_compute_duration_display", store=False)

    @api.model
    def _selection_operation(self):
        return job_operation_selection()

    @api.depends("outputs")
    def _compute_output_count(self):
        for job in self:
            job.output_count = len(job.outputs or [])

    @api.depends("processing_time")
    def _compute_duration_display(self):
        for job in self:
            seconds = job.processing_time or 0.0
            job.duration_display = f"{seconds:.1f}s" if seconds else ""

    def action_poll_status(self):
        service = self.env["photostudio.connector.service"]
        service._check_connector_access()
        for job in self:
            service._authorize_job(job)
        for job in self:
            if job.authorization_blocked:
                job.sudo().write({"authorization_blocked": False, "error_message": False})
            service.poll_job_isolated(job)
        return True

    def action_retry_writeback(self):
        service = self.env["photostudio.connector.service"]
        service._check_connector_access()
        for job in self:
            service._authorize_job(job)
            job.lock_for_update()
            job.invalidate_recordset()
            if job.status != "completed" or job.writeback_state not in ("expired", "failed"):
                raise UserError("Only completed jobs with failed or expired image downloads can be retried.")
            job.sudo().write({"writeback_state": "pending", "download_attempts": 0, "error_message": False})
            # Only GET existing output metadata. Do not submit another generation.
            service.poll_job_isolated(job)
        return True

    def action_cancel(self):
        self.env["photostudio.connector.service"]._check_connector_access()
        for job in self:
            self.env["photostudio.connector.service"]._cancel_job(job)
        return True

    @api.model
    def _cron_poll_active_jobs(self, limit=100):
        self.env["photostudio.connector.service"]._check_connector_access()
        params = self.env["ir.config_parameter"].sudo()
        if not params.get_param("photostudio_connector.api_key"):
            return
        try:
            interval_seconds = int(
                params.get_param("photostudio_connector.poll_interval") or 120
            )
        except ValueError:
            interval_seconds = 120
        interval_seconds = min(max(interval_seconds, 30), 3600)
        now = fields.Datetime.now()
        last_poll_at = fields.Datetime.to_datetime(
            params.get_param("photostudio_connector.last_poll_at")
        )
        if last_poll_at and (now - last_poll_at).total_seconds() < interval_seconds:
            return

        service = self.env["photostudio.connector.service"]
        max_attempts = service.MAX_DOWNLOAD_ATTEMPTS
        eligible = [("requesting_user_id", "!=", False), ("company_id", "!=", False),
                    ("authorization_blocked", "=", False)]
        remote_jobs = self.search(
            eligible + [("status", "in", ("queued", "processing"))],
            order="create_date asc",
            limit=limit,
        )
        writeback_jobs = self.search(
            eligible + [
                ("status", "=", "completed"),
                ("writeback_state", "=", "pending"),
                ("download_attempts", "<", max_attempts),
            ],
            order="create_date asc",
            limit=limit,
        )

        polled_batches = set()
        denied_batches = set()
        for job in remote_jobs:
            try:
                service._authorize_job(job)
                if job.batch_id:
                    if job.batch_id in polled_batches:
                        continue
                    if job.batch_id not in denied_batches:
                        try:
                            service.poll_batch_isolated(job.batch_id)
                            polled_batches.add(job.batch_id)
                            continue
                        except AccessError:
                            denied_batches.add(job.batch_id)
                    service.poll_job_isolated(job)
                else:
                    service.poll_job_isolated(job)
            except AccessError:
                job._block_authorization()

        for job in writeback_jobs:
            try:
                service.attach_isolated(job)
            except AccessError:
                job._block_authorization()

        params.set_param(
            "photostudio_connector.last_poll_at",
            fields.Datetime.to_string(now),
        )

    def _block_authorization(self):
        self.sudo().write({
            "authorization_blocked": True,
            "error_message": "Automatic processing blocked: authorization is no longer valid. "
                             "Restore access and use Poll Status to revalidate.",
        })

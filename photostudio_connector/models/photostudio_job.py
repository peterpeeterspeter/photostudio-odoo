from odoo import api, fields, models

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
        ondelete="set null",
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
        batch_ids = set(self.filtered(lambda job: job.batch_id).mapped("batch_id"))
        for batch_id in batch_ids:
            service.poll_batch_isolated(batch_id)
        for job in self.filtered(lambda record: not record.batch_id):
            service.poll_job_isolated(job)
        return True

    def action_cancel(self):
        for job in self:
            self.env["photostudio.connector.service"].cancel_job(job)
        return True

    @api.model
    def _cron_poll_active_jobs(self, limit=100):
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
        remote_jobs = self.search(
            [("status", "in", ("queued", "processing"))],
            order="create_date asc",
            limit=limit,
        )
        writeback_jobs = self.search(
            [
                ("status", "=", "completed"),
                ("writeback_state", "=", "pending"),
                ("download_attempts", "<", max_attempts),
            ],
            order="create_date asc",
            limit=limit,
        )

        polled_batches = set()
        for job in remote_jobs:
            if job.batch_id:
                if job.batch_id in polled_batches:
                    continue
                service.poll_batch_isolated(job.batch_id)
                polled_batches.add(job.batch_id)
            else:
                service.poll_job_isolated(job)

        for job in writeback_jobs:
            service.attach_isolated(job)

        params.set_param(
            "photostudio_connector.last_poll_at",
            fields.Datetime.to_string(now),
        )

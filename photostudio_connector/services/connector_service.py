import base64
import uuid

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError
from psycopg2 import IntegrityError

from ..utils.download_errors import PhotostudioDownloadError
from ..utils.generation_options import (
    DEFAULT_MODEL_PRESET,
    DEFAULT_POSE,
    DEFAULT_SCENE_PRESET,
    normalize_background_prompt,
    normalize_model_catalog_id,
    validate_model_preset,
    validate_pose,
    validate_scene_preset,
)
from ..utils.idempotency import attach_client_reference, build_batch_idempotency_key
from ..utils.mime_sniff import sniff_image_mime
from ..utils.operations import (
    OPERATION_LABELS,
    SUPPORTED_OPERATIONS,
    expand_requested_operations,
    extract_batch_job_payloads,
)

MAX_BASE64_BATCH_ASSETS = 50
MAX_BATCH_DECODED_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_ATTEMPTS = 5


class PhotostudioConnectorService(models.AbstractModel):
    _name = "photostudio.connector.service"
    _description = "Photostudio Connector Service"

    MAX_DOWNLOAD_ATTEMPTS = MAX_DOWNLOAD_ATTEMPTS

    def _check_connector_access(self):
        if not self.env.su and (not self.env.user.active or not self.env.user.has_group(
            "photostudio_connector.group_photostudio_user"
        )):
            raise AccessError("Photostudio connector permission is required.")

    def _authorize_products(self, records):
        self._check_connector_access()
        if records._name not in ("product.template", "product.product"):
            raise AccessError("Only product templates and variants are supported.")
        # Never inherit sudo from a caller-supplied recordset.
        records = records.with_env(self.env)
        if records.exists() != records:
            raise AccessError("The target product no longer exists.")
        records.check_access("read")
        records.check_access("write")
        if records._name == "product.product":
            records.product_tmpl_id.check_access("read")
            records.product_tmpl_id.check_access("write")
        return records

    def _authorize_job(self, job):
        self._check_connector_access()
        if job._name != "photostudio.job":
            raise AccessError("Invalid Photostudio job.")
        job = job.with_env(self.env)
        job.ensure_one()
        job.check_access("read")
        trusted = job.sudo()
        trusted._check_target_consistency()
        requester = trusted.requesting_user_id
        # Odoo's technical superuser is intentionally inactive, but remains a
        # valid explicit server-side requester (including synthetic test jobs).
        if not requester or (not requester.active and requester.id != SUPERUSER_ID) or not trusted.company_id:
            raise AccessError("This legacy job has no trusted requesting user and company.")
        requester_service = self.with_user(requester).with_context(
            {}, allowed_company_ids=[trusted.company_id.id]
        )
        if not requester_service.env.su and trusted.company_id not in requester.company_ids:
            raise AccessError("The requesting user no longer has access to this company.")
        target = trusted.product_id or trusted.product_tmpl_id
        target = requester_service._authorize_products(target)
        if not self.env.su:
            self._authorize_products(target)
        return target

    def _authorized_batch_jobs(self, batch_id):
        self._check_connector_access()
        jobs = self.env["photostudio.job"].sudo().search([("batch_id", "=", batch_id)]) if batch_id else self.env["photostudio.job"]
        if not jobs:
            raise AccessError("No authorized local batch exists.")
        for job in jobs:
            if job.authorization_blocked:
                raise AccessError("A batch member requires explicit authorization revalidation.")
            self._authorize_job(job)
        return jobs.with_env(self.env)

    def _submission_target_values(self, product_tmpl, product_id=False):
        product_tmpl = self._authorize_products(product_tmpl)
        product_tmpl.ensure_one()
        if product_id:
            variant = self._authorize_products(self.env["product.product"].browse(product_id))
            if variant.product_tmpl_id != product_tmpl:
                raise AccessError("The variant does not belong to the requested template.")
        return {"requesting_user_id": self.env.uid,
                "company_id": product_tmpl.company_id.id or self.env.company.id}

    @api.model
    def enqueue_generation(
        self,
        records,
        operation,
        replace_strategy,
        priority,
        auto_publish,
        use_extra_media=None,
        generation_options=None,
        regenerate_nonce=0,
    ):
        records = self._authorize_products(records)
        if not records:
            raise UserError("Select at least one product.")
        requested_operations = expand_requested_operations(operation)
        unsupported = [
            requested_operation
            for requested_operation in requested_operations
            if requested_operation not in SUPPORTED_OPERATIONS
        ]
        if unsupported:
            unsupported_label = OPERATION_LABELS.get(unsupported[0], unsupported[0])
            label = OPERATION_LABELS.get(operation, operation)
            raise UserError(
                f"{label} is not available in the Jobs API MVP. "
                f"Unsupported operation: {unsupported_label}."
            )
        if use_extra_media is None:
            use_extra_media = self._default_use_extra_media()
        generation_options = generation_options or {}
        config = self._job_config(priority)
        candidates = []
        image_service = self.env["photostudio.image.service"]

        for record in records:
            context = self._record_generation_context(record)
            images = image_service.extract(record)
            if not images:
                for requested_operation in requested_operations:
                    self._create_missing_image_job(
                        context["product_tmpl_id"],
                        requested_operation,
                        replace_strategy,
                        auto_publish,
                        product_id=context["product_id"],
                    )
                continue
            primary = images[0]
            additional_images = []
            if use_extra_media:
                additional_images = [
                    {
                        "base64": image["base64"],
                        "mime": image["mime"],
                        "filename": image["filename"],
                    }
                    for image in images[1:5]
                ]
            for requested_operation in requested_operations:
                job_input = {
                    "base64": primary["base64"],
                    "mime": primary["mime"],
                    "filename": primary["filename"],
                }
                if additional_images:
                    job_input["additional_images"] = additional_images
                job_input["options"] = self._input_options_for_operation(
                    requested_operation,
                    generation_options,
                )
                candidates.append(
                    {
                        "product_tmpl_id": context["product_tmpl_id"],
                        "product_id": context["product_id"],
                        "odoo_model": context["odoo_model"],
                        "odoo_record_id": context["odoo_record_id"],
                        "operation": requested_operation,
                        "base64_asset_count": 1 + len(additional_images),
                        "item": {
                            "job_type": "generation",
                            "operation": requested_operation,
                            "input": job_input,
                            "metadata": {
                                "odoo_model": context["odoo_model"],
                                "odoo_record_id": context["odoo_record_id"],
                            },
                        },
                    }
                )

        if not candidates:
            return {"jobs": self.env["photostudio.job"], "replay": False}

        client = self.env["photostudio.client"]
        created_jobs = self.env["photostudio.job"]
        saw_replay = False
        database_uuid = self._database_uuid()

        for chunk in self._chunk_by_base64_assets(candidates):
            batch_body = {
                "config": config,
                "metadata": {},
                "items": [candidate["item"] for candidate in chunk],
            }
            sniffed_body = self._apply_mime_sniff_to_batch(batch_body)
            key = build_batch_idempotency_key(
                database_uuid,
                replace_strategy,
                auto_publish,
                sniffed_body,
                regenerate_nonce=regenerate_nonce,
            )
            post_body = attach_client_reference(sniffed_body, key)
            self._enforce_batch_byte_budget(post_body)

            try:
                response = client._create_job_batch(post_body, key)
            except UserError as error:
                for candidate in chunk:
                    self._create_error_job(
                        candidate["product_tmpl_id"],
                        candidate["operation"],
                        replace_strategy,
                        auto_publish,
                        priority,
                        False,
                        {
                            "code": "BATCH_SUBMISSION_FAILED",
                            "message": str(error),
                            "retryable": True,
                        },
                        product_id=candidate["product_id"],
                    )
                continue

            remote_batch_id = response.get("batch_id")
            if remote_batch_id and self._is_local_replay(remote_batch_id):
                saw_replay = True

            created_jobs |= self._reconcile_submission_response(
                response,
                chunk,
                replace_strategy,
                auto_publish,
                priority,
            )
        return {"jobs": created_jobs, "replay": saw_replay}

    @api.model
    def _record_generation_context(self, record):
        if record._name == "product.product":
            return {
                "product_tmpl_id": record.product_tmpl_id,
                "product_id": record.id,
                "odoo_model": "product.product",
                "odoo_record_id": record.id,
            }
        return {
            "product_tmpl_id": record,
            "product_id": False,
            "odoo_model": "product.template",
            "odoo_record_id": record.id,
        }

    @api.model
    def _input_options_for_operation(self, operation, generation_options):
        options = {}
        if operation in (
            "product_image.on_model",
            "product_image.lifestyle",
            "product_image.photoshoot",
        ):
            model_preset = generation_options.get("model_preset") or DEFAULT_MODEL_PRESET
            validate_model_preset(model_preset)
            options["model_preset"] = model_preset
        if operation in ("product_image.on_model", "product_image.photoshoot"):
            pose = generation_options.get("pose") or DEFAULT_POSE
            validate_pose(pose)
            options["pose"] = pose
        if operation == "product_image.lifestyle":
            scene_preset = generation_options.get("scene_preset") or DEFAULT_SCENE_PRESET
            validate_scene_preset(scene_preset)
            options["scene_preset"] = scene_preset
        if operation == "product_image.photoshoot":
            background_prompt = normalize_background_prompt(
                generation_options.get("background_prompt")
            )
            if background_prompt:
                options["background_prompt"] = background_prompt
        if operation in (
            "product_image.on_model",
            "product_image.lifestyle",
            "product_image.photoshoot",
        ):
            model_catalog_id = normalize_model_catalog_id(
                generation_options.get("model_catalog_id")
            )
            if model_catalog_id:
                options["model_catalog_id"] = model_catalog_id
        if operation == "product_image.campaign":
            options["marketplace"] = generation_options.get("marketplace") or "zalando"
            options["view_code"] = generation_options.get("view_code") or "packshot"
        return options

    @api.model
    def poll_job(self, job):
        self._authorize_job(job)
        payload = self.env["photostudio.client"]._get_job(job.job_id)
        self._apply_job_payload(job, payload)
        return job

    @api.model
    def poll_job_isolated(self, job):
        self._authorize_job(job)
        try:
            payload = self.env["photostudio.client"]._get_job(job.job_id)
        except AccessError:
            raise
        except Exception:
            self._record_refresh_failure(job)
            return
        self._apply_payload_isolated(job, payload)

    @api.model
    def poll_batch(self, batch_id):
        self._authorized_batch_jobs(batch_id)
        payload = self.env["photostudio.client"]._get_batch(batch_id)
        for job_payload in extract_batch_job_payloads(payload):
            job = self.env["photostudio.job"].search(
                [("job_id", "=", job_payload.get("job_id")), ("batch_id", "=", batch_id)],
                limit=1,
            )
            if job:
                self._apply_job_payload(job, job_payload)
        return payload

    @api.model
    def poll_batch_isolated(self, batch_id):
        jobs = self._authorized_batch_jobs(batch_id)
        try:
            payload = self.env["photostudio.client"]._get_batch(batch_id)
            payloads = extract_batch_job_payloads(payload)
        except AccessError:
            raise
        except Exception:
            for job in jobs:
                self._record_refresh_failure(job)
            return
        for job_payload in payloads:
            if not isinstance(job_payload, dict):
                continue
            job = jobs.filtered(lambda record: record.job_id == job_payload.get("job_id"))[:1]
            if job:
                self._apply_payload_isolated(job, job_payload)

    def _apply_payload_isolated(self, job, payload):
        try:
            with self.env.cr.savepoint():
                self._apply_job_payload(job, payload)
        except AccessError:
            raise
        except Exception:
            self._record_refresh_failure(job)

    def _record_refresh_failure(self, job):
        if job.status == "completed" and job.writeback_state == "pending":
            self._record_writeback_failure(job)
        else:
            job.sudo().write({"error_message": "Photostudio status refresh failed; it will be retried."})

    @api.model
    def attach_isolated(self, job):
        self._authorize_job(job)
        if job.writeback_state != "pending" or job.status != "completed":
            return
        if job.download_attempts:
            # Read-only refresh mints current signed URLs; never generate again.
            self.poll_job_isolated(job)
            return
        self._attach_outputs_isolated(job)
        self._refresh_job_targets(job)

    def _attach_outputs_isolated(self, job):
        self._authorize_job(job)
        try:
            with self.env.cr.savepoint():
                self._attach_ready_outputs(job)
        except AccessError:
            raise
        except Exception:
            # A malformed image/stream must neither spin forever nor abort peers.
            self._record_writeback_failure(job)

    def _record_writeback_failure(self, job, status_code=None):
        job.lock_for_update()
        job.invalidate_recordset()
        attempts = job.download_attempts + 1
        job.sudo().write({
            "download_attempts": attempts,
            "writeback_state": "expired" if attempts >= self.MAX_DOWNLOAD_ATTEMPTS else "pending",
            "error_message": f"Image download/writeback failed ({status_code})." if status_code else
                             "Image download/writeback failed. Retry will refresh the download link.",
        })

    @api.model
    def _cancel_job(self, job):
        self._authorize_job(job)
        payload = self.env["photostudio.client"]._cancel_job(job.job_id)
        if payload.get("job"):
            self._apply_job_payload(job, payload["job"])
        return job

    def _job_config(self, priority):
        params = self.env["ir.config_parameter"].sudo()
        config = {
            "workspace": params.get_param("photostudio_connector.workspace") or "",
            "integration": "odoo",
            "priority": priority or params.get_param("photostudio_connector.priority")
            or "normal",
        }
        use_webhook = (
            params.get_param("photostudio_connector.use_webhook", "False") == "True"
        )
        webhook_secret = params.get_param("photostudio_connector.webhook_secret") or ""
        if use_webhook and webhook_secret:
            base_url = (params.get_param("web.base.url") or "").rstrip("/")
            if base_url.startswith("https://"):
                config["webhook_url"] = f"{base_url}/photostudio/webhook"
        return config

    def _database_uuid(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("database.uuid")
            or "unknown-database"
        )

    def _default_use_extra_media(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("photostudio_connector.use_extra_media", "True")
            == "True"
        )

    def _is_local_replay(self, batch_id):
        return bool(
            self.env["photostudio.job"].sudo().search_count([("batch_id", "=", batch_id)])
        )

    def _apply_mime_sniff_to_batch(self, batch_body):
        body = dict(batch_body)
        items = []
        for item in batch_body.get("items") or []:
            item_copy = dict(item)
            job_input = dict(item_copy.get("input") or {})
            if job_input.get("base64"):
                job_input["mime"] = sniff_image_mime(
                    job_input["base64"],
                    job_input.get("mime") or "image/jpeg",
                )
            additional = []
            for extra in job_input.get("additional_images") or []:
                extra_copy = dict(extra)
                if extra_copy.get("base64"):
                    extra_copy["mime"] = sniff_image_mime(
                        extra_copy["base64"],
                        extra_copy.get("mime") or "image/jpeg",
                    )
                additional.append(extra_copy)
            if additional:
                job_input["additional_images"] = additional
            item_copy["input"] = job_input
            items.append(item_copy)
        body["items"] = items
        return body

    def _enforce_batch_byte_budget(self, batch_body):
        total = 0
        for item in batch_body.get("items") or []:
            job_input = item.get("input") or {}
            for key in ("base64",):
                payload = job_input.get(key)
                if payload:
                    total += len(base64.b64decode(payload))
            for extra in job_input.get("additional_images") or []:
                payload = extra.get("base64")
                if payload:
                    total += len(base64.b64decode(payload))
        if total > MAX_BATCH_DECODED_BYTES:
            raise UserError(
                "This batch exceeds the 20 MB decoded image limit. "
                "Select fewer products or disable extra gallery references."
            )

    def _reconcile_submission_response(
        self,
        response,
        candidates,
        replace_strategy,
        auto_publish,
        priority,
    ):
        created_jobs = self.env["photostudio.job"]
        remote_batch_id = response.get("batch_id")
        candidate_by_key = {
            self._candidate_reconcile_key(candidate): candidate
            for candidate in candidates
        }
        response_items = response.get("items") or []
        if response_items:
            for item in response_items:
                index = item.get("index")
                candidate = None
                if type(index) is int and 0 <= index < len(candidates):
                    candidate = candidates[index]
                if item.get("job"):
                    job_payload = item["job"]
                    # Replay items are reindexed over successful jobs only.
                    # Position is not identity, even for the initial response.
                    candidate = candidate_by_key.get(
                        self._payload_reconcile_key(job_payload)
                    )
                    if not candidate:
                        continue
                    created_jobs |= self._create_or_update_job(
                        candidate["product_tmpl_id"],
                        job_payload,
                        candidate["operation"],
                        replace_strategy,
                        auto_publish,
                        priority,
                        remote_batch_id,
                        product_id=candidate["product_id"],
                    )
                elif item.get("error") and candidate:
                    self._create_error_job(
                        candidate["product_tmpl_id"],
                        candidate["operation"],
                        replace_strategy,
                        auto_publish,
                        priority,
                        remote_batch_id,
                        self._humanize_item_error(item["error"]),
                        product_id=candidate["product_id"],
                    )
            return created_jobs

        for job_payload in response.get("jobs") or []:
            candidate = candidate_by_key.get(self._payload_reconcile_key(job_payload))
            if not candidate:
                continue
            created_jobs |= self._create_or_update_job(
                candidate["product_tmpl_id"],
                job_payload,
                candidate["operation"],
                replace_strategy,
                auto_publish,
                priority,
                remote_batch_id,
                product_id=candidate["product_id"],
            )
        return created_jobs

    def _candidate_reconcile_key(self, candidate):
        return (
            candidate["odoo_model"],
            candidate["odoo_record_id"],
            candidate["operation"],
        )

    def _payload_reconcile_key(self, job_payload):
        metadata = job_payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            return None
        model = metadata.get("odoo_model") or "product.template"
        record_id = metadata.get("odoo_record_id") or metadata.get("odoo_product_id")
        if model not in ("product.template", "product.product") or type(record_id) is not int or record_id <= 0:
            return None
        if not isinstance(job_payload.get("operation"), str):
            return None
        return (model, record_id, job_payload.get("operation"))

    def _humanize_item_error(self, error):
        if not isinstance(error, dict):
            return error
        code = error.get("code")
        message = error.get("message") or error.get("error") or code
        if code == "FORBIDDEN":
            message = (
                f"{message} Ghost Mannequin requires the ghost_mannequin permission "
                "on your API key."
            )
        elif code == "PRIORITY_NOT_ALLOWED":
            message = (
                f"{message} High priority requires the priority_high permission "
                "on your API key."
            )
        return {**error, "message": message}

    def _chunk_by_base64_assets(self, candidates):
        chunk = []
        asset_count = 0
        for candidate in candidates:
            candidate_asset_count = candidate["base64_asset_count"]
            if chunk and asset_count + candidate_asset_count > MAX_BASE64_BATCH_ASSETS:
                yield chunk
                chunk = []
                asset_count = 0
            chunk.append(candidate)
            asset_count += candidate_asset_count
        if chunk:
            yield chunk

    def _create_or_update_job(
        self,
        product_tmpl,
        payload,
        operation,
        replace_strategy,
        auto_publish,
        priority,
        batch_id,
        product_id=False,
    ):
        self._submission_target_values(product_tmpl, product_id)
        job_model = self.env["photostudio.job"]
        job_id = payload["job_id"]
        values = self._job_values(
            product_tmpl,
            payload,
            operation,
            replace_strategy,
            auto_publish,
            priority,
            batch_id,
            product_id=product_id,
        )
        existing = job_model.search([("job_id", "=", job_id)], limit=1)
        if existing:
            return self._merge_replayed_job(existing, values)
        try:
            with self.env.cr.savepoint():
                job = job_model.sudo().create(values).with_env(self.env)
        except IntegrityError:
            existing = job_model.search([("job_id", "=", job_id)], limit=1)
            if existing:
                return self._merge_replayed_job(existing, values)
            raise
        self._refresh_job_targets(job)
        return job

    def _merge_replayed_job(self, job, values):
        self._authorize_job(job)
        job.lock_for_update()
        job.invalidate_recordset()
        if (job.product_tmpl_id.id != values["product_tmpl_id"]
                or job.product_id.id != values["product_id"]
                or job.operation != values["operation"]):
            raise UserError("Remote job identity conflicts with its existing product or operation.")
        # These values belong to the local submission/writeback, not to a replay.
        for key in ("product_tmpl_id", "product_id", "operation", "writeback_state",
                    "replace_strategy", "auto_publish", "priority", "created_at", "batch_id",
                    "requesting_user_id", "company_id"):
            values.pop(key, None)
        values["metadata"] = self._merge_remote_metadata(job, values.get("metadata"))
        if job.status in ("completed", "failed", "cancelled"):
            values["status"] = job.status
            values["progress"] = job.progress
        # Use the accepted status, not a stale replay's status, for writeback.
        if values.get("status") in ("failed", "cancelled") and job.writeback_state == "pending":
            values["writeback_state"] = "failed"
        # Empty remote errors carry no update to local writeback diagnostics.
        if not values.get("errors"):
            values.pop("errors", None)
            values.pop("error_message", None)
        if not values.get("outputs"):
            values.pop("outputs", None)
        job.sudo().write(values)
        self._refresh_job_targets(job)
        return job

    def _merge_remote_metadata(self, job, remote_metadata):
        metadata = {**(job.metadata or {}), **(remote_metadata or {})}
        # The remote service must never author the local attachment ledger.
        metadata.pop("attached_output_ids", None)
        if "attached_output_ids" in (job.metadata or {}):
            metadata["attached_output_ids"] = job.metadata["attached_output_ids"]
        return metadata

    def _create_error_job(
        self,
        product_tmpl,
        operation,
        replace_strategy,
        auto_publish,
        priority,
        batch_id,
        error,
        product_id=False,
    ):
        target_values = self._submission_target_values(product_tmpl, product_id)
        job = self.env["photostudio.job"].sudo().create(
            {
                **target_values,
                "job_id": f"local-error-{product_tmpl.id}-{uuid.uuid4()}",
                "batch_id": batch_id,
                "product_tmpl_id": product_tmpl.id,
                "product_id": product_id or False,
                "operation": operation,
                "status": "failed",
                "progress": "failed",
                "writeback_state": "failed",
                "replace_strategy": replace_strategy,
                "auto_publish": auto_publish,
                "priority": priority,
                "errors": [error],
                "error_message": error.get("message") or error.get("code"),
            }
        )
        self._refresh_job_targets(job)
        return job

    def _create_missing_image_job(
        self, product_tmpl, operation, replace_strategy, auto_publish, product_id=False
    ):
        error = {"code": "MISSING_IMAGE", "message": "Product has no image.", "retryable": False}
        return self._create_error_job(
            product_tmpl,
            operation,
            replace_strategy,
            auto_publish,
            "normal",
            False,
            error,
            product_id=product_id,
        )

    def _job_values(
        self,
        product_tmpl,
        payload,
        operation,
        replace_strategy,
        auto_publish,
        priority,
        batch_id,
        product_id=False,
    ):
        errors = payload.get("errors") or []
        remote_status = payload.get("status") or "queued"
        writeback_state = "pending"
        if remote_status in ("failed", "cancelled"):
            writeback_state = "failed"
        return {
            **self._submission_target_values(product_tmpl, product_id),
            "job_id": payload["job_id"],
            "batch_id": payload.get("batch_id") or batch_id,
            "product_tmpl_id": product_tmpl.id,
            "product_id": product_id or False,
            "job_type": payload.get("job_type") or "generation",
            "operation": payload.get("operation") or operation,
            "status": remote_status,
            "progress": payload.get("progress") or payload.get("status") or "queued",
            "writeback_state": writeback_state,
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "outputs": payload.get("outputs") or [],
            "errors": errors,
            "links": payload.get("links") or {},
            "metadata": {key: value for key, value in (payload.get("metadata") or {}).items()
                         if key != "attached_output_ids"},
            "replace_strategy": replace_strategy,
            "auto_publish": auto_publish,
            "priority": priority,
            "error_message": errors and errors[0].get("message") or False,
        }

    def _apply_job_payload(self, job, payload):
        self._authorize_job(job)
        if payload.get("job_id") and payload["job_id"] != job.job_id:
            raise AccessError("Remote job identity does not match the authorized job.")
        job.lock_for_update()
        job.invalidate_recordset()
        metadata = self._merge_remote_metadata(job, payload.get("metadata"))
        remote_status = payload.get("status") or job.status
        progress = payload.get("progress") or payload.get("status") or job.progress
        if job.status in ("completed", "failed", "cancelled"):
            remote_status = job.status
            progress = job.progress
        values = {
            "status": remote_status,
            "progress": progress,
            "updated_at": payload.get("updated_at"),
            "outputs": payload.get("outputs") or job.outputs,
            "errors": payload.get("errors") or job.errors,
            "links": payload.get("links") or job.links,
            "metadata": metadata,
        }
        if remote_status == "completed" and not job.completed_at:
            values["completed_at"] = fields.Datetime.now()
        elif remote_status in ("failed", "cancelled") and job.writeback_state == "pending":
            values["writeback_state"] = "failed"
        job.sudo().write(values)
        if job.status == "completed" and job.writeback_state == "pending":
            self._attach_outputs_isolated(job)
        self._refresh_job_targets(job)

    def _attach_target(self, job):
        return self._authorize_job(job)

    def _attach_ready_outputs(self, job):
        self._authorize_job(job)
        job.lock_for_update()
        job.invalidate_recordset()
        if job.writeback_state in ("attached", "expired", "failed"):
            return
        outputs = [
            output
            for output in job.outputs or []
            if output.get("type") == "image"
            and output.get("state") == "ready"
            and (output.get("download_url") or output.get("url"))
        ]
        if not outputs:
            return

        metadata = dict(job.metadata or {})
        attached_output_ids = set(metadata.get("attached_output_ids") or [])
        pending_outputs = [
            output
            for output in outputs
            if self._output_identity(output) not in attached_output_ids
        ]
        if not pending_outputs:
            job.sudo().write({"writeback_state": "attached", "error_message": False})
            return

        client = self.env["photostudio.client"]
        attach_target = self._attach_target(job)
        image_service = self.env["photostudio.image.service"].with_env(attach_target.env)
        product_tmpl = job.product_tmpl_id.with_env(attach_target.env)
        main_attached = job.main_output_attached

        try:
            with self.env.cr.savepoint():
                for output in pending_outputs:
                    output_url = output.get("download_url") or output.get("url")
                    output_id = self._output_identity(output)
                    if output_id in attached_output_ids:
                        continue
                    content = client._download_output(output_url)
                    if job.replace_strategy == "replace_main" and not main_attached:
                        image_service.replace_main(attach_target, content)
                        main_attached = True
                    else:
                        image_service.append_gallery(
                            attach_target,
                            content,
                            name=f"Photostudio {attach_target.display_name}",
                        )
                    attached_output_ids.add(output_id)

                metadata["attached_output_ids"] = sorted(attached_output_ids)
                write_values = {
                    "metadata": metadata,
                    "main_output_attached": main_attached,
                    "writeback_state": "attached",
                    "error_message": False,
                }
                if job.auto_publish and "is_published" in product_tmpl._fields:
                    product_tmpl.is_published = True
                job.sudo().write(write_values)
        except PhotostudioDownloadError as error:
            self._record_writeback_failure(job, error.status_code)

    def _output_identity(self, output):
        return str(
            output.get("image_id")
            or output.get("storage_path")
            or output.get("url")
            or output.get("download_url")
        )

    def _latest_batch_jobs(self, jobs):
        if not jobs:
            return jobs
        anchor = jobs[0]
        if anchor.batch_id:
            return jobs.filtered(lambda current: current.batch_id == anchor.batch_id)
        return anchor

    def _refresh_job_targets(self, job):
        target = self._authorize_job(job)
        service = self.with_env(target.env)
        service._refresh_product_state(job.product_tmpl_id)
        if job.product_id:
            service._refresh_variant_state(job.product_id)

    def _refresh_variant_state(self, variant):
        variant = self._authorize_products(variant)
        jobs = self.env["photostudio.job"].search(
            [("product_id", "=", variant.id)],
            order="create_date desc, id desc",
        )
        if not jobs:
            return

        batch_jobs = self._latest_batch_jobs(jobs)
        sync_state = self._sync_state_from_jobs(batch_jobs)
        attached_output_ids = set()
        completed_jobs = batch_jobs.filtered(lambda current: current.status == "completed")
        for completed_job in completed_jobs:
            attached_output_ids.update(
                (completed_job.metadata or {}).get("attached_output_ids") or []
            )
        last_completed = completed_jobs.sorted(
            key=lambda current: current.completed_at or fields.Datetime.now(),
            reverse=True,
        )[:1]
        variant.write(
            {
                "photostudio_sync_state": sync_state,
                "last_job_id": batch_jobs[:1].job_id,
                "last_generation": last_completed.completed_at if last_completed else False,
                "generated_images_count": len(attached_output_ids),
            }
        )

    def _sync_state_from_jobs(self, batch_jobs):
        statuses = set(batch_jobs.mapped("status"))
        writeback_states = set(batch_jobs.mapped("writeback_state"))
        if "processing" in statuses:
            return "processing"
        if "queued" in statuses:
            return "queued"
        if "failed" in statuses:
            return "failed"
        if "cancelled" in statuses:
            return "cancelled"
        if "completed" in statuses:
            if "pending" in writeback_states:
                return "attach_pending"
            if "expired" in writeback_states:
                return "attach_expired"
            return "completed"
        return "never"

    def _refresh_product_state(self, product):
        product = self._authorize_products(product)
        jobs = self.env["photostudio.job"].search(
            [
                ("product_tmpl_id", "=", product.id),
                ("product_id", "=", False),
            ],
            order="create_date desc, id desc",
        )
        if not jobs:
            return

        batch_jobs = self._latest_batch_jobs(jobs)
        sync_state = self._sync_state_from_jobs(batch_jobs)

        attached_output_ids = set()
        completed_jobs = batch_jobs.filtered(lambda current: current.status == "completed")
        for completed_job in completed_jobs:
            attached_output_ids.update(
                (completed_job.metadata or {}).get("attached_output_ids") or []
            )
        last_completed = completed_jobs.sorted(
            key=lambda current: current.completed_at or fields.Datetime.now(),
            reverse=True,
        )[:1]
        product.write(
            {
                "photostudio_sync_state": sync_state,
                "last_job_id": batch_jobs[:1].job_id,
                "last_generation": last_completed.completed_at if last_completed else False,
                "generated_images_count": len(attached_output_ids),
                "photostudio_metadata": batch_jobs[:1].metadata or {},
            }
        )

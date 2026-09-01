import base64
import json

import requests

from odoo import api, models
from odoo.exceptions import UserError

from ..utils.download_errors import PhotostudioDownloadError
from ..utils.idempotency import deterministic_key
from ..utils.url_allowlist import is_allowed_output_url

MAX_OUTPUT_BYTES = 30 * 1024 * 1024
BATCH_POST_TIMEOUT = 120


class PhotostudioClient(models.AbstractModel):
    _name = "photostudio.client"
    _description = "Photostudio Client"

    @api.model
    def create_job_batch(self, payload, idempotency_key):
        return self._request(
            "POST",
            "/v1/job-batches",
            payload=payload,
            idempotency_key=idempotency_key,
            timeout=BATCH_POST_TIMEOUT,
        )

    @api.model
    def get_job(self, job_id):
        return self._request("GET", f"/v1/jobs/{job_id}")

    @api.model
    def get_batch(self, batch_id):
        return self._request("GET", f"/v1/job-batches/{batch_id}")

    @api.model
    def cancel_job(self, job_id):
        return self._request(
            "POST",
            f"/v1/jobs/{job_id}/cancel",
            allowed_error_codes={"JOB_NOT_CANCELLABLE"},
        )

    @api.model
    def download_output(self, url):
        params = self.env["ir.config_parameter"].sudo()
        api_url = params.get_param("photostudio_connector.api_url") or ""
        if not is_allowed_output_url(url, api_url):
            raise PhotostudioDownloadError(
                "Photostudio output download blocked: URL origin does not match the configured API URL."
            )
        timeout = self._timeout()
        try:
            response = requests.get(url, timeout=timeout, stream=True)
        except requests.RequestException as error:
            raise PhotostudioDownloadError(
                f"Photostudio output download failed: {error}"
            ) from error
        with response:
            if response.status_code >= 400:
                raise PhotostudioDownloadError(
                    f"Photostudio output download failed ({response.status_code}).",
                    status_code=response.status_code,
                )
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            if not content_type.startswith("image/"):
                raise PhotostudioDownloadError("Photostudio output is not an image.")
            try:
                content_length = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                content_length = 0
            if content_length > MAX_OUTPUT_BYTES:
                raise PhotostudioDownloadError(
                    "Photostudio output exceeds the 30 MB download limit."
                )
            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                chunks.extend(chunk)
                if len(chunks) > MAX_OUTPUT_BYTES:
                    raise PhotostudioDownloadError(
                        "Photostudio output exceeds the 30 MB download limit."
                    )
            return bytes(chunks)

    @api.model
    def deterministic_key(self, *parts):
        return deterministic_key(*parts)

    def _request(
        self,
        method,
        path,
        payload=None,
        idempotency_key=None,
        allowed_error_codes=None,
        timeout=None,
    ):
        params = self.env["ir.config_parameter"].sudo()
        base_url = (params.get_param("photostudio_connector.api_url") or "").rstrip("/")
        api_key = params.get_param("photostudio_connector.api_key") or ""
        timeout = timeout or self._timeout()

        if not base_url:
            raise UserError("Configure the Photostudio API URL before generating images.")
        if not api_key:
            raise UserError("Configure a Photostudio API key before generating images.")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            response = requests.request(
                method,
                f"{base_url}{path}",
                headers=headers,
                data=json.dumps(payload) if payload is not None else None,
                timeout=timeout,
            )
        except requests.RequestException as error:
            raise UserError(f"Photostudio request failed: {error}") from error

        try:
            body = response.json()
        except ValueError as error:
            raise UserError("Photostudio returned a non-JSON response.") from error

        error_code = body.get("code") if isinstance(body, dict) else None
        is_allowed_error = error_code in (allowed_error_codes or set())
        if response.status_code >= 400 and not is_allowed_error:
            message = self._format_api_error(body, response.status_code, response.reason)
            raise UserError(message)
        return body

    @api.model
    def _format_api_error(self, body, status_code, reason):
        if not isinstance(body, dict):
            return f"Photostudio API error ({status_code}): {reason}"
        code = body.get("code")
        message = body.get("message") or body.get("error") or reason
        if code == "FORBIDDEN":
            return (
                f"Photostudio API error ({status_code}): {message} "
                "Ghost Mannequin requires the ghost_mannequin permission on your API key."
            )
        if code == "PRIORITY_NOT_ALLOWED":
            return (
                f"Photostudio API error ({status_code}): {message} "
                "High priority requires the priority_high permission on your API key."
            )
        return f"Photostudio API error ({status_code}): {message}"

    def _timeout(self):
        params = self.env["ir.config_parameter"].sudo()
        try:
            timeout = int(params.get_param("photostudio_connector.timeout") or 30)
        except ValueError:
            timeout = 30
        return min(max(timeout, 1), 120)

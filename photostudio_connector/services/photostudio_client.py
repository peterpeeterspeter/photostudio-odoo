import base64
import json

import requests

from odoo import api, models
from odoo.exceptions import AccessError, UserError

from ..utils.download_errors import PhotostudioDownloadError
from ..utils.idempotency import deterministic_key
from ..utils.url_allowlist import is_allowed_output_url

MAX_OUTPUT_BYTES = 30 * 1024 * 1024
BATCH_POST_TIMEOUT = 120


class PhotostudioClient(models.AbstractModel):
    _name = "photostudio.client"
    _description = "Photostudio Client"

    @api.model
    def _create_job_batch(self, payload, idempotency_key):
        return self._request(
            "POST",
            "/v1/job-batches",
            payload=payload,
            idempotency_key=idempotency_key,
            timeout=BATCH_POST_TIMEOUT,
        )

    @api.model
    def _get_job(self, job_id):
        return self._request("GET", f"/v1/jobs/{job_id}")

    @api.model
    def _get_batch(self, batch_id):
        return self._request("GET", f"/v1/job-batches/{batch_id}")

    @api.model
    def _cancel_job(self, job_id):
        return self._request(
            "POST",
            f"/v1/jobs/{job_id}/cancel",
            allowed_error_codes={"JOB_NOT_CANCELLABLE"},
        )

    @api.model
    def _download_output(self, url):
        self._check_client_access()
        params = self.env["ir.config_parameter"].sudo()
        api_url = params.get_param("photostudio_connector.api_url") or ""
        timeout = self._timeout()
        response = None
        try:
            if not is_allowed_output_url(url, api_url):
                raise PhotostudioDownloadError(
                    "Photostudio output download blocked: URL origin does not match the configured API URL."
                )
            response = requests.get(url, timeout=timeout, stream=True, allow_redirects=False)
            with response:
                if 300 <= response.status_code < 400:
                    raise PhotostudioDownloadError(
                        f"Photostudio output download redirect blocked ({response.status_code}).",
                        status_code=response.status_code,
                    )
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
        except PhotostudioDownloadError:
            raise
        except Exception as error:
            # The entire transport lifecycle, including iteration and close, is
            # untrusted. Never expose its exception text or chained signed URL.
            failed_response = getattr(error, "response", None)
            if failed_response is None:
                failed_response = response
            status_code = getattr(failed_response, "status_code", None)
            raise PhotostudioDownloadError(
                "Photostudio output download failed.",
                status_code=status_code if isinstance(status_code, int) else None,
            ) from None

    @api.model
    def _deterministic_key(self, *parts):
        self._check_client_access()
        return deterministic_key(*parts)

    def _check_client_access(self):
        if not self.env.su and not self.env.user.has_group(
            "photostudio_connector.group_photostudio_user"
        ):
            raise AccessError("You do not have permission to use the Photostudio connector.")

    def _request(
        self,
        method,
        path,
        payload=None,
        idempotency_key=None,
        allowed_error_codes=None,
        timeout=None,
    ):
        self._check_client_access()
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
                allow_redirects=False,
            )
            with response:
                if 300 <= response.status_code < 400:
                    raise UserError(
                        f"Photostudio request redirect blocked ({response.status_code})."
                    )
                try:
                    body = response.json()
                except ValueError:
                    raise UserError(
                        f"Photostudio returned a non-JSON response ({response.status_code})."
                    ) from None

                error_code = body.get("code") if isinstance(body, dict) else None
                is_allowed_error = isinstance(error_code, str) and error_code in (
                    allowed_error_codes or set()
                )
                if response.status_code >= 400 and not is_allowed_error:
                    message = self._format_api_error(body, response.status_code, response.reason)
                    raise UserError(message)
                return body
        except (requests.RequestException, OSError):
            # Requests exceptions can contain the URL, headers, or response body.
            raise UserError("Photostudio request failed.") from None

    @api.model
    def _format_api_error(self, body, status_code, reason):
        # Remote free text (including the HTTP reason) can echo credentials or
        # signed URLs. Keep only the status and locally defined remediation.
        message = f"Photostudio API error ({status_code})."
        code = body.get("code") if isinstance(body, dict) else None
        if code == "FORBIDDEN":
            return (
                f"{message} "
                "Ghost Mannequin requires the ghost_mannequin permission on your API key."
            )
        if code == "PRIORITY_NOT_ALLOWED":
            return (
                f"{message} "
                "High priority requires the priority_high permission on your API key."
            )
        if code == "JOB_TERMINAL":
            return f"{message} Job is already terminal."
        return message

    def _timeout(self):
        params = self.env["ir.config_parameter"].sudo()
        try:
            timeout = int(params.get_param("photostudio_connector.timeout") or 30)
        except ValueError:
            timeout = 30
        return min(max(timeout, 1), 120)

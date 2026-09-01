import hashlib
import hmac
import json
import time

from odoo import fields, http
from odoo.http import request
from psycopg2 import IntegrityError


class PhotostudioWebhookController(http.Controller):
    @http.route(
        "/photostudio/webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def webhook(self):
        raw_body = request.httprequest.get_data() or b""
        timestamp = request.httprequest.headers.get("X-Photostudio-Timestamp", "")
        signature = request.httprequest.headers.get("X-Photostudio-Signature", "")

        if not self._valid_signature(raw_body, timestamp, signature):
            return self._json_response({"error": "invalid_signature"}, status=401)

        try:
            envelope = json.loads(raw_body.decode("utf-8"))
        except ValueError:
            return self._json_response({"error": "invalid_json"}, status=400)

        event_id = envelope.get("event_id")
        event = envelope.get("event")
        data = envelope.get("data") or {}
        job_payload = data.get("job") if isinstance(data, dict) else None
        if not isinstance(job_payload, dict):
            job_payload = data
        if not event_id or not event or not isinstance(job_payload, dict):
            return self._json_response({"error": "invalid_event"}, status=422)

        event_model = request.env["photostudio.webhook.event"].sudo()
        if event_model.search_count([("event_id", "=", event_id)]):
            return self._json_response({"ok": True, "duplicate": True})

        try:
            with request.env.cr.savepoint():
                event_model.create(
                    {
                        "event_id": event_id,
                        "event": event,
                        "job_id": job_payload.get("job_id"),
                        "received_at": fields.Datetime.now(),
                    }
                )
        except IntegrityError:
            return self._json_response({"ok": True, "duplicate": True})
        self._apply_job_event(event, job_payload)
        return self._json_response({"ok": True})

    def _json_response(self, payload, status=200):
        body = json.dumps(payload)
        return request.make_response(
            body,
            headers=[("Content-Type", "application/json")],
            status=status,
        )

    def _valid_signature(self, raw_body, timestamp, signature):
        params = request.env["ir.config_parameter"].sudo()
        secret = params.get_param("photostudio_connector.webhook_secret") or ""
        if not secret or not timestamp or not signature.startswith("sha256="):
            return False
        try:
            age = abs(int(time.time()) - int(timestamp))
        except ValueError:
            return False
        if age > 300:
            return False

        signed = timestamp.encode("utf-8") + b"." + raw_body
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            signed,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def _apply_job_event(self, event, data):
        if not event.startswith("job."):
            return
        job_id = data.get("job_id")
        if not job_id:
            return
        job = request.env["photostudio.job"].sudo().search([("job_id", "=", job_id)], limit=1)
        if job:
            request.env["photostudio.connector.service"].sudo()._apply_job_payload(job, data)

from odoo import fields, models


class PhotostudioWebhookEvent(models.Model):
    _name = "photostudio.webhook.event"
    _description = "Photostudio Webhook Event"
    _order = "create_date desc, id desc"
    _photostudio_webhook_event_event_id_uniq = models.Constraint(
        "unique(event_id)",
        "A Photostudio webhook event can only be processed once.",
    )

    event_id = fields.Char(string="Event ID", required=True, copy=False, index=True)
    event = fields.Char(string="Event Type", required=True, index=True)
    job_id = fields.Char(index=True)
    received_at = fields.Datetime(default=fields.Datetime.now, required=True)

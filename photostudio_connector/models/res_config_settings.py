from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    photostudio_api_url = fields.Char(
        string="Photostudio API URL",
        config_parameter="photostudio_connector.api_url",
        default="https://dfqtcthccvwtutgxonym.supabase.co/functions/v1/api-jobs",
    )
    photostudio_api_key = fields.Char(
        string="API Key",
        config_parameter="photostudio_connector.api_key",
    )
    photostudio_workspace = fields.Char(
        string="Workspace",
        config_parameter="photostudio_connector.workspace",
    )
    photostudio_replace_strategy = fields.Selection(
        [
            ("replace_main", "Replace Main Image"),
            ("add_gallery", "Add to Gallery"),
        ],
        string="Replace Strategy",
        default="add_gallery",
        config_parameter="photostudio_connector.replace_strategy",
    )
    photostudio_auto_publish = fields.Boolean(
        string="Auto Publish",
        config_parameter="photostudio_connector.auto_publish",
    )
    photostudio_use_extra_media = fields.Boolean(
        string="Use Extra Gallery Images as References",
        config_parameter="photostudio_connector.use_extra_media",
        default=False,
    )
    photostudio_timeout = fields.Integer(
        string="Timeout",
        default=30,
        config_parameter="photostudio_connector.timeout",
    )
    photostudio_poll_interval = fields.Integer(
        string="Polling Interval",
        default=120,
        config_parameter="photostudio_connector.poll_interval",
    )
    photostudio_priority = fields.Selection(
        [
            ("low", "Low"),
            ("normal", "Normal"),
        ],
        string="Default Priority",
        default="normal",
        config_parameter="photostudio_connector.priority",
    )
    photostudio_use_webhook = fields.Boolean(
        string="Use Webhooks",
        config_parameter="photostudio_connector.use_webhook",
        help="Deliver job status via signed webhooks when a signing secret is configured.",
    )
    photostudio_webhook_secret = fields.Char(
        string="Webhook Signing Secret",
        config_parameter="photostudio_connector.webhook_secret",
        help="Per-key whsec_ value shown once when the API key was created.",
    )

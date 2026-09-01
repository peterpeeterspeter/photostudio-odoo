from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = "product.product"

    photostudio_sync_state = fields.Selection(
        [
            ("never", "Never Generated"),
            ("queued", "Queued"),
            ("processing", "Processing"),
            ("completed", "Completed"),
            ("attach_pending", "Generated, Attach Pending"),
            ("attach_expired", "Generated, Attach Expired"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        string="Photostudio Status",
        default="never",
        readonly=True,
        copy=False,
        index=True,
    )
    last_generation = fields.Datetime(
        string="Last Photostudio Generation",
        readonly=True,
        copy=False,
    )
    last_job_id = fields.Char(
        string="Last Photostudio Job",
        readonly=True,
        copy=False,
        index=True,
    )
    generated_images_count = fields.Integer(
        string="Generated Images",
        readonly=True,
        copy=False,
        default=0,
    )

    def action_photostudio_generate_ghost(self):
        return self._action_open_photostudio_wizard("product_image.ghost")

    def action_photostudio_generate_on_model(self):
        return self._action_open_photostudio_wizard("product_image.on_model")

    def action_photostudio_generate_lifestyle(self):
        return self._action_open_photostudio_wizard("product_image.lifestyle")

    def action_photostudio_generate_flatlay(self):
        return self._action_open_photostudio_wizard("product_image.flatlay")

    def action_photostudio_generate_photoshoot(self):
        return self._action_open_photostudio_wizard("product_image.photoshoot")

    def action_photostudio_generate_detail(self):
        return self._action_open_photostudio_wizard("product_image.detail")

    def action_photostudio_generate_campaign(self):
        return self._action_open_photostudio_wizard("product_image.campaign")

    def _action_open_photostudio_wizard(self, operation):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Generate Images",
            "res_model": "photostudio.generate.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "active_model": "product.product",
                "active_ids": self.ids,
                "default_operation": operation,
                "default_variant_ids": [(6, 0, self.ids)],
            },
        }

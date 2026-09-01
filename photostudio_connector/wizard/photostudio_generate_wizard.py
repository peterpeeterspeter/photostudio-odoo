from odoo import api, fields, models
from odoo.exceptions import UserError

from ..utils.generation_options import (
    DEFAULT_MODEL_PRESET,
    DEFAULT_POSE,
    DEFAULT_SCENE_PRESET,
    LIFESTYLE_SCENE_PRESET_IDS,
    MODEL_PRESET_IDS,
    POSE_IDS,
    normalize_model_catalog_id,
    validate_model_preset,
    validate_pose,
    validate_scene_preset,
)
from ..utils.operations import (
    CORE_SET_HELP,
    estimate_credits,
    wizard_operation_selection,
)

MAX_BASE64_BATCH_ASSETS = 50


class PhotostudioGenerateWizard(models.TransientModel):
    _name = "photostudio.generate.wizard"
    _description = "Generate Images with Photostudio"

    product_ids = fields.Many2many("product.template", string="Products")
    variant_ids = fields.Many2many("product.product", string="Variants")
    product_count = fields.Integer(compute="_compute_batch_metrics")
    operation = fields.Selection(
        selection="_selection_operation",
        required=True,
        default="product_image.ghost",
    )
    replace_strategy = fields.Selection(
        [
            ("replace_main", "Replace Main Image"),
            ("add_gallery", "Add to Gallery"),
        ],
        string="Publishing",
        required=True,
        default=lambda self: self._default_replace_strategy(),
    )
    auto_publish = fields.Boolean(default=lambda self: self._default_auto_publish())
    use_extra_media = fields.Boolean(
        string="Include Extra Gallery Images",
        default=lambda self: self._default_use_extra_media(),
    )
    priority = fields.Selection(
        [
            ("low", "Low"),
            ("normal", "Normal"),
        ],
        default=lambda self: self._default_priority(),
        required=True,
    )
    model_preset = fields.Selection(
        [(value, value.replace("_", " ").title()) for value in MODEL_PRESET_IDS],
        string="Model Preset",
        default=DEFAULT_MODEL_PRESET,
    )
    pose = fields.Selection(
        [(value, value.replace("_", " ").title()) for value in POSE_IDS],
        string="Pose",
        default=DEFAULT_POSE,
    )
    scene_preset = fields.Selection(
        [
            (value, value.replace("_", " ").title())
            for value in LIFESTYLE_SCENE_PRESET_IDS
        ],
        string="Scene",
        default=DEFAULT_SCENE_PRESET,
    )
    background_prompt = fields.Char(
        string="Background Prompt",
        help="Optional scene description for Photoshoot generations.",
    )
    model_catalog_id = fields.Char(
        string="Model Catalog ID",
        help="Optional UUID from the Photostudio model catalog.",
    )
    regenerate_nonce = fields.Integer(
        string="Regeneration Nonce",
        default=0,
        help="Increment to force a new billed batch for otherwise identical settings.",
    )
    confirm_replace_main = fields.Boolean(
        string="I understand the main image will be replaced",
    )
    confirm_external_processing = fields.Boolean(
        string=(
            "I agree to send selected product images to Photostudio "
            "for generation (credits billed by Photostudio)"
        ),
        help=(
            "Image generation uses the external Photostudio.io Jobs API. "
            "Configure API credentials under Settings if you have not already."
        ),
    )
    estimated_credits = fields.Integer(compute="_compute_batch_metrics", readonly=True)
    estimated_assets = fields.Integer(compute="_compute_batch_metrics", readonly=True)
    batch_size_warning = fields.Char(compute="_compute_batch_metrics", readonly=True)
    core_set_help = fields.Char(default=CORE_SET_HELP, readonly=True)

    @api.model
    def _selection_operation(self):
        return wizard_operation_selection()

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        active_model = self.env.context.get("active_model")
        active_ids = self.env.context.get("active_ids") or []
        if active_model == "product.template":
            values.setdefault("product_ids", [(6, 0, active_ids)])
        elif active_model == "product.product":
            values.setdefault("variant_ids", [(6, 0, active_ids)])
        return values

    @api.depends(
        "product_ids",
        "variant_ids",
        "operation",
        "use_extra_media",
    )
    def _compute_batch_metrics(self):
        for wizard in self:
            record_count = len(wizard.variant_ids or wizard.product_ids)
            wizard.product_count = record_count
            credits, assets = estimate_credits(
                record_count,
                wizard.operation or "product_image.ghost",
                wizard.use_extra_media,
            )
            wizard.estimated_credits = credits
            wizard.estimated_assets = assets
            if record_count and assets > MAX_BASE64_BATCH_ASSETS:
                wizard.batch_size_warning = (
                    "This selection exceeds the 50-image asset limit per batch. "
                    "Photostudio will split it into multiple billed batches."
                )
            elif record_count > 10:
                wizard.batch_size_warning = (
                    f"{record_count} products selected. Large batches may take longer to submit."
                )
            else:
                wizard.batch_size_warning = False

    def _generation_records(self):
        self.ensure_one()
        if self.variant_ids:
            return self.variant_ids
        if self.product_ids:
            return self.product_ids
        raise UserError("Select at least one product or variant.")

    def _generation_options(self):
        self.ensure_one()
        options = {}
        if self.operation in (
            "product_image.on_model",
            "product_image.lifestyle",
            "product_image.photoshoot",
            "all",
        ):
            options["model_preset"] = self.model_preset or DEFAULT_MODEL_PRESET
            validate_model_preset(options["model_preset"])
        if self.operation in ("product_image.on_model", "product_image.photoshoot", "all"):
            options["pose"] = self.pose or DEFAULT_POSE
            validate_pose(options["pose"])
        if self.operation in ("product_image.lifestyle", "all"):
            options["scene_preset"] = self.scene_preset or DEFAULT_SCENE_PRESET
            validate_scene_preset(options["scene_preset"])
        if self.operation == "product_image.photoshoot":
            if self.background_prompt:
                options["background_prompt"] = self.background_prompt
        if self.operation in (
            "product_image.on_model",
            "product_image.lifestyle",
            "product_image.photoshoot",
            "all",
        ) and self.model_catalog_id:
            options["model_catalog_id"] = normalize_model_catalog_id(
                self.model_catalog_id
            )
        return options

    def action_confirm(self):
        self.ensure_one()
        if not self.confirm_external_processing:
            raise UserError(
                "Confirm that selected product images may be sent to Photostudio "
                "for generation. Credits are billed by Photostudio, not by Odoo."
            )
        if self.replace_strategy == "replace_main" and not self.confirm_replace_main:
            raise UserError(
                "Confirm that you understand the main product image will be replaced."
            )
        try:
            generation_options = self._generation_options()
        except ValueError as error:
            raise UserError(str(error)) from error
        result = self.env["photostudio.connector.service"].enqueue_generation(
            self._generation_records(),
            self.operation,
            self.replace_strategy,
            self.priority,
            self.auto_publish,
            self.use_extra_media,
            generation_options=generation_options,
            regenerate_nonce=self.regenerate_nonce or 0,
        )
        if result.get("replay"):
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Photostudio",
                    "message": (
                        "Identical request already submitted; no new images generated."
                    ),
                    "type": "warning",
                    "sticky": False,
                    "next": {"type": "ir.actions.act_window_close"},
                },
            }
        return {"type": "ir.actions.act_window_close"}

    def _default_replace_strategy(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("photostudio_connector.replace_strategy", "add_gallery")
        )

    def _default_auto_publish(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("photostudio_connector.auto_publish")
            == "True"
        )

    def _default_use_extra_media(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("photostudio_connector.use_extra_media", "False")
            == "True"
        )

    def _default_priority(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("photostudio_connector.priority", "normal")
        )

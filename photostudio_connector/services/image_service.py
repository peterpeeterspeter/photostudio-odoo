import base64
import mimetypes

from odoo import api, models


class PhotostudioImageService(models.AbstractModel):
    _name = "photostudio.image.service"
    _description = "Photostudio Image Service"

    @api.model
    def extract(self, product):
        if product._name == "product.product":
            return self._extract_variant(product)
        return self._extract_template(product)

    @api.model
    def replace_main(self, product, image_bytes):
        product.image_1920 = self._encode_binary(image_bytes)

    @api.model
    def append_gallery(self, product, image_bytes, name=None):
        template = product.product_tmpl_id if product._name == "product.product" else product
        image_model = self.env["product.image"]
        image_model.create(
            {
                "name": name or f"Photostudio {product.display_name}",
                "product_tmpl_id": template.id,
                "image_1920": self._encode_binary(image_bytes),
            }
        )

    def _extract_template(self, product):
        images = []
        if product.image_1920:
            images.append(
                self._image_payload(
                    product.image_1920,
                    f"product-{product.id}-main.jpg",
                    "main",
                )
            )

        gallery_images = getattr(product, "product_template_image_ids", False)
        for index, image in enumerate(gallery_images or self.env["product.image"]):
            if not image.image_1920:
                continue
            images.append(
                self._image_payload(
                    image.image_1920,
                    f"product-{product.id}-gallery-{index + 1}.jpg",
                    "gallery",
                )
            )
        return images

    def _extract_variant(self, variant):
        images = []
        if variant.image_1920:
            images.append(
                self._image_payload(
                    variant.image_1920,
                    f"variant-{variant.id}-main.jpg",
                    "main",
                )
            )
        elif variant.product_tmpl_id.image_1920:
            images.append(
                self._image_payload(
                    variant.product_tmpl_id.image_1920,
                    f"variant-{variant.id}-template-main.jpg",
                    "main",
                )
            )
        template = variant.product_tmpl_id
        for index, image in enumerate(template.product_template_image_ids):
            if not image.image_1920:
                continue
            images.append(
                self._image_payload(
                    image.image_1920,
                    f"variant-{variant.id}-gallery-{index + 1}.jpg",
                    "gallery",
                )
            )
        return images

    def _image_payload(self, encoded_image, filename, purpose):
        if isinstance(encoded_image, bytes):
            encoded = encoded_image.decode("ascii")
        else:
            encoded = encoded_image
        mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
        return {
            "base64": encoded,
            "mime": mime,
            "filename": filename,
            "purpose": purpose,
        }

    def _encode_binary(self, image_bytes):
        return base64.b64encode(image_bytes).decode("ascii")

# Photostudio Connector (Odoo 19)

Generate ghost mannequin, flatlay, on-model, lifestyle, photoshoot, detail, and marketplace export images directly from Odoo product photos.

## Requirements

- **Odoo Community 19** or **Odoo Enterprise 19**
- **Internet access** (Photostudio Jobs API)
- **Photostudio.io account** with API credentials for image generation

## Installation

1. Copy the `photostudio_connector` folder into your Odoo addons path.
2. Update the apps list and install **Photostudio Connector**.
3. Open **Settings → Photostudio** to configure the Jobs API URL and credentials when you are ready to generate.

The module installs and runs without an activation key. Settings, product forms, and the Jobs menu work before any API key is configured.

## External service

Image generation requires connection to the external Photostudio.io service and valid API credentials. The API key authenticates to the Photostudio Jobs API and does not unlock or license this Odoo addon. Without credentials, **Generate** shows a configuration error instead of submitting jobs.

## Data sent to Photostudio

When you confirm generation, the module sends:

- Selected product or variant main image
- Extra gallery images when that option is enabled
- Job options such as operation, pose, scene, background prompt, and optional model catalog id

Photostudio returns generated image files that Odoo downloads and stores on the product. The wizard requires explicit opt-in before the first upload of a batch.

Privacy policy: https://photostudio.io/privacy

Support: peter@photostudio.io

## Operations

| Operation | Description |
|-----------|-------------|
| Ghost mannequin | Core set |
| On-model | Core set |
| Lifestyle | Core set |
| Flatlay | Individual |
| Photoshoot | Individual |
| Detail | Individual |
| Marketplace export | Canvas reframe |

Polling is the default completion path. Signed webhooks are optional when configured.

## Development

Run standalone helper tests from the monorepo:

```bash
python3 -m unittest discover -s integrations/odoo/photostudio_connector/tests -p 'test_*.py' -v
```

Odoo TransactionCase tests (Docker):

```bash
export ODOO_TEST_DB_PASSWORD="$(openssl rand -hex 16)"
docker compose -f integrations/odoo/docker-compose.test.yml up --abort-on-container-exit --exit-code-from odoo
```

## License

LGPL-3. See [LICENSE](photostudio_connector/LICENSE).

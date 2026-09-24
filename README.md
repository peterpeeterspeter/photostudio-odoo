# Photostudio Connector (Odoo 20)

Generate ghost mannequin, flatlay, on-model, lifestyle, photoshoot, detail, and marketplace export images directly from Odoo product photos.

## Requirements

- **Odoo Community 20** or **Odoo Enterprise 20**
- **Internet access** (Photostudio Jobs API)
- **Photostudio.io account** with API credentials for image generation

## Installation

1. Copy the `photostudio_connector` folder into your Odoo addons path.
2. Update the apps list and install **Photostudio Connector**.
3. Open **Settings → Photostudio** to configure the Jobs API URL and credentials when you are ready to generate.
4. Administrators assign **Photostudio Connector User** to operators who also have write access to the selected products. Company boundaries and the requesting user's current permissions are rechecked before images are attached.

The module installs without an activation key. Administrators and authorized connector operators can use the permitted menus before an API key is configured; ordinary employees cannot submit jobs or alter job records.

## Security upgrade: 19.0.1.0.3

Upgrade the installed module after replacing its files (`-u photostudio_connector`). This release includes the previously unpublished replay-safety and variant-gallery fixes, private API transport, company/product authorization, bounded download recovery, and redirect rejection. Legacy jobs without trusted requester/company context are quarantined rather than adopted; existing product images, attached-output history, and credentials are preserved.

**Image writeback is automatic**, according to the selected replace-main/add-gallery strategy. **Publish unpublished products after attachment** controls website publication only; leaving it off is not a review gate and does not prevent image changes on already-published products. Retry Downloads refreshes existing outputs without creating a new paid generation. Restore revoked access and use Poll Status to explicitly revalidate a blocked job.

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
for test in operations idempotency url_allowlist mime_sniff generation_options download_security photostudio_client; do
  python3 "integrations/odoo/photostudio_connector/tests/test_${test}.py" || exit 1
done
```

Odoo TransactionCase tests (Docker):

```bash
export ODOO_TEST_DB_PASSWORD="$(openssl rand -hex 16)"
docker compose -f integrations/odoo/docker-compose.test.yml up --abort-on-container-exit --exit-code-from odoo
```

## License

LGPL-3. See [LICENSE](photostudio_connector/LICENSE).

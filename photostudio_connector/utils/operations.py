"""Operation vocabulary — single source for wizard, job model, and enqueue."""

OPERATION_LABELS = {
    "all": "Core set (Ghost + On-model + Lifestyle)",
    "product_image.ghost": "Ghost Mannequin",
    "product_image.on_model": "On-model",
    "product_image.lifestyle": "Lifestyle",
    "product_image.flatlay": "Flatlay",
    "product_image.photoshoot": "Photoshoot",
    "product_image.detail": "Detail",
    "product_image.campaign": "Marketplace Export",
}

# Enqueueable via Jobs API.
SUPPORTED_OPERATIONS = (
    "product_image.ghost",
    "product_image.on_model",
    "product_image.lifestyle",
    "product_image.flatlay",
    "product_image.photoshoot",
    "product_image.detail",
    "product_image.campaign",
)

# Curated core-set fan-out for wizard key "all" — not an alias of SUPPORTED_OPERATIONS.
ALL_OPERATIONS = (
    "product_image.ghost",
    "product_image.on_model",
    "product_image.lifestyle",
)

RESERVED_OPERATIONS = ()

WIZARD_PSEUDO_OPERATIONS = ("all",)

CREDITS_PER_OPERATION = 2

CORE_SET_HELP = (
    "Runs Ghost Mannequin, On-model, and Lifestyle only. "
    "Flatlay and other formats are selected individually."
)


def job_operation_selection():
    return [
        (operation, OPERATION_LABELS[operation])
        for operation in SUPPORTED_OPERATIONS
    ]


def wizard_operation_selection():
    selection = [(pseudo, OPERATION_LABELS[pseudo]) for pseudo in WIZARD_PSEUDO_OPERATIONS]
    selection.extend(
        (operation, OPERATION_LABELS[operation]) for operation in SUPPORTED_OPERATIONS
    )
    return selection


def expand_requested_operations(operation):
    if operation == "all":
        return list(ALL_OPERATIONS)
    return [operation]


def estimate_credits(product_count, operation, use_extra_media=False):
    ops = expand_requested_operations(operation)
    assets_per_product = 5 if use_extra_media else 1
    return product_count * len(ops) * CREDITS_PER_OPERATION, len(ops) * assets_per_product


def extract_batch_job_payloads(payload):
    if not isinstance(payload, dict):
        return []

    jobs = payload.get("jobs")
    if isinstance(jobs, list):
        return [job for job in jobs if isinstance(job, dict)]

    items = payload.get("items")
    if isinstance(items, list):
        return [
            item["job"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("job"), dict)
        ]

    return []

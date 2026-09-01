"""Hardcoded generation vocabularies aligned with Jobs shared constants."""

import re
import uuid

MODEL_PRESET_IDS = (
    "neutral_female_S",
    "neutral_male_M",
    "curve_female_XL",
    "tall_male_L",
)

POSE_IDS = (
    "frontal",
    "three_quarter",
    "side",
    "back",
)

LIFESTYLE_SCENE_PRESET_IDS = (
    "urban_street",
    "cafe_interior",
    "park_nature",
    "beach_coastal",
    "minimalist_home",
    "gym_fitness",
    "rooftop_terrace",
    "hiking_trail",
)

DEFAULT_MODEL_PRESET = "neutral_female_S"
DEFAULT_POSE = "frontal"
DEFAULT_SCENE_PRESET = "urban_street"
MAX_BACKGROUND_PROMPT_LENGTH = 500


def validate_model_preset(value):
    if value not in MODEL_PRESET_IDS:
        raise ValueError(f"Invalid model preset: {value}")
    return value


def validate_pose(value):
    if value not in POSE_IDS:
        raise ValueError(f"Invalid pose: {value}")
    return value


def validate_scene_preset(value):
    if value not in LIFESTYLE_SCENE_PRESET_IDS:
        raise ValueError(f"Invalid scene preset: {value}")
    return value


def normalize_background_prompt(value):
    if not value:
        return ""
    normalized = re.sub(r"\s+", " ", str(value).strip())
    if len(normalized) > MAX_BACKGROUND_PROMPT_LENGTH:
        normalized = normalized[:MAX_BACKGROUND_PROMPT_LENGTH]
    return normalized


def normalize_model_catalog_id(value):
    if not value:
        return ""
    normalized = str(value).strip()
    if not normalized:
        return ""
    try:
        uuid.UUID(normalized)
    except ValueError as error:
        raise ValueError(f"Invalid model catalog id: {value}") from error
    return normalized

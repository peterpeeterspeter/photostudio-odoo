import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utils"))

from generation_options import (  # noqa: E402
    DEFAULT_MODEL_PRESET,
    DEFAULT_POSE,
    DEFAULT_SCENE_PRESET,
    LIFESTYLE_SCENE_PRESET_IDS,
    MODEL_PRESET_IDS,
    POSE_IDS,
    normalize_background_prompt,
    normalize_model_catalog_id,
    validate_model_preset,
    validate_pose,
    validate_scene_preset,
)


class TestGenerationOptions(unittest.TestCase):
    def test_hardcoded_vocabularies(self):
        self.assertEqual(len(MODEL_PRESET_IDS), 4)
        self.assertEqual(len(POSE_IDS), 4)
        self.assertEqual(len(LIFESTYLE_SCENE_PRESET_IDS), 8)
        self.assertNotIn("detail", POSE_IDS)

    def test_defaults_validate(self):
        validate_model_preset(DEFAULT_MODEL_PRESET)
        validate_pose(DEFAULT_POSE)
        validate_scene_preset(DEFAULT_SCENE_PRESET)

    def test_invalid_values_raise(self):
        with self.assertRaises(ValueError):
            validate_model_preset("unknown_preset")
        with self.assertRaises(ValueError):
            validate_pose("detail")
        with self.assertRaises(ValueError):
            validate_scene_preset("unknown_scene")

    def test_normalize_background_prompt_collapses_whitespace(self):
        self.assertEqual(normalize_background_prompt("  a park  "), "a park")

    def test_normalize_background_prompt_caps_length(self):
        self.assertEqual(len(normalize_background_prompt("x" * 600)), 500)

    def test_normalize_model_catalog_id_accepts_uuid(self):
        sample = "550e8400-e29b-41d4-a716-446655440000"
        self.assertEqual(normalize_model_catalog_id(sample), sample)

    def test_normalize_model_catalog_id_rejects_invalid(self):
        with self.assertRaises(ValueError):
            normalize_model_catalog_id("not-a-uuid")


if __name__ == "__main__":
    unittest.main()

import io
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError

import main
import model_manager


class ModelManagerTests(unittest.TestCase):
    def test_load_uses_first_successful_lm_studio_endpoint_only(self):
        calls = []

        def fake_request(method, url, cfg=None, payload=None, timeout=30.0):
            calls.append((method, url, payload))
            return {}

        with mock.patch.object(model_manager, "_request_json", side_effect=fake_request):
            ok = model_manager._post_model_action("load", "qwen/qwen3.6-27b", None, timeout=1.0)

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].endswith("/api/v1/models/load"))

    def test_load_falls_back_when_first_lm_studio_endpoint_is_unavailable(self):
        calls = []

        def fake_request(method, url, cfg=None, payload=None, timeout=30.0):
            calls.append((method, url, payload))
            if len(calls) == 1:
                raise HTTPError(
                    url,
                    404,
                    "not found",
                    hdrs=None,
                    fp=io.BytesIO(b"not found"),
                )
            return {}

        with mock.patch.object(model_manager, "_request_json", side_effect=fake_request):
            ok = model_manager._post_model_action("load", "qwen/qwen3.6-27b", None, timeout=1.0)

        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][1].endswith("/api/v1/models/load"))
        self.assertTrue(calls[1][1].endswith("/api/v0/models/load"))

    def test_duplicate_lm_studio_instance_suffix_matches_base_model(self):
        self.assertTrue(
            model_manager._text_id_matches(
                "qwen/qwen3.6-27b",
                "qwen/qwen3.6-27b:2",
            )
        )
        self.assertTrue(
            model_manager._text_id_matches(
                "qwen/qwen3.6-27b",
                "qwen3.6-27b:2",
            )
        )


class MainModelStageTests(unittest.TestCase):
    def test_text_model_stays_loaded_when_host_focus_vision_is_disabled(self):
        cfg = SimpleNamespace(LM_STUDIO_MODEL="qwen/qwen3.6-27b")

        with mock.patch.object(main, "_finish_text_model_stage") as finish:
            unloaded = main._finish_text_model_stage_for_vision_handoff(
                cfg,
                text_model_stage_started=True,
                vision_scoring_requested=False,
                active_stage="stage 5 clip scoring",
            )

        self.assertFalse(unloaded)
        finish.assert_not_called()

    def test_text_model_unloads_when_host_focus_vision_needs_handoff(self):
        cfg = SimpleNamespace(LM_STUDIO_MODEL="qwen/qwen3.6-27b")

        with mock.patch.object(main, "_finish_text_model_stage", return_value=True) as finish:
            unloaded = main._finish_text_model_stage_for_vision_handoff(
                cfg,
                text_model_stage_started=True,
                vision_scoring_requested=True,
                active_stage="stage 5 clip scoring",
            )

        self.assertTrue(unloaded)
        finish.assert_called_once_with(
            cfg,
            active_stage="stage 5 clip scoring",
            required=True,
        )


if __name__ == "__main__":
    unittest.main()

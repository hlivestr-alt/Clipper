import types
import unittest
from unittest.mock import patch

import clip_scorer


class ClipScorerAccountingTest(unittest.TestCase):
    def test_combined_text_call_returns_hook_and_counts_one_http_call(self):
        calls = []

        class FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(
                                content=(
                                    '{"content_score": 7.0, '
                                    '"content_flags": ["product_focus"], '
                                    '"content_summary": "Fokus pada produk.", '
                                    '"engagement_score": 8.0, '
                                    '"engagement_flags": ["promo_focus"], '
                                    '"engagement_metrics": {'
                                    '"price_mentioned": true, '
                                    '"product_name_mentioned": true, '
                                    '"demo_signal": false, '
                                    '"benefit_claim": true}, '
                                    '"hook_score": 8.5, '
                                    '"hook_summary": "Pembukaan kuat."}'
                                )
                            )
                        )
                    ]
                )

        fake_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=FakeCompletions())
        )
        cfg = types.SimpleNamespace(
            LM_STUDIO_BASE_URL="http://localhost:1234/v1",
            LM_STUDIO_API_KEY="lm-studio",
            LM_STUDIO_MODEL="qwen/qwen3.6-27b",
            LM_STUDIO_TIMEOUT=120,
        )
        transcript = [
            {"word": "serum", "start": 0.1, "end": 0.3},
            {"word": "promo", "start": 1.0, "end": 1.2},
        ]
        accounting = {"actual_text_qwen_calls": 0}

        with patch("openai.OpenAI", return_value=fake_client):
            result = clip_scorer._score_content_and_engagement_with_qwen(
                "serum promo hari ini",
                "Serum",
                cfg,
                transcript=transcript,
                accounting=accounting,
            )

        self.assertEqual(1, len(calls))
        self.assertEqual(1, accounting["actual_text_qwen_calls"])
        self.assertEqual(8.5, result["hook"]["score"])
        self.assertEqual("qwen_combined", result["hook"]["metrics"]["source"])
        user_message = calls[0]["messages"][1]["content"]
        self.assertIn('"hook_score"', user_message)
        self.assertIn("Transkrip pembuka", user_message)

    def test_accounting_stats_keep_explicit_zero_text_calls(self):
        stats = clip_scorer._build_scoring_optimization_stats(
            scores=[
                {
                    "metrics": {
                        "accounting": {
                            "actual_text_qwen_calls": 0,
                            "actual_vision_qwen_calls": 0,
                        }
                    }
                }
            ],
            groups=[
                {
                    "metrics": {
                        "accounting": {
                            "actual_text_qwen_calls": 0,
                            "actual_vision_qwen_calls": 0,
                        }
                    }
                }
            ],
        )

        self.assertEqual(0, stats["actual_text_qwen_calls"])
        self.assertEqual(2, stats["previous_text_qwen_calls"])
        self.assertEqual(2, stats["saved_text_qwen_calls"])


if __name__ == "__main__":
    unittest.main()

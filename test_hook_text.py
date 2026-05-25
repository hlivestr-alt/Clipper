import re
import unittest
from types import SimpleNamespace

from compliance_checker import check_compliance
from hook_text import build_hook_payload


RISKY_HARD_SELL = re.compile(
    r"\b("
    r"100\s*%|ampuh|auto|berubah\s+total|cuma\s+dalam|dijamin|gila|"
    r"hilang|instan|langsung|menghilang\w*|mengobati|menyembuh\w*|"
    r"nomor\s*1|no\.?\s*1|parah|pasti|seketika|stop|terbaik"
    r")\b",
    flags=re.IGNORECASE,
)


class HookTextTest(unittest.TestCase):
    def test_result_proof_hook_uses_soft_experience_language(self):
        payload = build_hook_payload(
            {
                "clip_id": "clip_0001",
                "product": "Serum",
                "hook": "3 HARI BERUBAH TOTAL",
                "reason": "kulit kusam langsung cerah dalam 3 hari",
                "keyword_category": "result_proof",
                "clip_type": "testimoni",
                "keywords_found": [
                    {"word": "langsung", "context": "langsung cerah dalam 3 hari"},
                    {"word": "cerah", "context": "kulit kusam jadi cerah"},
                ],
            }
        )

        rendered_text = " ".join(payload.values())
        self.assertIsNone(RISKY_HARD_SELL.search(rendered_text), payload)
        self.assertRegex(rendered_text, r"\b(TAMPAK|TERASA|RUTIN|PENGALAMAN|STEP)\b")

        result = check_compliance(
            "Serum ini membantu kulit terasa lebih lembap dengan pemakaian rutin.",
            "Serum",
            hook_text=payload,
            cfg=_cfg(),
            call_lm=False,
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["violation_count"], 0)

    def test_risky_llm_hook_is_not_reused_as_headline(self):
        payload = build_hook_payload(
            {
                "clip_id": "clip_0002",
                "product": "Toner",
                "hook": "GILA SIH FLEK HILANG TOTAL",
                "reason": "flek hitam tampak lebih samar setelah rutin pakai toner",
                "keyword_category": "pain_problem",
                "clip_type": "demo",
            }
        )

        self.assertNotEqual(payload["headline"], "GILA SIH FLEK HILANG TOTAL")
        self.assertIsNone(RISKY_HARD_SELL.search(" ".join(payload.values())), payload)


def _cfg():
    return SimpleNamespace(
        COMPLIANCE_AUTO_FIX=True,
        COMPLIANCE_BLOCK_HIGH=True,
        COMPLIANCE_LM_TIMEOUT=1,
        LM_STUDIO_BASE_URL="http://localhost:1234/v1",
        LM_STUDIO_API_KEY="lm-studio",
        LM_STUDIO_MODEL="qwen/qwen3.6-27b",
    )


if __name__ == "__main__":
    unittest.main()

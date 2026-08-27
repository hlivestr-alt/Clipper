from __future__ import annotations

import json
from typing import Any

from utils import lm_studio_openai_chat_kwargs

from .constants import PRODUCTS, ROLES

SYSTEM_PROMPT = """You identify reusable moments in an Indonesian skincare livestream transcript.

Use this systematic process for every transcript window:
1. Identify every product genuinely discussed.
2. For each product independently look for Hook, Benefits, Ingredients, and CTA moments.
3. Return every strong reusable segment found. Multiple different roles, and multiple meaningfully
   different candidates for one role, are expected when present; never fabricate a role to fill a category.
4. Within the same local discussion, when neighboring passages communicate essentially the same claim,
   return only the strongest, tightest version. Do not suppress genuinely different points elsewhere.

Product must be exactly one of: cleanser, toner, serum, eye_cream, mask, skin_cream.
Never translate, normalize, repair, or emit an alias as the product value.
Role must be exactly one of: hook, benefits, ingredients, cta.

Role definitions:
- hook: an attention-grabbing reusable opening built around a pain point, common problem, surprising
  question, curiosity, relatable complaint, or strong reason to keep watching. It must make sense at the
  start of a new short video. A generic product description alone is not a Hook.
- benefits: what the product helps with, the expected cosmetic/skincare outcome, a problem-to-solution
  relationship, or a practical reason to want it. Do not label ingredient explanation as Benefits merely
  because the ingredient has a benefit.
- ingredients: substantial explanation of named ingredients, formulation/components, ingredient function,
  or why an ingredient is included. A passing ingredient mention in general benefits is not enough.
- cta: a conversion or purchase action such as checking the yellow cart, checkout/buy instructions,
  etalase, a current promo, limited price, discount urgency, or buy-2-get-1. Product usage/tutorial steps
  (wet face, dispense, add water, foam, massage, rinse) are not CTA. If tutorial content fits none of the
  four roles, do not select it.

Every moment must be at least 15.000 seconds. Choose the tightest self-contained reusable range.
Preferred soft duration targets are Hook 15-25 seconds, CTA 15-25 seconds, Benefits 15-35 seconds, and
Ingredients 15-40 seconds. These are preferences, not hard maximums. Longer is allowed only when genuinely
needed. Do not include unnecessary setup or repeated claims, and do not extend merely because same-product
speech continues. Use only the absolute timestamps printed in the transcript.

Calibrate confidence instead of clustering scores at the high end:
- 0.90-1.00: product and role unmistakable, self-contained, excellent reuse, obvious boundaries.
- 0.75-0.89: good and useful; product/role clear, with minor context dependence or boundary uncertainty.
- 0.60-0.74: usable but weaker; product likely clear, role plausible, surrounding context may help.
- below 0.60: do not return unless there is a compelling reason.
Confidence is ranking information, not a quota; do not hardcode uniformly high values.

Do not invent wording or timestamps. Return an empty moments array only when no valid moment exists.
Return JSON only, matching the supplied schema."""

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "modular_scanner_moments",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["moments"],
            "properties": {
                "moments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start_seconds", "end_seconds", "product", "role", "confidence", "reason"],
                        "properties": {
                            "start_seconds": {"type": "number"},
                            "end_seconds": {"type": "number"},
                            "product": {"type": "string", "enum": list(PRODUCTS)},
                            "role": {"type": "string", "enum": list(ROLES)},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reason": {"type": "string"},
                        },
                    },
                }
            },
        },
    },
}


class ScannerAnalyzer:
    def __init__(self, cfg: Any, client: Any | None = None):
        self.cfg = cfg
        self.model_id = str(getattr(cfg, "LM_STUDIO_MOMENT_MODEL_ID"))
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=cfg.LM_STUDIO_BASE_URL, api_key=cfg.LM_STUDIO_API_KEY)
        self.client = client

    def analyze(self, window: dict[str, Any]) -> list[Any]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Analyze this absolute VOD interval [{window['start']:.3f}, {window['end']:.3f}].\n"
                f"Transcript:\n{window['text']}"
            )},
        ]
        kwargs = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": 8192,
            "timeout": getattr(self.cfg, "LM_STUDIO_TIMEOUT", 360),
            **lm_studio_openai_chat_kwargs(self.cfg, model_id=self.model_id),
        }
        try:
            response = self.client.chat.completions.create(**kwargs, response_format=RESPONSE_SCHEMA)
        except Exception as exc:
            if not _response_format_unsupported(exc):
                raise
            response = self.client.chat.completions.create(**kwargs)
        raw = response.choices[0].message.content
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"moments"} or not isinstance(payload["moments"], list):
            raise ValueError("LM Studio response does not match scanner JSON contract")
        return payload["moments"]


def _response_format_unsupported(exc: Exception) -> bool:
    text = str(exc).casefold()
    return isinstance(exc, TypeError) or (
        "response_format" in text and any(term in text for term in ("unsupported", "unknown", "invalid", "unexpected"))
    )

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from clipper_app.contracts.models import StrictModel


class ModularProduct(StrEnum):
    CLEANSER = "cleanser"
    TONER = "toner"
    SERUM = "serum"
    EYE_CREAM = "eye_cream"
    MASK = "mask"
    SKIN_CREAM = "skin_cream"


class ModularTemplate(StrEnum):
    STANDARD = "standard"
    INGREDIENT = "ingredient"
    BENEFIT_FOCUS = "benefit_focus"


class ModularCtaMode(StrEnum):
    USE_CTA = "use_cta"
    NO_CTA = "no_cta"


class IngredientShortagePolicy(StrEnum):
    PARTIAL = "partial"
    FALLBACK_TO_STANDARD = "fallback_to_standard"


class ModularPlannerRunCreateRequest(StrictModel):
    production_method: str = "modular_video"
    product: ModularProduct
    requested_count: int = Field(default=20, ge=1, le=100)
    requested_template: ModularTemplate = ModularTemplate.STANDARD
    cta_mode: ModularCtaMode = ModularCtaMode.USE_CTA
    target_min_duration: float = Field(ge=15, le=180)
    target_max_duration: float = Field(ge=15, le=180)
    ingredient_shortage_policy: IngredientShortagePolicy = IngredientShortagePolicy.PARTIAL
    seed: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_planner_request(self) -> "ModularPlannerRunCreateRequest":
        if self.production_method != "modular_video":
            raise ValueError("production_method must be modular_video")
        if self.target_min_duration >= self.target_max_duration:
            raise ValueError("target_min_duration must be less than target_max_duration")
        return self


class ModularPlannerRevisionRequest(StrictModel):
    expected_revision: int = Field(ge=1)


class ModularPlannerItem(StrictModel):
    position: int = Field(ge=0)
    segment_id: str
    scan_id: str
    scanner_generation: int = Field(ge=1)
    role: str
    source_id: str
    source_filename: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    transcript_text: str
    reason: str
    approved_usage_at_selection: int = Field(default=0, ge=0)
    current_run_usage_at_selection: int = Field(default=0, ge=0)
    ranking_metadata: dict[str, Any] = Field(default_factory=dict)


class ModularPlannerComposition(StrictModel):
    composition_id: str
    ordinal: int = Field(ge=1)
    requested_template: ModularTemplate
    actual_template: ModularTemplate
    fallback_reason: str | None = None
    cta_mode: ModularCtaMode
    target_min_duration: float
    target_max_duration: float
    actual_duration: float
    distinct_source_count: int = Field(ge=1)
    selection_score: float
    selection_metadata: dict[str, Any] = Field(default_factory=dict)
    exact_signature: str
    near_signature: str
    status: str
    items: tuple[ModularPlannerItem, ...] = ()


class ModularPlannerRunDetail(StrictModel):
    planner_run_id: str
    production_method: str
    product: ModularProduct
    requested_template: ModularTemplate
    ingredient_shortage_policy: IngredientShortagePolicy
    cta_mode: ModularCtaMode
    requested_count: int
    generated_count: int
    shortfall: int
    target_min_duration: float
    target_max_duration: float
    seed: str
    planner_version: str
    status: str
    revision: int
    warnings: tuple[dict[str, Any], ...] = ()
    search_statistics: dict[str, Any] = Field(default_factory=dict)
    compositions: tuple[ModularPlannerComposition, ...] = ()


SUGGESTED_DURATION_DEFAULTS: dict[tuple[str, str], tuple[float, float]] = {
    ("standard", "use_cta"): (45.0, 75.0),
    ("standard", "no_cta"): (30.0, 60.0),
    ("ingredient", "use_cta"): (60.0, 90.0),
    ("ingredient", "no_cta"): (45.0, 75.0),
    ("benefit_focus", "use_cta"): (60.0, 90.0),
    ("benefit_focus", "no_cta"): (45.0, 75.0),
}

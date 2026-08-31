from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from clipper_app.contracts.modular_planner_models import (
    IngredientShortagePolicy,
    ModularCtaMode,
    ModularProduct,
    ModularTemplate,
)
from clipper_app.contracts.models import StrictModel


class ModularWorkflowMode(StrEnum):
    AUTOMATIC = "automatic"
    REVIEW_FIRST = "review_first"


class ModularProductionProduct(StrEnum):
    CLEANSER = "cleanser"
    TONER = "toner"
    SERUM = "serum"
    EYE_CREAM = "eye_cream"
    MASK = "mask"
    SKIN_CREAM = "skin_cream"
    ALL_PRODUCTS = "all_products"


class ModularProductionJobCreateRequest(StrictModel):
    production_method: str = "modular_video"
    workflow_mode: ModularWorkflowMode = ModularWorkflowMode.AUTOMATIC
    product: ModularProductionProduct
    requested_base_count: int = Field(default=20, ge=1, le=100)
    requested_template: ModularTemplate = ModularTemplate.STANDARD
    cta_mode: ModularCtaMode = ModularCtaMode.USE_CTA
    target_min_duration: float = Field(ge=15, le=180)
    target_max_duration: float = Field(ge=15, le=180)
    ingredient_shortage_policy: IngredientShortagePolicy = IngredientShortagePolicy.PARTIAL
    variant_profile_id: str = Field(default="active", min_length=1, max_length=128)
    seed: str | None = Field(default=None, max_length=128)
    explicit_rerun: bool = False

    @model_validator(mode="after")
    def validate_request(self) -> "ModularProductionJobCreateRequest":
        if self.production_method != "modular_video":
            raise ValueError("production_method must be modular_video")
        if self.target_min_duration >= self.target_max_duration:
            raise ValueError("target_min_duration must be less than target_max_duration")
        if self.product == ModularProductionProduct.ALL_PRODUCTS and self.requested_base_count < 6:
            raise ValueError("All Products requires at least 6 base videos")
        return self


class ModularProductionContinueRequest(StrictModel):
    expected_planner_revision: int | None = Field(default=None, ge=1)
    expected_planner_revisions: dict[str, int] | None = None

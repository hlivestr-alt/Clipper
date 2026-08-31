from __future__ import annotations

from pydantic import Field, model_validator

from clipper_app.contracts.models import StrictModel


class ModularRenderRunCreateRequest(StrictModel):
    planner_run_id: str = Field(min_length=1, max_length=128)
    composition_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    manual_rerender: bool = False

    @model_validator(mode="after")
    def validate_selection(self) -> "ModularRenderRunCreateRequest":
        if len(set(self.composition_ids)) != len(self.composition_ids):
            raise ValueError("composition_ids must be unique")
        return self

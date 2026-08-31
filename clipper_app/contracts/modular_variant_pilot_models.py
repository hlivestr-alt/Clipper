from __future__ import annotations

from pydantic import Field, model_validator

from clipper_app.contracts.models import StrictModel


class ModularVariantBaseRef(StrictModel):
    render_run_id: str = Field(min_length=1, max_length=128)
    composition_id: str = Field(min_length=1, max_length=128)


class ModularVariantPilotCreateRequest(StrictModel):
    bases: tuple[ModularVariantBaseRef, ...] = Field(min_length=1, max_length=100)
    profile_id: str = Field(default="active", min_length=1, max_length=128)
    manual_rerun: bool = False

    @model_validator(mode="after")
    def unique_bases(self) -> "ModularVariantPilotCreateRequest":
        identities = [(item.render_run_id, item.composition_id) for item in self.bases]
        if len(identities) != len(set(identities)):
            raise ValueError("bases must be unique")
        return self

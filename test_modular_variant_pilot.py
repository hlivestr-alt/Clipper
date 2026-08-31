from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from clipper_app.contracts.modular_variant_pilot_models import ModularVariantPilotCreateRequest
from clipper_app.modular_variant_pilot import ModularVariantPilotConflict, ModularVariantPilotRepository, ModularVariantPilotService
from clipper_app.variant_generation import BaseClipArtifact, generate_base_clip_variants


COMPOSITION_ID = "composition-approved"
RENDER_RUN_ID = "render-v11"
PLANNER_RUN_ID = "planner-approved"


def composition():
    return {
        "composition_id": COMPOSITION_ID,
        "items": [
            {"position": 0, "role": "hook", "start_seconds": 10.0, "end_seconds": 40.0, "transcript_text": "Hook serum jelas"},
            {"position": 1, "role": "benefits", "start_seconds": 50.0, "end_seconds": 80.0, "transcript_text": "Manfaat serum lengkap"},
        ],
    }


class FakePlanner:
    status = "approved"

    def get_run(self, run_id):
        assert run_id == PLANNER_RUN_ID
        return {"planner_run_id": run_id, "status": self.status}

    def manifest(self, run_id, public=False):
        assert public is False
        return {"payload": {"product": "serum", "compositions": [composition()]}}


class FakeRendererRepository:
    status = "completed"
    product = "serum"
    version = "modular-renderer-v1.1"

    def get_run(self, run_id):
        return {"render_run_id": run_id, "planner_run_id": PLANNER_RUN_ID, "renderer_version": self.version, "items": []}

    def item(self, run_id, composition_id):
        return {
            "status": self.status, "product": self.product, "ordinal": 1,
            "diagnostics_json": '{"validated":true}', "composition_id": composition_id,
        }

    def list_runs(self, planner_run_id, limit):
        return [{
            "render_run_id": RENDER_RUN_ID, "planner_run_id": planner_run_id, "renderer_version": self.version,
            "items": [{"status": self.status, "composition_id": COMPOSITION_ID, "product": self.product, "ordinal": 1, "rendered_duration": 60.0}],
        }]


class FakeRenderer:
    def __init__(self, path):
        self.path = path
        self.repository = FakeRendererRepository()

    def media_path(self, run_id, composition_id):
        return self.path


@pytest.fixture
def setup(tmp_path):
    base = tmp_path / "base.mp4"
    base.write_bytes(b"base-video-identity")
    cfg = SimpleNamespace(WORKING_DIR=str(tmp_path), VARIANTS_PER_CLIP=6)
    renderer = FakeRenderer(base)
    planner = FakePlanner()
    repository = ModularVariantPilotRepository(tmp_path / "pilot.sqlite3")
    return cfg, renderer, planner, repository


def request(manual=False):
    return ModularVariantPilotCreateRequest.model_validate({
        "bases": [{"render_run_id": RENDER_RUN_ID, "composition_id": COMPOSITION_ID}],
        "profile_id": "active", "manual_rerun": manual,
    })


def test_only_completed_approved_validated_v11_render_is_eligible(setup):
    cfg, renderer, planner, repository = setup
    service = ModularVariantPilotService(cfg, renderer=renderer, planner=planner, repository=repository, start_worker=False)
    assert service.eligible(PLANNER_RUN_ID)[0]["product"] == "serum"

    renderer.repository.status = "failed"
    with pytest.raises(ModularVariantPilotConflict, match="completed"):
        service.create_run(request())
    renderer.repository.status = "completed"
    planner.status = "draft"
    with pytest.raises(ModularVariantPilotConflict, match="not approved"):
        service.create_run(request())


def test_adapter_preserves_exact_base_product_provenance_and_generates_six(setup):
    cfg, renderer, planner, repository = setup
    calls = []

    def generator(artifact, output_directory, cfg_arg, profile, expected_count):
        calls.append((artifact, expected_count, cfg_arg, profile))
        outputs = []
        for index in range(6):
            path = Path(output_directory) / f"var{index}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(f"variant-{index}".encode())
            outputs.append({
                "variant_index": index, "variant_id": f"v{index}", "variant_name": f"Variant {index + 1}",
                "output_path": str(path), "duration": 60.0, "width": 1080, "height": 1920,
                "has_video": True, "has_audio": True, "file_size": path.stat().st_size, "generation_seconds": 1.0,
            })
        return outputs

    busy = iter([True, False])
    service = ModularVariantPilotService(
        cfg, renderer=renderer, planner=planner, repository=repository, generator=generator,
        production_active=lambda: next(busy, False), wait_poll_seconds=0, start_worker=False,
    )
    created, reused = service.create_run(request())
    assert not reused and created["total_expected_outputs"] == 6
    assert service.process_pending_once()
    completed = service.get_run(created["run_id"])
    assert completed["status"] == "completed"
    assert completed["total_completed_outputs"] == 6
    assert [row["variant_index"] for row in completed["items"][0]["outputs"]] == list(range(6))
    artifact = calls[0][0]
    assert artifact.path == renderer.path
    assert artifact.product == "serum"
    assert len(artifact.transcript_words) == 6
    assert completed["transcript_bridge_version"] == "modular-transcript-bridge-v1"
    assert completed["items"][0]["transcript_diagnostics"]["timing_mode"] == "synthetic_distribution"
    assert completed["items"][0]["transcript_diagnostics"]["items"][0]["fallback_reasons"] == ["source_transcript_unavailable"]
    assert completed["items"][0]["planner_run_id"] == PLANNER_RUN_ID
    assert completed["items"][0]["renderer_version"] == "modular-renderer-v1.1"
    assert all("output_path" not in row for row in completed["items"][0]["outputs"])

    duplicate, reused = service.create_run(request())
    assert reused and duplicate["run_id"] == created["run_id"]
    assert len(calls) == 1


def test_restart_keeps_completed_item_and_does_not_duplicate(setup):
    cfg, renderer, planner, repository = setup
    calls = []

    def generator(artifact, output_directory, cfg_arg, profile, expected_count):
        calls.append(artifact.clip_id)
        rows = []
        for index in range(6):
            path = Path(output_directory) / f"v{index}.mp4"; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"valid")
            rows.append({"variant_index": index, "variant_id": f"v{index}", "variant_name": f"v{index}", "output_path": str(path), "duration": 60, "width": 1, "height": 1, "has_video": True, "has_audio": True, "file_size": 5, "generation_seconds": 0.1})
        return rows

    first = ModularVariantPilotService(cfg, renderer=renderer, planner=planner, repository=repository, generator=generator, production_active=lambda: False, start_worker=False)
    created, _ = first.create_run(request()); first.process_pending_once()
    restarted = ModularVariantPilotService(cfg, renderer=renderer, planner=planner, repository=ModularVariantPilotRepository(repository.path), generator=generator, production_active=lambda: False, start_worker=False)
    assert not restarted.process_pending_once()
    assert restarted.get_run(created["run_id"])["total_completed_outputs"] == 6
    assert len(calls) == 1


def test_public_contract_rejects_paths_and_old_renderer(setup):
    cfg, renderer, planner, repository = setup
    with pytest.raises(Exception):
        ModularVariantPilotCreateRequest.model_validate({"bases": [{"path": str(renderer.path)}]})
    renderer.repository.version = "modular-renderer-v1"
    service = ModularVariantPilotService(cfg, renderer=renderer, planner=planner, repository=repository, start_worker=False)
    with pytest.raises(ModularVariantPilotConflict, match="v1.1"):
        service.create_run(request())


def test_generic_variants_boundary_uses_existing_six_recipe_path_for_sixty_seconds(tmp_path, monkeypatch):
    import main
    import clipper_app.variant_generation as boundary

    base = tmp_path / "sixty-second-base.mp4"; base.write_bytes(b"base")
    profile = {"revision": "existing-profile", "variants": [{"name": f"Variant {index + 1}"} for index in range(6)]}
    cfg = SimpleNamespace(WORKING_DIR=str(tmp_path), VARIANTS_PER_CLIP=6)
    calls = []

    def process(job, video_path, transcript_words, product_events, cut_only, runtime_cfg):
        calls.append({
            "video_path": video_path, "product": job["product"], "compliance": runtime_cfg.COMPLIANCE_ENABLED,
            "target_size": runtime_cfg.RENDER_REQUIRE_TARGET_SIZE,
            "profile_revision": job["moment"]["_variant"].profile_revision,
        })
        output = Path(job["output_path"]); output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(b"variant")
        return {"status": "ok"}

    monkeypatch.setattr(main, "_process_clip_job", process)
    monkeypatch.setattr(boundary, "probe_media", lambda path: {
        "duration": 60.2, "width": 1080, "height": 1920, "has_video": True, "has_audio": True,
    })
    outputs = generate_base_clip_variants(
        BaseClipArtifact("base-1", base, "serum", ({"word": "serum", "start": 0, "end": 1},)),
        tmp_path / "outputs", cfg, profile,
    )
    assert len(outputs) == 6
    assert [row["variant_index"] for row in outputs] == list(range(6))
    assert all(row["duration"] == 60.2 for row in outputs)
    assert all(call == {
        "video_path": str(base.resolve()), "product": "serum", "compliance": False,
        "target_size": False,
        "profile_revision": "existing-profile",
    } for call in calls)

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from clipper_app.application.whatsapp_delivery import (
    WhatsAppConflict,
    WhatsAppDeliveryService,
    WhatsAppNotFound,
)
from clipper_app.contracts.whatsapp_delivery_models import WhatsAppClaimRequest


def _service(tmp_path: Path) -> WhatsAppDeliveryService:
    mirror = tmp_path / "mirror"
    return WhatsAppDeliveryService(mirror / "_whatsapp_state.sqlite3", mirror)


def _register(service: WhatsAppDeliveryService, batch: int) -> None:
    folder = service.mirror_root / str(batch)
    folder.mkdir(parents=True)
    service.register_media_batch(
        batch,
        folder,
        [
            {
                "relative_path": "clip.mp4",
                "size_bytes": 123,
                "fingerprint": "abc",
                "compliance": {"compliant": True},
            }
        ],
    )


def test_claim_is_idempotent_and_never_changes_numeric_folder(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _register(service, 42)
    request = WhatsAppClaimRequest(
        affiliate_name="Affiliate A",
        affiliate_identifier="sheet-row-1",
        idempotency_key="claim-key-0001",
    )
    first = service.claim(request, actor="test")
    second = service.claim(request, actor="test")
    assert first["affiliate_assignment_id"] == second["affiliate_assignment_id"]
    assert first["batch_number"] == 42
    assert Path(first["canonical_folder_path"]).name == "42"
    assert (service.mirror_root / "42").is_dir()


def test_two_concurrent_claims_receive_different_batches(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _register(service, 1)
    _register(service, 2)

    def claim(index: int) -> int:
        result = service.claim(
            WhatsAppClaimRequest(
                affiliate_name=f"Affiliate {index}",
                affiliate_identifier=f"affiliate-{index}",
                idempotency_key=f"concurrent-claim-{index:04d}",
            ),
            actor="test",
        )
        return int(result["batch_number"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = sorted(pool.map(claim, (1, 2)))
    assert claimed == [1, 2]


def test_sent_assignment_cannot_be_released(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _register(service, 7)
    assignment = service.claim(
        WhatsAppClaimRequest(
            affiliate_name="Affiliate",
            affiliate_identifier="affiliate",
            idempotency_key="release-test-0001",
        ),
        actor="test",
    )
    assignment = service.transition(
        assignment["affiliate_assignment_id"],
        "sending",
        expected_version=assignment["version"],
        actor="test",
        idempotency_key="start-sent-test-0001",
    )
    assignment = service.update_item(
        assignment["affiliate_assignment_id"],
        "clip.mp4",
        status="sent",
        whatsapp_message_id="wamid.1",
    )
    assignment = service.transition(
        assignment["affiliate_assignment_id"],
        "sent",
        expected_version=assignment["version"],
        actor="test",
        idempotency_key="finish-sent-test-0001",
    )
    with pytest.raises(WhatsAppConflict):
        service.transition(
            assignment["affiliate_assignment_id"],
            "unassigned",
            expected_version=assignment["version"],
            actor="test",
            idempotency_key="release-sent-test-0001",
            operator_reason="should fail",
        )


def test_no_ready_batch_returns_not_found(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(WhatsAppNotFound):
        service.claim(
            WhatsAppClaimRequest(
                affiliate_name="Affiliate",
                affiliate_identifier="affiliate",
                idempotency_key="empty-test-0001",
            ),
            actor="test",
        )


@pytest.mark.parametrize(
    ("direct_enabled", "legacy_disabled"),
    ((False, True), (True, False), (False, False)),
)
def test_claim_fails_closed_until_delivery_cutover_is_complete(
    tmp_path: Path, direct_enabled: bool, legacy_disabled: bool
) -> None:
    mirror = tmp_path / "mirror"
    service = WhatsAppDeliveryService(
        mirror / "_whatsapp_state.sqlite3",
        mirror,
        direct_pc_delivery_enabled=direct_enabled,
        legacy_drive_workflow_disabled=legacy_disabled,
    )
    _register(service, 9)

    status = service.status()
    assert status["cutover"]["claims_enabled"] is False
    with pytest.raises(WhatsAppConflict, match="Direct PC-to-WhatsApp delivery is blocked"):
        service.claim(
            WhatsAppClaimRequest(
                affiliate_name="Affiliate",
                affiliate_identifier="affiliate",
                idempotency_key=f"cutover-{direct_enabled}-{legacy_disabled}",
            ),
            actor="test",
        )


def test_status_reports_completed_delivery_cutover(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.status()["cutover"] == {
        "direct_pc_delivery_enabled": True,
        "legacy_drive_workflow_disabled": True,
        "claims_enabled": True,
        "blocking_reason": None,
    }


def test_transition_and_sheet_outbox_are_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _register(service, 8)
    assignment = service.claim(
        WhatsAppClaimRequest(
            affiliate_name="Affiliate",
            affiliate_identifier="affiliate",
            idempotency_key="transition-claim-0001",
        ),
        actor="test",
    )
    first = service.transition(
        assignment["affiliate_assignment_id"],
        "sending",
        expected_version=assignment["version"],
        actor="test",
        idempotency_key="transition-start-0001",
    )
    second = service.transition(
        assignment["affiliate_assignment_id"],
        "sending",
        expected_version=assignment["version"],
        actor="test",
        idempotency_key="transition-start-0001",
    )
    assert first["version"] == second["version"]
    outbox = service.pending_outbox()["items"]
    assert outbox
    acknowledged = service.acknowledge_outbox(
        outbox[0]["outbox_id"], success=True
    )
    assert acknowledged["status"] == "synced"

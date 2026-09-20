from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from merch.database import get_engine, session_scope
from merch.models import Base
from merch.pipeline import _checkpoint_etsy_publish
from merch.repository import RunRepository
from merch.schemas import Channel, RunInput
from merch.services.storefront import StorefrontVerificationError


@pytest.fixture
def checkpoint_run_id(isolated_app: Path) -> str:
    Base.metadata.create_all(get_engine())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    run_id = str(value.run_id)
    with session_scope() as session:
        repository = RunRepository(session)
        repository.create(value, f"checkpoint-{run_id}")
        publish = repository.publish_record(run_id, Channel.ETSY.value, "fixture")
        publish.response_data = {
            "publish_response": {"status": "accepted"},
            "native_poll_started": "2026-09-19T13:30:00+00:00",
        }
    return run_id


def progress(run_id: str) -> dict[str, Any]:
    with session_scope() as session:
        publish = RunRepository(session).publish_record(run_id, Channel.ETSY.value, "pending")
        return dict(publish.response_data or {})


def test_draft_reservation_blocks_duplicate_without_overwriting_checkpoint(
    checkpoint_run_id: str,
) -> None:
    _checkpoint_etsy_publish(
        checkpoint_run_id, draft_create_started=True, stage="creating_draft"
    )
    before = progress(checkpoint_run_id)
    with pytest.raises(StorefrontVerificationError, match="draft creation is already reserved"):
        _checkpoint_etsy_publish(
            checkpoint_run_id, draft_create_started=True, stage="second_worker"
        )
    assert progress(checkpoint_run_id) == before


@pytest.mark.parametrize("already_finished", [False, True])
def test_image_reservation_blocks_pending_or_completed_color(
    checkpoint_run_id: str, already_finished: bool,
) -> None:
    _checkpoint_etsy_publish(checkpoint_run_id, image_upload_started_color="Black")
    if already_finished:
        _checkpoint_etsy_publish(
            checkpoint_run_id,
            etsy_color_image_ids={"Black": 91},
            image_upload_started_color=None,
        )
    before = progress(checkpoint_run_id)
    with pytest.raises(StorefrontVerificationError, match="image upload is already reserved"):
        _checkpoint_etsy_publish(checkpoint_run_id, image_upload_started_color="Black")
    assert progress(checkpoint_run_id) == before


def test_stale_image_map_merges_completed_colors_and_retains_publish_metadata(
    checkpoint_run_id: str,
) -> None:
    _checkpoint_etsy_publish(
        checkpoint_run_id, etsy_listing_id=7, etsy_listing_owned=True,
        etsy_color_image_ids={"Black": 91},
    )
    _checkpoint_etsy_publish(
        checkpoint_run_id, etsy_color_image_ids={"Forest": 92}, stage="uploading_images"
    )
    saved = progress(checkpoint_run_id)
    assert saved["etsy_color_image_ids"] == {"Black": 91, "Forest": 92}
    assert saved["etsy_listing_id"] == 7
    assert saved["etsy_listing_owned"] is True
    assert saved["publish_response"] == {"status": "accepted"}
    assert saved["native_poll_started"] == "2026-09-19T13:30:00+00:00"


def test_completed_color_does_not_clear_another_workers_pending_upload(
    checkpoint_run_id: str,
) -> None:
    _checkpoint_etsy_publish(
        checkpoint_run_id, etsy_color_image_ids={"Black": 91},
        image_upload_started_color="Forest",
    )
    # A stale worker adopts the already uploaded Black mockup while Forest is in flight.
    _checkpoint_etsy_publish(
        checkpoint_run_id, etsy_color_image_ids={"Black": 91},
        image_upload_started_color=None,
    )
    assert progress(checkpoint_run_id)["image_upload_started_color"] == "Forest"
    _checkpoint_etsy_publish(
        checkpoint_run_id, etsy_color_image_ids={"Black": 91, "Forest": 92},
        image_upload_started_color=None,
    )
    assert progress(checkpoint_run_id)["image_upload_started_color"] is None
    assert progress(checkpoint_run_id)["etsy_color_image_ids"] == {"Black": 91, "Forest": 92}


def test_new_color_cannot_replace_an_in_flight_image_reservation(
    checkpoint_run_id: str,
) -> None:
    _checkpoint_etsy_publish(checkpoint_run_id, image_upload_started_color="Forest")
    before = progress(checkpoint_run_id)
    with pytest.raises(StorefrontVerificationError, match="image upload is already reserved"):
        _checkpoint_etsy_publish(checkpoint_run_id, image_upload_started_color="Black")
    assert progress(checkpoint_run_id) == before


def test_retry_keeps_failed_mockup_evidence_for_review(checkpoint_run_id: str) -> None:
    failed = {"status": "failed", "checks": [{"color": "Forest", "actual_object_key": "bad.jpg"}]}
    manifest = [{"color": "Forest", "source_object_key": "expected.jpg"}]
    _checkpoint_etsy_publish(
        checkpoint_run_id, mockup_manifest=manifest, mockup_verification=failed,
    )
    _checkpoint_etsy_publish(
        checkpoint_run_id, mockup_manifest=[], mockup_verification={"status": "preparing_sources"},
    )
    assert progress(checkpoint_run_id)["mockup_verification_history"] == [
        {"manifest": manifest, "verification": failed},
    ]


def test_late_worker_cannot_overwrite_successful_mockup_evidence(checkpoint_run_id: str) -> None:
    verified = {"status": "verified", "image_ids": {"Forest": 92}}
    _checkpoint_etsy_publish(checkpoint_run_id, mockup_verification=verified)
    with session_scope() as session:
        RunRepository(session).publish_record(
            checkpoint_run_id, Channel.ETSY.value, "pending",
        ).status = "succeeded"
    before = progress(checkpoint_run_id)
    with pytest.raises(StorefrontVerificationError, match="stale attempt stopped"):
        _checkpoint_etsy_publish(
            checkpoint_run_id, mockup_verification={"status": "failed"}, stage="preparing_sources",
        )
    assert progress(checkpoint_run_id) == before

from datetime import UTC, datetime, time
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from aion.channels.router import PushInput
from aion.modules.assistant.schemas import Answer
from aion.modules.planning.notifications import quiet_until
from aion.modules.planning.service import absolute
from aion.providers.storage import BlobStore
from aion.settings import Settings


def test_wall_time_rejects_dst_gaps_and_ambiguity():
    for value in (datetime(2026, 3, 29, 2, 30), datetime(2026, 10, 25, 2, 30)):
        with pytest.raises(HTTPException):
            absolute(value, "Europe/Berlin")
    assert absolute(datetime(2026, 1, 1, 10), "Europe/Saratov") == datetime(
        2026, 1, 1, 6, tzinfo=UTC
    )


def test_quiet_hours_cross_midnight():
    profile = {"timezone": "Europe/Saratov", "quiet_start": time(22), "quiet_end": time(8)}
    assert quiet_until(profile, datetime(2026, 1, 1, 20, tzinfo=UTC)) == datetime(
        2026, 1, 2, 4, tzinfo=UTC
    )
    assert quiet_until(profile, datetime(2026, 1, 1, 10, tzinfo=UTC)) is None


def test_typed_answer_cannot_write_while_clarifying():
    with pytest.raises(ValidationError):
        Answer.model_validate(
            {
                "reply": "When?",
                "clarification": True,
                "operations": [{"operation": "set_mode", "data": {"mode": "paused"}}],
            }
        )
    with pytest.raises(ValidationError):
        Answer.model_validate(
            {"reply": "x", "operations": [{"operation": "execute_sql", "sql": "DROP TABLE"}]}
        )


def test_storage_key_guard_and_push_endpoint_guard(tmp_path):
    store = BlobStore(Settings(_env_file=None, storage_path=str(tmp_path)))
    with pytest.raises(ValueError):
        store.path("../../secret")
    assert store.path(f"{uuid4()}/{uuid4()}").is_relative_to(tmp_path)
    with pytest.raises(ValidationError):
        PushInput(endpoint="https://127.0.0.1/internal", p256dh="x" * 60, auth="x" * 24)

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectInput(Input):
    title: str = Field(min_length=1, max_length=160)


class TaskInput(Input):
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=20000)
    first_step: str = Field(min_length=1, max_length=240)
    project_id: UUID | None = None
    due_at: datetime | None = None
    priority: int = Field(default=2, ge=1, le=3)
    estimated_minutes: int = Field(default=25, ge=1, le=1440)
    status: Literal["open", "done", "cancelled"] = "open"


class TaskUpdate(TaskInput):
    version: int = Field(ge=1)


class Overrides(Input):
    reminders: bool | None = None
    progress: bool | None = None
    results: bool | None = None


class ActivityInput(Input):
    title: str = Field(min_length=1, max_length=240)
    notes: str = Field(default="", max_length=20000)
    task_id: UUID | None = None
    starts_at: datetime
    ends_at: datetime
    fixed: bool = False
    keep_on_pause: bool = False
    notification_overrides: Overrides = Field(default_factory=Overrides)


class ActivityUpdate(ActivityInput):
    version: int = Field(ge=1)


class ResultInput(Input):
    version: int = Field(ge=1)
    outcome: Literal["done", "partial", "cancelled"]
    content: str = Field(default="", max_length=20000)
    intent_id: UUID | None = None


class ModeInput(Input):
    mode: Literal["normal", "light", "paused"]


class RulesInput(Input):
    reminders: bool = True
    progress: bool = False
    results: bool = True
    channels: list[Literal["web", "push", "telegram"]] = Field(default=["web"])
    lead_minutes: int = Field(default=60, ge=0, le=10080)
    result_delay_minutes: int = Field(default=30, ge=0, le=10080)
    progress_minutes: int = Field(default=120, ge=1, le=10080)
    daily_limit: int = Field(default=5, ge=0, le=100)
    repeat_count: int = Field(default=1, ge=0, le=5)
    repeat_minutes: int = Field(default=120, ge=1, le=10080)
    tone: Literal["спокойный", "нейтральный", "поддерживающий"] = "спокойный"
    message_format: Literal["кратко", "с подробностями"] = "кратко"
    version: int = Field(default=1, ge=1)

    @field_validator("channels")
    @classmethod
    def unique_channels(cls, value: list[str]) -> list[str]:
        return sorted(set(value) | {"web"})


class SnoozeInput(Input):
    minutes: int = Field(default=60, ge=5, le=10080)

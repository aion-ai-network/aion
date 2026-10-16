from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    email: EmailStr
    password: SecretStr = Field(min_length=1, max_length=256)


class Register(Input):
    token: str = Field(min_length=32, max_length=128)
    password: SecretStr = Field(min_length=12, max_length=256)
    display_name: str = Field(min_length=1, max_length=80)


class ResetPassword(Input):
    token: str = Field(min_length=32, max_length=128)
    password: SecretStr = Field(min_length=12, max_length=256)


class CreateInvite(Input):
    email: EmailStr
    hours: int = Field(default=48, ge=1, le=168)


class AccessUpdate(Input):
    status: Literal["active", "disabled"]


class ProfileUpdate(Input):
    display_name: str = Field(min_length=1, max_length=80)
    timezone: str = Field(max_length=64)
    day_start: time
    day_end: time
    quiet_start: time
    quiet_end: time
    mode: Literal["normal", "light", "paused"]
    notifications_enabled: bool
    onboarding_completed: bool = True
    version: int = Field(ge=1)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Unknown IANA timezone") from error
        return value

    @field_validator("day_start", "day_end", "quiet_start", "quiet_end")
    @classmethod
    def local_time(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("Use local time without a UTC offset")
        return value

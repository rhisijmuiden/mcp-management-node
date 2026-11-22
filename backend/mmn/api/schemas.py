from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from mmn.security import MIN_PASSWORD_LENGTH

USERNAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,63}$"


class Password(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str
    email: str | None
    full_name: str | None
    role: Literal["admin", "viewer"]
    language: str
    theme: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class UserCreate(Password):
    username: str = Field(pattern=USERNAME_PATTERN)
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=128)
    role: Literal["admin", "viewer"] = "viewer"
    language: Literal["en", "nl"] = "en"

    @field_validator("username")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=128)
    role: Literal["admin", "viewer"] | None = None
    language: Literal["en", "nl"] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=256)


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    remember: bool = False


class SetupIn(UserCreate):
    setup_token: str
    role: Literal["admin"] = "admin"


class PreferencesIn(BaseModel):
    language: Literal["en", "nl"] | None = None
    theme: Literal["system", "light", "dark"] | None = None
    full_name: str | None = Field(default=None, max_length=128)
    email: EmailStr | None = None


class ChangePasswordIn(Password):
    current_password: str


class ForgotIn(BaseModel):
    username_or_email: str = Field(max_length=255)


class ResetIn(Password):
    token: str

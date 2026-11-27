"""Request / response models with validation shared by the API layer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Lang = Literal["zh", "en"]
PromptNo = str


def _check_identifier(value: str, *, label: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 64:
        raise ValueError(f"{label} must be 1-64 characters")
    if any(char.isspace() for char in value) or "/" in value:
        raise ValueError(f"{label} must not contain whitespace or '/'")
    return value


def _check_variable_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("variable names must not be empty")
    if any(char.isspace() or char in "{}" for char in value):
        raise ValueError(
            "variable names must not contain whitespace or braces"
        )
    return value


class FirmwarePromptIn(BaseModel):
    prompt_no: PromptNo
    lang: Lang
    variables: list[str] = Field(default_factory=list)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"prompt_no": "P-101", "lang": "en", "variables": ["gate", "time"]}
        }
    )

    @field_validator("prompt_no")
    @classmethod
    def _validate_prompt_no(cls, value: str) -> str:
        return _check_identifier(value, label="prompt_no")

    @field_validator("variables")
    @classmethod
    def _validate_variables(cls, value: list[str]) -> list[str]:
        names = [_check_variable_name(item) for item in value]
        if len(set(names)) != len(names):
            raise ValueError("variables must be unique")
        return names


class FirmwareCreate(BaseModel):
    firmware_id: str
    name: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"firmware_id": "fw-2.3.0", "name": "Hall A devices"}
        }
    )

    @field_validator("firmware_id")
    @classmethod
    def _validate_firmware_id(cls, value: str) -> str:
        return _check_identifier(value, label="firmware_id")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 200:
            raise ValueError("name must be at most 200 characters")
        return value


class FirmwareEnable(BaseModel):
    prompts: list[FirmwarePromptIn] = Field(default_factory=list)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "prompts": [
                    {"prompt_no": "P-101", "lang": "en", "variables": ["gate"]},
                    {"prompt_no": "P-102", "lang": "zh", "variables": ["exhibit"]},
                ]
            }
        }
    )

    @model_validator(mode="after")
    def _unique_prompt_numbers(self) -> "FirmwareEnable":
        numbers = [prompt.prompt_no for prompt in self.prompts]
        if len(set(numbers)) != len(numbers):
            raise ValueError("prompt_no must be unique within a firmware")
        return self


class TemplateCreate(BaseModel):
    prompt_no: PromptNo
    lang: Lang
    content: str

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "prompt_no": "P-101",
                "lang": "en",
                "content": "Please gather at {gate} at {time}.",
            }
        }
    )

    @field_validator("prompt_no")
    @classmethod
    def _validate_prompt_no(cls, value: str) -> str:
        return _check_identifier(value, label="prompt_no")

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        if len(value) > 5000:
            raise ValueError("content must be at most 5000 characters")
        return value


class TemplateUpdate(BaseModel):
    content: str

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"content": "Please gather at {gate} by {time}."}
        }
    )

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        if len(value) > 5000:
            raise ValueError("content must be at most 5000 characters")
        return value


class PlayRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"variables": {"gate": "north gate"}}},
    )

    variables: dict[str, Any] = Field(default_factory=dict)

    @field_validator("variables")
    @classmethod
    def _check_names(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name in value:
            _check_variable_name(name)
        return value


class TemplateOut(BaseModel):
    id: int
    prompt_no: str
    lang: str
    content: str
    variables: list[str]
    status: str
    publish_seq: int
    created_at: str
    updated_at: str
    published_at: str | None


class FirmwareOut(BaseModel):
    firmware_id: str
    name: str | None
    enabled: bool
    prompts: list[FirmwarePromptIn]
    created_at: str
    updated_at: str
    enabled_at: str | None


class PlaybackOut(BaseModel):
    firmware_id: str
    prompt_no: str
    requested_lang: str
    used_lang: str
    fallback: bool
    template_id: int
    text: str


class MissingPrompt(BaseModel):
    firmware_id: str
    prompt_no: str
    lang: str


class ErrorOut(BaseModel):
    code: str
    message: str


class MissingErrorOut(ErrorOut):
    missing: list[MissingPrompt]

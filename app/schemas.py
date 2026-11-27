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


# ===========================================================================
# Recording studio: 制单 (orders) / 交件 (deliveries)
# ===========================================================================


_SAMPLE_KEYS = {
    "sample_no", "no", "id", "seq", "index", "number", "sampleNo",
    "编号", "样例号",
    "firmware_id", "firmwareId", "firmware", "fw", "固件号", "固件",
    "prompt_no", "promptNo", "prompt", "提示号",
    "variables", "variable_values", "values", "vars",
    "变量值", "变量",
}


def _first_key(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


class OrderSampleIn(BaseModel):
    """One numbered sample: firmware + prompt number + variable values."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sample_no": 1,
                "firmware_id": "fw-2.3.0",
                "prompt_no": "P-101",
                "variables": {"地点": "north gate", "时间": "10:00"},
            }
        }
    )

    sample_no: str | int | None = None
    firmware_id: str
    prompt_no: str
    variables: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_aliases_and_flat_variables(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        sample_no = _first_key(
            d, "sample_no", "sampleNo", "no", "seq", "index", "number", "id",
            "编号", "样例号",
        )
        firmware_id = _first_key(
            d, "firmware_id", "firmwareId", "firmware", "fw", "固件号", "固件"
        )
        prompt_no = _first_key(d, "prompt_no", "promptNo", "prompt", "提示号")
        variables = _first_key(
            d, "variables", "variable_values", "values", "vars",
            "变量值", "变量",
        )
        if variables is None:
            # Tolerate a flat sample object: unknown scalar fields are treated
            # as variable values, e.g. {"firmware_id": ..., "gate": "A"}.
            variables = {
                key: value
                for key, value in d.items()
                if key not in _SAMPLE_KEYS and isinstance(value, (str, int, float, bool))
            }
        return {
            "sample_no": sample_no,
            "firmware_id": firmware_id,
            "prompt_no": prompt_no,
            "variables": variables or {},
        }

    @field_validator("firmware_id", "prompt_no")
    @classmethod
    def _validate_identifiers(cls, value: str) -> str:
        return _check_identifier(value, label="identifier")

    @field_validator("sample_no")
    @classmethod
    def _validate_sample_no(cls, value: str | int | None) -> str | int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        value = value.strip()
        if not 1 <= len(value) <= 32:
            raise ValueError("sample_no must be 1-32 characters")
        return value

    @field_validator("variables")
    @classmethod
    def _check_variable_names(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name in value:
            _check_variable_name(name)
        return value


class OrderCreate(BaseModel):
    """制单 request: a recording batch number and its numbered samples."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "batch_no": "BATCH-DOCS-01",
                "note": "一楼展厅预录",
                "samples": [
                    {
                        "sample_no": "A1",
                        "firmware_id": "fw-rec-a",
                        "prompt_no": "REC-W1",
                        "variables": {},
                    },
                    {
                        "sample_no": "A2",
                        "firmware_id": "fw-rec-b",
                        "prompt_no": "REC-W2",
                        "variables": {},
                    },
                    {
                        "sample_no": "B1",
                        "firmware_id": "fw-rec-a",
                        "prompt_no": "REC-NUM",
                        "variables": {"num": 3},
                    },
                ],
            }
        }
    )

    batch_no: str
    note: str | None = None
    samples: list[OrderSampleIn] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _accept_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {
            "batch_no": _first_key(
                data,
                "batch_no", "batch_number", "batch_id", "recording_batch_no",
                "recording_batch", "batch", "录音批号", "批号",
            ),
            "note": _first_key(data, "note", "remark", "memo", "备注"),
            "samples": _first_key(
                data, "samples", "items", "cases", "examples", "sample_list",
                "样例", "样品",
            ),
        }

    @field_validator("batch_no")
    @classmethod
    def _validate_batch_no(cls, value: str) -> str:
        return _check_identifier(value, label="batch_no")

    @field_validator("note")
    @classmethod
    def _validate_note(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 500:
            raise ValueError("note must be at most 500 characters")
        return value


class DeliveryIn(BaseModel):
    """交件 request: batch + original order entry + the voice actor's rec no."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "batch_no": "BATCH-DOCS-01",
                "entry_no": 1,
                "recording_no": "REC-0001",
            }
        }
    )

    batch_no: str | None = None
    entry_id: int | None = None
    order_id: int | None = None
    revision: int | None = None
    entry_no: int | str | None = None
    recording_no: str

    @model_validator(mode="before")
    @classmethod
    def _accept_aliases_and_nested_entry(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        entry_id = _first_key(d, "entry_id", "entryId", "条目id", "条目ID")
        entry_no = _first_key(
            d, "entry_no", "entryNo", "entry_number", "条目编号", "条目号"
        )
        order_id = _first_key(d, "order_id", "orderId")
        revision = _first_key(d, "revision", "rev")
        entry_ref = _first_key(d, "entry", "原条目", "条目")
        if isinstance(entry_ref, dict):
            entry_id = entry_id or _first_key(
                entry_ref, "entry_id", "entryId", "id", "编号"
            )
            entry_no = entry_no or _first_key(
                entry_ref, "entry_no", "entryNo", "entry_number", "no",
                "number", "编号",
            )
            order_id = order_id or _first_key(entry_ref, "order_id", "orderId")
            revision = revision or _first_key(entry_ref, "revision", "rev")
        elif entry_id is None and entry_no is None and entry_ref is not None:
            # "entry": 3 or "entry": "E3"
            if isinstance(entry_ref, int):
                entry_id = entry_ref
            else:
                entry_no = entry_ref
        return {
            "batch_no": _first_key(
                d,
                "batch_no", "batch_number", "batch_id", "recording_batch_no",
                "recording_batch", "batch", "录音批号", "批号",
            ),
            "entry_id": entry_id,
            "order_id": order_id,
            "revision": revision,
            "entry_no": entry_no,
            "recording_no": _first_key(
                d, "recording_no", "recordingNo", "record_no", "recording_id",
                "record_id", "录音号",
            ),
        }

    @model_validator(mode="after")
    def _require_entry(self) -> "DeliveryIn":
        if self.entry_id is None and self.entry_no is None:
            raise ValueError("entry_id or entry_no is required")
        if not self.recording_no or not self.recording_no.strip():
            raise ValueError("recording_no must not be blank")
        if len(self.recording_no) > 80:
            raise ValueError("recording_no must be at most 80 characters")
        self.recording_no = self.recording_no.strip()
        if self.batch_no is not None:
            self.batch_no = _check_identifier(self.batch_no, label="batch_no")
        if isinstance(self.entry_no, str):
            text = self.entry_no.strip()
            if text[:1] in {"E", "e", "#"}:
                text = text[1:].strip()
            if text.startswith("条目"):
                text = text[2:].strip()
            if text.isdigit():
                self.entry_no = int(text)
            else:
                raise ValueError("entry_no must be an integer (or like 'E1')")
        return self


class ReviewRequest(BaseModel):
    """Locate an entry for a read-only re-review (逐例复核)."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"entry_id": 1}}
    )

    entry_id: int | None = None
    order_id: int | None = None
    revision: int | None = None
    entry_no: int | str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        entry_ref = _first_key(d, "entry", "原条目", "条目")
        entry_id = _first_key(d, "entry_id", "entryId", "条目id", "条目ID")
        entry_no = _first_key(
            d, "entry_no", "entryNo", "entry_number", "条目编号", "条目号"
        )
        if isinstance(entry_ref, dict):
            entry_id = entry_id or _first_key(
                entry_ref, "entry_id", "entryId", "id", "编号"
            )
            entry_no = entry_no or _first_key(
                entry_ref, "entry_no", "entryNo", "entry_number", "no",
                "number", "编号",
            )
        elif entry_id is None and entry_no is None and entry_ref is not None:
            if isinstance(entry_ref, int):
                entry_id = entry_ref
            else:
                entry_no = entry_ref
        return {
            "entry_id": entry_id,
            "order_id": _first_key(d, "order_id", "orderId"),
            "revision": _first_key(d, "revision", "rev"),
            "entry_no": entry_no,
        }

    @model_validator(mode="after")
    def _require_entry(self) -> "ReviewRequest":
        if self.entry_id is None and self.entry_no is None:
            raise ValueError("entry_id or entry_no is required")
        if isinstance(self.entry_no, str):
            text = self.entry_no.strip().lstrip("#").lstrip("Ee").strip()
            if text.isdigit():
                self.entry_no = int(text)
            else:
                raise ValueError("entry_no must be an integer (or like 'E1')")
        return self


class SampleResultOut(BaseModel):
    ordinal: int
    sample_no: str
    firmware_id: str
    prompt_no: str
    variables: dict[str, Any]
    status: Literal["resolved", "unresolved"]
    requested_lang: str | None = None
    used_lang: str | None = None
    fallback: bool = False
    template_id: int | None = None
    text: str | None = None
    firmware_enabled: bool | None = None
    reason_code: str | None = None
    reason_message: str | None = None
    missing_variables: list[str] | None = None


class EntryOut(BaseModel):
    entry_id: int
    order_id: int
    entry_no: int
    lang: str
    text: str
    template_id: int | None = None
    fallback: bool = False
    recording_no: str | None = None
    batch_recording_no: str | None = None
    library_recording_no: str | None = None
    recording_scope: Literal["batch", "library"] | None = None
    recorded: bool = False
    sample_count: int
    sample_numbers: list[str]
    samples: list[SampleResultOut]


class OrderOut(BaseModel):
    order_id: int
    batch_no: str
    revision: int
    reissued: bool
    note: str | None = None
    created_at: str
    sample_count: int
    resolved_count: int
    unresolved_count: int
    entry_count: int
    entries: list[EntryOut]
    unresolved: list[SampleResultOut]


class ReviewItemOut(BaseModel):
    ordinal: int
    sample_no: str
    firmware_id: str
    prompt_no: str
    variables: dict[str, Any]
    matches: bool
    reason_code: str | None = None
    reason_message: str | None = None
    requested_lang: str | None = None
    used_lang: str | None = None
    fallback: bool = False
    template_id: int | None = None
    text: str | None = None
    original_lang: str
    original_text: str
    missing_variables: list[str] | None = None
    current_recording_no: str | None = None
    current_recording_scope: Literal["batch", "library"] | None = None


class ReviewOut(BaseModel):
    batch_no: str
    order_id: int
    entry_id: int
    entry_no: int
    revision: int
    original_lang: str
    original_text: str
    reviewed_count: int
    matched_count: int
    mismatched_count: int
    matched: list[ReviewItemOut]
    mismatched: list[ReviewItemOut]


class DeliveryOut(BaseModel):
    delivery_id: int
    batch_no: str
    order_id: int
    entry_id: int
    entry_no: int
    revision: int
    recording_no: str
    status: Literal["booked", "reused", "rejected"]
    booked: bool
    effective_recording_no: str | None = None
    original_lang: str
    original_text: str
    reviewed_count: int
    matched_count: int
    mismatched_count: int
    matched: list[ReviewItemOut]
    mismatched: list[ReviewItemOut]
    created_at: str


class DeliveryRejectedOut(ErrorOut):
    code: str = "delivery_not_booked"
    batch_no: str
    order_id: int
    entry_id: int
    entry_no: int
    revision: int
    recording_no: str
    status: Literal["rejected"]
    booked: bool = False
    effective_recording_no: None = None
    original_lang: str
    original_text: str
    reviewed_count: int
    matched_count: int
    mismatched_count: int
    matched: list[ReviewItemOut]
    mismatched: list[ReviewItemOut]


class BatchSummaryOut(BaseModel):
    batch_no: str
    note: str | None = None
    revision: int
    order_count: int
    recording_count: int
    created_at: str
    updated_at: str


class RecordingOut(BaseModel):
    lang: str
    text: str
    recording_no: str
    source_batch_no: str | None = None
    scope: Literal["batch", "library"] | None = None

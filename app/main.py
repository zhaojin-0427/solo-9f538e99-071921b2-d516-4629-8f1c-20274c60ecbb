"""FastAPI application: thin HTTP layer over :mod:`app.service`."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from . import __version__, orders, service
from .db import DEFAULT_DATABASE_PATH, ensure_schema
from .errors import ApiError
from .schemas import (
    BatchSummaryOut,
    DeliveryIn,
    DeliveryOut,
    DeliveryRejectedOut,
    ErrorOut,
    FirmwareCreate,
    FirmwareEnable,
    FirmwareOut,
    MissingErrorOut,
    MissingPrompt,
    OrderCreate,
    OrderOut,
    PlaybackOut,
    PlayRequest,
    RecordingOut,
    ReviewOut,
    ReviewRequest,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
)


def create_app(db_path: str | None = None) -> FastAPI:
    database = db_path or DEFAULT_DATABASE_PATH

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        ensure_schema(database)
        yield

    app = FastAPI(
        title="Museum Guide Prompt API",
        version=__version__,
        lifespan=lifespan,
        description=(
            "Prompts for museum guide devices. Firmwares declare, per prompt "
            "number, the language and the variable names the device will send. "
            "Playback picks a published template whose variables the firmware "
            "can fill, preferring the required language, then English over "
            "Chinese as fallback, then the most variables, then the latest "
            "published. Enabling a firmware and withdrawing a template are "
            "refused unless every prompt on every enabled firmware stays "
            "playable."
        ),
        openapi_tags=[
            {"name": "templates", "description": "Draft, publish, withdraw, edit."},
            {"name": "firmwares", "description": "Register, configure, enable."},
            {"name": "playback", "description": "Resolve a prompt for a device."},
            {
                "name": "recording",
                "description": (
                    "制单：按最新已提交目录把带编号样例取句、按实际语种+成句全文"
                    "合并并标出已有录音号；交件：按批号+原条目逐例复核，至少一例"
                    "仍是原语种原句才入账，同批号同语种同句保留首个录音号。"
                ),
            },
            {"name": "meta", "description": "Health and diagnostics."},
        ],
    )

    app.state.db_path = database

    @app.exception_handler(ApiError)
    async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_busy_handler(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return JSONResponse(
                status_code=503,
                content={
                    "code": "database_busy",
                    "message": "the catalogue is busy; retry shortly",
                },
                headers={"Retry-After": "1"},
            )
        return JSONResponse(
            status_code=500,
            content={"code": "database_error", "message": "database operation failed"},
        )

    @app.get("/api/health", tags=["meta"], summary="Liveness probe")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    # -- templates ---------------------------------------------------------
    @app.post(
        "/api/templates",
        response_model=TemplateOut,
        status_code=status.HTTP_201_CREATED,
        tags=["templates"],
        summary="Create a draft template",
        responses={422: {"model": ErrorOut, "description": "Bad placeholder syntax"}},
    )
    def create_template(payload: TemplateCreate) -> dict[str, Any]:
        return service.create_template(database, payload)

    @app.get(
        "/api/templates",
        response_model=list[TemplateOut],
        tags=["templates"],
        summary="List templates",
    )
    def list_templates(
        prompt_no: str | None = Query(default=None),
        lang: Literal["zh", "en"] | None = Query(default=None),
        template_status: Literal["draft", "published", "withdrawn"] | None = Query(
            default=None, alias="status"
        ),
    ) -> list[dict[str, Any]]:
        return service.list_templates(database, prompt_no, lang, template_status)

    @app.get(
        "/api/templates/{template_id}",
        response_model=TemplateOut,
        tags=["templates"],
        summary="Get one template",
    )
    def get_template(template_id: int) -> dict[str, Any]:
        return service.get_template(database, template_id)

    @app.patch(
        "/api/templates/{template_id}",
        response_model=TemplateOut,
        tags=["templates"],
        summary="Edit template content",
        responses={
            409: {"model": MissingErrorOut, "description": "Edit would strand a firmware"}
        },
    )
    def update_template(template_id: int, payload: TemplateUpdate) -> dict[str, Any]:
        return service.update_template(database, template_id, payload)

    @app.post(
        "/api/templates/{template_id}/publish",
        response_model=TemplateOut,
        tags=["templates"],
        summary="Publish a draft or withdrawn template",
    )
    def publish_template(template_id: int) -> dict[str, Any]:
        return service.publish_template(database, template_id)

    @app.post(
        "/api/templates/{template_id}/withdraw",
        response_model=TemplateOut,
        tags=["templates"],
        summary="Withdraw a published template (refused if it would strand a firmware)",
        responses={
            409: {
                "model": MissingErrorOut,
                "description": "Refused: enabled firmwares would lose a prompt",
            }
        },
    )
    def withdraw_template(template_id: int) -> dict[str, Any]:
        return service.withdraw_template(database, template_id)

    # -- firmwares ---------------------------------------------------------
    @app.post(
        "/api/firmwares",
        response_model=FirmwareOut,
        status_code=status.HTTP_201_CREATED,
        tags=["firmwares"],
        summary="Register a firmware (starts disabled)",
    )
    def create_firmware(payload: FirmwareCreate) -> dict[str, Any]:
        return service.create_firmware(database, payload)

    @app.get(
        "/api/firmwares",
        response_model=list[FirmwareOut],
        tags=["firmwares"],
        summary="List firmwares",
    )
    def list_firmwares(enabled: bool | None = Query(default=None)) -> list[dict[str, Any]]:
        return service.list_firmwares(database, enabled)

    @app.get(
        "/api/firmwares/{firmware_id}",
        response_model=FirmwareOut,
        tags=["firmwares"],
        summary="Get a firmware and its prompt configuration",
    )
    def get_firmware(firmware_id: str) -> dict[str, Any]:
        return service.get_firmware(database, firmware_id)

    @app.put(
        "/api/firmwares/{firmware_id}/enable",
        response_model=FirmwareOut,
        tags=["firmwares"],
        summary="Configure prompts and enable (all-or-nothing)",
        responses={
            409: {
                "model": MissingErrorOut,
                "description": "Refused: at least one prompt is unplayable",
            },
            400: {"model": ErrorOut, "description": "Empty prompt set"},
        },
    )
    def enable_firmware(firmware_id: str, payload: FirmwareEnable) -> dict[str, Any]:
        return service.enable_firmware(database, firmware_id, payload)

    @app.post(
        "/api/firmwares/{firmware_id}/disable",
        response_model=FirmwareOut,
        tags=["firmwares"],
        summary="Take a firmware out of service",
    )
    def disable_firmware(firmware_id: str) -> dict[str, Any]:
        return service.disable_firmware(database, firmware_id)

    @app.get(
        "/api/firmwares/{firmware_id}/missing",
        response_model=list[MissingPrompt],
        tags=["firmwares"],
        summary="Audit: configured prompts with no playable template",
    )
    def firmware_missing(firmware_id: str) -> list[dict[str, str]]:
        return service.firmware_missing(database, firmware_id)

    # -- playback ----------------------------------------------------------
    @app.post(
        "/api/firmwares/{firmware_id}/prompts/{prompt_no}/play",
        response_model=PlaybackOut,
        tags=["playback"],
        summary="Resolve and fill a prompt (Chinese fallback for English)",
        responses={
            409: {"model": ErrorOut, "description": "Firmware disabled / prompt unavailable"},
            404: {"model": ErrorOut, "description": "Unknown firmware or unconfigured prompt"},
            422: {"model": ErrorOut, "description": "Missing or invalid variables"},
        },
    )
    def play(
        firmware_id: str,
        prompt_no: str,
        payload: PlayRequest | None = None,
        response: Response = Response(),
    ) -> dict[str, Any]:
        values = payload.variables if payload is not None else {}
        result = service.play(database, firmware_id, prompt_no, values)
        if result["fallback"]:
            response.headers["X-Prompt-Fallback"] = "en->zh"
        return result

    # -- recording: 制单 / 交件 -------------------------------------------
    @app.post(
        "/api/recording-batches",
        response_model=OrderOut,
        status_code=status.HTTP_201_CREATED,
        tags=["recording"],
        summary="制单：按录音批号和带编号样例出单（同批号再调即再次出单）",
        description=(
            "整单在同一个已提交目录快照上，按现有取句规则（已发布模板、固件声明"
            "变量、语言优先、en→zh 兜底、变量最多、发布最新）逐例取句；按**实际"
            "语种 + 成句全文**合并成条目，并标出本批号或全录音库已有的录音号。"
            "无法取句的样例放在 `unresolved` 中并带 `reason_code`。\n\n"
            "同一 `batch_no` 再次提交会生成新的 `revision`（再次出单），已入账"
            "录音号保留并自动标到新条目上。"
        ),
    )
    def create_recording_order(payload: OrderCreate) -> dict[str, Any]:
        return orders.create_order(database, payload)

    @app.post("/api/orders", response_model=OrderOut,
              status_code=status.HTTP_201_CREATED, include_in_schema=False)
    def create_recording_order_alias(payload: OrderCreate) -> dict[str, Any]:
        return orders.create_order(database, payload)

    @app.post("/api/recording-orders", response_model=OrderOut,
              status_code=status.HTTP_201_CREATED, include_in_schema=False)
    def create_recording_order_alias2(payload: OrderCreate) -> dict[str, Any]:
        return orders.create_order(database, payload)

    @app.post("/api/batches", response_model=OrderOut,
              status_code=status.HTTP_201_CREATED, include_in_schema=False)
    def create_recording_order_alias3(payload: OrderCreate) -> dict[str, Any]:
        return orders.create_order(database, payload)

    @app.get(
        "/api/recording-batches",
        response_model=list[BatchSummaryOut],
        tags=["recording"],
        summary="列出录音批号（制单）",
    )
    def list_recording_batches() -> list[dict[str, Any]]:
        return orders.list_batches(database)

    @app.get(
        "/api/recordings",
        response_model=list[RecordingOut],
        tags=["recording"],
        summary="录音库：按语种/成句查询已有录音号（跨批号复用）",
    )
    def list_recordings(
        lang: Literal["zh", "en"] | None = Query(default=None),
        text: str | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        return orders.list_library_recordings(database, lang, text)

    @app.get(
        "/api/recording-batches/{batch_no}",
        response_model=OrderOut,
        tags=["recording"],
        summary="取某批号最新一次出单",
    )
    def get_recording_batch(batch_no: str) -> dict[str, Any]:
        return orders.get_latest_order(database, batch_no)

    @app.get(
        "/api/recording-batches/{batch_no}/orders",
        tags=["recording"],
        summary="该批号的历次出单（revision 列表）",
    )
    def list_recording_orders(batch_no: str) -> list[dict[str, Any]]:
        return orders.list_order_revisions(database, batch_no)

    @app.get(
        "/api/recording-batches/{batch_no}/orders/{revision}",
        response_model=OrderOut,
        tags=["recording"],
        summary="取该批号指定 revision 的整张单",
    )
    def get_recording_order(batch_no: str, revision: int) -> dict[str, Any]:
        return orders.get_order_revision(database, batch_no, revision)

    @app.get(
        "/api/recording-batches/{batch_no}/recordings",
        response_model=list[RecordingOut],
        tags=["recording"],
        summary="该批号已入账的语种+成句→首个录音号",
    )
    def list_batch_recordings(batch_no: str) -> list[dict[str, Any]]:
        return orders.list_batch_recordings(database, batch_no)

    @app.get(
        "/api/recording-batches/{batch_no}/deliveries",
        response_model=list[DeliveryOut],
        tags=["recording"],
        summary="该批号已受理的交件记录（含逐例复核结果）",
    )
    def list_batch_deliveries(batch_no: str) -> list[dict[str, Any]]:
        return orders.list_deliveries(database, batch_no)

    @app.post(
        "/api/recording-batches/{batch_no}/review",
        response_model=ReviewOut,
        tags=["recording"],
        summary="逐例复核（不入账）：按最新已提交目录重算该条目全部样例",
        description=(
            "交件前的只读复核。用制单时保存的样例输入（固件号/提示号/变量值）"
            "在**最新已提交**的模板与固件配置上重新取句，逐例判断是否仍是该条目"
            "的原语种、原句。"
        ),
    )
    def review_recording_entry(
        batch_no: str, payload: ReviewRequest
    ) -> dict[str, Any]:
        return orders.review_entry(database, batch_no, payload)

    @app.post(
        "/api/deliveries",
        response_model=DeliveryOut,
        status_code=status.HTTP_201_CREATED,
        tags=["recording"],
        summary="交件：带批号、原条目（entry_id 或 entry_no）、录音号",
        description=(
            "只复核该条目在制单时合并的样例，按最新已提交目录重新取句。"
            "**至少还有一例**仍取到原语种和原句才登记入账，并在 `mismatched` "
            "中列出已不匹配样例；一例都不匹配时返回 `409 delivery_not_booked`"
            "且不写入。同批号下同一语种+同一句已登记过录音号时，保留首个录音号，"
            "本次交件返回 `status=reused` 与 `effective_recording_no`（首个号）。"
        ),
        responses={
            404: {"model": ErrorOut, "description": "Unknown batch or entry"},
            409: {
                "model": DeliveryRejectedOut,
                "description": "No sample still matches the original lang/sentence",
            },
        },
    )
    def submit_delivery(payload: DeliveryIn) -> dict[str, Any]:
        return orders.submit_delivery(database, payload)

    @app.post("/api/deliver", response_model=DeliveryOut,
              status_code=status.HTTP_201_CREATED, include_in_schema=False)
    def submit_delivery_singular_alias(payload: DeliveryIn) -> dict[str, Any]:
        return orders.submit_delivery(database, payload)

    @app.post(
        "/api/recording-batches/{batch_no}/deliveries",
        response_model=DeliveryOut,
        status_code=status.HTTP_201_CREATED,
        tags=["recording"],
        summary="交件（批号在路径中）",
        responses={
            404: {"model": ErrorOut, "description": "Unknown batch or entry"},
            409: {
                "model": DeliveryRejectedOut,
                "description": "No sample still matches the original lang/sentence",
            },
        },
    )
    def submit_batch_delivery(
        batch_no: str, payload: DeliveryIn
    ) -> dict[str, Any]:
        return orders.submit_delivery(database, payload, path_batch_no=batch_no)

    return app


app = create_app()

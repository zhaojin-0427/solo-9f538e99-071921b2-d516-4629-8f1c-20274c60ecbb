"""FastAPI application: thin HTTP layer over :mod:`app.service`."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from . import __version__, service
from .db import DEFAULT_DATABASE_PATH, ensure_schema
from .errors import ApiError
from .schemas import (
    ErrorOut,
    FirmwareCreate,
    FirmwareEnable,
    FirmwareOut,
    MissingErrorOut,
    MissingPrompt,
    PlaybackOut,
    PlayRequest,
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

    return app


app = create_app()

"""Small domain error type translated to a stable JSON error envelope."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}

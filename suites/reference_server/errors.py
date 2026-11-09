"""The error envelope (web_api_spec.md §5) and the codes the evidence calls add (data_format.md §7).

Every refusal is an ``ApiError``; the HTTP layer turns it into a status and the envelope
``{"error": {code, kind, request_id, message, path?, retry_after_s, details}}``.
"""

from __future__ import annotations

from . import faults


class ApiError(Exception):
    """A refusal: an HTTP status, a ``code``, its ``kind`` and one sentence for the user."""

    def __init__(
        self,
        status: int,
        code: str,
        kind: str,
        message: str,
        *,
        details: dict | None = None,
        path: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.kind = kind
        self.message = message
        self.details = details or {}
        self.path = path
        self.headers = headers or {}

    def envelope(self, request_id: str) -> dict:
        error = {
            "code": self.code,
            "kind": self.kind,
            "request_id": request_id,
            "message": self.message,
            "retry_after_s": None,
            "details": dict(self.details),
        }
        if self.path is not None:
            error["path"] = self.path
        return {"error": error}


def bad_request(message: str, **details: object) -> ApiError:
    return ApiError(400, "bad_request", "invalid", message, details=details)


def unsupported_filter(name: str) -> ApiError:
    return ApiError(
        400,
        "unsupported_filter",
        "invalid",
        f"This server does not filter by {name!r}.",
        details={"filter": name},
    )


def not_found(message: str) -> ApiError:
    return ApiError(404, "not_found", "not_found", message)


def forbidden(message: str) -> ApiError:
    return ApiError(403, "forbidden", "denied", message)


def not_a_manager() -> ApiError:
    if faults.on("3.5.not-a-manager"):
        return forbidden("This login is not a manager.")
    return ApiError(403, "not_a_manager", "denied", "This login is not a manager.")


def conflict(code: str, message: str, *, kind: str = "invalid", **details: object) -> ApiError:
    return ApiError(409, code, kind, message, details=details)

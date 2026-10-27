from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException


class ProblemDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    trace_id: str
    details: dict[str, Any] | None = None


def problem_details(
    *,
    code: str,
    message: str,
    trace_id: UUID | str,
    details: dict[str, Any] | None = None,
) -> ProblemDetails:
    return ProblemDetails(
        code=code,
        message=message,
        trace_id=str(trace_id),
        details=details,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exception: StarletteHTTPException
    ) -> JSONResponse:
        codes = {
            400: ("INVALID_REQUEST", "Invalid request"),
            401: ("AUTHENTICATION_REQUIRED", "Authentication required"),
            403: ("FORBIDDEN", "Access denied"),
            404: ("NOT_FOUND", "Resource not found"),
            409: ("STATE_CONFLICT", "Resource state conflict"),
            422: ("VALIDATION_FAILED", "Request validation failed"),
            429: ("QUOTA_EXHAUSTED", "Request limit exceeded"),
        }
        code, message = codes.get(
            exception.status_code,
            ("REQUEST_FAILED", "Request could not be completed"),
        )
        body = problem_details(
            code=code,
            message=message,
            trace_id=uuid4(),
        )
        return JSONResponse(
            status_code=exception.status_code,
            content=body.model_dump(mode="json", exclude_none=True),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_exception(
        request: Request, exception: RequestValidationError
    ) -> JSONResponse:
        body = problem_details(
            code="VALIDATION_FAILED",
            message="Request validation failed",
            trace_id=uuid4(),
            details={"fields": [".".join(map(str, error["loc"])) for error in exception.errors()]},
        )
        return JSONResponse(
            status_code=422,
            content=body.model_dump(mode="json", exclude_none=True),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(
        request: Request, exception: Exception
    ) -> JSONResponse:
        body = problem_details(
            code="INTERNAL_ERROR",
            message="An internal error occurred",
            trace_id=uuid4(),
        )
        return JSONResponse(
            status_code=500,
            content=body.model_dump(mode="json", exclude_none=True),
        )

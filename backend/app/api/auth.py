import hmac

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = structlog.get_logger()

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
API_KEY_HEADER = "x-api-key"


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, api_key: str | None) -> None:
        super().__init__(app)
        self._api_key = api_key or None

    @property
    def enabled(self) -> bool:
        return self._api_key is not None

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in _MUTATING_METHODS and self._api_key is not None:
            provided = request.headers.get(API_KEY_HEADER, "")
            if not provided or not hmac.compare_digest(provided, self._api_key):
                logger.warning(
                    "api_key_auth_rejected",
                    method=request.method,
                    path=request.url.path,
                )
                return JSONResponse(
                    {"detail": "invalid or missing api key"},
                    status_code=401,
                    headers={"WWW-Authenticate": API_KEY_HEADER},
                )
        return await call_next(request)

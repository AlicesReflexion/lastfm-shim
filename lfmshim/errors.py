"""Last.fm API error codes and the exception used to surface them.

Codes and messages follow https://www.last.fm/api/errorcodes. HTTP status
codes are an approximation of what ws.audioscrobbler.com sends; see
docs/COMPATIBILITY.md and compat-observations.md for observed behaviour.
"""

from __future__ import annotations

INVALID_SERVICE = 2
INVALID_METHOD = 3
AUTH_FAILED = 4
INVALID_FORMAT = 5
INVALID_PARAMS = 6
INVALID_RESOURCE = 7
OPERATION_FAILED = 8
INVALID_SESSION = 9
INVALID_API_KEY = 10
SERVICE_OFFLINE = 11
INVALID_SIGNATURE = 13
TOKEN_UNAUTHORIZED = 14
TOKEN_EXPIRED = 15
TEMPORARY_ERROR = 16
LOGIN_REQUIRED = 17
SUSPENDED_KEY = 26
RATE_LIMITED = 29

DEFAULT_MESSAGES = {
    INVALID_SERVICE: "Invalid service - This service does not exist",
    INVALID_METHOD: "Invalid Method - No method with that name in this package",
    AUTH_FAILED: "Authentication Failed - You do not have permissions to access the service",
    INVALID_FORMAT: "Invalid format - This service doesn't exist in that format",
    INVALID_PARAMS: "Invalid parameters - Your request is missing a required parameter",
    INVALID_RESOURCE: "Invalid resource specified",
    OPERATION_FAILED: "Operation failed - Something else went wrong",
    INVALID_SESSION: "Invalid session key - Please re-authenticate",
    INVALID_API_KEY: "Invalid API key - You must be granted a valid key by last.fm",
    SERVICE_OFFLINE: "Service Offline - This service is temporarily offline. Try again later.",
    INVALID_SIGNATURE: "Invalid method signature supplied",
    TOKEN_UNAUTHORIZED: "Unauthorized Token - This token has not been authorized",
    TOKEN_EXPIRED: "This token has expired",
    TEMPORARY_ERROR: "There was a temporary error processing your request. Please try again",
    LOGIN_REQUIRED: "Login: User requires to be logged in",
    SUSPENDED_KEY: "Suspended API key - Access for your account has been suspended, please contact Last.fm",
    RATE_LIMITED: "Rate limit exceeded - Your IP has made too many requests in a short period",
}

# HTTP status sent alongside each error body. Observed (compat-observations.md,
# 2026-11-19): error 6 from a method (unknown/missing artist) is HTTP 200;
# unknown method (error 3) is HTTP 400; invalid api_key (error 10) is HTTP 403.
# A missing api_key is error 6 with HTTP 400 (see missing_api_key). The
# remaining values are unobserved approximations.
HTTP_STATUS = {
    INVALID_SERVICE: 400,
    INVALID_METHOD: 400,
    AUTH_FAILED: 403,
    INVALID_FORMAT: 400,
    INVALID_PARAMS: 200,
    INVALID_RESOURCE: 400,
    OPERATION_FAILED: 500,
    INVALID_SESSION: 403,
    INVALID_API_KEY: 403,
    SERVICE_OFFLINE: 503,
    INVALID_SIGNATURE: 403,
    TOKEN_UNAUTHORIZED: 403,
    TOKEN_EXPIRED: 403,
    TEMPORARY_ERROR: 503,
    LOGIN_REQUIRED: 403,
    SUSPENDED_KEY: 403,
    RATE_LIMITED: 429,
}


class LastFmError(Exception):
    """An error reported to the client in Last.fm's error envelope."""

    def __init__(self, code: int, message: str | None = None, http_status: int | None = None,
                 links: bool | None = None):
        self.code = int(code)
        self.message = message or DEFAULT_MESSAGES.get(self.code, "Unknown error")
        self.http_status = http_status or HTTP_STATUS.get(self.code, 400)
        # JSON "links": [] was observed only on HTTP 200 error-6 responses.
        self.links = (self.code == INVALID_PARAMS and self.http_status == 200) if links is None else links
        super().__init__(f"[{self.code}] {self.message}")


def missing_api_key() -> LastFmError:
    """Observed: no api_key gives error 6 with HTTP 400 and no links, even for an
    unknown method name (the key is checked first)."""
    return LastFmError(INVALID_PARAMS, DEFAULT_MESSAGES[INVALID_PARAMS], http_status=400)


def not_found(kind: str) -> LastFmError:
    """Last.fm reports unknown entities as error 6 with an entity-specific text."""
    messages = {
        "artist": "The artist you supplied could not be found",
        "album": "Album not found",
        "track": "Track not found",
        "user": "User not found",
        "tag": "Tag not found",
    }
    return LastFmError(INVALID_PARAMS, messages.get(kind, f"{kind} not found"))


def missing(param: str) -> LastFmError:
    return LastFmError(INVALID_PARAMS, f"Invalid parameters - Your request is missing a required parameter ({param})")


class UpstreamError(Exception):
    """An upstream metadata service failed (network, HTTP 5xx, bad payload)."""

    def __init__(self, service: str, detail: str, status: int | None = None):
        self.service = service
        self.status = status
        super().__init__(f"{service}: {detail}")

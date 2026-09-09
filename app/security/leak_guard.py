"""Runtime guarantee that a rate ceiling never leaves the building.

The type system already helps: `CarrierLoad` has no max_rate field, so the
common path cannot leak. This is the belt to that pair of braces — a check on
the actual bytes being returned, which also covers values that reach a response
through a route added later, a free-text field, or a debugging change nobody
reviewed carefully.

When a request loads a ceiling from the TMS it registers the number here. Just
before the response goes out, the body is walked and every numeric leaf and
digit sequence is compared against the registered values. A hit is a defect, so
the request fails closed with a 500 and a CRITICAL log rather than quietly
telling a carrier what the broker is willing to pay.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_protected: ContextVar[frozenset[int] | None] = ContextVar("protected_values", default=None)
_disclosed: ContextVar[frozenset[int] | None] = ContextVar("disclosed_values", default=None)

# Small numbers collide with counts, rounds and page sizes. Ceilings are dollar
# amounts in the hundreds and up, so below this we would produce noise, not signal.
MIN_PROTECTED_VALUE = 100


def reset_protected_values() -> None:
    _protected.set(frozenset())
    _disclosed.set(frozenset())


def protect_value(value: int | None) -> None:
    """Register a number that must not appear in this request's response."""
    if value is None or value < MIN_PROTECTED_VALUE:
        return
    current = _protected.get() or frozenset()
    _protected.set(current | {int(value)})


def allow_value(value: int | None) -> None:
    """Exempt a number the carrier themselves put on the call.

    A carrier who offers exactly the ceiling and has it accepted has not been
    told anything: they named the figure. Echoing their own number back is not
    disclosure, and blocking it would break legitimate deals at the top of the
    range.
    """
    if value is None:
        return
    current = _disclosed.get() or frozenset()
    _disclosed.set(current | {int(value)})


def protected_values() -> frozenset[int]:
    return (_protected.get() or frozenset()) - (_disclosed.get() or frozenset())


def _numeric_leaves(node: Any, path: str = "$") -> Iterable[tuple[str, int]]:
    if isinstance(node, bool):
        return
    if isinstance(node, int):
        yield path, node
    elif isinstance(node, float):
        if node.is_integer():
            yield path, int(node)
    elif isinstance(node, str):
        for match in re.finditer(r"\d[\d,]*", node):
            digits = match.group(0).replace(",", "")
            if digits:
                yield f"{path}(text)", int(digits)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _numeric_leaves(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _numeric_leaves(value, f"{path}[{index}]")


def find_leaks(payload: Any, secrets_: frozenset[int]) -> list[tuple[str, int]]:
    if not secrets_:
        return []
    return [(path, value) for path, value in _numeric_leaves(payload) if value in secrets_]


BLOCKED_BODY = json.dumps(
    {
        "error": "response_blocked",
        "detail": (
            "The response was withheld because it contained a protected internal "
            "value. This is a defect; the call should continue without quoting a rate."
        ),
    }
).encode()


class MaxRateLeakGuard:
    """Fails a response closed if it carries a protected rate ceiling.

    Written as raw ASGI rather than on top of BaseHTTPMiddleware on purpose:
    BaseHTTPMiddleware runs the downstream app in its own task, so context
    variables set inside a route handler are not visible to it. This guard
    depends on exactly that propagation, so it stays in the same task.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        reset_protected_values()
        start: dict = {}
        chunks: list[bytes] = []

        async def guarded_send(message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                return

            if message["type"] != "http.response.body":
                await send(message)
                return

            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return

            start_message, out_body = self._resolve(scope, start, b"".join(chunks))
            await send(start_message)
            await send({"type": "http.response.body", "body": out_body, "more_body": False})

        await self.app(scope, receive, guarded_send)

    def _resolve(self, scope, start: dict, body: bytes) -> tuple[dict, bytes]:
        status = start.get("status", 200)
        headers = [(k.lower(), v) for k, v in start.get("headers", [])]
        content_type = next(
            (v.decode() for k, v in headers if k == b"content-type"), ""
        )

        secrets_ = protected_values()
        if not secrets_ or status >= 500 or not content_type.startswith("application/json"):
            return start, body

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return start, body

        leaks = find_leaks(payload, secrets_)
        if not leaks:
            return start, body

        logger.critical(
            "RATE_CEILING_LEAK_BLOCKED path=%s leaks=%s",
            scope.get("path"), [p for p, _ in leaks],
        )
        blocked = {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(BLOCKED_BODY)).encode()),
            ],
        }
        return blocked, BLOCKED_BODY

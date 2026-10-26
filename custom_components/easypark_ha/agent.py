"""Client for the companion Agent's token-protected LAN API.

The Agent is a generic Android AccessibilityService: it reads the active window
and performs taps, swipes, Back, text input and app launches on request. All
EasyPark knowledge lives in this integration, not on the phone.
"""
from __future__ import annotations

import base64
from typing import Any

import aiohttp


class AgentError(Exception):
    """The Agent refused or could not perform a request."""


class AgentPausedError(AgentError):
    """Automation is paused on the phone."""


class AgentClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str,
        timeout_s: float = 10.0,
    ) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._headers = {"X-Token": token}
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> tuple[int, str]:
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                params=params,
                headers=self._headers,
                timeout=self._timeout,
            ) as response:
                return response.status, (await response.text()).strip()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AgentError(f"agent unreachable: {err}") from err

    async def _action(self, path: str, params: dict[str, Any] | None = None) -> None:
        status, body = await self._request("POST", path, params)
        if status == 202:
            return
        if body == "automation_paused":
            raise AgentPausedError("automation is paused on the phone")
        raise AgentError(f"{path} rejected: {status} {body}")

    async def health(self) -> dict[str, Any]:
        import json

        status, body = await self._request("GET", "/health")
        if status != 200:
            raise AgentError(f"health failed: {status} {body}")
        return json.loads(body)

    async def version(self, package: str) -> str:
        status, body = await self._request("GET", "/version", {"package": package})
        return body if status == 200 else ""

    async def is_foreground(self, package: str) -> bool:
        status, body = await self._request("GET", "/foreground", {"package": package})
        if body == "automation_paused":
            raise AgentPausedError("automation is paused on the phone")
        return status == 200 and body == "true"

    async def snapshot(self) -> str:
        """XML of whatever window is active (any package)."""
        status, body = await self._request("GET", "/snapshot-active")
        if status != 200:
            if body == "automation_paused":
                raise AgentPausedError("automation is paused on the phone")
            raise AgentError(f"snapshot failed: {status} {body}")
        return base64.b64decode(body).decode("utf-8")

    async def launch(self, package: str) -> None:
        await self._action("/launch", {"package": package})

    async def tap(self, x: int, y: int) -> None:
        await self._action("/tap", {"x": x, "y": y})

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        await self._action(
            "/swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration_ms}
        )

    async def back(self) -> None:
        await self._action("/back")

    async def set_text(self, text: str) -> None:
        await self._action("/set-text", {"text": text})

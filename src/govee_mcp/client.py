"""Async client for the Govee Platform API (v2).

Docs: https://developer.govee.com/reference
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

BASE_URL = "https://openapi.api.govee.com/router/api/v1"

# Capability (type, instance) pairs used by this server.
POWER = ("devices.capabilities.on_off", "powerSwitch")
BRIGHTNESS = ("devices.capabilities.range", "brightness")
COLOR_RGB = ("devices.capabilities.color_setting", "colorRgb")
COLOR_TEMP = ("devices.capabilities.color_setting", "colorTemperatureK")
LIGHT_SCENE = ("devices.capabilities.dynamic_scene", "lightScene")
DIY_SCENE = ("devices.capabilities.dynamic_scene", "diyScene")


class GoveeError(ToolError):
    """Raised for expected failures; the message is shown to the AI as a tool error."""


@dataclass
class Device:
    sku: str
    device: str  # MAC-style device id
    name: str
    type: str
    capabilities: list[dict[str, Any]] = field(default_factory=list)

    def capability(self, cap: tuple[str, str]) -> dict[str, Any] | None:
        for c in self.capabilities:
            if (c.get("type"), c.get("instance")) == cap:
                return c
        return None

    def supports(self, cap: tuple[str, str]) -> bool:
        return self.capability(cap) is not None

    def range_bounds(self, cap: tuple[str, str]) -> tuple[int, int] | None:
        c = self.capability(cap)
        r = (c or {}).get("parameters", {}).get("range")
        if not r:
            return None
        return int(r["min"]), int(r["max"])


class GoveeClient:
    def __init__(self, api_key: str, device_cache_ttl: float = 300.0):
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Govee-API-Key": api_key, "Content-Type": "application/json"},
            timeout=15.0,
        )
        self._cache_ttl = device_cache_ttl
        self._devices: list[Device] = []
        self._devices_at = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None
        if payload is not None:
            body = {"requestId": str(uuid.uuid4()), "payload": payload}
        resp = await self._http.request(method, path, json=body)
        if resp.status_code == 401:
            raise GoveeError("Govee rejected the API key (401). Check GOVEE_API_KEY.")
        if resp.status_code == 429:
            raise GoveeError("Govee rate limit hit (429). Wait a bit and try again.")
        try:
            data = resp.json()
        except ValueError:
            raise GoveeError(f"Govee returned HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200 or data.get("code") not in (200, None):
            raise GoveeError(f"Govee API error {data.get('code', resp.status_code)}: {data.get('msg') or data.get('message')}")
        return data

    async def list_devices(self, refresh: bool = False) -> list[Device]:
        if refresh or not self._devices or time.monotonic() - self._devices_at > self._cache_ttl:
            data = await self._request("GET", "/user/devices")
            self._devices = [
                Device(
                    sku=d["sku"],
                    device=d["device"],
                    name=d.get("deviceName") or d["device"],
                    type=d.get("type", ""),
                    capabilities=d.get("capabilities", []),
                )
                for d in data.get("data", [])
            ]
            self._devices_at = time.monotonic()
        return self._devices

    async def get_state(self, dev: Device) -> dict[str, Any]:
        data = await self._request("POST", "/device/state", {"sku": dev.sku, "device": dev.device})
        state: dict[str, Any] = {}
        for c in data.get("payload", {}).get("capabilities", []):
            state[c.get("instance")] = (c.get("state") or {}).get("value")
        return state

    async def control(self, dev: Device, cap: tuple[str, str], value: Any) -> None:
        await self._request(
            "POST",
            "/device/control",
            {
                "sku": dev.sku,
                "device": dev.device,
                "capability": {"type": cap[0], "instance": cap[1], "value": value},
            },
        )

    async def _scene_options(self, path: str, dev: Device) -> list[dict[str, Any]]:
        data = await self._request("POST", path, {"sku": dev.sku, "device": dev.device})
        options: list[dict[str, Any]] = []
        for c in data.get("payload", {}).get("capabilities", []):
            for opt in c.get("parameters", {}).get("options", []):
                options.append({"name": opt.get("name"), "value": opt.get("value"), "instance": c.get("instance")})
        return options

    async def list_scenes(self, dev: Device) -> list[dict[str, Any]]:
        return await self._scene_options("/device/scenes", dev)

    async def list_diy_scenes(self, dev: Device) -> list[dict[str, Any]]:
        return await self._scene_options("/device/diy-scenes", dev)

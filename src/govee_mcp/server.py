"""MCP server exposing Govee lights as tools."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from .auth import SCOPE, OwnerOAuthProvider
from .client import (
    BRIGHTNESS,
    COLOR_RGB,
    COLOR_TEMP,
    DIY_SCENE,
    LIGHT_SCENE,
    POWER,
    Device,
    GoveeClient,
    GoveeError,
)

# Setting GOVEE_MCP_PUBLIC_URL (e.g. https://govee.example.com) switches to HTTP with OAuth;
# otherwise the server speaks stdio for local clients.
PUBLIC_URL = os.environ.get("GOVEE_MCP_PUBLIC_URL", "").strip().rstrip("/")
oauth: OwnerOAuthProvider | None = None
if PUBLIC_URL:
    password = os.environ.get("GOVEE_MCP_PASSWORD", "")
    if len(password) < 12:
        raise SystemExit("GOVEE_MCP_PASSWORD must be set to at least 12 characters when GOVEE_MCP_PUBLIC_URL is set.")
    oauth = OwnerOAuthProvider(
        PUBLIC_URL,
        password,
        Path(os.environ.get("GOVEE_MCP_STATE", "~/.local/state/govee-mcp/oauth.json")).expanduser(),
    )

mcp = MCPServer(
    "govee",
    instructions=(
        "Controls the user's Govee smart lights. Call list_devices first to learn device names. "
        "Every control tool takes `device`: a device name (case-insensitive, partial matches allowed), "
        "a device id, a comma-separated list of names, or 'all'."
    ),
    auth_server_provider=oauth,
    auth=AuthSettings(
        issuer_url=PUBLIC_URL,
        resource_server_url=f"{PUBLIC_URL}/mcp",
        validate_token_resource=True,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    if oauth
    else None,
)

if oauth:
    mcp.custom_route("/login", methods=["GET", "POST"])(oauth.login)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return PlainTextResponse("ok")

_client: GoveeClient | None = None


def client() -> GoveeClient:
    global _client
    if _client is None:
        key = os.environ.get("GOVEE_API_KEY", "").strip()
        if not key:
            raise GoveeError(
                "GOVEE_API_KEY is not set. Get a key in the Govee Home app: "
                "Profile > Settings > Apply for API Key."
            )
        _client = GoveeClient(key)
    return _client


COLORS: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "warm white": (255, 180, 107),
    "yellow": (255, 255, 0),
    "orange": (255, 120, 0),
    "amber": (255, 170, 0),
    "gold": (255, 200, 0),
    "purple": (128, 0, 255),
    "violet": (148, 0, 211),
    "magenta": (255, 0, 255),
    "pink": (255, 105, 180),
    "hot pink": (255, 20, 147),
    "cyan": (0, 255, 255),
    "teal": (0, 128, 128),
    "turquoise": (64, 224, 208),
    "lime": (50, 205, 50),
    "mint": (152, 255, 152),
    "lavender": (180, 140, 255),
    "indigo": (75, 0, 130),
    "sky blue": (135, 206, 235),
    "coral": (255, 127, 80),
    "salmon": (250, 128, 114),
    "crimson": (220, 20, 60),
}


def parse_color(color: str) -> tuple[int, int, int]:
    c = color.strip().lower()
    if c in COLORS:
        return COLORS[c]
    if m := re.fullmatch(r"#?([0-9a-f]{6})", c):
        n = int(m.group(1), 16)
        return (n >> 16) & 255, (n >> 8) & 255, n & 255
    if m := re.fullmatch(r"(?:rgb\()?\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)?", c):
        rgb = tuple(int(x) for x in m.groups())
        if all(0 <= x <= 255 for x in rgb):
            return rgb  # type: ignore[return-value]
    raise GoveeError(
        f"Unrecognized color '{color}'. Use a hex code like #FF8800, 'r,g,b', or one of: {', '.join(COLORS)}."
    )


async def resolve(device: str) -> list[Device]:
    """Turn a user-supplied device reference into one or more devices."""
    devices = await client().list_devices()
    if not devices:
        raise GoveeError("No devices found on this Govee account.")
    if device.strip().lower() in ("all", "everything", "*"):
        return devices

    found: list[Device] = []
    for ref in (r.strip().lower() for r in device.split(",") if r.strip()):
        exact = [d for d in devices if ref in (d.name.lower(), d.device.lower())]
        matches = exact or [d for d in devices if ref in d.name.lower()]
        if not matches:
            names = ", ".join(d.name for d in devices)
            raise GoveeError(f"No device matches '{ref}'. Known devices: {names}")
        found.extend(m for m in matches if m not in found)
    return found


async def apply(device: str, cap: tuple[str, str], value_for, label: str) -> str:
    """Send a command to every matching device that supports `cap`; report per-device results."""
    targets = await resolve(device)

    async def one(d: Device) -> str:
        if not d.supports(cap):
            return f"{d.name}: skipped (doesn't support {cap[1]})"
        try:
            await client().control(d, cap, value_for(d))
            return f"{d.name}: {label}"
        except GoveeError as e:
            return f"{d.name}: failed ({e})"

    results = await asyncio.gather(*(one(d) for d in targets))
    return "\n".join(results)


def clamp(value: int, d: Device, cap: tuple[str, str], default: tuple[int, int]) -> int:
    lo, hi = d.range_bounds(cap) or default
    return max(lo, min(hi, value))


@mcp.tool()
async def list_devices(refresh: bool = False) -> list[dict[str, Any]]:
    """List all Govee devices on the account with what each one supports.

    Args:
        refresh: Re-fetch from Govee instead of using the cached list (cached for 5 minutes).
    """
    devices = await client().list_devices(refresh=refresh)
    return [
        {
            "name": d.name,
            "id": d.device,
            "model": d.sku,
            "type": d.type.rsplit(".", 1)[-1],
            "supports": sorted({c.get("instance") for c in d.capabilities if c.get("instance")}),
        }
        for d in devices
    ]


@mcp.tool()
async def get_state(device: str) -> list[dict[str, Any]]:
    """Get current state (online, on/off, brightness, color, color temperature) of one or more devices."""
    targets = await resolve(device)

    async def one(d: Device) -> dict[str, Any]:
        try:
            s = await client().get_state(d)
        except GoveeError as e:
            return {"name": d.name, "error": str(e)}
        out: dict[str, Any] = {"name": d.name, "online": s.get("online")}
        if "powerSwitch" in s:
            out["power"] = "on" if s["powerSwitch"] == 1 else "off"
        if "brightness" in s:
            out["brightness"] = s["brightness"]
        rgb = s.get("colorRgb")
        if isinstance(rgb, int) and rgb > 0:
            out["color"] = f"#{rgb:06X}"
        if s.get("colorTemperatureK"):
            out["color_temperature_k"] = s["colorTemperatureK"]
        return out

    return list(await asyncio.gather(*(one(d) for d in targets)))


@mcp.tool()
async def set_power(device: str, on: bool) -> str:
    """Turn devices on or off.

    Args:
        device: Device name, id, comma-separated names, or 'all'.
        on: True to turn on, False to turn off.
    """
    return await apply(device, POWER, lambda d: 1 if on else 0, "on" if on else "off")


@mcp.tool()
async def set_brightness(device: str, brightness: int) -> str:
    """Set brightness as a percentage (1-100). Setting brightness also turns the light on.

    Args:
        device: Device name, id, comma-separated names, or 'all'.
        brightness: 1 (dimmest) to 100 (brightest).
    """
    return await apply(
        device,
        BRIGHTNESS,
        lambda d: clamp(brightness, d, BRIGHTNESS, (1, 100)),
        f"brightness {brightness}%",
    )


@mcp.tool()
async def set_color(device: str, color: str) -> str:
    """Set an RGB color.

    Args:
        device: Device name, id, comma-separated names, or 'all'.
        color: A color name (e.g. 'red', 'warm white', 'purple'), a hex code like '#FF8800', or 'r,g,b'.
    """
    r, g, b = parse_color(color)
    return await apply(device, COLOR_RGB, lambda d: (r << 16) | (g << 8) | b, f"color #{r:02X}{g:02X}{b:02X}")


@mcp.tool()
async def set_color_temperature(device: str, kelvin: int) -> str:
    """Set white color temperature in Kelvin. ~2700 is warm/cozy, ~4000 neutral, ~6500 cool daylight.

    Args:
        device: Device name, id, comma-separated names, or 'all'.
        kelvin: Color temperature; clamped to each device's supported range.
    """
    return await apply(
        device,
        COLOR_TEMP,
        lambda d: clamp(kelvin, d, COLOR_TEMP, (2000, 9000)),
        f"{kelvin}K",
    )


@mcp.tool()
async def list_scenes(device: str) -> dict[str, list[str]]:
    """List the built-in light scenes and the user's DIY scenes available on a device."""
    targets = await resolve(device)
    out: dict[str, list[str]] = {}
    for d in targets:
        names: list[str] = []
        if d.supports(LIGHT_SCENE):
            names += [s["name"] for s in await client().list_scenes(d)]
        if d.supports(DIY_SCENE):
            names += [f"{s['name']} (DIY)" for s in await client().list_diy_scenes(d)]
        out[d.name] = names
    return out


@mcp.tool()
async def set_scene(device: str, scene: str) -> str:
    """Activate a named scene (e.g. 'Sunset', 'Aurora', 'Movie'). Use list_scenes to see options.

    Args:
        device: Device name, id, comma-separated names, or 'all'.
        scene: Scene name (case-insensitive; partial matches allowed).
    """
    want = scene.strip().lower().removesuffix(" (diy)")
    targets = await resolve(device)

    async def one(d: Device) -> str:
        options: list[tuple[tuple[str, str], dict[str, Any]]] = []
        if d.supports(LIGHT_SCENE):
            options += [(LIGHT_SCENE, s) for s in await client().list_scenes(d)]
        if d.supports(DIY_SCENE):
            options += [(DIY_SCENE, s) for s in await client().list_diy_scenes(d)]
        if not options:
            return f"{d.name}: skipped (no scenes)"
        exact = [o for o in options if (o[1]["name"] or "").lower() == want]
        match = exact or [o for o in options if want in (o[1]["name"] or "").lower()]
        if not match:
            return f"{d.name}: no scene matching '{scene}'"
        cap, s = match[0]
        try:
            await client().control(d, cap, s["value"])
            return f"{d.name}: scene '{s['name']}'"
        except GoveeError as e:
            return f"{d.name}: failed ({e})"

    return "\n".join(await asyncio.gather(*(one(d) for d in targets)))


def main() -> None:
    if not PUBLIC_URL:
        mcp.run()
        return
    host = os.environ.get("GOVEE_MCP_HOST", "127.0.0.1")
    domain = PUBLIC_URL.split("://", 1)[-1]
    mcp.run(
        "streamable-http",
        host=host,
        port=int(os.environ.get("GOVEE_MCP_PORT", "8000")),
        transport_security=TransportSecuritySettings(
            allowed_hosts=[domain, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[PUBLIC_URL, "https://claude.ai", "https://claude.com"],
        ),
    )


if __name__ == "__main__":
    main()

"""Per-panel control of Nanoleaf devices (Skylight, Shapes, Lines, …).

Home Assistant sees a Nanoleaf (via HomeKit or Matter) as one light. Nanoleaf's
local Open API exposes each panel, and its "extControl" streaming mode accepts
a colour for every panel in a single UDP packet, many times a second. Light FX
uses that to treat each panel as its own slot in an effect.

Open API: http://<host>:16021/api/v1/<token>/...
Stream:   UDP <host>:60222, extControl v2 packets (built in nanoleaf_frames.py)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .nanoleaf_frames import build_packet

_LOGGER = logging.getLogger(__name__)

API_PORT = 16021
STREAM_PORT = 60222
# Effect names that are a mode rather than a saved scene, so can't be re-selected.
NON_SCENES = {"*Solid*", "*Dynamic*", "*ExtControl*", "*Static*"}


class _Datagram(asyncio.DatagramProtocol):
    pass


class NanoleafError(Exception):
    """A request to the device failed. Its message never includes the address
    of the request, because that address contains the device's token."""


class NanoleafPanels:
    """One Nanoleaf device whose panels Light FX drives individually."""

    def __init__(self, hass: HomeAssistant, entity_id: str, host: str, token: str) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.host = host
        self._base = f"http://{host}:{API_PORT}/api/v1/{token}"
        self.panel_ids: list[int] = []
        self._transport: asyncio.DatagramTransport | None = None

    async def _api(self, method: str, path: str = "", body: dict[str, Any] | None = None) -> Any:
        session = async_get_clientsession(self.hass)
        what = f"{method} {path or 'info'}"
        try:
            async with session.request(
                method, f"{self._base}/{path}", json=body, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status >= 400:
                    raise NanoleafError(f"{what}: the device answered {resp.status}")
                return await resp.json() if resp.content_type == "application/json" else None
        except TimeoutError:
            raise NanoleafError(f"{what}: no answer from {self.host}") from None
        except aiohttp.ClientError as err:
            # not str(err): aiohttp puts the address, token and all, in its messages
            raise NanoleafError(f"{what}: {type(err).__name__} talking to {self.host}") from None

    async def async_load_layout(self) -> None:
        """Read panel IDs, ordered left-to-right then top-to-bottom."""
        info = await self._api("GET")
        positions = info["panelLayout"]["layout"]["positionData"]
        positions = [p for p in positions if p.get("shapeType") != 12]  # 12 = controller unit
        self.panel_ids = [p["panelId"] for p in sorted(positions, key=lambda p: (p["x"], p["y"]))]
        _LOGGER.info("%s: %d Nanoleaf panels %s", self.entity_id, len(self.panel_ids), self.panel_ids)

    async def async_snapshot(self) -> dict[str, Any]:
        info = await self._api("GET")
        state = info["state"]
        return {
            "on": state["on"]["value"],
            "brightness": state["brightness"]["value"],
            "mode": state.get("colorMode"),
            "hue": state["hue"]["value"],
            "sat": state["sat"]["value"],
            "ct": state["ct"]["value"],
            "effect": info["effects"].get("select"),
        }

    async def async_start_stream(self) -> None:
        if not self.panel_ids:
            await self.async_load_layout()
        # Effects fold brightness into the colours, so run the device at full.
        await self._api("PUT", "state", {"on": {"value": True}, "brightness": {"value": 100}})
        await self._api(
            "PUT",
            "effects",
            {"write": {"command": "display", "animType": "extControl", "extControlVersion": "v2"}},
        )
        if self._transport is None or self._transport.is_closing():
            loop = asyncio.get_running_loop()
            self._transport, _ = await loop.create_datagram_endpoint(
                _Datagram, remote_addr=(self.host, STREAM_PORT)
            )

    def send(self, colors: list[tuple[int, int, int]], transition: float) -> None:
        """Push one colour per panel (same order as panel_ids) in one packet."""
        if self._transport is None or self._transport.is_closing():
            return
        self._transport.sendto(build_packet(self.panel_ids, colors, transition))

    async def async_turn_off(self) -> None:
        await self._api("PUT", "state", {"on": {"value": False}})

    async def async_restore(self, snap: dict[str, Any]) -> None:
        """Put the device back the way async_snapshot found it."""
        self.close_stream()
        if snap["effect"] and snap["effect"] not in NON_SCENES:
            await self._api("PUT", "effects", {"select": snap["effect"]})
        elif snap["mode"] == "ct":
            await self._api("PUT", "state", {"ct": {"value": snap["ct"]}})
        else:
            await self._api("PUT", "state", {"hue": {"value": snap["hue"]}, "sat": {"value": snap["sat"]}})
        await self._api(
            "PUT", "state", {"brightness": {"value": snap["brightness"]}, "on": {"value": snap["on"]}}
        )

    def close_stream(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

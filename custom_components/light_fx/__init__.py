"""Light FX: a software effects engine for any colour light.

Runs effects from `effects.py` by sending ordinary `light.turn_on` calls, so it
works on lights whose integration has no effects of its own (Matter, HomeKit…).

Actions:
  light_fx.start  — run an effect on one or more lights
  light_fx.stop   — stop effects (all, or on given lights), restoring how the
                    lights looked before by default

An effect on a light also stops by itself, without restoring, when anything
else sends that light a command (a dashboard tap, an automation, a voice
assistant), so normal control always wins.

Enable with `light_fx:` in configuration.yaml. Nanoleaf devices can be
listed so each panel becomes its own slot in effects (see nanoleaf.py):

    light_fx:
      nanoleaf:
        - entity_id: light.my_nanoleaf
          host: 192.0.2.10
          token: !secret nanoleaf_token
"""

from __future__ import annotations

import asyncio
from collections import deque
import colorsys
import inspect
import logging
import random
import time
from typing import Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, EVENT_CALL_SERVICE
from homeassistant.core import (
    Context,
    Event,
    HomeAssistant,
    ServiceCall,
    State,
    callback,
)
from homeassistant.helpers import config_validation as cv, service as service_helper, target as target_helper
from homeassistant.helpers.state import async_reproduce_state
from homeassistant.helpers.typing import ConfigType

from .effects import EFFECTS, Effect, Frame, Params
from .nanoleaf import NanoleafPanels
from .nanoleaf_frames import frame_to_rgb

_LOGGER = logging.getLogger(__name__)

DOMAIN = "light_fx"
MIN_INTERVAL = 0.4  # seconds; faster floods smart bulbs and gets dropped
COLOR_MODES = ("hs", "xy", "rgb", "rgbw", "rgbww")
FINAL_FRAME_WAIT = 10  # seconds to wait for a busy light before the last frame

NANOLEAF_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("host"): cv.string,
        vol.Required("token"): cv.string,
    }
)
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Any(
            None,
            vol.Schema({vol.Optional("nanoleaf", default=[]): [NANOLEAF_SCHEMA]}),
        )
    },
    extra=vol.ALLOW_EXTRA,
)

START_SCHEMA = vol.Schema(
    {
        vol.Required("effect"): vol.In(list(EFFECTS)),
        vol.Optional("speed", default=1.0): vol.All(vol.Coerce(float), vol.Range(0.1, 10)),
        vol.Optional("brightness", default=100): vol.All(vol.Coerce(int), vol.Range(1, 100)),
        vol.Optional("color"): vol.All(
            vol.ExactSequence((cv.byte, cv.byte, cv.byte)), vol.Coerce(tuple)
        ),
    },
    extra=vol.ALLOW_EXTRA,  # target keys (entity_id / area_id / …)
)
STOP_SCHEMA = vol.Schema(
    {vol.Optional("restore", default=True): cv.boolean}, extra=vol.ALLOW_EXTRA
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    manager = EffectManager(hass)
    hass.data[DOMAIN] = manager
    for dev in (config.get(DOMAIN) or {}).get("nanoleaf", []):
        panels = NanoleafPanels(hass, dev["entity_id"], dev["host"], dev["token"])
        try:
            await panels.async_load_layout()
        except Exception as err:  # device offline at startup: retried on first use
            _LOGGER.warning("Nanoleaf %s not reachable yet: %s", dev["host"], err)
        manager.panels[dev["entity_id"]] = panels

    async def start(call: ServiceCall) -> None:
        entity_ids = await _target_lights(hass, call)
        if not entity_ids:
            _LOGGER.warning("light_fx.start: no lights targeted")
            return
        hue, sat = None, 100.0
        if color := call.data.get("color"):
            h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in color))
            hue, sat = h * 360, s * 100
        params = Params(
            speed=call.data["speed"],
            brightness=call.data["brightness"],
            hue=hue,
            saturation=sat,
        )
        await manager.async_start(EFFECTS[call.data["effect"]], entity_ids, params)

    async def stop(call: ServiceCall) -> None:
        targeted = any(k in call.data for k in ("entity_id", "area_id", "device_id", "floor_id", "label_id"))
        entity_ids = await _target_lights(hass, call) if targeted else None
        await manager.async_stop(entity_ids, restore=call.data["restore"])

    hass.services.async_register(DOMAIN, "start", start, schema=START_SCHEMA)
    hass.services.async_register(DOMAIN, "stop", stop, schema=STOP_SCHEMA)
    _LOGGER.info("Light FX loaded with %d effects: %s", len(EFFECTS), ", ".join(EFFECTS))
    return True


async def _target_lights(hass: HomeAssistant, call: ServiceCall) -> list[str]:
    ids = service_helper.async_extract_entity_ids(call)
    if inspect.isawaitable(ids):
        ids = await ids
    return sorted(e for e in ids if e.startswith("light."))


class EffectManager:
    """Owns all running effects and the lights' pre-effect states."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.runs: set[EffectRun] = set()
        self.owner: dict[str, EffectRun] = {}
        self.snapshots: dict[str, State] = {}
        # Lights driven per panel through Nanoleaf's API, and their snapshots.
        self.panels: dict[str, NanoleafPanels] = {}
        self.panel_snaps: dict[str, dict[str, Any]] = {}
        # At most one command in flight per light. A slow light skips ticks
        # instead of building a backlog that would replay after a stop.
        self.inflight: dict[str, asyncio.Task] = {}
        self._our_contexts: deque[str] = deque(maxlen=500)
        self._our_context_set: set[str] = set()
        # Starting and stopping wait on lights and devices; one at a time, so
        # two overlapping calls can't both think they own a light.
        self._lock = asyncio.Lock()
        self._warned_targets = False
        hass.bus.async_listen(EVENT_CALL_SERVICE, self._on_service_call)

    def new_context(self) -> Context:
        ctx = Context()
        if len(self._our_contexts) == self._our_contexts.maxlen:
            self._our_context_set.discard(self._our_contexts[0])
        self._our_contexts.append(ctx.id)
        self._our_context_set.add(ctx.id)
        return ctx

    async def async_start(self, effect: Effect, entity_ids: list[str], params: Params) -> None:
        async with self._lock:
            await self._start(effect, entity_ids, params)

    async def _start(self, effect: Effect, entity_ids: list[str], params: Params) -> None:
        entity_ids = list(entity_ids)
        for eid in list(entity_ids):
            if eid in self.owner:
                self._release(eid)  # keep its original snapshot
            elif eid in self.panels:
                try:
                    self.panel_snaps[eid] = await self.panels[eid].async_snapshot()
                except Exception as err:
                    _LOGGER.warning("Can't reach Nanoleaf for %s: %s", eid, err)
                    entity_ids.remove(eid)
                    continue
            elif state := self.hass.states.get(eid):
                self.snapshots[eid] = state
            if eid in self.panels:
                try:
                    await self.panels[eid].async_start_stream()
                except Exception as err:
                    _LOGGER.warning("Can't start Nanoleaf stream for %s: %s", eid, err)
                    entity_ids.remove(eid)
                    await self._abandon_panels(eid)
        if not entity_ids:
            return
        run = EffectRun(self, effect, entity_ids, params)
        self.runs.add(run)
        for eid in entity_ids:
            self.owner[eid] = run
        run.task = self.hass.async_create_background_task(
            run.async_loop(), f"light_fx {effect.name}"
        )
        _LOGGER.info("Started %s on %s", effect.name, ", ".join(entity_ids))

    async def _abandon_panels(self, eid: str) -> None:
        """A Nanoleaf's stream wouldn't start: put the device back and let go.

        Starting may have got as far as switching the device to streaming, so
        restore what the snapshot recorded instead of leaving it there.
        """
        dev = self.panels[eid]
        snap = self.panel_snaps.pop(eid, None)
        if snap is not None:
            try:
                await dev.async_restore(snap)
            except Exception as err:  # noqa: BLE001 - it was unreachable a moment ago too
                _LOGGER.warning("Couldn't restore %s: %s", eid, err)
        dev.close_stream()

    async def async_stop(self, entity_ids: list[str] | None, *, restore: bool) -> None:
        async with self._lock:
            await self._stop(entity_ids, restore=restore)

    async def _stop(self, entity_ids: list[str] | None, *, restore: bool) -> None:
        targets = list(self.owner) if entity_ids is None else [e for e in entity_ids if e in self.owner]
        for eid in targets:
            self._release(eid)
        # Let commands already sent finish, so none land after the restore.
        pending = [t for e in targets if (t := self.inflight.get(e)) and not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)
        if restore:
            states = [self.snapshots[e] for e in targets if e in self.snapshots]
            if states:
                await async_reproduce_state(self.hass, states, context=self.new_context())
            for eid in targets:
                if eid in self.panel_snaps:
                    try:
                        await self.panels[eid].async_restore(self.panel_snaps[eid])
                    except Exception as err:
                        _LOGGER.warning("Couldn't restore %s: %s", eid, err)
        for eid in targets:
            self.snapshots.pop(eid, None)
            self.panel_snaps.pop(eid, None)
            if eid in self.panels:
                self.panels[eid].close_stream()  # restoring closes it too; this covers the rest

    @callback
    def _release(self, eid: str) -> None:
        """Take a light out of its run (cancelling the run when it's empty)."""
        run = self.owner.pop(eid, None)
        if run is None:
            return
        run.entity_ids.remove(eid)
        if not run.entity_ids:
            self.runs.discard(run)
            if run.task:
                run.task.cancel()

    @callback
    def finished(self, run: EffectRun) -> None:
        """An effect with a duration ended on its own: leave lights as they are."""
        for eid in list(run.entity_ids):
            self.owner.pop(eid, None)
            self.snapshots.pop(eid, None)
            self.panel_snaps.pop(eid, None)
            if eid in self.panels:
                self.panels[eid].close_stream()
        run.entity_ids.clear()
        self.runs.discard(run)

    @callback
    def _on_service_call(self, event: Event) -> None:
        """Hand a light back when something else commands it."""
        if not self.owner or event.context.id in self._our_context_set:
            return
        data = event.data
        if data.get("domain") not in ("light", "homeassistant", "scene"):
            return
        # the lights the call is for, however it names them: by entity, or by
        # device, area, floor or label ("turn off the living room")
        service_data = data.get("service_data") or {}
        ids = service_data.get(ATTR_ENTITY_ID)
        if ids == "all":
            touched = set(self.owner)
        else:
            try:
                selected = target_helper.async_extract_referenced_entity_ids(
                    self.hass, target_helper.TargetSelection(service_data)
                )
                touched = selected.referenced | selected.indirectly_referenced
            except Exception as err:  # noqa: BLE001 - fall back to its entity ids
                if not self._warned_targets:
                    self._warned_targets = True
                    _LOGGER.warning(
                        "Couldn't work out which lights a %s.%s call is for (%s: %s). "
                        "Until this is fixed, only calls that name a light by entity "
                        "stop its effect; calls by area, device, floor or label don't. "
                        "(Logged once.)",
                        data.get("domain"), data.get("service"), type(err).__name__, err,
                    )
                touched = set([ids] if isinstance(ids, str) else ids or [])
        for eid in touched:
            if eid in self.owner:
                _LOGGER.info("%s was controlled elsewhere; stopping its effect", eid)
                self._release(eid)
                self.snapshots.pop(eid, None)
                self.panel_snaps.pop(eid, None)
                if eid in self.panels:
                    self.panels[eid].close_stream()


class EffectRun:
    """One effect running on a group of lights."""

    def __init__(self, manager: EffectManager, effect: Effect, entity_ids: list[str], params: Params) -> None:
        self.manager = manager
        self.effect = effect
        self.entity_ids = list(entity_ids)
        self.params = params
        self.task: asyncio.Task | None = None
        self._rngs: dict[str, random.Random] = {}

    async def async_loop(self) -> None:
        hass = self.manager.hass
        interval = max(MIN_INTERVAL, self.effect.interval / self.params.speed)
        transition = round(interval * 0.9, 2)
        start = time.monotonic()
        beats = 0
        while self.entity_ids:
            tick = time.monotonic()
            if self.effect.beat:
                t = beats * self.effect.interval
                beats += 1
            else:
                t = (tick - start) * self.params.speed
            if self.effect.duration is not None and t >= self.effect.duration:
                # Land exactly on the final frame, then hand the lights back.
                # A light still busy with the last update would normally skip
                # a tick; this one it must not miss (sunset ends in "off"), so
                # wait for it, and send regardless if it is still busy.
                inflight = self.manager.inflight
                busy = [t for e in self.entity_ids if (t := inflight.get(e)) and not t.done()]
                if busy:
                    await asyncio.wait(busy, timeout=FINAL_FRAME_WAIT)
                await self._send_all(self.effect.duration, transition, final=True)
                self.manager.finished(self)
                return
            await self._send_all(t, transition)
            await asyncio.sleep(max(0.05, interval - (time.monotonic() - tick)))

    def _frame(self, key: str, t: float, i: int, n: int) -> Frame | None:
        rng = self._rngs.setdefault(key, random.Random(hash((key, id(self)))))
        try:
            return self.effect.fn(t, i, n, self.params, rng)
        except Exception:  # a buggy effect must not kill the loop silently
            _LOGGER.exception("Effect %s failed", self.effect.name)
            return None

    async def _send_all(self, t: float, transition: float, *, final: bool = False) -> None:
        hass = self.manager.hass
        inflight = self.manager.inflight
        panels = self.manager.panels
        lights = list(self.entity_ids)
        # Each ordinary light is one slot; a Nanoleaf contributes one per panel.
        n = sum(len(panels[e].panel_ids) if e in panels else 1 for e in lights)
        i = 0
        for eid in lights:
            if eid in panels:
                dev = panels[eid]
                frames = [self._frame(f"{eid}#{k}", t, i + k, n) for k in range(len(dev.panel_ids))]
                i += len(dev.panel_ids)
                if any(f is None for f in frames):
                    continue
                if all(f.off for f in frames):
                    hass.async_create_background_task(self._panels_off(dev), "light_fx nanoleaf off")
                    continue
                dev.send([frame_to_rgb(f, self.params.brightness) for f in frames], transition)
                continue
            slot = i
            i += 1
            if not final and (busy := inflight.get(eid)) and not busy.done():
                continue  # light hasn't finished the last update; skip this tick
            state = hass.states.get(eid)
            if state is None or state.state == "unavailable":
                continue
            frame = self._frame(eid, t, slot, n)
            if frame is None:
                continue
            inflight[eid] = hass.async_create_background_task(
                self._send(eid, state, frame, transition), f"light_fx send {eid}"
            )

    @staticmethod
    async def _panels_off(dev: NanoleafPanels) -> None:
        try:
            await dev.async_turn_off()
        except Exception as err:  # noqa: BLE001 - try again next tick
            _LOGGER.debug("Couldn't turn %s off: %s", dev.entity_id, err)

    async def _send(self, eid: str, state: State, frame: Frame, transition: float) -> None:
        hass = self.manager.hass
        ctx = self.manager.new_context()
        if frame.off:
            await hass.services.async_call(
                "light", "turn_off", {ATTR_ENTITY_ID: eid, "transition": transition},
                blocking=True, context=ctx,
            )
            return
        data: dict[str, Any] = {ATTR_ENTITY_ID: eid, "transition": transition}
        modes = state.attributes.get("supported_color_modes") or []
        if frame.kelvin is not None and "color_temp" in modes:
            lo = state.attributes.get("min_color_temp_kelvin") or frame.kelvin
            hi = state.attributes.get("max_color_temp_kelvin") or frame.kelvin
            data["color_temp_kelvin"] = min(max(frame.kelvin, lo), hi)
        elif frame.kelvin is not None and any(m in modes for m in COLOR_MODES):
            # A colour-only light: Home Assistant turns the temperature into
            # the nearest colour the light can show.
            data["color_temp_kelvin"] = frame.kelvin
        elif frame.hs is not None and any(m in modes for m in COLOR_MODES):
            data["hs_color"] = (round(frame.hs[0] % 360, 1), round(min(max(frame.hs[1], 0), 100), 1))
        if frame.brightness is not None and modes != ["onoff"]:
            pct = frame.brightness * self.params.brightness / 100
            data["brightness_pct"] = round(min(max(pct, 1), 100))
        try:
            await hass.services.async_call("light", "turn_on", data, blocking=True, context=ctx)
        except Exception as err:  # unreachable bulb etc.; try again next tick
            _LOGGER.debug("Update to %s failed: %s", eid, err)

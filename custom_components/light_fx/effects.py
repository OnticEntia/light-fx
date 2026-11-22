"""The effects library.

Each effect is a pure function that answers one question: "at effect-time
`t`, what should light `i` of `n` look like?" The runner calls it once per
light on every tick and sends the result to the light, so an effect never
deals with Home Assistant, timing or devices.

To add an effect, write a function with this signature and decorate it:

    @effect("my_effect", "One-line description", interval=1.0)
    def my_effect(t: float, i: int, n: int, p: Params, rng: random.Random) -> Frame:
        return Frame(hs=(hue, saturation), brightness=percent)

- `t` is seconds since the effect started, already multiplied by `speed`,
  so effects are written for speed 1 and speed scaling comes for free.
- `i`/`n` let lights in a group do different things (waves, alternation).
- `p` holds the user's options (base colour, brightness ceiling, speed).
- `rng` is a per-light random generator, for flicker-style effects.
- `interval` is the seconds between updates at speed 1. Smart bulbs are
  slow and rate-limited, so keep it at 0.4 s or more; the light's own
  transition smooths between updates.
- `beat=True` is for on/off patterns (police, pulse): each update advances
  `t` by exactly one `interval`, so speeding up can't skip beats.
- `duration`, if set, makes the effect end by itself (e.g. sunrise) and
  leave the lights in their final state.

Then restart Home Assistant and add the name to `services.yaml` so it
shows in the UI picker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import random


@dataclass(frozen=True, slots=True)
class Params:
    """User options for a run."""

    speed: float = 1.0  # >1 is faster
    brightness: int = 100  # percent; ceiling the effect is scaled into
    hue: float | None = None  # base hue 0-360 for single-colour effects
    saturation: float = 100.0  # base saturation 0-100


@dataclass(frozen=True, slots=True)
class Frame:
    """What one light should look like right now."""

    hs: tuple[float, float] | None = None  # (hue 0-360, saturation 0-100)
    kelvin: int | None = None  # white colour temperature
    brightness: float | None = None  # percent 0-100, before the ceiling
    off: bool = False


EffectFn = Callable[[float, int, int, Params, random.Random], Frame]


@dataclass(frozen=True, slots=True)
class Effect:
    name: str
    description: str
    fn: EffectFn
    interval: float = 1.0
    duration: float | None = None
    beat: bool = False  # on/off style: advance exactly one interval per update


EFFECTS: dict[str, Effect] = {}


def effect(
    name: str,
    description: str,
    *,
    interval: float = 1.0,
    duration: float | None = None,
    beat: bool = False,
) -> Callable[[EffectFn], EffectFn]:
    def register(fn: EffectFn) -> EffectFn:
        EFFECTS[name] = Effect(name, description, fn, interval, duration, beat)
        return fn

    return register


def _wave(t: float, period: float, phase: float = 0.0) -> float:
    """Smooth 0..1..0 wave."""
    return 0.5 - 0.5 * math.cos(2 * math.pi * (t / period + phase))


def _lerp(a: float, b: float, x: float) -> float:
    return a + (b - a) * min(max(x, 0.0), 1.0)


# --- Colour cycles ----------------------------------------------------------


@effect("colorloop", "All lights slowly cycle through every colour together", interval=1.0)
def colorloop(t, i, n, p, rng):
    return Frame(hs=((t * 12) % 360, p.saturation), brightness=100)


@effect("rainbow", "A rainbow spread across the lights, rotating", interval=1.0)
def rainbow(t, i, n, p, rng):
    return Frame(hs=((t * 12 + i * 360 / n) % 360, p.saturation), brightness=100)


@effect("party", "A random new colour on each light every beat", interval=1.0)
def party(t, i, n, p, rng):
    return Frame(hs=(rng.uniform(0, 360), 100), brightness=rng.uniform(70, 100))


# --- Ambient ----------------------------------------------------------------


@effect("breathe", "Slow fade up and down in one colour (default warm white)", interval=0.6)
def breathe(t, i, n, p, rng):
    hue = 30.0 if p.hue is None else p.hue
    sat = 60.0 if p.hue is None else p.saturation
    return Frame(hs=(hue, sat), brightness=10 + 90 * _wave(t, 6.0))


@effect("aurora", "Greens, teals and purples drifting across the lights", interval=2.0)
def aurora(t, i, n, p, rng):
    phase = i / max(n, 1)
    hue = 205 + 85 * math.sin(2 * math.pi * (t / 40 + phase))
    return Frame(hs=(hue % 360, 85), brightness=45 + 55 * _wave(t, 23, phase * 0.7))


@effect("ocean", "Deep blues and aquas rolling like water", interval=1.5)
def ocean(t, i, n, p, rng):
    phase = i / max(n, 1)
    hue = 205 + 25 * math.sin(2 * math.pi * (t / 18 + phase))
    return Frame(hs=(hue, 90), brightness=35 + 65 * _wave(t, 9, phase))


@effect("candle", "Warm, gently flickering candlelight", interval=0.5)
def candle(t, i, n, p, rng):
    return Frame(hs=(rng.uniform(26, 34), rng.uniform(80, 92)), brightness=rng.uniform(45, 85))


@effect("fireplace", "Orange and red flames, livelier than candle", interval=0.5)
def fireplace(t, i, n, p, rng):
    return Frame(hs=(rng.uniform(8, 32), 100), brightness=rng.uniform(35, 100))


# --- Attention --------------------------------------------------------------


@effect("police", "Red and blue alternating between lights", interval=0.8, beat=True)
def police(t, i, n, p, rng):
    beat = int(t / 0.8)
    red = (beat + i) % 2 == 0
    return Frame(hs=(0 if red else 240, 100), brightness=100)


@effect("pulse", "Short bright pulses in one colour (default red)", interval=0.6, beat=True)
def pulse(t, i, n, p, rng):
    hue = 0.0 if p.hue is None else p.hue
    on = int(t / 0.6) % 2 == 0
    return Frame(hs=(hue, p.saturation), brightness=100 if on else 15)


# --- Wake / sleep (end on their own) ----------------------------------------


@effect(
    "sunrise",
    "15-minute wake-up: deep red to warm orange to bright daylight, then stays on",
    interval=5.0,
    duration=900,
)
def sunrise(t, i, n, p, rng):
    x = t / 900
    if x < 0.6:
        y = x / 0.6
        return Frame(hs=(_lerp(2, 30, y), _lerp(100, 70, y)), brightness=_lerp(1, 40, y))
    y = (x - 0.6) / 0.4
    return Frame(kelvin=int(_lerp(2700, 5000, y)), brightness=_lerp(40, 100, y))


@effect(
    "sunset",
    "15-minute wind-down: warm white fading to dim red, then off",
    interval=5.0,
    duration=900,
)
def sunset(t, i, n, p, rng):
    x = t / 900
    if x >= 0.995:
        return Frame(off=True)
    if x < 0.4:
        return Frame(kelvin=int(_lerp(3000, 2700, x / 0.4)), brightness=_lerp(80, 40, x / 0.4))
    y = (x - 0.4) / 0.6
    return Frame(hs=(_lerp(30, 2, y), _lerp(70, 100, y)), brightness=_lerp(40, 1, y))

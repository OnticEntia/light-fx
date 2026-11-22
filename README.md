# Light FX

Software light effects for Home Assistant: colour loops, candlelight, sunrise and more, on any light that Home Assistant can set a colour or brightness on.

Many lights (Matter, HomeKit, Zigbee bulbs without firmware effects) offer no effects of their own in Home Assistant. Light FX runs the effect inside Home Assistant and sends ordinary `light.turn_on` commands, so it works with them all the same. Nanoleaf panel devices can optionally be driven panel by panel.

## What it adds

Two actions and nothing else: no entities, no devices.

| Action | What it does |
| --- | --- |
| `light_fx.start` | Runs an effect on the lights you target. |
| `light_fx.stop` | Stops effects, on the lights you target or on all lights, and by default puts the lights back the way they were. |

An effect on a light also stops by itself when anything else commands that light: a dashboard tap, an automation, a voice assistant. Normal control always wins, and in that case the light is left as the other command set it.

### Effects

| Effect | Description |
| --- | --- |
| `colorloop` | All lights slowly cycle through every colour together. |
| `rainbow` | A rainbow spread across the lights, rotating. |
| `party` | A random new colour on each light every beat. |
| `breathe` | Slow fade up and down in one colour (default warm white). |
| `aurora` | Greens, teals and purples drifting across the lights. |
| `ocean` | Deep blues and aquas rolling like water. |
| `candle` | Warm, gently flickering candlelight. |
| `fireplace` | Orange and red flames, livelier than candle. |
| `police` | Red and blue alternating between lights. |
| `pulse` | Short bright pulses in one colour (default red). |
| `sunrise` | 15-minute wake-up: deep red to warm orange to bright daylight, then stays on. |
| `sunset` | 15-minute wind-down: warm white fading to dim red, then off. |

`sunrise` and `sunset` end by themselves and leave the lights in their final state. The others run until stopped.

## Requirements

- Home Assistant 2026.11 or newer. That is the version this is tested on; older versions may work but are not checked.
- Lights that are entities in Home Assistant's `light` domain.
  - Colour effects need a light with a colour mode (`hs`, `xy`, `rgb`, `rgbw` or `rgbww`).
  - On a white-only or dimmable-only light, an effect changes what the light can change (colour temperature, brightness) and skips the rest.
- Lights are updated at most every 0.4 seconds each. Faster than that, most smart bulbs drop commands. Effects are designed around this and use the light's own transition to smooth between updates.

### Nanoleaf per-panel control (optional, experimental)

Home Assistant sees a Nanoleaf panel device as a single light. If you list the device in the configuration, Light FX talks to it directly over Nanoleaf's local Open API and gives every panel its own colour, so `rainbow` spreads across the panels instead of colouring the whole device at once.

This needs:

- a Nanoleaf device that supports the local Open API and its external-control ("extControl" v2) streaming mode;
- the device reachable from Home Assistant on TCP port 16021 and UDP port 60222;
- an Open API token for the device (see below).

This path is new and has had little testing on real hardware. Please open an issue with the device model if it misbehaves.

## Installation

### HACS (custom repository)

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/OnticEntia/light-fx` with the type **Integration**.
3. Find **Light FX** in HACS and download it.
4. Add the configuration below, then restart Home Assistant.

### Manual

1. Copy the `custom_components/light_fx` folder from this repository into the `custom_components` folder of your Home Assistant configuration directory (create `custom_components` if it does not exist).
2. Add the configuration below, then restart Home Assistant.

## Configuration

Light FX is set up in `configuration.yaml`; there is no setup screen in the UI.

```yaml
light_fx:
```

That is all that is needed for ordinary lights.

To drive a Nanoleaf device panel by panel, list it:

```yaml
light_fx:
  nanoleaf:
    - entity_id: light.my_nanoleaf
      host: 192.0.2.10
      token: !secret nanoleaf_token
```

| Option | Required | Description |
| --- | --- | --- |
| `entity_id` | yes | The light entity Home Assistant already has for the device. Targeting this entity in `light_fx.start` uses per-panel control. |
| `host` | yes | The device's IP address or host name. Give it a fixed address in your router. |
| `token` | yes | The device's Open API token. Keep it in `secrets.yaml`. |

### Getting a Nanoleaf token

1. Hold the power button on the device's controller for 5 to 7 seconds, until its lights start flashing.
2. Within 30 seconds, send an empty POST request to the device from any computer on the same network:

   ```bash
   curl -X POST http://192.0.2.10:16021/api/v1/new
   ```

3. The reply contains the token: `{"auth_token": "..."}`. Put it in `secrets.yaml` as `nanoleaf_token: "..."`.

## Using the actions

Both actions take the usual target: entities, devices, areas, floors or labels. Only lights are used from whatever you target.

### `light_fx.start`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `effect` | yes | | One of the effects listed above. |
| `speed` | no | `1` | `0.1` to `10`. `2` is twice as fast, `0.5` half speed. A 15-minute sunrise at speed `3` takes 5 minutes. |
| `brightness` | no | `100` | `1` to `100`. The brightest the effect will go, in percent. |
| `color` | no | | An RGB colour, for example `[255, 120, 0]`. Sets the colour of `breathe` and `pulse`, and the saturation of `colorloop` and `rainbow`. |

Starting an effect on a light that is already running one replaces it. The state remembered for `light_fx.stop` is still the one from before the first effect.

```yaml
action: light_fx.start
target:
  area_id: living_room
data:
  effect: rainbow
  speed: 0.5
  brightness: 60
```

```yaml
action: light_fx.start
target:
  entity_id: light.bedside
data:
  effect: breathe
  color: [0, 80, 255]
```

A wake-up light, as an automation:

```yaml
automation:
  - alias: Wake-up light
    triggers:
      - trigger: time
        at: "06:30:00"
    actions:
      - action: light_fx.start
        target:
          entity_id: light.bedroom
        data:
          effect: sunrise
```

### `light_fx.stop`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `restore` | no | `true` | Put the lights back the way they were before the effect started. With `false` they stay on their last frame. |

With no target, it stops every running effect.

```yaml
action: light_fx.stop
```

```yaml
action: light_fx.stop
target:
  entity_id: light.bedside
data:
  restore: false
```

## Troubleshooting

**The actions do not exist.** Check that `light_fx:` is in `configuration.yaml` and that Home Assistant was restarted afterwards. Look in the log for errors mentioning `light_fx`.

**"no lights targeted" in the log.** The target contained no entities from the `light` domain. Switches and scenes are ignored.

**The effect stops on its own after a moment.** Something else is commanding the light: another automation, a scene, an adaptive-lighting integration, or a light group being controlled elsewhere. Light FX hands the light back whenever that happens. The log line "was controlled elsewhere; stopping its effect" names the light; set the logger to `info` to see it (below).

**The effect looks jerky or lights lag behind.** A light that has not finished one update skips the next. Lower `speed`, or run the effect on fewer lights at once. Bulbs on a busy Zigbee or Thread network are the usual cause.

**A light only changes brightness.** It has no colour mode in Home Assistant. Check `supported_color_modes` on the entity under Developer tools → States.

**Nanoleaf: "not reachable yet" at startup, or "Can't reach Nanoleaf".** Check the address, that the device is on, and that ports 16021 (TCP) and 60222 (UDP) are not blocked between Home Assistant and the device. A reply of 401 or 403 means the token is wrong. The device is tried again each time an effect starts on it.

**Nanoleaf panels light in an unexpected order.** Panels are ordered left to right, then top to bottom, by the positions the device reports. That follows the layout as the device knows it, which may be rotated relative to how it hangs.

To see more detail in the log:

```yaml
logger:
  logs:
    custom_components.light_fx: debug
```

Device tokens are never written to the log.

## Development

The effects live in `custom_components/light_fx/effects.py`. Each is one small function answering "at time `t`, what should light `i` of `n` look like?"; the file's header explains how to add one. A new effect also needs its name added to the dropdown in `services.yaml`; a test checks the two stay in step.

The tests need only Python, not Home Assistant:

```bash
python3 -m unittest discover -s tests
```

## Licence

MIT. See [LICENSE](LICENSE).

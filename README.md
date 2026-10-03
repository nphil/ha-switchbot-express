<img src="custom_components/switchbot_express/brand/icon.png" width="96" align="right" alt="">

# SwitchBot Express

A drop-in replacement for Home Assistant's core `switchbot` integration for
Bluetooth SwitchBots, with a **connection policy you control**. Same protocol
library (`PySwitchbot`), same entity IDs, far fewer commands that make you wait.

[![hacs][hacs-badge]][hacs] [![release][release-badge]][releases] [![validate][validate-badge]][validate]

## Why this exists

A SwitchBot curtain is controlled over a GATT connection, not by an
advertisement. Core's integration connects on demand and then drops the link
8.5 seconds later (`DISCONNECT_DELAY` in pySwitchbot's `device.py`). So unless
you send two commands within 8.5 seconds of each other, **every command pays a
full connect first**: on this network, through an ESPHome Bluetooth proxy, that
was measured at **1.1 s to 6.1 s**. That is the pause between pressing the
button and the curtain moving, and it is also the part most likely to fail —
the command itself is a couple of bytes.

Nothing is wrong with the protocol handling, so this integration does not
reimplement it. It subclasses pySwitchbot's device class and replaces only the
connection lifecycle:

- **Linger** — keep the link for a few seconds after a command you issued, so
  the second and third nudge of a curtain are instant.
- **Prewarm** — an action your automation calls *before* it needs the device,
  so the command that follows finds the link already open.
- **Hold** — an opt-in permanent connection, for solar or mains fed units.
- **Fail fast** — a hard cap on a single connect attempt, so a bad proxy path
  gives up in seconds and the retry gets re-scored onto another proxy, instead
  of wedging your command for a minute.
- **A Connection sensor** that tells you which proxy is holding the link, how
  often it drops, and whether a reconnect is in flight.

## Battery cost model

Read this before turning anything up.

A SwitchBot that is **advertising** wakes its radio briefly, on its own
schedule, and says nothing to anyone in particular. A SwitchBot that is
**connected** must wake for a connection event at the interval the proxy chose,
every interval, for as long as the link is up, and answer. Holding a link is
therefore not free, and it is not cheap: it is the single most expensive thing
you can ask a battery powered SwitchBot to do.

That is why the defaults here are small:

- `linger_seconds` is **15**, and it is armed **only by a command you issued**
  (open, close, set position, stop). A background poll, the read-back the
  library performs after a command, and an expired prewarm all fall back to the
  library's own 8.5 s. The point of the default is to carry the link across a
  *burst of adjustments*, not across an idle afternoon.
- `hold_connection` is **off**, and its label says what it is for: solar or
  mains powered units. On a battery, it will flatten it.
- Both are **suspended automatically** at or below `low_battery_percent`, and a
  repair issue explains why while that is in force.

## How to get an instant first command without hurting the battery

Do not hold the link and hope. **Prewarm on intent**: fire
`switchbot_express.prewarm` from the same trigger that is about to move the
device, 30 to 60 seconds ahead. The radio is awake for that one minute instead
of all day, and the command that follows runs with the link already open.

```yaml
automation:
  - alias: Warm the curtain before the sunset close
    triggers:
      - trigger: sun
        event: sunset
        offset: "-00:01:00" # 60 s before the close below
    actions:
      - action: switchbot_express.prewarm
        target:
          entity_id: cover.living_room_curtain

  - alias: Close the curtain at sunset
    triggers:
      - trigger: sun
        event: sunset
    actions:
      - action: cover.close_cover
        target:
          entity_id: cover.living_room_curtain
```

The same shape works for anything you can predict a minute ahead: a presence
sensor on the hallway, a calendar trigger, an alarm clock helper, the moment a
scene starts. Prewarm keeps working when the battery is low — it is explicit,
bounded, and asked for by name, which is exactly the trade-off the automatic
policies cannot make for you.

## Options

All of these live under the integration's **Configure** button, per device.

| Option | Default | Range | What it does |
| --- | --- | --- | --- |
| Keep the connection after a command (`linger_seconds`) | `15` | 0-600 s | Seconds to keep the link after a command **you** issued, reset by each new command. `0` behaves like core (the library's own 8.5 s idle disconnect). |
| Hold connection, solar or mains powered units only (`hold_connection`) | `off` | on/off | Keep the link open permanently, with a reconnect supervisor. Suspended below the low battery threshold. |
| Prewarm duration (`prewarm_seconds`) | `60` | 5-900 s | How long the `prewarm` action keeps the link open. |
| Low battery threshold (`low_battery_percent`) | `15` | 0-100 % | At or below this, linger and hold are suspended and a repair is raised. Prewarm still works. |
| Command retries (`retry_count`) | `3` | 0-10 | Passed straight to pySwitchbot. Each retry reconnects, and Home Assistant re-scores the proxies for it. |
| Connect attempt timeout (`connect_timeout`) | `10` | 3-60 s | Hard cap on one connect attempt. bleak-retry-connector's own default is 4 attempts of up to 20 s; this cuts a bad path short so the retry can pick a different proxy. |

## Entities

For a Curtain or Curtain 3, named `Living Room Curtain`:

| Entity | Notes |
| --- | --- |
| `cover.living_room_curtain` | Position, open, close, stop. Reports `is_opening` / `is_closing` optimistically the moment a command is accepted, then corrects from the read-back, and carries `last_run_success`. |
| `binary_sensor.living_room_curtain_calibration` | Diagnostic. |
| `sensor.living_room_curtain_battery` | Diagnostic. |
| `sensor.living_room_curtain_light_level` | From the advertisement. |
| `sensor.living_room_curtain_connection` | Diagnostic, enabled by default. See below. |

### Startup never waits on the radio

Setting up a device returns within 5 seconds whatever the SwitchBot is doing, so
a curtain that is out of range or a busy proxy cannot slow Home Assistant's
start. If the device has not been heard in that time its entities show as
unavailable and fill in by themselves when it is; the battery, light level and
calibration entities appear the first time the device advertises them. Nothing is
moved when a device comes back.

### The Connection sensor

Its state is the **name of the proxy or adapter currently holding a GATT link**
to the device (from Home Assistant's own slot allocations), or `disconnected`.
Attributes:

- `hold` — whether the link is being held open right now. Reports `false` while
  the battery saver has suspended holding, even if the option is on.
- `drops_1h` — unexpected disconnects in the trailing hour.
- `last_drop` — when the last one was (UTC), or `null`.
- `reconnect_attempt` — which reconnect attempt is in flight, `0` when
  connected or not holding. Backoff is 1, 2, 5, 10, 30, 60 s with ±20 % jitter.

Because every reconnect goes back through Home Assistant's Bluetooth manager,
which scores proxies by signal, free connection slots and recent failures, a
device that has drifted closer to a different proxy roams onto it by itself.

## Actions

### `switchbot_express.prewarm`

Connects now and keeps the link for `prewarm_seconds`. Target an entity, a
device, or an area. No other fields.

## Installation

**HACS → custom repository.** In HACS, open the three-dot menu, choose *Custom
repositories*, add `https://github.com/nphil/ha-switchbot-express` with
category *Integration*, then install **SwitchBot Express** and restart Home
Assistant. Your SwitchBot should appear under *Settings → Devices & services*
as discovered; otherwise use *Add integration → SwitchBot Express*.

Manual: copy `custom_components/switchbot_express` into your `config`
directory and restart.

## Migrating from the core SwitchBot integration

Entity IDs are derived from the device name, so they survive the move as long
as you set the same name.

1. Note the current name of the device (**Settings → Devices & services →
   SwitchBot Bluetooth → your device**), for example `Living Room Curtain`.
2. Delete the core `switchbot` config entry for that device. This removes its
   entities; long-term statistics for the sensors are keyed by entity ID and
   are picked up again in step 4.
3. Add **SwitchBot Express** and pick the device. In the confirmation step,
   **type the exact name from step 1** — the default is the name core would
   have generated (`Curtain 3 1932`), which would produce different entity IDs.
4. Check the entity IDs match what you had: `cover.living_room_curtain`,
   `sensor.living_room_curtain_battery`, and so on. Automations, dashboards and
   statistics carry on untouched.

Both integrations can be installed at once, but do not set up the same physical
device in both: they would fight over the one connection slot the device has.

## Requirements

- Home Assistant **2026.9.0** or newer, with the `bluetooth` integration and at
  least one **connectable** adapter or ESPHome Bluetooth proxy in range.
- `PySwitchbot==2.4.1`, installed automatically.

Supported today: **Curtain** and **Curtain 3**. Bot, Plug Mini and Blind Tilt
fit the same device layer and are not implemented yet. Encrypted models
(locks), cloud accounts and advertisement-only sensors are out of scope — core's
integration handles those well, and nothing here would help them.

## Development

The connection policy — linger, hold, prewarm, the battery threshold, the
backoff sequence and the drop window — lives in
`custom_components/switchbot_express/policy.py` with no Home Assistant, bleak or
pySwitchbot imports, and is tested on its own:

```bash
python3 -m pytest tests
```

`custom_components/switchbot_express/device.py` is the only file that touches
pySwitchbot internals. Its module docstring lists every upstream method it
overrides, wraps or relies on; check that list when bumping the pin in
`manifest.json`.

Brand images for HACS and the Home Assistant UI live in
`custom_components/switchbot_express/brand/` (all eight PNGs: icon, logo, and
their `dark_` and `@2x` variants). They are rendered from `icon.svg` by
`python3 tools/render_brand.py` (Pillow only).

## Licence

MIT © nphil

[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-custom-41BDF5.svg
[release-badge]: https://img.shields.io/github/v/release/nphil/ha-switchbot-express
[releases]: https://github.com/nphil/ha-switchbot-express/releases
[validate]: https://github.com/nphil/ha-switchbot-express/actions/workflows/validate.yml
[validate-badge]: https://github.com/nphil/ha-switchbot-express/actions/workflows/validate.yml/badge.svg

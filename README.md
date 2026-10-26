# H3X Modbus Translator

Local HACS integration translating **Shelly Pro 3EM / Pro 3EM-63 Modbus TCP**
measurements into a **read-only SDM630 register subset**. Independent of
[H3X Bridge](https://github.com/shuffleznl/h3x-bridge) and
[Predictive Dispatch](https://github.com/shuffleznl/h3x-predictive-dispatch): no
inverter connection, charge commands, EMS changes or arbitration with either controller.

## Important: The H3X Meter Port Is Not TCP

**This is not a software-only way to connect Shelly to the H3X meter port.**
The documented H3X meter connection is physical RS485. Its inbound Modbus TCP
control server does not make it a TCP meter client. A meter-model selection without
a meter IP/port cannot query a TCP meter server. On the deployment for which this
was written, there is no RS485 gateway, so native H3X meter ingestion is **not
connected**. Ordinary HA dashboards/Modbus clients can use the translated TCP data.

Required topology for the native meter port:

```text
Shelly Pro 3EM TCP server :502
        ^ FC04 polls
        |
HA translator TCP client -> validated immutable snapshot -> SDM630 TCP server :5020
                                                               ^
                                              TCP requests/responses
                                                               |
                              REVERSE RTU-to-TCP protocol gateway
                                                               ^
                                      RS485 Modbus RTU queries |
                                                               |
                                              H3X meter-port master
```

The gateway must accept an **RTU master's requests on its serial port**, forward
them as TCP requests to HA and return the responses as RTU. The usual gateway
that lets a TCP client poll a physical RTU meter is the **opposite direction**.
A transparent serial tunnel is not sufficient: MBAP/RTU framing and CRC conversion
are needed. Do not buy a gateway without confirming this reverse role, unit-ID
forwarding and timeout behavior. No particular gateway has been hardware-validated.
An isolated USB-RS485 meter-slave server could be another future transport; this
release implements TCP only. Have a qualified installer handle inverter wiring.

This does **not** establish a fix for forced charging. This installation previously
charged under external control while native meter power reported zero. Do not
assume a missing native meter explains the separate charging regression.

## Why SDM630

SDM630 is an H3X-selectable three-phase meter with a documented float32 map.
It maps Shelly's V, A, signed W, VA, power factor, frequency and import/export Wh
without integer scaling guesses. SEM3 has additional dual-channel semantics;
DTSU666 variants have different scaling/register profiles. Emulating several
unverified models would obscure the actual compatibility boundary.

**Subset, not a certified meter clone.** Shelly's documented EM Modbus map does
not provide signed instantaneous reactive power, phase angle, line-line voltage,
THD or demand. Those values are not invented. A read spanning any unsupported
address returns exception `02`, including a wide read crossing a gap. An H3X
firmware that requires such a block is not compatible with this subset without
further evidence and implementation. The H3X polling sequence has not been captured
on a physical gateway, so native compatibility is unverified. The identity explicitly
identifies this software, not a genuine Eastron meter.

## Install With HACS

Requires Home Assistant **2026.10.4+**, a Shelly operating in three-phase mode,
and enabled Shelly Modbus TCP. Validation pins `pymodbus==3.13.1`, matching
HA 2026.10.4. The runtime requirement is `pymodbus>=3.13.1,<4` so it can follow
HA's core dependency rather than forcing a conflicting exact version. New library
versions still need validation; this release does not certify every future 3.x API.

1. HACS > Custom repositories: add `https://github.com/shuffleznl/h3x-modbus-translator`, category **Integration**.
2. Download the tagged release and restart HA once.
3. Settings > Devices & services > Add integration > **H3X Modbus Translator**.
4. Set the Shelly hostname/IP, port (default `502`) and unit ID (default `1`).
5. Set the HA bind IP and listener port (default `5020`, configurable `1024..65535`).
6. Enter the **gateway/client IP** in Allowed clients. Default is loopback only,
   so it will not serve LAN clients until explicitly permitted. Comma-separated
   IPs/CIDRs are supported; prefer a single static IP, not a whole network.

Enable Modbus in Shelly's own web interface first. The translator does not enable
it remotely or change CT settings. Its options flow edits all network settings and
reloads only this integration. If the port is occupied, setup retries with a clear
bind error. Offline Shelly does not prevent the listener/diagnostics from loading.

Use an isolated trusted LAN/VLAN and firewall. Modbus TCP has no authentication or
encryption; an allowlist is not protection against a compromised approved device.
Never port-forward this server to the Internet. HA Container needs host networking
or an explicit port mapping. HA OS normally exposes its host-network listener.
At most eight clients are admitted; idle connections close after 120 seconds.

## Dashboard

The dashboard ships **inside the custom component**, so HACS includes it:
`custom_components/h3x_modbus_translator/dashboards/h3x-modbus-translator.yaml`.
It uses only native HA cards. Settings/health are first, grid-power history in the
middle, energy counters and diagnostics last. There is deliberately no fake
"H3X connected" or "charging fixed" indicator. Client activity proves requests,
not that an inverter accepted measurements.

For a dashboard that follows HACS file updates, add this to HA's existing Lovelace
configuration (merge with any existing `lovelace:` / `dashboards:` keys):

```yaml
lovelace:
  dashboards:
    h3x-modbus-translator:
      mode: yaml
      title: H3X Meter Translator
      icon: mdi:swap-horizontal
      show_in_sidebar: true
      filename: custom_components/h3x_modbus_translator/dashboards/h3x-modbus-translator.yaml
```

Alternatively import its YAML through a new dashboard's raw editor. Storage-mode
imports do not update when HACS replaces the file; reimport after releases. The
Predictive Dispatch dashboard also has a conditional translator panel, hidden when
this integration is absent. One translator entry per HA instance keeps its stable
entity IDs unambiguous. Explicit user renames of entity IDs require matching YAML.

## Register Map

All target values are FC04 input registers, **float32 ABCD**, two registers each.
The table uses **zero-based wire addresses**. SDM630 printed `30001` is wire `0`.
Shelly printed `31020` is wire **`1020`**, not `1019`; its values use **CDAB**.

| SDM630 wire address | Measurement | Shelly wire address / calculation |
| ---: | --- | --- |
| 0 / 2 / 4 | L1 / L2 / L3 voltage, V | 1020 / 1040 / 1060 |
| 6 / 8 / 10 | L1 / L2 / L3 current, A | 1022 / 1042 / 1062 |
| 12 / 14 / 16 | L1 / L2 / L3 active power, W | 1024 / 1044 / 1064 |
| 18 / 20 / 22 | L1 / L2 / L3 apparent power, VA | 1026 / 1046 / 1066 |
| 30 / 32 / 34 | L1 / L2 / L3 signed power factor | 1028 / 1048 / 1068, sign from active power |
| 42 | Mean phase-to-neutral voltage, V | Arithmetic mean of measured phases |
| 46 / 48 | Mean / sum of phase currents, A | Measured phases |
| 52 | Total active power, W | 1013; checked against phase sum |
| 56 | Total apparent power, VA | 1015 |
| 62 | Total signed power factor | Total W / total VA (0 at zero VA) |
| 70 | Frequency, Hz | Mean of 1033 / 1053 / 1073 |
| 72 / 74 | Import / export active energy, kWh | 1162 / 1164, Wh divided by 1000 |
| 342 | Total active energy, kWh | Import + export, not net |

Positive W means import; negative W means export. Validate actual CT direction and
phase matching against a known load **before enabling native meter-based control**.
Global reversal flips all W/PF signs and swaps the import/export counters. It is
not a fix for one reversed CT: correct individual CT configuration on Shelly.

FC03/configuration writes, coils, FC06/FC16 and read-write operations are not
supported. No target-side write can reach Shelly or the inverter. Reads must start
on an even address and request an even count of **2..80** registers. Unknown unit
IDs receive no reply. Unsupported addresses return `02`, unsupported functions
`01`, invalid alignment/count `03`, unavailable/stale source `04`.

## Failure Behavior

- Single persistent serialized Shelly client; zero read retries and bounded polling.
- Atomic map replacement after timestamp, alarms, finite/range and phase-sum checks.
- Timestamp changes during a read trigger one new full-snapshot attempt.
- Any failed poll invalidates outputs immediately; the previous map is not served.
- Maximum source age defaults to 10 seconds; a frozen EM timestamp also fails closed.
- Energy update timestamp must advance within 180 seconds. HA and source wall clocks
  need not agree: freshness uses monotonic time and timestamp progress.
- On failure, energy/power entities become unavailable; the server returns exception
  `04`, never a plausible-looking zero or frozen valid power. Recovery is automatic.
- Failed polls back off up to 30 seconds, preventing tight reconnect loops.
- On unload/HA stop, the poll task is cancelled and awaited and both sockets close.

**This is not a protection relay or a certified export limiter.** A gateway or H3X
may handle meter exceptions differently; prove its disconnected/stale-meter behavior
under installer supervision. Keep independent fuse, inverter and export protections.
Do not use this uncommissioned software to meet DSO/zero-export compliance.

## Validation And Commissioning

Offline tests use real PyModbus TCP servers and clients: emulated Shelly source ->
translator -> SDM630 client. They cover signs/byte order/Wh scaling, phase coherence,
alarms, missing/frozen data, write rejection, unknown registers, allowlists, source
recovery, fragmented TCP frames, transaction IDs, concurrency, and port cleanup.
HA entity-registry/schema tests check dashboard references, including a renamed
device. This is **software validation, not physical H3X acceptance**.

```powershell
uv sync --group dev
uv run pytest
uv run ruff check .
```

Commission in stages: verify signed Shelly data; read wire `52` count `2` using
FC04 at HA's listener; then validate the reverse gateway; capture the actual H3X
register requests; compare H3X native grid power with Shelly during import/export;
disconnect the source/gateway and confirm the H3X's safe failure mode. Do not turn
on battery/export control merely because this server responds successfully.

## References

- [Shelly Modbus TCP and addressing examples](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Modbus/)
- [Shelly EM phase/power registers](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/EM/)
- [Shelly EMData energy registers](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/EMData/)
- [Eastron SDM630 protocol V1.8](https://www.eastroneurope.com/images/uploads/products/protocol/SDM630_MODBUS_Protocol.pdf)
- [Pylontech H3X three-phase user manual, meter wiring](https://skarebo.nl/documents/Force_H3X_HY_3P_Series_User_Manual_20250708112222A556.pdf)
- [PyModbus 3.13.1 server API](https://pymodbus.readthedocs.io/en/v3.13.1/source/server.html)
- [Home Assistant YAML dashboards](https://www.home-assistant.io/dashboards/dashboards/)

## Release 0.1.0

Initial experimental software release: Shelly TCP source, SDM630 TCP subset,
editable endpoint/unit/allowlist/freshness options, native dashboard, and emulator
tests. No live installation, HA restart or H3X commissioning was performed for
this release. Hardware path and H3X polling compatibility remain unverified.

# Channel mapping

The canonical schema (`src/pitwall/channels.py`) is the cross-game translation
layer. Every game reader maps its native fields **into** these canonical names;
nothing downstream of ingest knows which game produced the data.

The ACC column below is **validated against real captured sessions**
(Nurburgring / Ford Mustang GT3, in `src/pitwall/games/acc/{structs,mapping}.py`):
field names, struct offsets, units, value ranges, and the `accG` axis/sign all
decode correctly. The `accG` convention was confirmed by correlating against
braking and steering over a real lap (see those rows).

The iRacing column is the `.ibt` mapping implemented in
`src/pitwall/games/iracing/mapping.py`. The `.ibt` decode layer
(`iracing/{ibt_types,ibt,normalize}.py`) is a near-verbatim port of the
racing-telemetry-visualiser project's reader, so the two stay easy to reconcile;
only this canonical translation is pitwall-specific. iRacing field names drift
slightly between builds, so each channel takes the **first present** of a few
candidate var names (e.g. `LFpressure` or `LFpress`; `LFtempCM`/`CL`/`M`/`L`).

| Canonical | Unit | dtype | ACC source (page.field) | iRacing (.ibt) | Notes |
|---|---|---|---|---|---|
| `lap_distance_m` | m | float32 | graphics.`normalizedCarPosition` × static.`trackSPlineLength` | `LapDist` | Distance bin key; monotonic within a lap. iRacing reports metres directly. **ACC fallback:** ACC reports `trackSPlineLength == 0` even in clean sessions, so when it is 0 we use `naming.TRACK_LENGTHS[code]` (metres) — resolved once in `acc/mapping.resolve_track_length`. Only an *unknown* ACC track code falls back to the raw `normalizedCarPosition` 0..1. |
| `throttle` | 0–1 | float32 | physics.`gas` | `Throttle` | |
| `brake` | 0–1 | float32 | physics.`brake` | `Brake` | |
| `clutch` | 0–1 | float32 | physics.`clutch` | `1 − Clutch` | iRacing's `Clutch` is **inverted** (1.0 = fully disengaged); flipped to canonical 1=engaged. |
| `steering_angle` | rad | float32 | physics.`steerAngle` | `SteeringWheelAngle` | Negative left, positive right. |
| `gear` | gear | int8 | physics.`gear` − 1 | `Gear` | iRacing is **already canonical** (−1=R,0=N,1..n) — no offset. ACC needs −1 (0=R,1=N,2=1st). |
| `rpm` | rpm | int32 | physics.`rpms` | `RPM` (round) | |
| `speed_kmh` | km/h | float32 | physics.`speedKmh` | `Speed` × 3.6 | iRacing `Speed` is m/s. |
| `world_x` | m | float32 | graphics.`carCoordinates[playerCarID][0]` | — (None) | iRacing exposes only GPS `Lat`/`Lon`/`Alt`, not a cartesian world frame. Left None for v1; projection is a follow-up. Not used by the coach. |
| `world_y` | m | float32 | graphics.`carCoordinates[playerCarID][1]` | — (None) | Vertical axis in ACC. |
| `world_z` | m | float32 | graphics.`carCoordinates[playerCarID][2]` | — (None) | |
| `tire_temp_fl` | °C | float32 | physics.`tyreCoreTemperature[0]` | `LFtempCM`* | *first present of `LFtempCM`/`CL`/`M`/`L`. |
| `tire_temp_fr` | °C | float32 | physics.`tyreCoreTemperature[1]` | `RFtempCM`* | |
| `tire_temp_rl` | °C | float32 | physics.`tyreCoreTemperature[2]` | `LRtempCM`* | iRacing corner `LR` → canonical `rl`. |
| `tire_temp_rr` | °C | float32 | physics.`tyreCoreTemperature[3]` | `RRtempCM`* | |
| `tire_pressure_fl` | psi | float32 | physics.`wheelsPressure[0]` | `LFpressure` × 0.145 | iRacing pressures are kPa → psi. *first present of `LFpressure`/`LFpress`. |
| `tire_pressure_fr` | psi | float32 | physics.`wheelsPressure[1]` | `RFpressure` × 0.145 | |
| `tire_pressure_rl` | psi | float32 | physics.`wheelsPressure[2]` | `LRpressure` × 0.145 | |
| `tire_pressure_rr` | psi | float32 | physics.`wheelsPressure[3]` | `RRpressure` × 0.145 | |
| `g_lat` | g | float32 | **−**physics.`accG[0]` | `LatAccel` ÷ 9.80665 | ACC: `accG[0]` negated → positive = right. **iRacing sign is to be confirmed against a real lap** (negate in `iracing/mapping.py` if inverted). |
| `g_lon` | g | float32 | physics.`accG[2]` | `LongAccel` ÷ 9.80665 | positive = accelerating, negative = braking. |
| `fuel_kg` | kg | float32 | physics.`fuel` (L) × 0.745 | `FuelLevel` × 0.745 | `FUEL_DENSITY_KG_PER_L = 0.745` (defined in each game's `mapping.py`). |

### iRacing metadata & lap/sector derivation (not trace channels)

| Field | iRacing source | Notes |
|---|---|---|
| track code / name | `WeekendInfo.TrackName` / `TrackDisplayName` | Display name rides through `StaticInfo.track_name` (iRacing has no code→name table; the YAML supplies it). |
| car code / name | player driver's `CarPath` / `CarScreenName` | via `DriverInfo.Drivers[DriverCarIdx]`. |
| track length | `WeekendInfo.TrackLength` (`"3.25 km"` → 3250 m) | Supplied directly — no `TRACK_LENGTHS` table needed. |
| sector count / bounds | `SplitTimeInfo.Sectors[].SectorStartPct` | iRacing has **no** per-frame sector channel, so `sector_index` is computed from `LapDistPct` vs these starts (`mapping.sector_index_for`), replacing ACC's `currentSectorIndex`. |
| lap number | `Lap` channel via the ported `LapTracker` | Same lap-detection logic as the visualiser; projected onto `CanonicalFrame.lap_count`. |
| in pit | `OnPitRoad` | out/in-lap flags. |
| lap/sector times | *derived from frame timestamps* | Same as ACC; not iRacing's `LapLastLapTime`. |
| lap valid | — | No per-frame invalidation flag in `.ibt` telemetry; defaults `True` (**limitation**). |
| tyre set | — | No reliable per-frame tyre-set channel; constant 0, single-stint (**limitation**). |

## Metadata sources (not trace channels)

| Field | ACC source | Used by |
|---|---|---|
| track code | static.`track` | `naming.track_name` |
| car code | static.`carModel` | `naming.car_name` |
| sector count | static.`sectorCount` | lap segmentation |
| track length | static.`trackSPlineLength` (0 → `naming.TRACK_LENGTHS[code]`) | `lap_distance_m` |
| lap number | graphics.`completedLaps` | lap segmentation |
| sector index | graphics.`currentSectorIndex` | lap/sector segmentation |
| lap valid | graphics.`isValidLap` | `get_lap_summary` validity flag |
| in pit | graphics.`isInPitLane` | out/in-lap flags (whole pit-lane traversal, not just the box) |
| lap/sector times | *derived from frame timestamps*, not graphics.`iLastTime`/`lastSectorTime` | lap & sector tables |
| tyre set | graphics.`currentTyreSet` | stint detection |
| session type | graphics.`session` (AC_SESSION_TYPE) | `StaticInfo.session_type` |
| air / road temp | physics.`airTemp` / `roadTemp` | `StaticInfo.air_temp_c` / `road_temp_c` |
| session state | graphics.`status` (AC_STATUS) | live-reader termination + "game not running" |

**Lap & sector timing.** Lap time is wall-clock between start/finish-line crossings
(`completedLaps` increments); sector times come from a forward-only partition of that
same interval at `currentSectorIndex` steps (`ingest/pipeline._sector_breakdown`), so
sector times telescope to the lap time by construction. We deliberately do **not** use
ACC's `iLastTime`/`lastSectorTime`: `lastSectorTime` is unreliable across ACC versions,
and mixing ACC's lap clock with frame-derived sectors would reintroduce the sum≠lap-time
drift. Boundary frames are kept un-decimated by `downsample`, so the wall-clock times are
accurate to ~one native tick (~3 ms at 333 Hz).

# Channel mapping

The canonical schema (`src/pitwall/channels.py`) is the cross-game translation
layer. Every game reader maps its native fields **into** these canonical names;
nothing downstream of ingest knows which game produced the data.

The ACC column below is **validated against real captured sessions**
(Nurburgring / Ford Mustang GT3, in `src/pitwall/games/acc/{structs,mapping}.py`):
field names, struct offsets, units, value ranges, and the `accG` axis/sign all
decode correctly. The `accG` convention was confirmed by correlating against
braking and steering over a real lap (see those rows).

| Canonical | Unit | dtype | ACC source (page.field) | iRacing (v0.2) | Notes |
|---|---|---|---|---|---|
| `lap_distance_m` | m | float32 | graphics.`normalizedCarPosition` × static.`trackSPlineLength` | `LapDist` | Distance bin key; monotonic within a lap. **Fallback:** ACC reports `trackSPlineLength == 0` even in clean sessions, so when it is 0 we use `naming.TRACK_LENGTHS[code]` (metres) — resolved once in `acc/mapping.resolve_track_length`, shared by the session row and every per-frame distance. Only an *unknown* track code falls back to the raw `normalizedCarPosition` 0..1. |
| `throttle` | 0–1 | float32 | physics.`gas` | `Throttle` | |
| `brake` | 0–1 | float32 | physics.`brake` | `Brake` | |
| `clutch` | 0–1 | float32 | physics.`clutch` | `Clutch` | iRacing clutch is inverted (1=disengaged) — handle in v0.2. |
| `steering_angle` | rad | float32 | physics.`steerAngle` | `SteeringWheelAngle` | Negative left, positive right. |
| `gear` | gear | int8 | physics.`gear` − 1 | `Gear` | ACC: 0=R,1=N,2=1st → subtract 1 for display. |
| `rpm` | rpm | int32 | physics.`rpms` | `RPM` | |
| `speed_kmh` | km/h | float32 | physics.`speedKmh` | `Speed` (m/s ×3.6) | |
| `world_x` | m | float32 | graphics.`carCoordinates[playerCarID][0]` | `Lat`/`Lon`→proj | Indexed by `playerCarID` (out-of-range falls back to 0). |
| `world_y` | m | float32 | graphics.`carCoordinates[playerCarID][1]` | — | Vertical axis in ACC. |
| `world_z` | m | float32 | graphics.`carCoordinates[playerCarID][2]` | — | |
| `tire_temp_fl` | °C | float32 | physics.`tyreCoreTemperature[0]` | `LFtempCL` | |
| `tire_temp_fr` | °C | float32 | physics.`tyreCoreTemperature[1]` | `RFtempCL` | |
| `tire_temp_rl` | °C | float32 | physics.`tyreCoreTemperature[2]` | `LRtempCL` | |
| `tire_temp_rr` | °C | float32 | physics.`tyreCoreTemperature[3]` | `RRtempCL` | |
| `tire_pressure_fl` | psi | float32 | physics.`wheelsPressure[0]` | `LFpress` | |
| `tire_pressure_fr` | psi | float32 | physics.`wheelsPressure[1]` | `RFpress` | |
| `tire_pressure_rl` | psi | float32 | physics.`wheelsPressure[2]` | `LRpress` | |
| `tire_pressure_rr` | psi | float32 | physics.`wheelsPressure[3]` | `RRpress` | |
| `g_lat` | g | float32 | **−**physics.`accG[0]` | `LatAccel` (÷9.81) | Confirmed: `accG[0]` is lateral but signed opposite to steering, so **negated** → positive = right (right turns measured −1.36 g raw, left +1.39 g). |
| `g_lon` | g | float32 | physics.`accG[2]` | `LongAccel` (÷9.81) | Confirmed: positive = accelerating, negative = braking (hard braking measured −1.46 g, full throttle +0.38 g). |
| `fuel_kg` | kg | float32 | physics.`fuel` (L) × 0.745 | `FuelLevel` (×density) | `FUEL_DENSITY_KG_PER_L = 0.745` in `acc/mapping.py`. |

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

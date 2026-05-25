"""A deterministic synthetic ACC-like telemetry source.

This is the key to testing the whole pipeline -- storage, segmentation, the MCP
tools, the five demo queries -- without launching the game. It fabricates a
believable multi-lap session over a parametric track: a handful of corners with
realistic throttle/brake/speed/steer shapes, three sectors, tyre warm-up across
a stint, and a deliberate mix of laps:

    out-lap, normal, fastest, 2nd-fastest, invalid (off-track), then a fresh
    tyre set (new stint), normal, in-lap.

Seeded, so tests can assert the exact fastest-lap and stint structure. The live
ACC reader (C3) swaps in for this with no pipeline changes.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass

from pitwall.ingest.frames import CanonicalFrame, StaticInfo
from pitwall.ingest.source import TelemetrySource


@dataclass
class LapSpec:
    """How one synthetic lap behaves."""

    pace: float  # speed multiplier; higher = faster lap = lower lap time
    valid: bool = True
    tyre_set: int = 0
    outlap: bool = False
    inlap: bool = False


# Lap numbers are 1-based in the order below: out-lap, normal, fastest,
# 2nd-fastest, invalid, [tyre change] normal, normal, in-lap.
DEFAULT_LAPS: list[LapSpec] = [
    LapSpec(0.80, outlap=True, tyre_set=0),
    LapSpec(0.970, tyre_set=0),
    LapSpec(1.000, tyre_set=0),   # fastest valid lap
    LapSpec(0.995, tyre_set=0),   # second-fastest valid lap
    LapSpec(0.990, valid=False, tyre_set=0),  # invalidated (off-track)
    LapSpec(0.960, tyre_set=1),   # fresh tyres -> new stint
    LapSpec(0.985, tyre_set=1),
    LapSpec(0.780, inlap=True, tyre_set=1),
]

# Corner apexes as (normalised lap position, apex speed m/s, steer direction).
_CORNERS: tuple[tuple[float, float, int], ...] = (
    (0.04, 30.0, +1),
    (0.13, 48.0, -1),
    (0.22, 24.0, +1),
    (0.34, 58.0, -1),
    (0.46, 33.0, +1),
    (0.58, 42.0, -1),
    (0.70, 27.0, +1),
    (0.82, 52.0, -1),
    (0.93, 38.0, +1),
)
_TOP_SPEED = 80.0  # m/s, ~288 km/h
_CORNER_WIDTH = 0.018


def _corner_weight(norm: float, apex_norm: float) -> float:
    d = abs(norm - apex_norm)
    d = min(d, 1.0 - d)  # wrap-around distance on the closed lap
    return math.exp(-(d * d) / (2.0 * _CORNER_WIDTH * _CORNER_WIDTH))


def _base_speed(norm: float) -> float:
    """Unpaced target speed (m/s) at a normalised lap position."""
    s = _TOP_SPEED
    for apex_norm, apex_speed, _dir in _CORNERS:
        s += (apex_speed - _TOP_SPEED) * _corner_weight(norm, apex_norm)
    return max(18.0, s)


def _steer(norm: float) -> float:
    """Steering angle (rad-ish, ~[-0.5, 0.5]); slower corners steer harder."""
    val = 0.0
    for apex_norm, apex_speed, direction in _CORNERS:
        intensity = 1.0 - apex_speed / _TOP_SPEED
        val += direction * intensity * _corner_weight(norm, apex_norm)
    return max(-1.0, min(1.0, val)) * 0.5


def _throttle_brake(norm: float) -> tuple[float, float]:
    ahead = (norm + 0.004) % 1.0
    grad = _base_speed(ahead) - _base_speed(norm)  # m/s over ~0.4% of the lap
    if grad >= 0.0:
        return min(1.0, 0.35 + grad * 0.5), 0.0
    return 0.0, min(1.0, -grad * 0.45)


def _sector_of(norm: float, sector_count: int) -> int:
    return min(sector_count - 1, int(norm * sector_count))


class SyntheticACCSource(TelemetrySource):
    """A reproducible fake ACC session. See module docstring."""

    native_hz = 333.0

    def __init__(
        self,
        *,
        seed: int = 42,
        laps: list[LapSpec] | None = None,
        track_code: str = "spa",
        car_code: str = "mclaren_720s_gt3",
        track_length_m: float = 7004.0,
        sector_count: int = 3,
        started_at_utc: str = "2026-05-23T14:00:00+00:00",
        start_fuel_kg: float = 50.0,
        native_hz: float = 333.0,
    ) -> None:
        self.seed = seed
        self.laps = laps if laps is not None else DEFAULT_LAPS
        self.track_code = track_code
        self.car_code = car_code
        self.track_length_m = track_length_m
        self.sector_count = sector_count
        self.started_at_utc = started_at_utc
        self.start_fuel_kg = start_fuel_kg
        self.native_hz = native_hz
        # Burn ~22 kg of fuel across the whole session.
        self.fuel_per_m = 22.0 / (len(self.laps) * track_length_m)

    def static_info(self) -> StaticInfo:
        return StaticInfo(
            game="acc",
            track_code=self.track_code,
            car_code=self.car_code,
            started_at_utc=self.started_at_utc,
            sector_count=self.sector_count,
            track_length_m=self.track_length_m,
            session_type="practice",
            air_temp_c=24.0,
            road_temp_c=30.0,
        )

    def _values(
        self,
        *,
        dist: float,
        norm: float,
        speed_ms: float,
        throttle: float,
        brake: float,
        steer: float,
        laps_on_tyres: int,
        fuel: float,
    ) -> dict[str, float]:
        speed_frac = speed_ms / _TOP_SPEED
        gear = max(1, min(6, int(speed_frac * 6) + 1))
        rpm = int(max(4000.0, min(7800.0, 4200.0 + speed_frac * 4200.0)))

        # Tyre temps: warm up across the stint and the lap, plus cornering load.
        load = brake * 8.0 + abs(steer) * 10.0
        base_t = 72.0 + laps_on_tyres * 3.5 + norm * 2.0 + load
        left_load = max(0.0, -steer) * 5.0
        right_load = max(0.0, steer) * 5.0
        t_fl = base_t + 3.0 + left_load
        t_fr = base_t + 3.0 + right_load
        t_rl = base_t + left_load * 0.8
        t_rr = base_t + right_load * 0.8

        def press(temp: float) -> float:
            return 26.0 + (temp - 75.0) * 0.04

        theta = 2.0 * math.pi * norm
        radius = self.track_length_m / (2.0 * math.pi)

        return {
            "lap_distance_m": dist,
            "throttle": throttle,
            "brake": brake,
            "clutch": 1.0,
            "steering_angle": steer,
            "gear": gear,
            "rpm": rpm,
            "speed_kmh": speed_ms * 3.6,
            "world_x": radius * math.cos(theta),
            "world_y": 5.0 * math.sin(theta * 3.0),
            "world_z": radius * math.sin(theta),
            "tire_temp_fl": t_fl,
            "tire_temp_fr": t_fr,
            "tire_temp_rl": t_rl,
            "tire_temp_rr": t_rr,
            "tire_pressure_fl": press(t_fl),
            "tire_pressure_fr": press(t_fr),
            "tire_pressure_rl": press(t_rl),
            "tire_pressure_rr": press(t_rr),
            "g_lat": steer * 2.0 + math.copysign(1.0 - speed_frac, steer) * 1.5,
            "g_lon": throttle * 1.1 - brake * 1.8,
            "fuel_kg": fuel,
        }

    def frames(self) -> Iterator[CanonicalFrame]:
        dt = 1.0 / self.native_hz
        length = self.track_length_m
        rng = random.Random(self.seed)
        t = 0.0
        dist = 0.0
        fuel = self.start_fuel_kg
        lap_idx = 0
        laps_on_tyres = 0
        prev_tyre = self.laps[0].tyre_set

        while lap_idx < len(self.laps):
            spec = self.laps[lap_idx]
            norm = dist / length
            sector = _sector_of(norm, self.sector_count)
            throttle, brake = _throttle_brake(norm)
            steer = _steer(norm)
            speed = max(8.0, _base_speed(norm) * spec.pace + rng.uniform(-0.25, 0.25))
            in_pit = (spec.outlap and dist < 250.0) or (spec.inlap and dist > length - 250.0)

            yield CanonicalFrame(
                t=t,
                values=self._values(
                    dist=dist, norm=norm, speed_ms=speed, throttle=throttle,
                    brake=brake, steer=steer, laps_on_tyres=laps_on_tyres, fuel=fuel,
                ),
                lap_count=lap_idx,
                sector_index=sector,
                lap_valid=spec.valid,
                in_pit=in_pit,
                tyre_set=spec.tyre_set,
            )

            ds = speed * dt
            dist += ds
            t += dt
            fuel = max(0.5, fuel - ds * self.fuel_per_m)
            if dist >= length:
                dist -= length
                lap_idx += 1
                if lap_idx < len(self.laps):
                    new_tyre = self.laps[lap_idx].tyre_set
                    laps_on_tyres = 0 if new_tyre != prev_tyre else laps_on_tyres + 1
                    prev_tyre = new_tyre

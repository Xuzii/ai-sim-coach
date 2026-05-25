"""Canonical channel schema -- the cross-game translation layer.

Every telemetry source (ACC now, iRacing in v0.2) maps its native field names
*into* these canonical names before anything else in the pipeline touches the
data. Defining this module first is a deliberate architecture decision: the
schema is the contract that keeps storage, the ingest pipeline, and the MCP
tools game-agnostic. ACC says ``gas``; iRacing says ``Throttle``; both become
``throttle`` here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Channel:
    """One canonical telemetry channel.

    ``dtype`` values are pyarrow/numpy-compatible strings so the storage layer
    can build Parquet schemas directly from this table without a second source
    of truth.
    """

    name: str
    dtype: str
    unit: str
    description: str


# Ordered canonical schema. The order is stable and load-bearing: it defines the
# column order of every per-lap Parquet trace. Treat this as append-only --
# never reorder, because existing trace files are positional on disk.
CANONICAL_CHANNELS: tuple[Channel, ...] = (
    Channel(
        "lap_distance_m",
        "float32",
        "m",
        "Distance travelled along the lap from the start/finish line. This is the "
        "distance bin key for traces; monotonically increasing within a single lap.",
    ),
    Channel("throttle", "float32", "fraction", "Throttle application, 0.0 (off) to 1.0 (full)."),
    Channel("brake", "float32", "fraction", "Brake application, 0.0 (off) to 1.0 (full)."),
    Channel("clutch", "float32", "fraction", "Clutch engagement, 0.0 (disengaged) to 1.0 (engaged)."),
    Channel("steering_angle", "float32", "rad", "Steering wheel angle in radians; negative = left, positive = right."),
    Channel("gear", "int8", "gear", "Selected gear for display: -1 = reverse, 0 = neutral, 1..n = forward gears."),
    Channel("rpm", "int32", "rpm", "Engine speed in revolutions per minute."),
    Channel("speed_kmh", "float32", "km/h", "Vehicle ground speed in kilometres per hour."),
    Channel("world_x", "float32", "m", "World-space X coordinate of the car."),
    Channel("world_y", "float32", "m", "World-space Y coordinate (vertical axis in ACC)."),
    Channel("world_z", "float32", "m", "World-space Z coordinate of the car."),
    Channel("tire_temp_fl", "float32", "degC", "Front-left tyre core temperature."),
    Channel("tire_temp_fr", "float32", "degC", "Front-right tyre core temperature."),
    Channel("tire_temp_rl", "float32", "degC", "Rear-left tyre core temperature."),
    Channel("tire_temp_rr", "float32", "degC", "Rear-right tyre core temperature."),
    Channel("tire_pressure_fl", "float32", "psi", "Front-left tyre pressure."),
    Channel("tire_pressure_fr", "float32", "psi", "Front-right tyre pressure."),
    Channel("tire_pressure_rl", "float32", "psi", "Rear-left tyre pressure."),
    Channel("tire_pressure_rr", "float32", "psi", "Rear-right tyre pressure."),
    Channel("g_lat", "float32", "g", "Lateral acceleration in g; positive = right (shares sign with steering_angle)."),
    Channel("g_lon", "float32", "g", "Longitudinal acceleration in g; positive = accelerating, negative = braking."),
    Channel("fuel_kg", "float32", "kg", "Fuel mass remaining."),
)

CHANNEL_NAMES: tuple[str, ...] = tuple(c.name for c in CANONICAL_CHANNELS)
CHANNEL_BY_NAME: dict[str, Channel] = {c.name: c for c in CANONICAL_CHANNELS}

# The distance bin key (a canonical channel) and the time index the pipeline
# adds to every trace. ``t_ms`` is bookkeeping, not a measured channel, so it
# lives outside CANONICAL_CHANNELS but is still a Parquet column.
DISTANCE_KEY = "lap_distance_m"
TIME_KEY = "t_ms"

# Convenience groupings. WHEEL_ORDER is the canonical wheel-array index order
# (front-left, front-right, rear-left, rear-right) that game readers map onto.
WHEEL_ORDER: tuple[str, ...] = ("fl", "fr", "rl", "rr")
TIRE_TEMP_CHANNELS: tuple[str, ...] = tuple(f"tire_temp_{w}" for w in WHEEL_ORDER)
TIRE_PRESSURE_CHANNELS: tuple[str, ...] = tuple(f"tire_pressure_{w}" for w in WHEEL_ORDER)


def trace_columns() -> tuple[str, ...]:
    """Column order for a per-lap Parquet trace: distance key, time key, then
    the remaining canonical channels in schema order."""
    return (DISTANCE_KEY, TIME_KEY) + tuple(n for n in CHANNEL_NAMES if n != DISTANCE_KEY)


def dtype_for(name: str) -> str:
    """Return the pyarrow/numpy dtype string for any trace column, including the
    pipeline-added time key."""
    if name == TIME_KEY:
        return "int32"
    return CHANNEL_BY_NAME[name].dtype


# Fail fast at import if the schema is internally inconsistent. Cheap insurance
# against a careless edit (duplicate name, missing distance key).
assert len(CHANNEL_NAMES) == len(set(CHANNEL_NAMES)), "duplicate canonical channel name"
assert DISTANCE_KEY in CHANNEL_BY_NAME, "distance key must be a canonical channel"

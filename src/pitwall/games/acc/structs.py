"""ctypes layouts for ACC's three shared-memory pages.

This is the highest-risk module in the ACC reader: a single wrong field width or
a missing entry shifts every subsequent offset and decodes garbage. The layouts
below follow the Kunos-documented ACC Shared Memory (cross-checked against the
``rrennoir/PyAccSharedMemory`` port and validated byte-for-byte against a real
captured session -- Nurburgring / Ford Mustang GT3). Module-level ``assert``\\s on
``sizeof`` and the field offsets we actually read fail the import the instant the
layout drifts, the same fail-fast guard :mod:`pitwall.channels` uses.

**String fields are raw UTF-16-LE bytes (``c_char * 2N``), not ``c_wchar * N``.**
``c_wchar`` is 2 bytes on Windows but 4 on Linux, which would change ``sizeof``
and the offsets off-Windows and break this module's import (and CI) on any
non-Windows box. A byte array is platform-identical -- same offsets, same size
everywhere -- and a string field's alignment never shifts the following field
(those are all 4-aligned ints/floats), so the layout is byte-identical to the
Windows ``c_wchar`` one. We decode with :func:`wstr`.

All three pages use ``_pack_ = 4`` (ACC's ``#pragma pack(4)``). Physics and
graphics start with ``int packetId``; static starts with ``smVersion``.
"""

from __future__ import annotations

import ctypes
from enum import IntEnum

c_int = ctypes.c_int32
c_float = ctypes.c_float


def _char(n_chars: int) -> type[ctypes.Array]:
    """A UTF-16-LE string field of ``n_chars`` wide chars, as ``2*n_chars`` bytes.

    Uses ``c_ubyte`` rather than ``c_char``: a ``c_char`` array auto-truncates at
    the first NUL on attribute access, which for UTF-16-LE (``n\\x00u\\x00...``)
    would return just the first byte. ``c_ubyte`` hands back the raw array, and
    :func:`wstr` decodes the full field.
    """
    return ctypes.c_ubyte * (2 * n_chars)


def wstr(field: ctypes.Array | bytes) -> str:
    """Decode a UTF-16-LE wchar field to a Python str, trimming at the first NUL."""
    return bytes(field).decode("utf-16-le", errors="replace").split("\x00", 1)[0]


# --------------------------------------------------------------------------- #
# Enums (the int-coded fields we read)
# --------------------------------------------------------------------------- #
class AcStatus(IntEnum):
    OFF = 0
    REPLAY = 1
    LIVE = 2
    PAUSE = 3


class AcSessionType(IntEnum):
    UNKNOWN = -1
    PRACTICE = 0
    QUALIFY = 1
    RACE = 2
    HOTLAP = 3
    TIME_ATTACK = 4
    DRIFT = 5
    DRAG = 6
    HOTSTINT = 7
    HOTSTINTSUPERPOLE = 8


_SESSION_TYPE_NAMES = {
    AcSessionType.PRACTICE: "practice",
    AcSessionType.QUALIFY: "qualify",
    AcSessionType.RACE: "race",
    AcSessionType.HOTLAP: "hotlap",
    AcSessionType.TIME_ATTACK: "time_attack",
    AcSessionType.DRIFT: "drift",
    AcSessionType.DRAG: "drag",
    AcSessionType.HOTSTINT: "hotstint",
    AcSessionType.HOTSTINTSUPERPOLE: "hotstint_superpole",
}


def session_type_name(session: int) -> str | None:
    """Lowercase session-type label (matching the synthetic source), or None."""
    try:
        return _SESSION_TYPE_NAMES.get(AcSessionType(session))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Physics page (~333 Hz) -- sizeof 800
# --------------------------------------------------------------------------- #
class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int),
        ("gas", c_float),
        ("brake", c_float),
        ("fuel", c_float),
        ("gear", c_int),
        ("rpms", c_int),
        ("steerAngle", c_float),
        ("speedKmh", c_float),
        ("velocity", c_float * 3),
        ("accG", c_float * 3),
        ("wheelSlip", c_float * 4),
        ("wheelLoad", c_float * 4),
        ("wheelsPressure", c_float * 4),
        ("wheelAngularSpeed", c_float * 4),
        ("tyreWear", c_float * 4),
        ("tyreDirtyLevel", c_float * 4),
        ("tyreCoreTemperature", c_float * 4),
        ("camberRAD", c_float * 4),
        ("suspensionTravel", c_float * 4),
        ("drs", c_float),
        ("tc", c_float),
        ("heading", c_float),
        ("pitch", c_float),
        ("roll", c_float),
        ("cgHeight", c_float),
        ("carDamage", c_float * 5),
        ("numberOfTyresOut", c_int),
        ("pitLimiterOn", c_int),
        ("abs", c_float),
        ("kersCharge", c_float),
        ("kersInput", c_float),
        ("autoshifterOn", c_int),
        ("rideHeight", c_float * 2),
        ("turboBoost", c_float),
        ("ballast", c_float),
        ("airDensity", c_float),
        ("airTemp", c_float),
        ("roadTemp", c_float),
        ("localAngularVel", c_float * 3),
        ("finalFF", c_float),
        ("performanceMeter", c_float),
        ("engineBrake", c_int),
        ("ersRecoveryLevel", c_int),
        ("ersPowerLevel", c_int),
        ("ersHeatCharging", c_int),
        ("ersIsCharging", c_int),
        ("kersCurrentKJ", c_float),
        ("drsAvailable", c_int),
        ("drsEnabled", c_int),
        ("brakeTemp", c_float * 4),
        ("clutch", c_float),
        ("tyreTempI", c_float * 4),
        ("tyreTempM", c_float * 4),
        ("tyreTempO", c_float * 4),
        ("isAIControlled", c_int),
        ("tyreContactPoint", (c_float * 3) * 4),
        ("tyreContactNormal", (c_float * 3) * 4),
        ("tyreContactHeading", (c_float * 3) * 4),
        ("brakeBias", c_float),
        ("localVelocity", c_float * 3),
        ("P2PActivation", c_int),
        ("P2PStatus", c_int),
        ("currentMaxRpm", c_int),
        ("mz", c_float * 4),
        ("fz", c_float * 4),
        ("my", c_float * 4),
        ("slipRatio", c_float * 4),
        ("slipAngle", c_float * 4),
        ("tcinAction", c_int),
        ("absInAction", c_int),
        ("suspensionDamage", c_float * 4),
        ("tyreTemp", c_float * 4),
        ("waterTemp", c_float),
        ("brakePressure", c_float * 4),
        ("frontBrakeCompound", c_int),
        ("rearBrakeCompound", c_int),
        ("padLife", c_float * 4),
        ("discLife", c_float * 4),
        ("ignitionOn", c_int),
        ("starterEngineOn", c_int),
        ("isEngineRunning", c_int),
        ("kerbVibration", c_float),
        ("slipVibrations", c_float),
        ("gVibrations", c_float),
        ("absVibrations", c_float),
    ]


# --------------------------------------------------------------------------- #
# Graphics page (~30-60 Hz) -- sizeof 1588
# --------------------------------------------------------------------------- #
class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int),
        ("status", c_int),
        ("session", c_int),
        ("currentTime", _char(15)),
        ("lastTime", _char(15)),
        ("bestTime", _char(15)),
        ("split", _char(15)),
        ("completedLaps", c_int),
        ("position", c_int),
        ("iCurrentTime", c_int),
        ("iLastTime", c_int),
        ("iBestTime", c_int),
        ("sessionTimeLeft", c_float),
        ("distanceTraveled", c_float),
        ("isInPit", c_int),
        ("currentSectorIndex", c_int),
        ("lastSectorTime", c_int),
        ("numberOfLaps", c_int),
        ("tyreCompound", _char(33)),
        ("replayTimeMultiplier", c_float),
        ("normalizedCarPosition", c_float),
        ("activeCars", c_int),
        ("carCoordinates", (c_float * 3) * 60),
        ("carID", c_int * 60),
        ("playerCarID", c_int),
        ("penaltyTime", c_float),
        ("flag", c_int),
        ("penalty", c_int),
        ("idealLineOn", c_int),
        ("isInPitLane", c_int),
        ("surfaceGrip", c_float),
        ("mandatoryPitDone", c_int),
        ("windSpeed", c_float),
        ("windDirection", c_float),
        ("isSetupMenuVisible", c_int),
        ("mainDisplayIndex", c_int),
        ("secondaryDisplayIndex", c_int),
        ("TC", c_int),
        ("TCCUT", c_int),
        ("EngineMap", c_int),
        ("ABS", c_int),
        ("fuelXLap", c_float),
        ("rainLights", c_int),
        ("flashingLights", c_int),
        ("lightStage", c_int),
        ("exhaustTemperature", c_float),
        ("wiperStage", c_int),
        ("driverStintTotalTimeLeft", c_int),
        ("driverStintTimeLeft", c_int),
        ("rainTyres", c_int),
        ("sessionIndex", c_int),
        ("usedFuel", c_float),
        ("deltaLapTime", _char(15)),
        ("ideltaLapTime", c_int),
        ("estimatedLapTime", _char(15)),
        ("iestimatedLapTime", c_int),
        ("isDeltaPositive", c_int),
        ("iSplit", c_int),
        ("isValidLap", c_int),
        ("fuelEstimatedLaps", c_float),
        ("trackStatus", _char(33)),
        ("missingMandatoryPits", c_int),
        ("Clock", c_float),
        ("directionLightsLeft", c_int),
        ("directionLightsRight", c_int),
        ("GlobalYellow", c_int),
        ("GlobalYellow1", c_int),
        ("GlobalYellow2", c_int),
        ("GlobalYellow3", c_int),
        ("GlobalWhite", c_int),
        ("GlobalGreen", c_int),
        ("GlobalChequered", c_int),
        ("GlobalRed", c_int),
        ("mfdTyreSet", c_int),
        ("mfdFuelToAdd", c_float),
        ("mfdTyrePressure", c_float * 4),
        ("trackGripStatus", c_int),
        ("rainIntensity", c_int),
        ("rainIntensityIn10min", c_int),
        ("rainIntensityIn30min", c_int),
        ("currentTyreSet", c_int),
        ("strategyTyreSet", c_int),
        ("gapAhead", c_int),
        ("gapBehind", c_int),
    ]


# Readable alias: the rest of the reader speaks "Graphics".
Graphics = SPageFileGraphic


# --------------------------------------------------------------------------- #
# Static page (per session) -- sizeof 820
# --------------------------------------------------------------------------- #
class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", _char(15)),
        ("acVersion", _char(15)),
        ("numberOfSessions", c_int),
        ("numCars", c_int),
        ("carModel", _char(33)),
        ("track", _char(33)),
        ("playerName", _char(33)),
        ("playerSurname", _char(33)),
        ("playerNick", _char(33)),
        ("sectorCount", c_int),
        ("maxTorque", c_float),
        ("maxPower", c_float),
        ("maxRpm", c_int),
        ("maxFuel", c_float),
        ("suspensionMaxTravel", c_float * 4),
        ("tyreRadius", c_float * 4),
        ("maxTurboBoost", c_float),
        ("deprecated_1", c_float),
        ("deprecated_2", c_float),
        ("penaltiesEnabled", c_int),
        ("aidFuelRate", c_float),
        ("aidTireRate", c_float),
        ("aidMechanicalDamage", c_float),
        ("allowTyreBlankets", c_int),
        ("aidStability", c_float),
        ("aidAutoClutch", c_int),
        ("aidAutoBlip", c_int),
        ("hasDRS", c_int),
        ("hasERS", c_int),
        ("hasKERS", c_int),
        ("kersMaxJ", c_float),
        ("engineBrakeSettingsCount", c_int),
        ("ersPowerControllerCount", c_int),
        ("trackSPlineLength", c_float),
        ("trackConfiguration", _char(33)),
        ("ersMaxJ", c_float),
        ("isTimedRace", c_int),
        ("hasExtraLap", c_int),
        ("carSkin", _char(33)),
        ("reversedGridPositions", c_int),
        ("PitWindowStart", c_int),
        ("PitWindowEnd", c_int),
        ("isOnline", c_int),
        ("dryTyresName", _char(33)),
        ("wetTyresName", _char(33)),
    ]


# Player-identity fields scrubbed before a recording becomes a committed fixture.
# (offset, byte length) within the static page.
PLAYER_IDENTITY_FIELDS = ("playerName", "playerSurname", "playerNick")


def parse_physics(buf: bytes) -> SPageFilePhysics:
    """Parse the physics page from raw bytes (slicing off any page padding)."""
    n = ctypes.sizeof(SPageFilePhysics)
    _require_len(buf, n, "physics")
    return SPageFilePhysics.from_buffer_copy(buf[:n])


def parse_graphics(buf: bytes) -> SPageFileGraphic:
    """Parse the graphics page from raw bytes (slicing off any page padding)."""
    n = ctypes.sizeof(SPageFileGraphic)
    _require_len(buf, n, "graphics")
    return SPageFileGraphic.from_buffer_copy(buf[:n])


def parse_static(buf: bytes) -> SPageFileStatic:
    """Parse the static page from raw bytes (slicing off any page padding)."""
    n = ctypes.sizeof(SPageFileStatic)
    _require_len(buf, n, "static")
    return SPageFileStatic.from_buffer_copy(buf[:n])


def _require_len(buf: bytes, n: int, name: str) -> None:
    if len(buf) < n:
        raise ValueError(f"{name} page too short: got {len(buf)} bytes, need {n}")


# Fail fast at import if the layout drifts. Sizes are platform-independent because
# string fields are byte arrays (see module docstring). The offset checks pin the
# fields we actually read -- the rest of the struct can be padding for all we care.
assert ctypes.sizeof(SPageFilePhysics) == 800, ctypes.sizeof(SPageFilePhysics)
assert ctypes.sizeof(SPageFileGraphic) == 1588, ctypes.sizeof(SPageFileGraphic)
assert ctypes.sizeof(SPageFileStatic) == 820, ctypes.sizeof(SPageFileStatic)

assert SPageFilePhysics.gas.offset == 4
assert SPageFilePhysics.accG.offset == 44
assert SPageFilePhysics.wheelsPressure.offset == 88
assert SPageFilePhysics.tyreCoreTemperature.offset == 152
assert SPageFilePhysics.airTemp.offset == 288
assert SPageFilePhysics.clutch.offset == 364

assert SPageFileGraphic.completedLaps.offset == 132
assert SPageFileGraphic.currentSectorIndex.offset == 164
assert SPageFileGraphic.normalizedCarPosition.offset == 248
assert SPageFileGraphic.carCoordinates.offset == 256
assert SPageFileGraphic.playerCarID.offset == 1216
assert SPageFileGraphic.isInPitLane.offset == 1236
assert SPageFileGraphic.isValidLap.offset == 1408
assert SPageFileGraphic.currentTyreSet.offset == 1572

assert SPageFileStatic.carModel.offset == 68
assert SPageFileStatic.track.offset == 134
assert SPageFileStatic.playerName.offset == 200
assert SPageFileStatic.sectorCount.offset == 400
assert SPageFileStatic.trackSPlineLength.offset == 520

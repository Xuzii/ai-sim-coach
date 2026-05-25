"""The ACC ctypes layout safety net: sizes, the offsets we read, round-trips.

These run without the game and without a recording -- a handcrafted byte buffer
written at known offsets must round-trip back through the structs. A failure here
means a field width or order drifted, which would silently decode garbage live.
"""

from __future__ import annotations

import ctypes
import struct

import pytest

from pitwall.games.acc import structs as s


def test_struct_sizes():
    assert ctypes.sizeof(s.SPageFilePhysics) == 800
    assert ctypes.sizeof(s.SPageFileGraphic) == 1588
    assert ctypes.sizeof(s.SPageFileStatic) == 820


def test_pack_is_four():
    assert s.SPageFilePhysics._pack_ == 4
    assert s.SPageFileGraphic._pack_ == 4
    assert s.SPageFileStatic._pack_ == 4


def test_physics_read_offsets():
    assert s.SPageFilePhysics.packetId.offset == 0
    assert s.SPageFilePhysics.gas.offset == 4
    assert s.SPageFilePhysics.brake.offset == 8
    assert s.SPageFilePhysics.fuel.offset == 12
    assert s.SPageFilePhysics.gear.offset == 16
    assert s.SPageFilePhysics.rpms.offset == 20
    assert s.SPageFilePhysics.speedKmh.offset == 28
    assert s.SPageFilePhysics.accG.offset == 44
    assert s.SPageFilePhysics.wheelsPressure.offset == 88
    assert s.SPageFilePhysics.tyreCoreTemperature.offset == 152
    assert s.SPageFilePhysics.airTemp.offset == 288
    assert s.SPageFilePhysics.roadTemp.offset == 292
    assert s.SPageFilePhysics.clutch.offset == 364


def test_graphics_read_offsets():
    assert s.SPageFileGraphic.packetId.offset == 0
    assert s.SPageFileGraphic.status.offset == 4
    assert s.SPageFileGraphic.session.offset == 8
    assert s.SPageFileGraphic.completedLaps.offset == 132
    assert s.SPageFileGraphic.currentSectorIndex.offset == 164
    assert s.SPageFileGraphic.normalizedCarPosition.offset == 248
    assert s.SPageFileGraphic.carCoordinates.offset == 256
    assert s.SPageFileGraphic.playerCarID.offset == 1216
    assert s.SPageFileGraphic.isInPitLane.offset == 1236
    assert s.SPageFileGraphic.isValidLap.offset == 1408
    assert s.SPageFileGraphic.currentTyreSet.offset == 1572


def test_static_read_offsets():
    assert s.SPageFileStatic.carModel.offset == 68
    assert s.SPageFileStatic.track.offset == 134
    assert s.SPageFileStatic.playerName.offset == 200
    assert s.SPageFileStatic.sectorCount.offset == 400
    assert s.SPageFileStatic.maxRpm.offset == 412
    assert s.SPageFileStatic.trackSPlineLength.offset == 520


def test_physics_round_trip():
    buf = bytearray(ctypes.sizeof(s.SPageFilePhysics))
    struct.pack_into("<i", buf, s.SPageFilePhysics.packetId.offset, 4242)
    struct.pack_into("<f", buf, s.SPageFilePhysics.gas.offset, 0.75)
    struct.pack_into("<f", buf, s.SPageFilePhysics.brake.offset, 0.25)
    struct.pack_into("<f", buf, s.SPageFilePhysics.clutch.offset, 1.0)
    struct.pack_into("<i", buf, s.SPageFilePhysics.gear.offset, 3)
    struct.pack_into("<f", buf, s.SPageFilePhysics.speedKmh.offset, 211.5)
    struct.pack_into("<3f", buf, s.SPageFilePhysics.accG.offset, 1.0, 2.0, 3.0)
    struct.pack_into("<4f", buf, s.SPageFilePhysics.tyreCoreTemperature.offset, 80.0, 81.0, 82.0, 83.0)
    struct.pack_into("<f", buf, s.SPageFilePhysics.fuel.offset, 50.0)

    p = s.parse_physics(bytes(buf))
    assert p.packetId == 4242
    assert p.gas == pytest.approx(0.75)
    assert p.brake == pytest.approx(0.25)
    assert p.gear == 3
    assert p.speedKmh == pytest.approx(211.5)
    assert list(p.accG) == [1.0, 2.0, 3.0]
    assert list(p.tyreCoreTemperature) == [80.0, 81.0, 82.0, 83.0]
    assert p.fuel == pytest.approx(50.0)


def test_static_wchar_round_trip():
    buf = bytearray(ctypes.sizeof(s.SPageFileStatic))
    enc = "nurburgring".encode("utf-16-le")
    off = s.SPageFileStatic.track.offset
    buf[off : off + len(enc)] = enc
    struct.pack_into("<i", buf, s.SPageFileStatic.sectorCount.offset, 3)
    struct.pack_into("<f", buf, s.SPageFileStatic.trackSPlineLength.offset, 5783.0)

    st = s.parse_static(bytes(buf))
    assert s.wstr(st.track) == "nurburgring"
    assert st.sectorCount == 3
    assert st.trackSPlineLength == pytest.approx(5783.0)


def test_parse_rejects_short_buffer():
    with pytest.raises(ValueError, match="physics"):
        s.parse_physics(b"\x00" * 16)


def test_session_type_name():
    assert s.session_type_name(0) == "practice"
    assert s.session_type_name(1) == "qualify"
    assert s.session_type_name(2) == "race"
    assert s.session_type_name(3) == "hotlap"
    assert s.session_type_name(999) is None

"""Low-level access to ACC's three shared-memory pages.

ACC publishes telemetry as three named, page-file-backed memory mappings that
exist only while the game is running on the same machine:

* ``Local\\acpmf_physics``  -- per-tick physics (~333 Hz)
* ``Local\\acpmf_graphics`` -- session/lap state (~30-60 Hz)
* ``Local\\acpmf_static``   -- per-session track/car/rules metadata

This module is deliberately struct-free: it opens a page and hands back its raw
bytes. Parsing those bytes into ctypes structs is the *next* C3 step, validated
offline against a recording -- nothing here can be wrong about field layout.

Two Windows gotchas shape the design:

1. Opening a tagname with :func:`mmap.mmap` and ``fileno=-1`` *creates* the
   mapping when it's absent, so a naive open would silently return a zero-filled
   buffer when ACC isn't running. We probe existence with ``OpenFileMappingW``
   first and raise :class:`GameNotRunningError` instead.
2. The exact page size varies between ACC versions. Rather than hardcode struct
   sizes we may get wrong, :func:`probe_size` finds the true size by binary
   search: Windows accepts any view length <= the real mapping and rejects
   anything larger, so the boundary *is* the size. This captures the whole page
   -- including the extended graphics fields -- regardless of game version.
"""

from __future__ import annotations

import ctypes
import mmap
import sys

PHYSICS = r"Local\acpmf_physics"
GRAPHICS = r"Local\acpmf_graphics"
STATIC = r"Local\acpmf_static"

#: Probe ceiling. Every ACC page is a few KiB; 64 KiB is comfortably above them.
_PROBE_MAX = 1 << 16

_IS_WINDOWS = sys.platform == "win32"


class GameNotRunningError(RuntimeError):
    """ACC's shared memory isn't available (game not launched / not in a session)."""


if _IS_WINDOWS:
    # Base ctypes types (not ctypes.wintypes) so this module imports anywhere;
    # WinDLL itself is only ever referenced on Windows.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _OpenFileMappingW = _kernel32.OpenFileMappingW
    _OpenFileMappingW.argtypes = (ctypes.c_uint32, ctypes.c_int32, ctypes.c_wchar_p)
    _OpenFileMappingW.restype = ctypes.c_void_p
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (ctypes.c_void_p,)
    _CloseHandle.restype = ctypes.c_int32
    _FILE_MAP_READ = 0x0004


def mapping_exists(name: str) -> bool:
    """Return True if a named page already exists (i.e. ACC created it).

    Uses ``OpenFileMappingW`` on purpose -- it opens *existing* objects only and
    never creates one, unlike :func:`mmap.mmap` with ``fileno=-1``.
    """
    if not _IS_WINDOWS:
        return False
    handle = _OpenFileMappingW(_FILE_MAP_READ, False, name)
    if not handle:
        return False
    _CloseHandle(handle)
    return True


def _try_open(name: str, size: int) -> mmap.mmap | None:
    """Open a read-only view of ``name`` at ``size`` bytes, or None if it won't map."""
    try:
        return mmap.mmap(-1, size, tagname=name, access=mmap.ACCESS_READ)
    except (OSError, ValueError):
        return None


def open_page(name: str, size: int) -> mmap.mmap:
    """Open a read-only view of an existing shared-memory page.

    Raises :class:`GameNotRunningError` when ACC isn't running, rather than
    silently creating an empty mapping (see module docstring).
    """
    if not _IS_WINDOWS:
        raise GameNotRunningError("ACC shared memory is only available on Windows.")
    if not mapping_exists(name):
        raise GameNotRunningError(
            f"shared-memory page '{name}' not found -- launch ACC and enter a session first."
        )
    mm = _try_open(name, size)
    if mm is None:
        raise GameNotRunningError(f"failed to map shared-memory page {name!r}.")
    return mm


def probe_size(name: str, *, max_size: int = _PROBE_MAX) -> int:
    """Return the exact byte size of an existing page.

    Finds the largest view length :func:`mmap.mmap` will accept: Windows maps any
    length <= the real mapping and rejects anything larger, so the boundary is the
    true size. If the page is somehow larger than ``max_size`` we return the
    ceiling rather than search unbounded.
    """
    if not mapping_exists(name):
        raise GameNotRunningError(
            f"shared-memory page '{name}' not found -- launch ACC and enter a session first."
        )

    def opens(n: int) -> bool:
        mm = _try_open(name, n)
        if mm is None:
            return False
        mm.close()
        return True

    if opens(max_size):
        return max_size
    lo, hi = 1, max_size
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if opens(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


class LivePages:
    """Snapshots ACC's three live pages as raw bytes.

    Sizes each page once on construction, then keeps the views open so each
    snapshot is a cheap copy. The default page provider for :mod:`capture`; tests
    inject a fake provider with the same surface instead.
    """

    def __init__(self) -> None:
        self._sizes = {
            "static": probe_size(STATIC),
            "physics": probe_size(PHYSICS),
            "graphics": probe_size(GRAPHICS),
        }
        self._mm = {
            "static": open_page(STATIC, self._sizes["static"]),
            "physics": open_page(PHYSICS, self._sizes["physics"]),
            "graphics": open_page(GRAPHICS, self._sizes["graphics"]),
        }

    def sizes(self) -> dict[str, int]:
        """Byte size of each page, keyed ``static``/``physics``/``graphics``."""
        return dict(self._sizes)

    def _read(self, kind: str) -> bytes:
        # A whole-page copy. ACC may write mid-read (~333 Hz); occasional tearing
        # is fine for raw capture and handled via packetId in the reader later.
        return bytes(self._mm[kind][: self._sizes[kind]])

    def static_bytes(self) -> bytes:
        return self._read("static")

    def physics_bytes(self) -> bytes:
        return self._read("physics")

    def graphics_bytes(self) -> bytes:
        return self._read("graphics")

    def close(self) -> None:
        for mm in self._mm.values():
            mm.close()
        self._mm.clear()

    def __enter__(self) -> LivePages:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

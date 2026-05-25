# Why distance-binning beats time-binning for coaching

**Thesis:** A coach reasons about *places on the track* ("you brake too early into
T1"), not wall-clock offsets. Binning telemetry by lap distance makes two laps
directly comparable at the same point on track, regardless of how long each took.

To capture while building:
- [ ] Concrete example: comparing two laps time-binned vs distance-binned, and why
      the time-binned comparison is misleading near a corner.
- [ ] Edge case write-up: `normalizedCarPosition` wrapping 1.0 → 0.0 at the line,
      and segmenting on sector index instead of distance to handle it.

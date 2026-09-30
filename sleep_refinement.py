"""Pure port of NOOP WakeMotionRefinement; no inferred step counts or actuation.

Source: reference/noop @ 7f396e98, StrandAnalytics/WakeMotionRefinement.swift.
Derived under PolyForm Noncommercial 1.0.0; Copyright 2026 NoopApp.
Segments use unix-second start/end and stage. Steps are actual device samples:
{ts: unix seconds, counter: u16, activity_class: int | None}.
"""
from collections import Counter, defaultdict
import math

SOURCE = "NOOP WakeMotionRefinement (7f396e98)"


def _minutes(start, end):
    return range(int(start) // 60, (int(end) - 1) // 60 + 1) if end > start else range(0)


def _density(rows, start, end, minimum):
    mins = _minutes(start, end)
    if not mins:
        return 0.0
    counts = Counter(int(row[0]) // 60 for row in rows)
    return sum(counts[m] >= minimum for m in mins) / len(mins)


def _steps(rows):
    # Reject estimated daily counts and rows missing the actual counter schema.
    out = []
    for row in rows:
        if not isinstance(row, dict) or "ts" not in row or "counter" not in row:
            continue
        ts, counter, cls = row["ts"], row["counter"], row.get("activity_class")
        if isinstance(ts, (int, float)) and math.isfinite(ts) and type(counter) is int and 0 <= counter <= 65535:
            out.append((int(ts), counter, cls))
    return sorted(out, key=lambda row: row[0])


def _variance(rows):
    if len(rows) < 2:
        return None
    means = [sum(row[i] for row in rows) / len(rows) for i in (1, 2, 3)]
    return sum(sum((row[i + 1] - means[i]) ** 2 for i in range(3)) for row in rows) / len(rows)


def apply(segments, gravity, steps, enabled=False):
    """Return refinement and whole-window coverage, preserving passthrough identity.

    Input segments should tile an ordered contiguous window, as in the source.
    A WHOOP4 stream lacking real StepSample records always passes through.
    Callers must recompute stage totals/efficiency if ``applied`` is true.
    """
    grav = [tuple(row) for row in gravity if len(row) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in row)]
    step_rows = _steps(steps)
    start, end = (segments[0]["start"], segments[-1]["end"]) if segments else (0, 0)
    coverage = {"dense_gravity_fraction": _density(grav, start, end, 2),
                "dense_step_fraction": _density(step_rows, start, end, 1)}
    def result(value, reason):
        return {"value": value, "applied": value != segments, "coverage": coverage,
                "source": SOURCE, "reason": reason}
    if enabled is not True:
        return result(segments, "disabled")
    if not segments or end <= start:
        return result(segments, "degenerate_window")
    if min(coverage.values()) < .80:
        return result(segments, "insufficient_motion_density")
    buckets = defaultdict(list)
    for row in grav:
        buckets[int(row[0]) // 60].append(row)
    ticks = defaultdict(int)
    for previous, current in zip(step_rows, step_rows[1:]):
        if current[2] in (1, 2):
            ticks[current[0] // 60] += (current[1] - previous[1]) & 0xffff
    out = []
    def append(piece):
        if out and out[-1]["stage"] == piece["stage"] and out[-1]["end"] == piece["start"]:
            out[-1]["end"] = piece["end"]
        else:
            out.append(dict(piece))
    for seg in segments:
        mins = _minutes(seg["start"], seg["end"])
        if str(seg["stage"]).strip().lower() not in ("wake", "awake") or seg["end"] - seg["start"] < 300:
            append(seg)
            continue
        consecutive, locomotion = 0, False
        for minute in mins:
            consecutive = consecutive + 1 if ticks[minute] >= 10 else 0
            if ticks[minute] >= 40 or consecutive >= 2:
                locomotion = True
                break
        bursts = {m for m in mins if (v := _variance(buckets[m])) is None or v >= .05}
        if locomotion or not mins or (len(mins) - len(bursts)) / len(mins) < .80:
            append(seg)
            continue
        keep = {m + delta for m in bursts for delta in (-1, 0, 1) if m + delta in mins}
        for index, minute in enumerate(mins):
            append({**seg, "start": seg["start"] if index == 0 else minute * 60,
                    "end": seg["end"] if index == len(mins) - 1 else (minute + 1) * 60,
                    "stage": "wake" if minute in keep else "light"})
    return result(out, "refined" if out != segments else "no_eligible_change")

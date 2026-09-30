"""Pure NOOP biofeedback ports; no clock, files, network, store or BLE actions.

Reference commit 7f396e98ed9d259df08e3a0a58cfac05fc70615c:
BreathPacer.swift, ResonanceEngine.swift, HRDownPacer.swift, LiveSessionEngine.swift,
PreSleepHeartRateFeedback.swift and Baselines.rollingMeanSD. Literal Swift test
oracles live in tests/test_biofeedback.py. Python API keys are snake_case.

Integration contract:
* make_bio_plan(options) -> (kind, title, phases). Phase duration is seconds.
  resonance: quick=True, seconds_per_pace=120; complete rounded breath cycles
  exactly match BiofeedbackController.startSweep. Phases carry pace_bpm and
  pace_index. Score actual ingested RR separately; scheduling never implies lock.
  hr-down: a guided timer envelope only; call hr_down_step on fresh smoothed HR
  for EVERY pulse interval, honor stop, and gate start on resting HR 55..120.
  live-session: the silent guardian, not a fabricated fixed breathing protocol.
  breathe/custom: bpm + inhale_fraction (or inhale_ratio/exhale_ratio).
* score_resonance([{bpm, start_ts, end_ts, rr:[{ts, rr_ms}]}]) -> scores, lock.
  RR timestamps are unix SECONDS, RR values milliseconds; stable duplicate order.
* LiveSessionEngine(config, start_ts).update(now, bpm) retains the exact state
  machine. Config keys resting_hr, hr_max, charge; no input sample means None.
* live_feedback(kind, observations, elapsed) is a stateless replay convenience:
  live-session observations={config, start_ts?, hr:[{ts,bpm}]}; elapsed is the
  session-relative seconds. Pass genuine observations, never repeat a held HR as
  if new. For long-running sessions use the class instead of replaying history.
  pre-sleep observations={enabled, day, sessions:[{start,end}], hr:[{ts,bpm}],
  history:[{day,mean_bpm}], journal_entries?, minimum_valid_samples?,
  pre_sleep_window_seconds?}; elapsed does not affect retrospective feedback.

LiveSessionEngine has no protocol catalog: it coaches a recovery-gated band.
The fixed guided breathing catalog remains the separately vendored
BreathProtocolCatalog, rather than being relabeled as new guardian protocols.
Windows timer-envelope duration limits are adapter bounds, not NOOP algorithm
thresholds. Cue decisions describe intent, never claim successful haptic delivery
or a therapeutic effect. Pre-sleep feedback has no recommendation/causal inference.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import statistics as stats

REFERENCE = "7f396e98ed9d259df08e3a0a58cfac05fc70615c"
FULL_SWEEP_PACES = (4.5, 5.0, 5.5, 6.0, 6.5, 7.0)
QUICK_SWEEP_PACES = (4.5, 5.5, 6.5)
HR_DOWN_DEFAULTS = dict(start_delta_bpm=3.0, max_delta_bpm=8.0,
                        delta_ramp_seconds=120.0, hr_floor_bpm=50.0,
                        recompute_seconds=15.0, calm_target_bpm=60.0,
                        max_duration_seconds=180.0)
INT_MIN, INT_MAX = -(2**63), 2**63-1


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(value, name):
    if not finite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _bounded(value, low, high, name):
    value = _number(value, name)
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _round(value):
    """Swift Double.rounded(): nearest, ties away from zero (not Python bankers)."""
    return math.floor(value+.5) if value >= 0 else math.ceil(value-.5)


def _clamp(value, low, high):
    return max(low, min(high, value))


def breath_schedule(bpm, inhale_fraction=.4, cycles=1):
    """Exact BreathPacer.schedule cue list, offsets in integer milliseconds."""
    if not isinstance(cycles, int) or isinstance(cycles, bool):
        raise ValueError("cycles must be an integer")
    if cycles < 1:
        return []
    if cycles > 100_000:
        raise ValueError("Schedule exceeds the bounded adapter limit")
    pace = _clamp(_number(bpm, "bpm"), 3.0, 12.0)
    fraction = _clamp(_number(inhale_fraction, "inhale_fraction"), .1, .9)
    cycle_ms = _round(60_000/pace)
    inhale_ms = _round(cycle_ms*fraction)
    return [dict(offset_ms=i*cycle_ms+offset, phase=phase, loops=loops)
            for i in range(cycles)
            for offset, phase, loops in ((0, "inhale", 1), (inhale_ms, "exhale", 2))]


def breath_duration_ms(bpm, cycles):
    if not isinstance(cycles, int) or isinstance(cycles, bool):
        raise ValueError("cycles must be an integer")
    return _round(60_000/_clamp(_number(bpm, "bpm"), 3.0, 12.0))*max(0, cycles)


def _paced_phases(bpm, fraction, cycles, pace_index=None):
    cues = breath_schedule(bpm, fraction, cycles)
    end = breath_duration_ms(bpm, cycles)
    phases = []
    for i, cue in enumerate(cues):
        stop = cues[i+1]["offset_ms"] if i+1 < len(cues) else end
        item = dict(phase=cue["phase"].capitalize(), type=cue["phase"],
                    duration=(stop-cue["offset_ms"])/1000, round=i//2+1,
                    pace_bpm=_clamp(bpm, 3., 12.))
        if pace_index is not None:
            item["pace_index"] = pace_index
        phases.append(item)
    return phases


def make_bio_plan(options):
    if not isinstance(options, dict):
        raise ValueError("Plan options must be an object")
    kind = options.get("kind", "resonance")
    if kind == "resonance":
        quick = options.get("quick", True)
        if not isinstance(quick, bool):
            raise ValueError("quick must be true or false")
        seconds = _bounded(options.get("seconds_per_pace", 120), 60, 600, "seconds_per_pace")
        fraction = _number(options.get("inhale_fraction", .4), "inhale_fraction")
        phases = []
        for index, bpm in enumerate(QUICK_SWEEP_PACES if quick else FULL_SWEEP_PACES):
            cycles = max(1, _round(seconds*bpm/60))
            phases.extend(_paced_phases(bpm, fraction, cycles, index))
        return kind, "Quick resonance sweep" if quick else "Full resonance sweep", phases
    if kind == "hr-down":
        config = _hr_down_config(options.get("config"))
        # No fixed bpm/pulses: the controller must ask the live HR step function.
        return kind, "Below-heart-rate relaxation", [dict(phase="Follow your live heart rate", type="guided",
                                                        duration=config["max_duration_seconds"], round=1)]
    if kind == "live-session":
        duration = _bounded(options.get("duration_minutes", 60), 1, 720, "duration_minutes")*60
        return kind, "Silent guardian", [dict(phase="Guard your heart-rate band", type="guided",
                                            duration=duration, round=1)]
    if kind in ("breathe", "breathe-custom"):
        bpm = _number(options.get("bpm", 6), "bpm")
        fraction = options.get("inhale_fraction")
        if fraction is None:
            inhale = _bounded(options.get("inhale_ratio", 2), .001, 1000, "inhale_ratio")
            exhale = _bounded(options.get("exhale_ratio", 3), .001, 1000, "exhale_ratio")
            fraction = inhale/(inhale+exhale)
        fraction = _number(fraction, "inhale_fraction")
        duration = _bounded(options.get("duration_minutes", 5), .1, 120, "duration_minutes")*60
        cycles = max(1, _round(duration*_clamp(bpm, 3., 12.)/60))
        return kind, "Custom paced breathing", _paced_phases(bpm, fraction, cycles)
    raise ValueError("Choose resonance, hr-down, live-session or breathe-custom")


def _clean_rr(rr):
    """HRVAnalyzer.cleanRR, including order-preserving Malik radius-2 filter."""
    ranged = [float(v) for v in rr if finite(v) and 300 <= v <= 2000]
    if len(ranged) <= 2:
        return ranged
    kept = []
    for i, value in enumerate(ranged):
        neighbors = [v for j, v in enumerate(ranged[max(0,i-2):i+3], max(0,i-2)) if j != i]
        if len(neighbors) < 2 or abs(value-stats.median(neighbors))/stats.median(neighbors) <= .20:
            kept.append(value)
    return kept


def _beat(row):
    if isinstance(row, dict):
        return row.get("ts", row.get("timestamp")), row.get("rr_ms", row.get("rrMs"))
    if isinstance(row, (list, tuple)) and len(row) == 2:
        return row[0], row[1]
    raise ValueError("RR beats must be {ts,rr_ms} or [seconds,milliseconds]")


def score_pace(sample):
    bpm = _number(sample.get("bpm"), "bpm")
    start = _number(sample.get("start_ts", sample.get("startTs")), "start_ts")
    end = _number(sample.get("end_ts", sample.get("endTs")), "end_ts")
    if end < start:
        raise ValueError("Pace end must follow start")
    cycle_sec = 60/max(bpm, 3.)
    window_start = start+30
    steady = sorted([_beat(r) for r in sample.get("rr", [])
                     if finite(_beat(r)[0]) and window_start <= _beat(r)[0] <= end], key=lambda r:r[0])
    clean = _clean_rr([v for _,v in steady])
    output = dict(bpm=bpm, rsa_amplitude=None, rmssd_ms=None, clean_beats=len(clean),
                  scored_cycles=0, scored=False, source="ResonanceEngine.swift")
    if len(clean) < 20:
        output["reason"] = "Requires 20 clean beats after the 30-second settling transient"
        return output
    repaired, si = [], 0
    for value in clean:
        while si < len(steady) and steady[si][1] != value:
            si += 1
        if si < len(steady):
            repaired.append((steady[si][0], value))
            si += 1
    output["rmssd_ms"] = math.sqrt(sum((clean[i]-clean[i-1])**2 for i in range(1,len(clean)))/(len(clean)-1))
    swings, cycle_hrs, cycle_idx = [], [], 0
    first_ts = repaired[0][0]
    for ts, rr in repaired:
        index = int((ts-first_ts)/cycle_sec)
        if index != cycle_idx:
            if len(cycle_hrs) >= 2:
                swings.append(max(cycle_hrs)-min(cycle_hrs))
            cycle_hrs, cycle_idx = [], index
        cycle_hrs.append(60_000/rr)
    if len(cycle_hrs) >= 2:
        swings.append(max(cycle_hrs)-min(cycle_hrs))
    output["scored_cycles"] = len(swings)
    if len(swings) < 3:
        output["reason"] = "Requires 3 breath cycles with at least 2 clean beats each"
        return output
    output.update(rsa_amplitude=sum(swings)/len(swings), scored=True, reason=None)
    return output


def score_resonance(pace_samples):
    scores = [score_pace(s) for s in pace_samples]
    scored = [s for s in scores if s["scored"]]
    locked = len(scored) >= 3
    best = max(scored, key=lambda s:(s["rsa_amplitude"], s["rmssd_ms"] or 0, -s["bpm"])) if locked else None
    return dict(scores=scores, locked_bpm=best["bpm"] if best else 5.5, did_lock=locked,
                reason=None if locked else "Fewer than 3 scored paces; 5.5 breaths/min is a fallback, not a lock",
                source="ResonanceEngine.swift", estimate=True,
                limits="PPG-derived RR estimates; a lock is session-specific and does not prove a haptic effect")


def _hr_down_config(config):
    if config is not None and not isinstance(config, dict):
        raise ValueError("HR-down config must be an object")
    unknown = set(config or {})-set(HR_DOWN_DEFAULTS)
    if unknown:
        raise ValueError("Unknown HR-down setting: "+", ".join(sorted(unknown)))
    result = {**HR_DOWN_DEFAULTS, **(config or {})}
    for key, value in result.items():
        result[key] = _number(value, key)
    # The pure Swift config is unrestricted; adapter validation protects execution
    # from nonfinite/negative timing and absurd floors without altering defaults.
    if not 0 <= result["start_delta_bpm"] <= result["max_delta_bpm"] <= 30:
        raise ValueError("Require 0 <= start delta <= maximum delta <= 30 bpm")
    if not 1 <= result["hr_floor_bpm"] <= 220 or not 1 <= result["calm_target_bpm"] <= 220:
        raise ValueError("HR-down floor and calm target must be 1–220 bpm")
    if result["delta_ramp_seconds"] < 0 or not 0 < result["recompute_seconds"] <= 300 or not 0 < result["max_duration_seconds"] <= 7200:
        raise ValueError("HR-down timing is outside adapter bounds")
    return result


def hr_down_step(current_hr, elapsed, config=None):
    cfg = _hr_down_config(config)
    if not finite(current_hr) or current_hr <= 0:
        return dict(interval_ms=None, stop=True, target_bpm=None, stop_reason="invalidHR")
    elapsed = _number(elapsed, "elapsed")
    if elapsed >= cfg["max_duration_seconds"]:
        return dict(interval_ms=None, stop=True, target_bpm=None, stop_reason="timeout")
    if current_hr <= cfg["calm_target_bpm"]:
        return dict(interval_ms=None, stop=True, target_bpm=None, stop_reason="settled")
    # Extra fail-closed Windows adapter guard for contradictory custom floors:
    # NOOP's unrestricted final max(floor, HR-1) would otherwise exceed HR.
    # Default/reference golden vectors remain identical.
    if cfg["hr_floor_bpm"] > current_hr-1:
        return dict(interval_ms=None, stop=True, target_bpm=None, stop_reason="invalidHR",
                    reason="The configured floor leaves no tempo below the current heart rate")
    ramp = cfg["delta_ramp_seconds"]
    delta = cfg["max_delta_bpm"] if ramp <= 0 else cfg["start_delta_bpm"]+(cfg["max_delta_bpm"]-cfg["start_delta_bpm"])*_clamp(elapsed,0,ramp)/ramp
    target = max(current_hr-delta, cfg["hr_floor_bpm"])
    target = min(target, current_hr)
    if target > current_hr-1:
        target = max(cfg["hr_floor_bpm"], current_hr-1)
    return dict(interval_ms=_round(60_000/target), stop=False, target_bpm=target,
                stop_reason=None, delta_bpm=delta)


def live_band(config):
    rest = _number(config.get("resting_hr", 60), "resting_hr")
    maximum = _number(config.get("hr_max", 190), "hr_max")
    charge = config.get("charge")
    fraction = .5 if charge is None else _clamp(_number(charge, "charge")/100, 0, 1)
    ceiling = .60+(.82-.60)*fraction
    floor = max(ceiling-.15, .40)
    reserve = max(maximum-rest, 1.)
    return dict(floor_bpm=rest+floor*reserve, ceiling_bpm=rest+ceiling*reserve,
                floor_pct_hrr=floor, ceiling_pct_hrr=ceiling)


class LiveSessionEngine:
    """Exact silent-guardian state machine; update once per real sample or time tick."""
    def __init__(self, config=None, start_ts=0):
        self.config = {"resting_hr":60., "hr_max":190., "charge":None, **(config or {})}
        self.base_band = live_band(self.config)
        self.start_ts = self.last_update_ts = _number(start_ts, "start_ts")
        self.buffer, self.smoothed_history = [], []
        self.last_valid_ts = self.last_accepted_bpm = None
        self.current_position, self.in_band_seconds = "inBand", 0.
        self.below_since = self.above_since = self.above_slow_since = None
        self.last_climb = self.last_push = self.last_ease = None
        self.ceiling_drift = 0.

    def current_band(self):
        band = dict(self.base_band)
        if self.ceiling_drift:
            band["ceiling_bpm"] += self.ceiling_drift
            band["ceiling_pct_hrr"] = (band["ceiling_bpm"]-self.config["resting_hr"])/max(self.config["hr_max"]-self.config["resting_hr"],1.)
        return band

    def update(self, now, bpm=None):
        now = _number(now, "now")
        gap = max(now-self.last_update_ts,0)
        arrived = False
        plausible = finite(bpm) and 25 <= bpm <= self.config["hr_max"]+5
        if plausible and self.last_valid_ts is not None and now-self.last_valid_ts <= 12 and abs(bpm-self.last_accepted_bpm)>45:
            plausible = False
        if plausible:
            self.buffer.append((now,bpm))
            self.last_valid_ts, self.last_accepted_bpm, arrived = now, bpm, True
        self.buffer = [(ts,v) for ts,v in self.buffer if ts>=now-12]
        smoothed = stats.median(v for _,v in self.buffer) if self.buffer else None
        since_valid = now-(self.last_valid_ts if self.last_valid_ts is not None else self.start_ts)
        band = self.current_band()
        def output(status, position, smoothed, cue=None):
            return dict(status=status, position=position, smoothed_bpm=smoothed, band=band,
                        in_band_seconds=self.in_band_seconds, sample_arrived=arrived, cue=cue)
        if since_valid>8 or smoothed is None:
            self.last_update_ts = now
            return output("stale", self.current_position, None)
        self.smoothed_history.append((now,smoothed))
        self.smoothed_history = [(ts,v) for ts,v in self.smoothed_history if ts>=now-17]
        past = next((v for ts,v in self.smoothed_history if now-ts>=15),None)
        if past is not None and smoothed-past>=8:
            self.last_climb = now
        if smoothed>band["ceiling_bpm"]+2:
            pos="above"
        elif smoothed<band["floor_bpm"]-2:
            pos="below"
        elif band["floor_bpm"]+2<=smoothed<=band["ceiling_bpm"]-2:
            pos="inBand"
        else:
            pos=self.current_position
        if pos=="below":
            if self.current_position!="below":self.below_since=now
            self.above_since=self.above_slow_since=None
        elif pos=="above":
            if self.current_position!="above":
                self.above_since=now
                self.above_slow_since=None if self.last_climb is not None and now-self.last_climb<=20 else now
            self.below_since=None
        else:
            self.below_since=self.above_since=self.above_slow_since=None
        if pos=="inBand":self.in_band_seconds+=min(gap,5)
        status="warmup" if now-self.start_ts<60 else "active"
        cue=None
        if status=="active":
            if pos=="below" and self.below_since is not None and now-self.below_since>=25 and (self.last_push is None or now-self.last_push>=50) and (self.last_climb is None or now-self.last_climb>=45):
                cue="pushNudge";self.last_push=self.below_since=now
            elif pos=="above" and self.above_since is not None and now-self.above_since>=25 and (self.last_ease is None or now-self.last_ease>=50) and self.above_slow_since is None:
                cue="easeOff";self.last_ease=self.above_since=now
        if pos=="above" and self.above_slow_since is not None and now-self.above_slow_since>=90 and self.ceiling_drift<8:
            self.ceiling_drift=min(self.ceiling_drift+2,8)
            self.above_slow_since=now
        self.current_position,self.last_update_ts=pos,now
        return output(status,pos,smoothed,cue)


def _day(key):
    if not isinstance(key,str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",key):return None
    try:return dt.date.fromisoformat(key)
    except ValueError:return None


def _hr(row):
    return (row.get("ts"),row.get("bpm")) if isinstance(row,dict) else row


def pre_sleep_feedback(observations):
    """Exact PreSleepHeartRateFeedback.evaluate, first timestamp/day wins."""
    data = observations
    out = dict(eligibility=None, observation=None, comparison=None, uncertainty=[],
               inference="notEstablished", recommendation="unsupported", journal_context=[],
               source="PreSleepHeartRateFeedback.swift")
    def unavailable(reason, **counts):
        out["eligibility"] = reason
        if counts:out["eligibility_detail"]=counts
        return out
    if not data.get("enabled",False):return unavailable("disabled")
    date = _day(data.get("day"))
    if date is None:return unavailable("invalidDay")
    required = data.get("minimum_valid_samples",10)
    window = data.get("pre_sleep_window_seconds",1800)
    if not finite(required) or required<=0 or not finite(window) or window<=0:return unavailable("invalidWindow")
    sessions=[]
    for s in data.get("sessions",[]):
        a,b=s.get("start"),s.get("end")
        if finite(a) and finite(b) and INT_MIN<=a<=INT_MAX and INT_MIN<=b<=INT_MAX and 0<b-a<=INT_MAX:sessions.append(s)
    if not sessions:return unavailable("missingPrimarySleep")
    # Swift max(by:) retains the first equivalent maximum.
    primary = max(sessions,key=lambda s:s["end"]-s["start"])
    start = primary["start"]-window
    if not INT_MIN<=start<=INT_MAX:return unavailable("invalidWindow")
    seen, in_window = set(), []
    for row in data.get("hr",[]):
        ts,bpm=_hr(row)
        if not finite(ts) or ts in seen:continue
        seen.add(ts)
        if start<=ts<primary["start"]:in_window.append((ts,bpm))
    valid=[bpm for _,bpm in in_window if finite(bpm) and 30<=bpm<=220]
    if len(valid)<required:return unavailable("insufficientPreSleepSamples",valid=len(valid),required=required)
    mean=sum(valid)/len(valid)
    out["observation"]=dict(primary_sleep_start_ts=primary["start"],primary_sleep_end_ts=primary["end"],
                            window_start_ts=start,window_end_ts=primary["start"],mean_bpm=mean,
                            valid_samples=len(valid),total_timestamp_samples=len(in_window))
    seen_days, prior = set(), []
    for r in data.get("history",[]):
        key=r.get("day");past=_day(key)
        if past is None or past>=date or key in seen_days:continue
        seen_days.add(key)
        prior.append((past,r.get("mean_bpm",r.get("meanBpm"))))
    prior.sort(key=lambda r:r[0])
    usable=[(past,v) for past,v in prior if finite(v) and 30<=v<=220]
    trailing=usable[-30:]
    n=len(trailing)
    nights_since=(date-usable[-1][0]).days if usable else 0
    baseline=sum(v for _,v in trailing)/n if n else 125.
    status="stale" if nights_since>14 and n>=4 else "calibrating" if n<4 else "provisional" if n<14 else "trusted"
    out["journal_context"]=[dict(day=r.get("day"),question=r.get("question"),
                                answered_yes=r.get("answered_yes",r.get("answeredYes",r.get("answer"))),
                                numeric_value=r.get("numeric_value",r.get("numericValue",r.get("value"))))
                             for r in data.get("journal_entries",[]) if r.get("day")==data["day"]]
    if n<4:
        out["uncertainty"]=["noPersonalComparison"]
        return unavailable("insufficientBaseline",valid_nights=n,required=4)
    if status=="stale":
        out["uncertainty"]=[dict(kind="staleBaseline",days_since_update=nights_since)]
        return unavailable("staleBaseline",days_since_update=nights_since)
    out["comparison"]=dict(baseline_bpm=baseline,delta_bpm=mean-baseline,baseline_nights=n,baseline_status=status)
    out["uncertainty"]=[] if status=="trusted" else ["provisionalBaseline"]
    return unavailable("eligible")


def live_feedback(kind, observations, elapsed):
    if not isinstance(observations,dict):
        observations={"hr":observations}
    if kind in ("pre-sleep", "pre_sleep"):
        return pre_sleep_feedback(observations)
    if kind=="resonance":
        return score_resonance(observations.get("pace_samples",[]))
    if kind=="hr-down":
        return hr_down_step(observations.get("current_hr"),elapsed,observations.get("config"))
    if kind not in ("live-session", "guardian"):
        raise ValueError("Choose live-session, pre-sleep, hr-down or resonance feedback")
    start=observations.get("start_ts",0)
    now=start+_number(elapsed,"elapsed")
    engine=LiveSessionEngine(observations.get("config"),start)
    rows=sorted([_hr(r) for r in observations.get("hr",[]) if finite(_hr(r)[0]) and start<=_hr(r)[0]<=now],key=lambda r:r[0])
    outputs=[]
    for ts,bpm in rows:outputs.append(engine.update(ts,bpm))
    if not rows or rows[-1][0]!=now:outputs.append(engine.update(now,None))
    result=dict(outputs[-1],source="LiveSessionEngine.swift",elapsed_seconds=elapsed,
                cue_history=[dict(ts=ts,cue=o["cue"]) for (ts,_),o in zip(rows,outputs) if o["cue"]])
    # Runner's watchdog requires actual update/tick history to know continuous
    # stale start; don't fabricate auto-end from elapsed alone in sparse replays.
    result["runner_stale_auto_end_seconds"]=600
    return result

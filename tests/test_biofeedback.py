"""Literal fixture/gate oracles from the pinned NOOP Swift XCTest sources.

These run offline. They do not claim Bluetooth, haptic effectiveness or clinical
validation. BreathPacer/HRDown/LiveSession expected numbers are copied from Swift
golden tests; resonance generator matches Swift's integer triangle fixture.
"""
import math
import unittest

import biofeedback as b


def paced(bpm, swing, start=0, duration=150):
    # ResonanceEngineTests.pacedBeats: Swift Int truncates toward zero.
    cycle=60/bpm
    rr=[]
    for t in range(start,start+duration+1):
        phase=math.fmod(t-start,cycle)/cycle
        tri=phase*2 if phase<.5 else 2-phase*2
        rr.append(dict(ts=t,rr_ms=900+int((tri-.5)*swing)))
    return dict(bpm=bpm,start_ts=start,end_ts=start+duration,rr=rr)


class BreathPacerGolden(unittest.TestCase):
    def test_6bpm_two_cycles_literal(self):
        self.assertEqual(b.breath_schedule(6,.4,2),[
            dict(offset_ms=0,phase="inhale",loops=1),
            dict(offset_ms=4000,phase="exhale",loops=2),
            dict(offset_ms=10000,phase="inhale",loops=1),
            dict(offset_ms=14000,phase="exhale",loops=2)])
        self.assertEqual(b.breath_duration_ms(6,2),20000)

    def test_coherence_integer_offsets_literal(self):
        self.assertEqual([c["offset_ms"] for c in b.breath_schedule(5.5,cycles=3)],
                         [0,4364,10909,15273,21818,26182])

    def test_clamp_and_zero_golden(self):
        self.assertEqual(b.breath_schedule(.5,-1,1)[1]["offset_ms"],2000)
        self.assertEqual(b.breath_schedule(99,5,1)[1]["offset_ms"],4500)
        self.assertEqual(b.breath_schedule(6,cycles=-3),[])
        self.assertEqual(b.breath_duration_ms(6,0),0)
        with self.assertRaises(ValueError):b.breath_schedule(float("nan"))

    def test_adapter_sweep_matches_controller_rounding(self):
        kind,title,phases=b.make_bio_plan({"kind":"resonance","quick":True})
        self.assertEqual(kind,"resonance")
        self.assertEqual(list(dict.fromkeys(p["pace_bpm"] for p in phases)),[4.5,5.5,6.5])
        # Controller 120s*pace/60 rounded full cycles: 9/11/13 cycles.
        self.assertEqual([len([p for p in phases if p["pace_index"]==i])//2 for i in range(3)], [9,11,13])
        self.assertAlmostEqual(sum(p["duration"] for p in phases),
                               (13333*9+10909*11+9231*13)/1000)
        self.assertEqual(len(b.make_bio_plan({"kind":"resonance","quick":False})[2]),138)


class ResonanceGolden(unittest.TestCase):
    def test_injected_middle_peak_locks_5p5(self):
        samples=[paced(4.5,40),paced(5.5,120,1000),paced(6.5,40,2000)]
        got=b.score_resonance(samples)
        self.assertTrue(got["did_lock"])
        self.assertEqual(got["locked_bpm"],5.5)
        self.assertGreater(got["scores"][1]["rsa_amplitude"],got["scores"][0]["rsa_amplitude"])

    def test_thin_paces_are_not_scored_and_no_lock(self):
        sparse=dict(bpm=4.5,start_ts=1000,end_ts=1200,rr=[dict(ts=1040+i,rr_ms=900) for i in range(5)])
        self.assertIsNone(b.score_pace(sparse)["rsa_amplitude"])
        got=b.score_resonance([paced(6,60),sparse])
        self.assertFalse(got["did_lock"])
        self.assertEqual(got["locked_bpm"],5.5)
        self.assertIsNotNone(got["reason"])
        self.assertFalse(b.score_resonance([])["did_lock"])

    def test_transient_flat_series_and_slower_tie(self):
        flat=b.score_pace(paced(5.5,0))
        self.assertEqual(flat["clean_beats"],121)
        self.assertEqual(flat["rsa_amplitude"],0)
        self.assertEqual(flat["rmssd_ms"],0)
        tied=b.score_resonance([paced(6.5,0),paced(5.5,0),paced(4.5,0)])
        self.assertEqual(tied["locked_bpm"],4.5)

    def test_stable_same_timestamp_order_and_malik_gate(self):
        rr=[dict(ts=30+i//2,rr_ms=800 if i%2==0 else 810) for i in range(60)]
        sample=dict(bpm=6,start_ts=0,end_ts=100,rr=rr)
        self.assertEqual(b.score_pace(sample)["rmssd_ms"],10)
        # Huge out-of-range pulses cannot manufacture a swing.
        bad=dict(sample,rr=[dict(ts=i,rr_ms=100) for i in range(100)])
        self.assertFalse(b.score_pace(bad)["scored"])

    def test_enough_beats_without_cycles_cannot_lock(self):
        sample=dict(bpm=5.5,start_ts=0,end_ts=40,rr=[dict(ts=35,rr_ms=900) for _ in range(30)])
        got=b.score_pace(sample)
        self.assertEqual(got['clean_beats'],30)
        self.assertEqual(got['scored_cycles'],1)
        self.assertFalse(got['scored'])
        self.assertIsNone(got['rsa_amplitude'])


class HRDownGolden(unittest.TestCase):
    def test_start_and_ramped_steps_literal(self):
        start=b.hr_down_step(84,0)
        self.assertEqual((start["target_bpm"],start["interval_ms"]),(81,741))
        ramp=b.hr_down_step(84,120)
        self.assertEqual((ramp["target_bpm"],ramp["interval_ms"]),(76,789))
        self.assertEqual(b.hr_down_step(84,60)["delta_bpm"],5.5)

    def test_floor_stop_reason_precedence_and_invalid_inputs(self):
        self.assertEqual(b.hr_down_step(75,120,{"hr_floor_bpm":70,"calm_target_bpm":55})["target_bpm"],70)
        self.assertEqual(b.hr_down_step(59,30)["stop_reason"],"settled")
        self.assertEqual(b.hr_down_step(90,180)["stop_reason"],"timeout")
        self.assertEqual(b.hr_down_step(59,180)["stop_reason"],"timeout")
        for hr in (0,-5,None,float("nan"),float("inf")):
            self.assertEqual(b.hr_down_step(hr,0)["stop_reason"],"invalidHR")
        with self.assertRaises(ValueError):b.hr_down_step(84,0,{"max_delta_bpm":-2})
        self.assertEqual(b.hr_down_step(65,0,{"hr_floor_bpm":70,"calm_target_bpm":55})["stop_reason"],"invalidHR")

    def test_descent_stays_bounded_without_claiming_success(self):
        targets=[]
        for i,hr in enumerate([88,86,84,82,80,78,76,74,72,70,68,66]):
            step=b.hr_down_step(hr,i*15)
            if step["stop"]:continue
            target=step["target_bpm"];targets.append(target)
            self.assertGreaterEqual(target,50)
            self.assertGreaterEqual(target,hr-8)
            self.assertLessEqual(target,hr)
        self.assertEqual(targets,sorted(targets,reverse=True))
        plan=b.make_bio_plan({"kind":"hr-down"})[2]
        self.assertNotIn("pace_bpm",plan[0])
        self.assertEqual(plan[0]["type"],"guided")


class GuardianGolden(unittest.TestCase):
    def engine(self,charge=None):return b.LiveSessionEngine(dict(resting_hr=55,hr_max=190,charge=charge))
    def feed(self,e,hr,start,seconds):return [e.update(start+i,hr) for i in range(seconds)]

    def test_band_curve_literal_swift_examples(self):
        band=b.live_band(dict(resting_hr=55,hr_max=190,charge=41))
        self.assertAlmostEqual(band["ceiling_pct_hrr"],.6902)
        self.assertAlmostEqual(band["floor_pct_hrr"],.5402)
        self.assertAlmostEqual(band["ceiling_bpm"],148.177)
        self.assertAlmostEqual(band["floor_bpm"],127.927)
        unknown=self.engine().base_band
        self.assertAlmostEqual(unknown["ceiling_pct_hrr"],.71)
        self.assertAlmostEqual(unknown["floor_pct_hrr"],.56)

    def test_warmup_silence_then_one_push_at_60_seconds(self):
        outs=self.feed(self.engine(),110,0,90)
        self.assertTrue(all(o["cue"] is None and o["status"]=="warmup" for o in outs[:60]))
        cues=[(i,o["cue"]) for i,o in enumerate(outs) if o["cue"]]
        self.assertEqual(cues,[(60,"pushNudge")])

    def test_in_band_silent_time_and_stall_cap(self):
        e=self.engine();outs=self.feed(e,140,0,120)
        self.assertTrue(all(o["cue"] is None for o in outs))
        self.assertEqual(outs[-1]["in_band_seconds"],119)
        self.assertEqual(e.update(300,140)["in_band_seconds"],124)

    def test_sharp_climb_vs_slow_drift(self):
        sharp=self.engine();self.feed(sharp,140,0,70)
        hot=self.feed(sharp,178,70,60)
        self.assertTrue(any(o["cue"]=="easeOff" for o in hot))
        self.assertFalse(any(o["cue"]=="pushNudge" for o in hot))
        slow=self.engine();self.feed(slow,140,0,70)
        ramp=[slow.update(70+i,145+int(i*25/120)) for i in range(120)]
        self.assertTrue(all(o["cue"]!="easeOff" for o in ramp))

    def test_artifact_rejection_stale_gate_and_hysteresis(self):
        e=self.engine();self.feed(e,140,0,20)
        noise=e.update(20,250)
        self.assertFalse(noise["sample_arrived"])
        self.assertEqual(noise["smoothed_bpm"],140)
        stale=e.update(40,None)
        self.assertEqual(stale["status"],"stale")
        self.assertIsNone(stale["cue"])
        self.assertIsNone(stale["smoothed_bpm"])
        self.assertEqual(stale["in_band_seconds"],noise["in_band_seconds"])

    def test_stateless_replay_preserves_actual_samples(self):
        got=b.live_feedback('live-session',dict(config=dict(resting_hr=55,hr_max=190),hr=[dict(ts=i,bpm=110) for i in range(90)]),89)
        self.assertEqual(got["cue_history"],[dict(ts=60,cue="pushNudge")])
        self.assertEqual(b.live_feedback('live-session',dict(hr=[]),40)["status"],"stale")

    def test_cooldown_and_bounded_slow_ceiling_drift(self):
        easy=self.feed(self.engine(),110,0,170)
        self.assertEqual([i for i,o in enumerate(easy) if o['cue']=='pushNudge'],[60,110,160])
        e=self.engine();self.feed(e,140,0,70)
        for i in range(120):e.update(70+i,145+int(i*25/120))
        outputs=self.feed(e,170,190,500)
        self.assertTrue(all(o['cue'] is None for o in outputs))
        self.assertEqual(e.ceiling_drift,8)
        self.assertAlmostEqual(e.current_band()['floor_bpm'],e.base_band['floor_bpm'])

    def test_exact_stale_boundary_and_reentry_silence(self):
        e=self.engine();e.update(0,140)
        self.assertEqual(e.update(8,None)['status'],'warmup')
        self.assertEqual(e.update(9,None)['status'],'stale')
        e=self.engine();self.feed(e,110,0,90)
        back=self.feed(e,140,90,40)
        self.assertTrue(all(o['cue'] is None for o in back))
        self.assertTrue(all(o['position']=='inBand' for o in back[-20:]))


class PreSleepGolden(unittest.TestCase):
    def data(self):
        return dict(enabled=True,day='2026-08-05',sessions=[dict(start=10000,end=36000)],
                    hr=[dict(ts=8800+i*60,bpm=70) for i in range(12)]+[dict(ts=10000+i*60,bpm=55) for i in range(12)],
                    history=[dict(day=f'2026-08-0{i+1}',mean_bpm=60+i) for i in range(4)],
                    journal_entries=[dict(day='2026-08-05',question='Late meal',answered_yes=True,notes='not projected')])

    def test_observation_comparison_and_no_recommendation_literal(self):
        got=b.pre_sleep_feedback(self.data())
        self.assertEqual(got["eligibility"],"eligible")
        self.assertEqual(got["observation"]["window_start_ts"],8200)
        self.assertEqual(got["observation"]["window_end_ts"],10000)
        self.assertEqual(got["observation"]["mean_bpm"],70)
        self.assertEqual(got["observation"]["valid_samples"],12)
        self.assertEqual(got["comparison"]["baseline_bpm"],61.5)
        self.assertEqual(got["comparison"]["delta_bpm"],8.5)
        self.assertEqual(got["uncertainty"],["provisionalBaseline"])
        self.assertEqual(got["inference"],"notEstablished")
        self.assertEqual(got["recommendation"],"unsupported")
        self.assertNotIn('notes',got["journal_context"][0])

    def test_first_timestamp_first_prior_day_and_future_exclusion(self):
        data=self.data();data['history']+=[dict(day='2026-08-04',mean_bpm=200),dict(day='2026-08-05',mean_bpm=200),dict(day='2026-08-06',mean_bpm=200)]
        data['hr'] += [dict(ts=8800,bpm=200)]
        got=b.pre_sleep_feedback(data)
        self.assertEqual(got['observation']['mean_bpm'],70)
        self.assertEqual(got['comparison']['baseline_nights'],4)
        self.assertEqual(got['comparison']['baseline_bpm'],61.5)
        data['history']=[dict(day='2026-08-04',mean_bpm=v) for v in [60,61,62,63]]
        self.assertEqual(b.pre_sleep_feedback(data)['eligibility_detail']['valid_nights'],1)

    def test_stale_calendar_boundary_and_implausible_recent_values(self):
        data=self.data();data['day']='2024-03-01';data['history']=[dict(day=f'2024-02-{i:02}',mean_bpm=60) for i in range(1,15)]
        data['history'] += [dict(day='2024-02-28',mean_bpm=float('nan')),dict(day='2024-02-28',mean_bpm=70),dict(day='2024-02-29',mean_bpm=221)]
        got=b.pre_sleep_feedback(data)
        self.assertEqual(got['eligibility'],'staleBaseline')
        self.assertEqual(got['eligibility_detail']['days_since_update'],16)
        self.assertIsNone(got['comparison'])
        data['history']=[dict(day=f'2023-01-{i:02}',mean_bpm=60) for i in range(1,14)]+[dict(day='2023-12-18',mean_bpm=60)]
        data['day']='2024-01-01'
        self.assertEqual(b.pre_sleep_feedback(data)['comparison']['baseline_status'],'trusted')

    def test_explicit_gate_states_and_int64_overflow(self):
        data=self.data();data['enabled']=False
        self.assertEqual(b.pre_sleep_feedback(data)['eligibility'],'disabled')
        data['enabled']=True;data['day']='2026-02-31'
        self.assertEqual(b.pre_sleep_feedback(data)['eligibility'],'invalidDay')
        data=self.data();data['minimum_valid_samples']=0
        self.assertEqual(b.pre_sleep_feedback(data)['eligibility'],'invalidWindow')
        data=self.data();data['hr']=data['hr'][:3]
        self.assertEqual(b.pre_sleep_feedback(data)['eligibility'],'insufficientPreSleepSamples')
        data=self.data();data['sessions']=[dict(start=b.INT_MIN,end=b.INT_MAX)]
        self.assertEqual(b.pre_sleep_feedback(data)['eligibility'],'missingPrimarySleep')
        data['sessions']=[dict(start=b.INT_MIN+1,end=b.INT_MIN+2)];data['pre_sleep_window_seconds']=b.INT_MAX
        self.assertEqual(b.pre_sleep_feedback(data)['eligibility'],'invalidWindow')


if __name__=='__main__':unittest.main()

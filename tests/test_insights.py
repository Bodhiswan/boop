from datetime import date,timedelta
import math
from pathlib import Path
import tempfile
import unittest

from features import FeatureStore
from insights import (InsightsService,behavior_effect,best_lag,compare_series,circular_clock_hour,
                      compare_periods,cosinor,cycle_from_logs,cycle_fused_index,
                      cycle_phase,dose_response,month_over_month,pearson,shift_plan,
                      streaks,weekly_digest)


class InsightMathTests(unittest.TestCase):
    def test_circular_clock_center_midnight_and_degenerate_history(self):
        # SleepStageTotals.circularMeanSec: nearest-second unit-vector mean.
        self.assertEqual(circular_clock_hour([23.5,.5]),0)
        self.assertEqual(circular_clock_hour([7,9]),8)
        self.assertIsNone(circular_clock_hour([0,12]))
        self.assertIsNone(circular_clock_hour([]))
    def test_noop_behavior_pooled_variance_and_group_gate(self):
        result=behavior_effect([70,72,74,76,78],[60,62,64,66,68])
        self.assertEqual((74,64,10,5,5),(result["mean_with"],result["mean_without"],result["delta"],result["n_with"],result["n_without"]))
        self.assertAlmostEqual(3.162277660168379,result["cohens_d"])
        self.assertAlmostEqual(15.625,result["pct_change"])
        self.assertTrue(result["significant"])
        self.assertLess(result["p_approx"],.00001)
        self.assertFalse(behavior_effect([100,101],[1,2])["significant"])
        self.assertIsNone(behavior_effect([10,11],[]))
        self.assertIsNone(behavior_effect([10],[11]))
        constant=behavior_effect([10]*5,[20]*5)
        self.assertEqual((0,0),(constant["cohens_d"],constant["p_approx"]))

    def test_explicit_controls_and_fixed_lag_gate(self):
        yes={f"2026-01-{d:02}":True for d in range(1,6)}
        no={f"2026-01-{d:02}":False for d in range(10,15)}
        outcomes={f"2026-01-{d:02}":1000 for d in range(1,30)}
        for day in yes:
            outcomes[(date.fromisoformat(day)+timedelta(days=1)).isoformat()]=50
        for day in no:
            outcomes[(date.fromisoformat(day)+timedelta(days=1)).isoformat()]=80
        result=best_lag(yes|no,outcomes,"Coffee","hrv")
        self.assertIsNotNone(result)
        self.assertEqual(5,result["n_with"])
        self.assertEqual(5,result["n_without"])
        self.assertIsNone(best_lag(yes,outcomes,"Coffee","hrv"))
        self.assertIsNone(best_lag({"2026-01-01":True,"2026-01-02":False},outcomes,"Coffee","hrv"))

    def test_noop_dose_next_day_shrinkage_prior_and_clamp_literals(self):
        doses={f"2026-01-{d+1:02}":d for d in range(4)}
        outcomes={f"2026-01-{d+2:02}":100-2*d for d in range(4)}
        result=dose_response(doses,outcomes,"Alcohol","charge",(-5,-15,2))
        self.assertEqual(4,result["n_user"])
        self.assertEqual(-2,result["user_slope"])
        self.assertAlmostEqual(1/3,result["weight"])
        self.assertAlmostEqual(-4,result["per_unit"])
        self.assertTrue(result["prior_dominated"])
        self.assertAlmostEqual(-12,result["curve"][3]["outcome_delta"])
        empty=dose_response({}, {},"Alcohol","charge",(-5,-15,2))
        self.assertEqual((-5,None,0),(empty["per_unit"],empty["user_slope"],empty["n_user"]))
        clamp=dose_response(doses,{f"2026-01-{d+2:02}":1000-100*d for d in range(4)},"Caffeine","hrv",(-4,-20,4))
        self.assertEqual(-20,clamp["per_unit"])

    def test_pearson_ols_overlap_and_constant_missing_gates(self):
        result=pearson([(1,3),(2,5),(3,7)])
        self.assertEqual((1,2,1,0),(result["r"],result["slope"],result["intercept"],result["p_approx"]))
        self.assertIsNone(pearson([(1,3),(1,5),(1,7)]))
        comparison=compare_series({"2026-01-01":1,"2026-01-02":2,"2026-01-03":3,"2026-01-04":4},{"2026-01-01":3,"2026-01-02":5,"2026-01-03":7})
        self.assertEqual(.75,comparison["overlap"])
        self.assertEqual(-1,comparison["normalized"][0]["x"])
        self.assertIsNone(compare_series({}, {})["r"])

    def test_noop_cosinor_literal_hr_phase_and_planner(self):
        # NOOP's absolute-amplitude exception: 5.5 bpm on mesor 74.7.
        fit=cosinor([(h,74.7+5.5*math.cos(2*math.pi*(h-16)/24)) for h in range(24)])
        self.assertAlmostEqual(74.7,fit["mesor"])
        self.assertAlmostEqual(5.5,fit["amplitude"])
        self.assertAlmostEqual(16,fit["acrophase_hours"])
        self.assertIsNone(cosinor([(1,70),(1,75),(1,72)]))
        plan=shift_plan(2.5,23,7)
        self.assertEqual("advance",plan["direction"])
        self.assertEqual(3,plan["estimated_days"])
        self.assertEqual((20.5,4.5),(plan["days"][-1]["target_sleep_hour"],plan["days"][-1]["target_wake_hour"]))
        self.assertEqual(4.5,plan["days"][-1]["bright_light_start_hour"])
        self.assertEqual("none",shift_plan(.4,23,7)["direction"])
        self.assertEqual([],shift_plan(None,None,None)["days"])

    def test_streak_yesterday_grace_and_calendar_gaps(self):
        days=["2026-01-01","2026-01-02","2026-01-03","2026-01-05","invalid"]
        self.assertEqual((1,3),(streaks(days,"2026-01-06")["current"],streaks(days,"2026-01-06")["longest"]))
        self.assertEqual(0,streaks(days,"2026-01-07")["current"])
        self.assertEqual(3,streaks(days,"2026-01-03")["current"])

    def test_weekly_monday_normalized_mover_and_sparse_gate(self):
        values={"2026-09-21":50,"2026-09-22":50,"2026-09-23":50,"2026-09-28":62,"2026-09-29":62,"2026-09-30":62}
        result=weekly_digest({"charge":values},"2026-09-30")
        self.assertEqual("2026-09-28",result["week_start"])
        self.assertEqual(12,result["metrics"]["charge"]["delta"])
        self.assertEqual(1,result["movers"][0]["normalized_move"])
        sparse=weekly_digest({"charge":values},"2026-09-29")
        self.assertEqual([],sparse["movers"])
        self.assertTrue(sparse["metrics"]["charge"]["rough"])
        missing=weekly_digest({"charge":{}},"2026-09-30")
        self.assertIsNone(missing["metrics"]["charge"]["current"]["mean"])
        self.assertIsNone(missing["metrics"]["charge"]["delta"])

    def test_voluntary_cycle_calendar_never_invents_length_or_signal(self):
        self.assertEqual("unknown",cycle_from_logs([],"2026-01-15")["phase"])
        row={"day":"2026-01-01","type":"period_start"}
        unknown=cycle_from_logs([row],"2026-01-15")
        self.assertEqual(15,unknown["cycle_day"])
        self.assertEqual("unknown",unknown["phase"])
        known=cycle_from_logs([row|{"cycle_length_days":28}],"2026-01-15")
        self.assertEqual("luteal_calendar",known["phase"])
        self.assertEqual("calendar_only",known["confidence"])

    def biphasic(self,cycles=3):
        # Literal inputs from NOOP CyclePhaseEngineTests.biphasic.
        nights=[]
        for i in range(cycles*28):
            luteal=i%28>=16
            nights.append(dict(day=(date(2026,1,1)+timedelta(days=i)).isoformat(),temp_z=1.4 if luteal else -.2,rhr_z=1 if luteal else -.1,hrv_z=-1 if luteal else .1))
        return nights

    def test_noop_temperature_cycle_literal_biphasic_and_range(self):
        nights=self.biphasic()
        result=cycle_phase(nights,True)
        self.assertEqual("luteal",result["phase"])
        self.assertEqual("solid",result["confidence"])
        self.assertEqual(28,result["length_days"])
        self.assertEqual(3,len(result["shift_markers"]))
        self.assertEqual((23,27),(result["cycle_day_low"],result["cycle_day_high"]))
        self.assertLess(result["next_period_window"]["earliest_day"],result["next_period_window"]["latest_day"])
        self.assertAlmostEqual(1.24,cycle_fused_index(1.4,1,-1))
        self.assertEqual(1,cycle_fused_index(None,1,None))
        self.assertIsNone(cycle_fused_index(None,None,None))

    def test_noop_temperature_cycle_flat_thin_and_missing_baseline(self):
        nights=self.biphasic()
        self.assertEqual("learning",cycle_phase(nights[:28],True)["phase"])
        self.assertEqual("learning",cycle_phase(nights,False)["phase"])
        flat=[dict(day=n["day"],temp_z=.05,rhr_z=0,hrv_z=0) for n in nights]
        result=cycle_phase(flat,True)
        self.assertEqual("unknown",result["phase"])
        self.assertIsNone(result["length_days"])
        self.assertIsNone(result["next_period_window"])
        missing=[dict(day=n["day"]) for n in nights]
        self.assertEqual("learning",cycle_phase(missing,True)["phase"])

    def test_noop_temperature_cycle_follicular_after_shift_and_mistimed_log(self):
        nights=self.biphasic()
        last=date.fromisoformat(nights[-1]["day"])
        for i in range(1,9):
            nights.append(dict(day=(last+timedelta(days=i)).isoformat(),temp_z=-.2,rhr_z=-.1,hrv_z=.1))
        self.assertEqual("follicular",cycle_phase(nights,True)["phase"])
        bad=(date.fromisoformat(nights[-1]["day"])-timedelta(days=50)).isoformat()
        result=cycle_phase(nights,True,[bad])
        self.assertIn("logged",result["note"])

    def test_noop_comparison_statistics_period_and_calendar_month(self):
        comparison=compare_periods([10,20,30],[5,10,15])
        self.assertEqual((20,20,10,30,10,3,10),(comparison["current"]["mean"],comparison["current"]["median"],comparison["current"]["minimum"],comparison["current"]["maximum"],comparison["current"]["stdev"],comparison["current"]["n"],comparison["current"]["slope_per_day"]))
        self.assertEqual((10,100,1),(comparison["delta"],comparison["pct_change"],comparison["direction"]))
        empty=compare_periods([], [1,2,3])
        self.assertIsNone(empty["delta"])
        self.assertEqual(0,empty["direction"])
        monthly=month_over_month({"2025-12-20":5,"2026-01-01":10,"2026-01-20":20,"2026-02-01":999},"2026-01-25")
        self.assertEqual("2025-12",monthly["previous_month"])
        self.assertEqual(15,monthly["current"]["mean"])
        self.assertEqual(10,monthly["delta"])


class FakeAnalytics:
    def __init__(self,entries):
        self.entries=entries

    def trends(self,device,days,settings,through):
        return dict(days=self.entries,through=through or "2026-01-20")


class InsightBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.features=FeatureStore(Path(self.temp.name)/"features.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def entries(self,count=20,rhythm=False):
        result=[]
        for i in range(count):
            entry=dict(day=(date(2026,1,1)+timedelta(days=i)).isoformat(),hourly_hr={h:74.7+5.5*math.cos(2*math.pi*(h-16)/24) for h in range(24)} if rhythm else {},sleep={"main":None},workouts={"value":[]})
            for metric in ("charge","effort","rest","hrv","resting_hr","steps"):
                entry[metric]={"value":50+i if metric=="charge" else None}
            result.append(entry)
        return result

    def test_bundle_explicit_answers_experiment_windows_and_no_db_overwrite(self):
        for day in range(1,11):
            self.features.save_record("journal",{"day":f"2026-01-{day:02}","question":"Alcohol","answer":day<=5,"dose":1 if day<=5 else 0})
        self.features.save_record("habit",{"type":"experiment","name":"Earlier bedtime","baseline_start":"2026-01-01","baseline_end":"2026-01-05","intervention_start":"2026-01-06","intervention_end":"2026-01-10","outcome":"charge"})
        before=self.features.store.summary()["readings"]
        result=InsightsService(self.features,FakeAnalytics(self.entries())).bundle("owned",20,through="2026-01-20")
        self.assertEqual(10,result["behaviour"]["logged_days"])
        self.assertEqual(.5,result["behaviour"]["coverage"])
        effect=result["effects"][0]
        self.assertEqual((5,5),(effect["n_with"],effect["n_without"]))
        self.assertEqual("observed",result["experiments"][0]["status"])
        self.assertEqual(5,result["experiments"][0]["baseline_days"])
        self.assertEqual(before,self.features.store.summary()["readings"])
        self.assertEqual("unreadable",result["circadian"]["confidence"])

    def test_bundle_thin_data_and_circadian_absolute_gate(self):
        thin=InsightsService(self.features,FakeAnalytics(self.entries(2))).bundle("owned",2,through="2026-01-02")
        self.assertEqual([],thin["effects"])
        self.assertEqual([],thin["experiments"])
        self.assertIsNone(thin["comparisons"][0]["r"])
        self.assertEqual("unknown",thin["cycle"]["phase"])
        strong=InsightsService(self.features,FakeAnalytics(self.entries(14,True))).bundle("owned",14,through="2026-01-14")
        self.assertEqual("wide",strong["circadian"]["confidence"])
        self.assertAlmostEqual(4,strong["circadian"]["temp_min_hour"])

    def test_bundle_learns_sleep_clock_across_midnight(self):
        entries=self.entries(2)
        from datetime import datetime,timezone
        first=datetime(2026,1,1,23,30,tzinfo=timezone.utc).timestamp()
        second=datetime(2026,1,2,0,30,tzinfo=timezone.utc).timestamp()
        for entry,start in zip(entries,[first,second]):entry['sleep']['main']=dict(start=start,end=start+8*3600)
        settings=dict(timezone='UTC',habitual_sleep_hour=-1,habitual_wake_hour=-1,circadian_shift_hours=1)
        result=InsightsService(self.features,FakeAnalytics(entries)).bundle('owned',2,settings,'2026-01-02')
        self.assertEqual(result['circadian']['plan']['days'][0]['target_sleep_hour'],23)


if __name__=="__main__":
    unittest.main()

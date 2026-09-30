"""Literal oracle values from NOOP's Swift tests at the pinned reference commit."""
import datetime as dt
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

import analytics as a


class NoopAnalyticsOracles(unittest.TestCase):
    def test_hrv_golden_from_hrv_analyzer_tests(self):
        rr=[800,810,805,815,800,820,810,800,815,805,810,800,820,815,805,810,800,815,810,805,800,820]
        got=a.hrv(rr)
        self.assertAlmostEqual(got["rmssd_ms"],11.649647450214351,places=9)
        self.assertAlmostEqual(got["sdnn_ms"],7.101612523427368,places=9)
        self.assertEqual(got["coverage"]["clean_beats"],22)

    def test_malik_rejection_and_gap_aware_differences(self):
        rr=[800.]*30
        rr[15]=1400
        nn,adjacent=a.clean_rr(rr)
        self.assertEqual(len(nn),29)
        self.assertFalse(adjacent[15])
        self.assertEqual(a.hrv(rr)["value"],0)
        self.assertIsNone(a.hrv([800]*19)["value"])
        self.assertIsNone(a.hrv([800]*24+[100]*16,max_rejected_fraction=.35)["value"])

    def test_overcount_and_banked_rr_do_not_claim_sdnn(self):
        over=a.hrv([1000]*30,[i//2 for i in range(30)])
        self.assertIsNone(over["value"])
        self.assertIsNone(over["sdnn_ms"])
        self.assertIn("110%",over["reason"])
        self.assertIsNone(a.respiration([(i//6,1000) for i in range(300)])["value"])

    def test_edwards_golden_from_strain_scorer_tests(self):
        got=a.effort([(i,185) for i in range(600)],190,60)
        self.assertEqual(got["value"],44.27)
        self.assertAlmostEqual(got["trimp"],50,places=10)
        self.assertIsNone(a.effort([(i,150) for i in range(599)],190,60)["value"])
        self.assertIsNone(a.effort([(i,150) for i in range(600)],60,60)["value"])
        self.assertEqual(a.effort([(i,60) for i in range(600)],190,60)["value"],0)

    def test_dropouts_do_not_invent_hours_of_effort(self):
        self.assertEqual(a.durations([(0,185),(1,185),(3601,185)]),[1,120,120])
        self.assertEqual(a.hr_zones([(0,185),(1,185),(3601,185)],190)["value"],[0,0,0,0,3])

    def test_sleep_debt_oracles_from_sleep_debt_tests(self):
        self.assertEqual(a.sleep_debt([("1",480),("2",None),("3",0),("4",420)])["balance_min"],-33)
        self.assertEqual(a.sleep_debt([(str(i),420) for i in range(16)])["balance_min"],-73.3)
        self.assertEqual(a.sleep_debt([("1",360),("2",546),("3",540)])["balance_min"],0)
        self.assertEqual(a.sleep_debt([("1",440)])["balance_min"],-22)
        self.assertEqual(a.sleep_debt([("1",462)])["balance_min"],0)

    def test_sleep_need_population_floor(self):
        self.assertEqual(a.sleep_need([5]*14,30),8)
        self.assertEqual(a.sleep_need([5]*14,16),9)
        self.assertEqual(a.sleep_need([10]*14,30),9.5)
        self.assertEqual(a.rest(8*3600,8*3600,1,4*3600,8,1,2*3600),100)

    def test_training_load_literal_swift_oracles(self):
        def days(loads):
            return [((dt.date(2000,1,1)+dt.timedelta(days=i)).isoformat(),v) for i,v in enumerate(loads)]
        got=a.training_load(days([50]*7+[100]*7))
        self.assertAlmostEqual(got["value"]["ctl"],57.67591375546929,places=10)
        self.assertAlmostEqual(got["value"]["atl"],81.6060279414279,places=10)
        self.assertAlmostEqual(got["value"]["tsb"],-23.930114185958608,places=10)
        steady=a.training_load(days([50]*42))
        self.assertEqual(steady["state"],"established")
        self.assertEqual(steady["value"]["ctl"],50)
        self.assertEqual(steady["value"]["tsb"],0)
        self.assertEqual(len(steady["points"]),36)
        missing=days([50]*20)
        del missing[14]
        self.assertEqual(a.training_load(missing)["contiguous_days"],5)
        self.assertIsNone(a.training_load(days([50]*13))["value"])

    def test_baseline_and_charge_cold_start(self):
        self.assertFalse(a.baseline([50]*3)["usable"])
        b=a.baseline([50]*4)
        self.assertTrue(b["usable"])
        self.assertEqual(b["mean"],50)
        self.assertEqual(b["spread"],5)
        self.assertIsNone(a.charge(50,60,a.baseline([50]*3))["value"])
        self.assertAlmostEqual(a.charge(50,60,b)["value"],57.932425214874945,places=9)
        self.assertEqual(a.baseline([50]*14+[None]*15)["status"],"stale")

    def test_correlation_no_data_and_linear(self):
        self.assertEqual(a.correlation([(1,3),(2,5),(3,7)])["value"],1)
        self.assertEqual(a.correlation([(1,3),(2,5),(3,7)])["slope"],2)
        self.assertEqual(a.correlation([(1,3),(2,5),(3,7)])["intercept"],1)
        self.assertIsNone(a.correlation([(1,3),(1,5),(1,7)])["value"])

    def test_v2_frozen_light_shape_without_resp(self):
        start=1751513600
        length=20*60
        gravity=[(start+i,1 if i%2==0 else 0,0,0 if i%2==0 else 1) for i in range(length)]
        hr=[(start+i,50+(i//60)%3) for i in range(length)]
        got=a.stage_sleep(start,start+length,hr,gravity,[])
        self.assertEqual(got["value"],[dict(start=start,end=start+length,stage="light")])

    def test_workout_requires_motion_and_sustained_intensity(self):
        self.assertIsNone(a.workouts([(i,150) for i in range(600)],[],60,190)["value"])
        gravity=[(i,float(i%2),0,1) for i in range(601)]
        got=a.workouts([(i,150) for i in range(601)],gravity,60,190)
        self.assertEqual(len(got["value"]),1)
        self.assertEqual(got["value"][0]["start"],1)
        self.assertEqual(got["value"][0]["end"],600)

    def test_resp_rate_rsa_reference_vectors(self):
        # RespRateRsaTests.swift uses these literal planted rates and tolerances.
        for base,amplitude,hz,duration,expected,tolerance in [(1000,40,.25,420,15,3),(60000/55,45,11/60,480,11,2)]:
            rows=[]
            t=0.
            while t<duration:
                rr=base+amplitude*math.sin(2*math.pi*hz*t)
                t+=rr/1000
                rows.append((1700000000+int(t),int(rr)))
            got=a.respiration(rows)
            self.assertIsNotNone(got["value"],got["reason"])
            self.assertLessEqual(abs(got["value"]-expected),tolerance)

    def test_activity_cost_literal_reference(self):
        tagged={f"2026-06-{i:02d}" for i in range(1,10)}
        recovery={day:50 for day in tagged}
        recovery.update({f"2026-06-{i:02d}":70 for i in range(20,28)})
        got=a.activity_cost({"running":tagged},recovery)["value"][0]
        self.assertEqual(got["baseline_mean"],70)
        self.assertEqual(got["mean_next_morning"],50)
        self.assertEqual(got["delta"],20)
        self.assertEqual(got["n"],8)
        self.assertEqual(got["confidence"],"solid")

    def test_stress_literal_reference(self):
        rr=[700,720,740,760,780,800,820,840,860,800,800,800,800,820,780,800,810,790,800,800,805,795]
        got=a.stress_index(rr)
        self.assertAlmostEqual(got["value"],223.82920110192836,places=9)
        self.assertAlmostEqual(got["mo_seconds"],.825,places=9)
        self.assertAlmostEqual(got["amplitude_percent"],59.09090909090909,places=9)

    def test_read_only_service_json_and_sensor_only_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"data.db"
            conn=sqlite3.connect(path)
            conn.executescript("CREATE TABLE readings(frame_id INTEGER,device TEXT,kind TEXT,timestamp_ms INTEGER,received_ms INTEGER,device_seconds INTEGER,hr INTEGER,rr_json TEXT,contact INTEGER,gx REAL,gy REAL,gz REAL,layout INTEGER); CREATE TABLE sensors(frame_id INTEGER,device TEXT,timestamp_ms INTEGER,received_ms INTEGER,source TEXT,values_json TEXT);")
            midnight=dt.datetime(2026,10,1,tzinfo=dt.timezone.utc).timestamp()
            conn.executemany("INSERT INTO readings VALUES(?, 'd','history',?,0,0,150,'[]',1,NULL,NULL,NULL,0)",[(i,int((midnight+i)*1000)) for i in range(600)])
            conn.commit()
            before=conn.total_changes
            service=a.AnalyticsService(SimpleNamespace(path=path))
            got=service.day("d","2026-10-01",dict(timezone="UTC",hr_max=190,hr_rest=60))
            self.assertIsNotNone(got["effort"]["value"])
            self.assertIsNone(got["charge"]["value"])
            self.assertIsNone(got["hrv"]["value"])
            self.assertIsNone(got["workouts"]["value"])
            self.assertEqual(got["timezone"],"UTC")
            json.dumps(got,allow_nan=False)
            self.assertEqual(conn.total_changes,before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0],600)
            conn.close()

    def test_hydration_and_forecast_literal_noop_oracles(self):
        self.assertEqual(a.hydration_goal("male",63),4150)
        self.assertEqual(a.hydration_goal("female",50),3050)
        self.assertEqual(a.hydration_goal("",None),3200)
        neutral=a.recovery_forecast([60]*14,[50]*14,50,8)
        self.assertEqual(neutral["value"],60)
        self.assertEqual(neutral["band"],8)
        self.assertEqual(a.recovery_forecast([60]*14,[50]*14,100,8)["value"],48)
        self.assertEqual(a.recovery_forecast([60]*14,[50]*14,50,4)["value"],53)
        self.assertIsNone(a.recovery_forecast([60]*4,[],None,8)["value"])

    def test_fitness_lift_vitality_literal_noop(self):
        # FitnessAgeEngineTests reference peer: four active strain60 days -> PAI5.
        got=a.fitness_age(40,"male",65,[60,60,60,60],90)
        self.assertEqual(got["value"],40)
        self.assertAlmostEqual(got["vo2max_estimate"],46.275)
        female=a.fitness_age(40,"female",65,[60]*4,80)
        self.assertAlmostEqual(female["vo2max_estimate"],37.72)
        self.assertIsNone(a.fitness_age(40,"male",65,[60]*3)["value"])
        lift=a.lift_metrics([dict(exercise="Squat",weight_kg=100,reps=5),dict(exercise="Squat",weight_kg=140,reps=0)])
        self.assertEqual(lift["performed_sets"],1)
        self.assertEqual(lift["exercises"][0]["volume_kg"],500)
        self.assertAlmostEqual(lift["exercises"][0]["estimated_1rm_kg"],116.6667,places=3)
        self.assertEqual(a.vitality(40,rhr=65,sleep_hours=7.5,steps=7000)["value"],50)
        self.assertIsNone(a.vitality(40,rhr=65)["value"])

    def test_frequency_source_span_and_band_gates(self):
        self.assertIsNone(a.frequency_hrv([1000]*60)["value"])
        short=a.frequency_hrv([1000]*61)["value"]
        self.assertEqual(short,dict(hf=0,lf=None,lf_hf=None,total_power=0))
        rr=[];t=0
        while t<300:
            value=900+30*math.sin(2*math.pi*.25*t);rr.append(value);t+=value/1000
        got=a.frequency_hrv(rr)["value"]
        self.assertGreater(got["hf"],got["lf"]*3)
        self.assertLess(got["lf_hf"],1)

    def test_steps_calibration_and_explicit_controls(self):
        self.assertIsNone(a.steps_estimate(10)["value"])
        got=a.steps_estimate(10,[(10,100),(10,100),(10,100)])
        self.assertEqual(got["value"],100)
        self.assertEqual(got["coefficient"],10)
        self.assertAlmostEqual(got["confidence"],.5+.5*3/14)
        self.assertEqual(a.weighted_median([(10,1),(20,1)]),15)
        self.assertIsNone(a.behavior_effect([60]*5,[])["value"])
        effect=a.behavior_effect([70]*5,[60]*5)
        self.assertEqual(effect["value"],10)
        self.assertTrue(effect["significant"])
        self.assertEqual(effect["cohens_d"],0) # NOOP's zero-spread handling

    def test_circadian_and_illness_gates(self):
        bins=[(h,60+10*math.cos(2*math.pi*(h-16)/24)) for h in range(24)]
        got=a.circadian_phase(bins,14,7)
        self.assertAlmostEqual(got["value"]["acrophase_hour"],16)
        self.assertAlmostEqual(got["value"]["estimated_temperature_min_hour"],4)
        self.assertAlmostEqual(got["value"]["offset_vs_schedule_minutes"],-30)
        self.assertEqual(got["confidence"],"solid")
        self.assertEqual(a.circadian_phase(bins,6)["confidence"],"unreadable")
        self.assertEqual(a.illness_signal(dict(rhr=4,hrv=4),False)["level"],"quiet")
        self.assertEqual(a.illness_signal(dict(rhr=4,hrv=4),True)["value"],80)
        self.assertEqual(a.illness_signal(dict(rhr=4,hrv=4),True,["alcohol"])["value"],36)

    def test_import_arbitration_mutable_cache_and_summary_fragments(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"data.db";conn=sqlite3.connect(path)
            conn.executescript("CREATE TABLE readings(frame_id INTEGER,device TEXT,kind TEXT,timestamp_ms INTEGER,received_ms INTEGER,device_seconds INTEGER,hr INTEGER,rr_json TEXT,contact INTEGER,gx REAL,gy REAL,gz REAL,layout INTEGER);CREATE TABLE feature_records(id TEXT,kind TEXT,payload_json TEXT,deleted_ms INTEGER);")
            t=int(dt.datetime(2026,10,1,tzinfo=dt.timezone.utc).timestamp()*1000)
            conn.execute("INSERT INTO readings VALUES(1,'d','history',?,0,0,100,'[]',1,0,0,1,0)",(t,))
            records=[("hr","health_sample",dict(source="apple-health",timestamp_ms=t,hr=200)),("hr2","health_sample",dict(source="apple-health",timestamp_ms=t+1000,hr=110)),("rhr","daily_metric",dict(source="oura",day="2026-10-01",resting_hr=55)),("hrv","daily_metric",dict(source="oura",day="2026-10-01",hrv_ms=45)),("nap","daily_metric",dict(source="oura",day="2026-10-01",total_sleep_min=50,source_sleep_type="nap")),("night","daily_metric",dict(source="oura",day="2026-10-01",total_sleep_min=400,source_sleep_type="long_sleep"))]
            conn.executemany("INSERT INTO feature_records VALUES(?,?,?,NULL)",[(i,k,json.dumps(r)) for i,k,r in records]);conn.commit()
            service=a.AnalyticsService(SimpleNamespace(path=path));settings=dict(timezone="UTC",hr_max=190,hr_rest=60,profile_provenance=dict(hr_rest="default"))
            got=service.day("d","2026-10-01",settings)
            self.assertEqual(got["coverage"]["hr_samples"],2)
            self.assertEqual(got["hrv"]["value"],45)
            self.assertEqual(got["sleep"]["total_sleep_min"],400)
            self.assertEqual(got["provenance"]["hr_rest_source"],"noop_default")
            self.assertEqual(got["hourly_hr"]["0"],105)
            conn.execute("UPDATE feature_records SET payload_json=? WHERE id='hr2'",(json.dumps(dict(source="apple-health",timestamp_ms=t+1000,hr=120)),));conn.commit()
            updated=service.day("d","2026-10-01",settings)
            self.assertEqual(updated["hourly_hr"]["0"],110)
            json.dumps(service.report("d",7,settings,through="2026-10-01"),allow_nan=False)
            conn.close()

    def test_sleep_bridge_literal_noop_active_interruption(self):
        # SleepStagerActiveBridgeTests exact period fixtures.
        periods=[[True,0,3000],[False,3000,3900],[True,3900,9000]]
        calm=[(t,50) for t in range(0,9001,60)]
        bridged,trace=a.bridge_sleep_periods(periods,calm,60)
        self.assertEqual(bridged,[[True,0,9000]])
        self.assertEqual(trace[0]["reason"],"bridged")
        self.assertEqual(a.bridge_sleep_periods(periods,calm,60,False)[0],periods)
        hot=[(t,110 if 3000<t<=3900 else 50) for t in range(0,9001,60)]
        self.assertEqual(a.bridge_sleep_periods(periods,hot,60)[0],periods)
        long=[[True,0,3000],[False,3000,6660],[True,6660,9000]]
        self.assertEqual(a.bridge_sleep_periods(long,calm,60)[1][0]["reason"],"activeTooLong")
        self.assertEqual(a.bridge_sleep_periods([[True,0,3000],[True,3600,9000]],calm,60)[1][0]["active_cap_seconds"],0)

    def test_sleep_sparse_end_to_end_source_fixture(self):
        # SleepStagerActiveBridgeTests.interruptedNight, same 1/min gravity and 1Hz HR.
        midnight=1749513600
        def still(a_,b_):return [(t,0,0,1) for t in range(midnight+a_,midnight+b_,60)]
        grav=still(0,1800)+still(3600,7200)+[(midnight+7200+i*60,1 if i%2==0 else 0,0 if i%2==0 else 1,0) for i in range(15)]+still(8100,21600)
        hr=[(midnight+i,52 if 7200<=i<8100 else 50) for i in range(21600)]
        got=a.detect_sleep(hr,grav)
        self.assertTrue(got["coverage"]["sparse_gravity"])
        self.assertEqual(len(got["value"]),1)
        self.assertGreater(got["value"][0]["end"]-got["value"][0]["start"],18000)
        hot=[(t,110 if midnight+7200<=t<midnight+8100 else h) for t,h in hr]
        refused=a.detect_sleep(hot,grav)
        self.assertTrue(len(refused["value"])!=1 or sum(r["end"]-r["start"] for r in refused["value"])<18000)

    def test_sleep_off_wrist_union_sparse_and_morning_source_oracles(self):
        sparse=[(t,52) for t in (0,1500,3000,4500)]
        self.assertEqual(a.off_wrist_fraction(0,5400,sparse),0)
        self.assertAlmostEqual(a.off_wrist_fraction(0,5400,sparse,[(0,3000)]),3000/5400)
        gappy=[(t,50) for t in range(601)]+[(t,50) for t in range(1860,3601)]
        self.assertAlmostEqual(a.off_wrist_fraction(0,3600,gappy,[(800,1500)]),1260/3600)
        self.assertAlmostEqual(a.off_wrist_fraction(0,3600,gappy,[(2400,3000)]),1860/3600)
        self.assertEqual(a.off_wrist_fraction(0,3600,[]),0)
        self.assertFalse(a.passes_morning_guard(9*3600,11*3600,74,80,8*3600))
        self.assertTrue(a.passes_morning_guard(9*3600,11*3600,70,78,8*3600))
        states=[(9*3600+i*60,2 if i<80 else 1) for i in range(100)]
        self.assertTrue(a.passes_morning_guard(9*3600,11*3600,74,80,8*3600,states))
        self.assertTrue(a.passes_morning_guard(14*3600,16*3600,70,80))

    def test_sleep_quiescence_adaptive_floor_and_exact_duration_gates(self):
        self.assertEqual(a.adaptive_overnight_baseline([35,37,39]),40)
        self.assertEqual(a.adaptive_overnight_baseline([50,54,52]),52)
        self.assertIsNone(a.adaptive_overnight_baseline([None,float("nan")]))
        dense=[(t,0,0,1) for t in range(3601)]
        self.assertTrue(a.deeply_quiescent(0,3600,dense))
        self.assertFalse(a.deeply_quiescent(0,3600,[(t,0,0,1) for t in range(0,3600,60)]))
        exact=a.detect_sleep([(t,50) for t in range(3601)],dense)
        self.assertEqual(exact["value"],[])
        self.assertEqual(exact["candidates"][0]["gate"],"minSleepMin")
        cap=a.detect_sleep([],[(t,0,0,1) for t in range(0,57662,60)])
        self.assertEqual(cap["candidates"][0]["gate"],"maxMainSleepSpanS")

    def test_skin_raw_per_device_anchor_source_gates(self):
        self.assertEqual(a.skin_temp_anchor([1290]*100)["value"],1290)
        self.assertEqual(a.skin_temp_anchor([1290]*99+[509,2047])["value"],826)
        self.assertEqual(a.skin_temp_anchor([1200]*50+[1400]*50)["value"],1300)

    def test_lift_muscle_rpe_and_session_load_source_literals(self):
        sets=[dict(exercise="Bench",weight_kg=100,reps=5,rpe=7.5,primaryMuscle="chest",secondaryMuscles=["chest","triceps","frontDelts"]),dict(exercise="Bench",weight_kg=50,reps=5,rpe=8,primaryMuscle="chest"),dict(exercise="Bench",weight_kg=50,reps=5,rpe=9.5,primaryMuscle="chest"),dict(exercise="Bench",weight_kg=200,reps=1,rpe=3,isWarmup=True,primaryMuscle="chest"),dict(exercise="Bench",weight_kg=50,reps=0,primaryMuscle="chest")]
        got=a.lift_metrics(sets)
        self.assertEqual(got["muscle_counts"]["fractional"]["chest"],3)
        self.assertEqual(got["muscle_counts"]["fractional"]["triceps"],.5)
        self.assertNotIn("chest",got["muscle_counts"]["indirect"])
        self.assertEqual(got["rpe_profile"]["sets_at_or_above_threshold"],2)
        self.assertEqual(got["rpe_profile"]["rated_sets"],3)
        self.assertEqual(got["exercises"][0]["warmup_sets"],1)
        self.assertAlmostEqual(got["exercises"][0]["estimated_1rm_kg"],116.6667,places=3)
        self.assertEqual(a.lift_session_load(7,3600)["value"],420)
        self.assertIsNone(a.lift_session_load(None,3600)["value"])

    def test_day_cycle_literal_source_boundaries(self):
        # DayCycleTests.swift: exact18h branch, rolling branch and UTC-5.
        self.assertEqual(a.fallback_midnight_after(21600),86400)
        self.assertEqual(a.fallback_midnight_after(82800),172800)
        self.assertEqual(a.fallback_midnight_after(39600,-18000),104400)
        sleep=dict(id="sleep",start=72000,day="1970-01-01",source="detected_sleep")
        self.assertEqual(a.active_day_cycle("sleep_onset",sleep,172800)["start"],72000)
        self.assertEqual(a.active_day_cycle("sleep_onset",dict(sleep,start=0),144000)["source"],"synthetic_midnight")
        self.assertEqual(a.active_day_cycle("midnight",None,86500)["start"],86400)
        windows=a.physiological_cycle_windows([dict(id="one",start=100),dict(id="two",start=500),dict(id="one",start=100)],900)
        self.assertEqual([(w["start"],w["end"]) for w in windows],[(100,500),(500,900)])
        self.assertEqual(a.analytics_settings({})["day_cycle_mode"],"sleep_onset")

    def test_manual_steps_control_is_source_direct_coefficient(self):
        # StepsEstimateEngineTests.swift uses calibration ratios100steps/motion.
        settings=dict(step_calibration=100,profile_provenance=dict(step_calibration="user"))
        got=a.steps_estimate(20,manual=a.manual_steps_coefficient(settings))
        self.assertEqual(got["value"],2000)
        self.assertEqual(got["source"],"manual")
        self.assertTrue(got["estimated"])
        self.assertEqual(a.steps_estimate(20,manual=a.manual_steps_coefficient(settings|dict(step_calibration=110)))["value"],2200)
        self.assertIsNone(a.manual_steps_coefficient(dict(step_calibration=1,profile_provenance=dict(step_calibration="default"))))
        self.assertIsNone(a.manual_steps_coefficient(dict(step_calibration=0)))

    def test_baseline_epoch_source_reseed_literal(self):
        days=["2026-06-08","2026-06-09","2026-06-10","2026-06-11","2026-06-12","2026-06-13","2026-06-15","2026-06-16","2026-06-17","2026-06-18","2026-06-19","2026-06-20"]
        values=[90,91,89,90,92,88,54,55,53,54,56,54]
        epoch=dt.datetime(2026,6,15,tzinfo=dt.timezone.utc).timestamp()*1000
        got=a.baseline(values,day_keys=days,since_ms=epoch)
        self.assertEqual(got["valid_nights"],6)
        self.assertAlmostEqual(got["mean"],54,delta=2)
        self.assertGreater(a.baseline(values)["mean"],got["mean"]+10)
        self.assertEqual(a.baseline(values,day_keys=days,since_ms=0),a.baseline(values))

    def test_custom_zone_validation_and_banister_source_ceiling(self):
        self.assertEqual(a.valid_zone_thresholds("100,120,140,160,180"),[100,120,140,160,180])
        self.assertIsNone(a.valid_zone_thresholds("100,120,120,160,180"))
        self.assertIsNone(a.valid_zone_thresholds("10,120,140,160,180"))
        for sex in ("male","female"):
            self.assertEqual(a.effort([(t,185) for t in range(86400)],185,55,"banister",sex)["value"],100)
        # StrainBanisterDenominatorTests:45%HRR hour Edwards0, Banister>40.
        hr=[(t,113.5) for t in range(3600)]
        self.assertEqual(a.effort(hr,185,55,"edwards")["value"],0)
        self.assertGreater(a.effort(hr,185,55,"banister","male")["value"],40)
        self.assertEqual(a.effort(hr+[(t,55) for t in range(3600,7200)],185,55,"banister","male")["trimp"],a.effort(hr,185,55,"banister","male")["trimp"])

    def test_service_sleep_cycle_assigns_onset_boundary_without_timestamp_shift(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"cycle.db";conn=sqlite3.connect(path)
            conn.execute("CREATE TABLE readings(frame_id INTEGER,device TEXT,kind TEXT,timestamp_ms INTEGER,hr INTEGER,rr_json TEXT,contact INTEGER,gx REAL,gy REAL,gz REAL)")
            midnight=dt.datetime(2026,6,1,tzinfo=dt.timezone.utc).timestamp()
            onset=midnight+22*3600
            rows=[(i,"d","history",int((midnight+21*3600+i)*1000),130,"[]",1,None,None,None) for i in range(600)]
            rows += [(600+i,"d","history",int((onset+i)*1000),185,"[]",1,None,None,None) for i in range(600)]
            rows += [(1200+i,"d","history",int((midnight+86400+7200+i)*1000),50,"[]",1,None,None,None) for i in range(300)]
            conn.executemany("INSERT INTO readings VALUES(?,?,?,?,?,?,?,?,?,?)",rows);conn.commit();conn.close()
            settings=dict(timezone="UTC",hr_max=185,hr_rest=55,day_cycle_mode="sleep_onset",sleep_sessions=[dict(start=midnight-7200,end=midnight+21600,stages=[dict(start=midnight-7200,end=midnight+21600,stage="deep")]),dict(start=onset,end=midnight+86400+21600,stages=[dict(start=onset,end=midnight+86400+21600,stage="deep")])])
            entries=a.AnalyticsService(SimpleNamespace(path=path)).trends("d",2,settings,"2026-06-02")["days"]
            first,second=entries
            self.assertEqual(first["day_cycle"]["end"],onset)
            self.assertEqual(second["day_cycle"]["start"],onset)
            self.assertEqual(first["effort"]["coverage"]["samples"],600)
            self.assertEqual(second["effort"]["coverage"]["samples"],900)
            self.assertLess(first["effort"]["value"],second["effort"]["value"])
            self.assertEqual(first["calendar_metrics"]["effort"]["coverage"]["samples"],1200)
            conn=sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT timestamp_ms FROM readings WHERE frame_id=600").fetchone()[0],int(onset*1000))
            conn.close()

    def test_main_night_fragments_are_not_canonical_naps(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'sleep.db';conn=sqlite3.connect(path)
            conn.execute('CREATE TABLE readings(frame_id INTEGER,device TEXT,kind TEXT,timestamp_ms INTEGER,hr INTEGER,rr_json TEXT,contact INTEGER,gx REAL,gy REAL,gz REAL)');conn.commit();conn.close()
            midnight=dt.datetime(2026,6,1,tzinfo=dt.timezone.utc).timestamp()
            sessions=[]
            # Source bridge:60min gap is<90min for03:00 overnight re-onset.
            for start,end,kind in [(midnight-7200,midnight+7200,'unclassified'),(midnight+10800,midnight+21600,'unclassified'),(midnight+50400,midnight+54000,'nap')]:
                sessions.append(dict(start=start,end=end,sleep_kind=kind,stages=[dict(start=start,end=end,stage='light')]))
            got=a.AnalyticsService(SimpleNamespace(path=path))._calculate('d','2026-06-01',dict(timezone='UTC',sleep_sessions=sessions))
            self.assertEqual(got['sleep']['main_indices'],[0,1])
            self.assertEqual(len(got['sleep']['main_sessions']),2)
            self.assertEqual(len(got['sleep']['naps']),1)
            self.assertEqual([s['is_nap'] for s in got['sleep']['value']],[False,False,True])
            history=a.AnalyticsService(SimpleNamespace(path=path)).trends('d',1,dict(timezone='UTC',sleep_sessions=sessions),'2026-06-01')
            self.assertEqual(history['days'][0]['sleep']['debt']['nights'][0]['slept_min'],480)

    def test_hrv_final_closed_bucket_and_source_deep_selection(self):
        # HrvWindowSweepOracleTests: the session end belongs to its final bucket.
        beats=[(i,800) for i in range(0,301,10)]
        windows=a.hrv_windows(0,300,beats,[dict(start=0,end=300,stage="deep")])
        self.assertEqual(len(windows),1)
        self.assertEqual(windows[0]["clean_beats"],31)
        self.assertEqual(windows[0]["stage"],"deep")
        sessions=[dict(start=0,end=600,hrv=dict(value=20,coverage={}),hrv_windows=[dict(stage="deep",rmssd_ms=30,sdnn_ms=10)]),dict(start=600,end=900,hrv=dict(value=50,coverage={}),hrv_windows=[dict(stage="light",rmssd_ms=50,sdnn_ms=20)])]
        self.assertEqual(a.daily_hrv(sessions,"whole")["value"],30)
        self.assertEqual(a.daily_hrv(sessions,"deep")["value"],30)
        sessions[0]["hrv"]=dict(value=None,coverage=dict(beat_coverage=1.2))
        self.assertIsNone(a.daily_hrv(sessions,"whole",[0])["value"])
        self.assertEqual(a.daily_hrv(sessions,"deep",[0])["value"],30)
        self.assertIsNotNone(a.daily_hrv(sessions,"deep",[0])["integrity_warning"])
        self.assertIsNone(a.daily_hrv(sessions[1:],"deep")["value"])

    def test_sdnn_index_source_drift_and_sparse_oracles(self):
        # HRVAnalyzerTests.swift ±5ms segments centered800/900/1000ms.
        rr=[(start+t,center+(-5 if t%2==0 else 5)) for start,center in [(0,800),(100,900),(200,1000)] for t in range(50)]
        index=a.sdnn_index(rr,100)
        self.assertAlmostEqual(index,5.05,delta=1.5)
        self.assertGreater(a.hrv([v for _,v in rr])["sdnn_ms"],50)
        self.assertIsNone(a.sdnn_index([(i,800) for i in range(10)],100))
        self.assertIsNone(a.sdnn_index(rr,0))
        self.assertIsNone(a.sdnn_index([(0,800)]*50,100))

    def test_day_energy_swift_literal_oracle_and_birth_date(self):
        profile=a.profile_for_day(dict(age=0,birth_date="1991-06-16",weight_kg=80,height_cm=180,sex="male"),"2026-06-15")
        self.assertEqual(profile["age"],34)
        self.assertEqual(a.profile_for_day(profile|dict(age=0),"2026-06-16")["age"],35)
        profile["age"]=35
        # DayCaloriesTests.swift parity vectors copied verbatim, not generated here.
        for hr,rest,active,total,seconds in [([(i,130) for i in range(600)],12.675326388889,103.105766084605,115.781092473494,600), ([(i,130) for i in range(0,600,30)],12.675326388889,103.105766084603,115.781092473492,600), ([(0,130),(3600,130)],2.535065277778,20.621153216921,23.156218494699,120)]:
            got=a.day_energy(hr,profile,185,55)
            self.assertAlmostEqual(got["resting_kcal"],rest,places=9)
            self.assertAlmostEqual(got["active_kcal"],active,places=9)
            self.assertAlmostEqual(got["value"],total,places=9)
            self.assertEqual(got["coverage"]["observed_seconds"],seconds)
        self.assertIsNone(a.day_energy([(0,130)],dict(age=0),185,55)["value"])

    def test_imported_oxygen_fraction_and_raw_adc_gates(self):
        # AppleHealthImporter sample unit% value .97 maps to97 via explicit HK type.
        samples=[dict(source="apple",timestamp_ms=1000,values=dict(sensor_kind="HKQuantityTypeIdentifierOxygenSaturation",sensor_values=dict(value="0.97",unit="%")))]
        self.assertEqual(a.blood_oxygen(samples)["value"],97)
        samples[0]["values"]["sensor_values"]["value"]=98
        self.assertEqual(a.blood_oxygen(samples)["value"],98)
        raw=a.blood_oxygen([dict(values=dict(ppg_red_ir=500,spo2_raw=42))])
        self.assertIsNone(raw["value"])
        self.assertEqual(raw["coverage"]["raw_only_samples"],1)

    def test_hr_only_source_percentile_minute_and_dropout_gates(self):
        # SleepStager p10 uses floor((n-1)*.1), not interpolated percentile.
        hr=[(t,50) for t in range(3660)]+[(t,80) for t in range(3660,7200)]
        got=a.hr_only_sleep(hr)
        self.assertEqual(got["anchor_bpm"],50)
        self.assertEqual(got["value"][0]["start"],0)
        self.assertEqual(got["value"][0]["end"],3659)
        self.assertTrue(got["value"][0]["hr_only"])
        self.assertEqual(a.hr_only_sleep([(t,50) for t in range(0,1800)]+[(t,50) for t in range(3600,5400)])["value"],[])

    def test_explicit_lift_sessions_previous_exercise_volume(self):
        records=[dict(id="a",kind="lifting_set",day="2026-06-01",start_ms=1000,session_id="s1",exercise="Bench",weight_kg=100,reps=5),dict(id="b",kind="lifting_set",day="2026-06-01",start_ms=2000,session_id="s2",exercise="Bench",weight_kg=110,reps=5),dict(id="unlinked",kind="lifting_set",day="2026-06-01",exercise="Bench",weight_kg=500,reps=5)]
        got=a.lift_session_progression(records)
        self.assertEqual(got["coverage"]["sessions"],2)
        self.assertEqual(got["coverage"]["unassociated_sets"],1)
        exercise=got["value"][1]["exercises"][0]
        self.assertEqual(exercise["previous_volume_kg"],500)
        self.assertEqual(exercise["volume_delta_kg"],50)
        self.assertEqual(exercise["previous_session_id"],"s1")
        for r in records:r.pop("start_ms",None)
        self.assertIsNone(a.lift_session_progression(records)["value"][1]["exercises"][0]["previous_session_id"])

    def test_subsecond_owned_stream_precedes_imported_same_second(self):
        imported=dict(timestamp_ms=1000,kind="imported",hr=200,rr_json="[800]",contact=1,source="apple",gx=9,gy=9,gz=9)
        owned=dict(timestamp_ms=1900,kind="live",hr=60,rr_json="[1000]",contact=1,source="strap",gx=0,gy=0,gz=1)
        hr,rr,grav=a.AnalyticsService._streams([imported,owned],[])
        self.assertEqual(hr,[(1,60)])
        self.assertEqual(rr,[(1,1000)])
        self.assertEqual(grav,[(1,0,0,1)])


if __name__=="__main__":
    unittest.main()

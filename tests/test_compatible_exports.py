"""Pinned NOOP WhoopCsvExporter layout/conversion fixtures for computed days."""
import csv
import io
import unittest

from compatible_exports import export_computed,stage_minutes


def rows(value):
    return list(csv.DictReader(io.StringIO(value,newline="")))


class CompatibleComputedExports(unittest.TestCase):
    def test_literal_cycle_fixture_columns_and_scale_conversions(self):
        # WhoopCsvExporterTests.swift's DailyMetric+series golden values.
        main=dict(start=1780250400,end=1780277700,efficiency=.923,stage_seconds=dict(light=12600,deep=5700,rem=6900,awake=2100))
        day=dict(day="2026-06-01",device="d",timezone="Australia/Brisbane",charge=dict(value=72),resting_hr=dict(value=52),hrv=dict(value=68.4),skin_temperature=dict(value=33.1),blood_oxygen=dict(value=96),effort=dict(value=12.5,display_value=2.625),calories=dict(value=2450),max_hr=165,avg_hr=68,rest=dict(value=85),respiration=dict(value=14.2),sleep=dict(main=main,value=[main],naps=[],consistency=dict(value=.88),need_hours=8,debt=dict(balance_min=-60)))
        files=export_computed([day],dict(timezone="Australia/Brisbane",effort_scale="whoop"))
        expected_header="Cycle start time,Cycle end time,Cycle timezone,Recovery score %,Resting heart rate (bpm),Heart rate variability (ms),Skin temp (celsius),Blood oxygen %,Day Strain,Energy burned (cal),Max HR (bpm),Average HR (bpm),Sleep onset,Wake onset,Sleep performance %,Respiratory rate (rpm),Asleep duration (min),In bed duration (min),Light sleep duration (min),Deep (SWS) duration (min),REM duration (min),Awake duration (min),Sleep efficiency %,Sleep consistency %,Sleep need (min),Sleep debt (min),Source\r\n"
        self.assertTrue(files["physiological_cycles.csv"].startswith(expected_header))
        self.assertEqual(rows(files["physiological_cycles.csv"])[0],dict(zip(expected_header.strip().split(","),["2026-06-01 00:00:00","","UTC+00:00","72","52","68.4","33.1","96","2.625","2450","165","68","","","85","14.2","420","455","210","95","115","35","92.30000000000001","88","480","60","boop (APPROXIMATE)"])))
        sleep=rows(files["sleeps.csv"])[0]
        self.assertEqual(sleep["Cycle start time"],"2026-06-01 00:00:00")
        self.assertEqual(sleep["Sleep onset"],"2026-05-31 18:00:00")
        self.assertEqual(sleep["Wake onset"],"2026-06-01 01:35:00")
        self.assertEqual(sleep["Cycle timezone"],"UTC+00:00")
        self.assertEqual(sleep["Nap"],"false")
        self.assertEqual(sleep["Sleep performance %"],"")
        self.assertEqual(sleep["Asleep duration (min)"],"420")

    def test_workout_literal_source_fields_escaping_and_canonical_zones(self):
        workout=dict(start=1780315200,end=1780318800,sport='Run, "tempo"\nintervals',effort=11.2,calories=dict(value=540),avg_hr=158,peak_hr=182,distance_m=8000,zone_percent=[0,5,20,40,30,5])
        files=export_computed([dict(day="2026-06-01",workouts=dict(value=[workout]))])
        row=rows(files["workouts.csv"])[0]
        self.assertEqual(list(row),"Cycle start time,Workout start time,Workout end time,Cycle timezone,Activity name,Activity Strain,Energy burned (cal),Max HR (bpm),Average HR (bpm),HR Zone 1 %,HR Zone 2 %,HR Zone 3 %,HR Zone 4 %,HR Zone 5 %,Distance (meters),Source".split(","))
        self.assertEqual(row["Workout start time"],"2026-06-01 12:00:00")
        self.assertEqual(row["Workout end time"],"2026-06-01 13:00:00")
        self.assertAlmostEqual(float(row["Activity Strain"]),2.352)
        self.assertEqual(row["Activity name"],workout["sport"])
        self.assertEqual(row["HR Zone 3 %"],"40")
        self.assertEqual(row["Distance (meters)"],"8000")
        self.assertEqual(row["Source"],"boop (APPROXIMATE)")
        workout["sport"]="=SUM(A1:A2)"
        self.assertEqual(rows(export_computed([dict(day="2026-06-01",workouts=dict(value=[workout]))])["workouts.csv"])[0]["Activity name"],"'=SUM(A1:A2)")

    def test_partial_night_does_not_invent_sleep_and_missing_days_skip(self):
        day=dict(day="2026-06-01",coverage=dict(hr_samples=6892,rr_intervals=4483,gravity_samples=7133,sensor_samples=8129),effort=dict(value=0),hrv=dict(value=None),rest=dict(value=None),charge=dict(value=None),sleep=dict(value=[],main=None,need_hours=8,debt=dict(balance_min=0)),workouts=dict(value=[]))
        files=export_computed([day,dict(day="2026-05-31",sleep=dict(need_hours=8))])
        self.assertEqual(rows(files["sleeps.csv"]),[])
        self.assertEqual(rows(files["workouts.csv"]),[])
        cycle=rows(files["physiological_cycles.csv"])
        self.assertEqual(len(cycle),1)
        for key in ("Recovery score %","Sleep performance %","Asleep duration (min)","Sleep need (min)","Sleep debt (min)","Blood oxygen %","Max HR (bpm)"):
            self.assertEqual(cycle[0][key],"")
        self.assertEqual(cycle[0]["Day Strain"],"0")
        self.assertEqual(rows(export_computed([])["physiological_cycles.csv"]),[])

    def test_source_tolerant_stage_shapes_and_honest_naps(self):
        for session,light,deep,asleep in [(dict(stages=dict(light=210,deep=95,rem=115,awake=35)),210,95,420),(dict(stages=[dict(stage="light",min=200),dict(stage="deep",min=80)]),200,80,280),(dict(stages=[dict(start=2000000000,end=2000003600,stage="light"),dict(start=2000003600,end=2000007200,stage="deep")]),60,60,120)]:
            got=stage_minutes(session)
            self.assertEqual((got["light"],got["deep"],got["asleep"]),(light,deep,asleep))
        self.assertIsNone(stage_minutes({})["asleep"])
        self.assertIsNone(stage_minutes(dict(stages=dict(awake=35)))["asleep"])
        nap=dict(start=1780322400,end=1780324200,sleep_kind="nap",stages=[dict(start=1780322400,end=1780324200,stage="light")])
        day=dict(day="2026-06-01",sleep=dict(value=[nap],main=nap,naps=[nap]))
        files=export_computed([day,day])
        self.assertEqual(len(rows(files["sleeps.csv"])),1)
        self.assertEqual(rows(files["sleeps.csv"])[0]["Nap"],"true")
        fragment=dict(start=1780264800,end=1780275600,is_nap=False,stages=[dict(start=1780264800,end=1780275600,stage='light')])
        row=rows(export_computed([dict(day='2026-06-01',sleep=dict(value=[fragment],naps=[fragment]))])["sleeps.csv"])[0]
        self.assertEqual(row['Nap'],'false')


if __name__=="__main__":unittest.main()

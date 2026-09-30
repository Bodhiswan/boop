"""Literal reachable NOOP HRR, WorkoutSource and LiftSession oracle fixtures."""
import json
import unittest

from workout_tools import (heart_rate_recovery,hr_samples,manual_workout_rescore,
                           rescore_improves,rescore_workout,merge_workouts,
                           workout_edit_plan,LiftSession,rest_event_times,
                           after_session_program)


class WorkoutToolsTests(unittest.TestCase):
    def hrr_samples(self,end_hr=170):
        return [(t,end_hr if t>=9970 else 145) for t in range(9700,10001)]
    def reading(self,minutes,values):
        return [(10000+minutes*60-len(values)//2+i,h) for i,h in enumerate(values)]

    def test_hrr_literal_robust_one_two_five_minute_drops(self):
        samples=self.hrr_samples()+self.reading(1,[146,146,220,146,146])+self.reading(2,[132]*3)+self.reading(5,[112]*3)
        got=heart_rate_recovery(list(reversed(samples)),9700,10000,200)
        self.assertEqual(got['value'],dict(end_hr_bpm=170,after_1_minute_bpm=24,after_2_minute_bpm=38,after_5_minute_bpm=58))
        self.assertEqual(heart_rate_recovery(self.hrr_samples(160)+self.reading(1,[165]*3),9700,10000,200)['value']['after_1_minute_bpm'],-5)

    def test_hrr_actual_continuity_coverage_and_exact_caps(self):
        exact=[(t,170) for t in range(9880,10000,10)]+[(10000,120)]+self.reading(1,[140]*3)
        self.assertEqual(heart_rate_recovery(exact,9700,10000,200)['value'],dict(end_hr_bpm=170,after_1_minute_bpm=30,after_2_minute_bpm=None,after_5_minute_bpm=None))
        sparse=[(t,170) for t in range(9700,10001,15)]+self.reading(1,[140]*3)
        self.assertIsNone(heart_rate_recovery(sparse,9700,10000,200)['value'])
        self.assertIsNone(heart_rate_recovery(self.hrr_samples(),9700,10000,200)['value'])
        self.assertIsNone(heart_rate_recovery(self.hrr_samples()+self.reading(1,[140]*3),9940,10000,200)['value'])
        thin=heart_rate_recovery(self.hrr_samples()+self.reading(1,[150]*3)+self.reading(5,[110]*2),9700,10000,200)['value']
        self.assertEqual(thin['after_1_minute_bpm'],20)
        self.assertIsNone(thin['after_5_minute_bpm'])
        self.assertEqual(hr_samples([{'hr':140},{'timestamp_ms':1000,'hr':140},{'timestamp_ms':2000,'hr':150,'contact':0}]),[(1,140)])

    def test_rescore_literal_varied_mean_and_conservative_gate(self):
        profile=dict(weight_kg=80,height_cm=180,age=30,sex='male')
        samples=[(1000+t,100+t) for t in range(60)]+[(1060+t,180) for t in range(60)]
        got=manual_workout_rescore(samples,profile,190)
        self.assertEqual(got['value']['avg_hr'],155)
        self.assertEqual(got['value']['peak_hr'],180)
        self.assertIsNone(got['value']['effort'])
        self.assertIsNone(manual_workout_rescore([(1000,140)],profile,190)['value'])
        dense=manual_workout_rescore([(1000+t,140) for t in range(1200)],profile,190)['value']
        self.assertGreater(dense['calories'],50)
        self.assertIsNotNone(dense['effort'])
        self.assertFalse(rescore_improves(dict(calories=220,effort=9),219.5))
        self.assertFalse(rescore_improves(dict(calories=120,effort=9),300))
        self.assertTrue(rescore_improves(dict(calories=120,effort=9),300,None,True))
        self.assertFalse(rescore_improves(dict(calories=120,effort=9),300,9,True))
        record=dict(id='m',source='manual',start=1000,end=2199,calories=300,effort=None)
        changed=rescore_workout(record,[(1000+t,100) for t in range(1200)],profile,190,60,allow_effort_only_fill=True)
        self.assertIsNotNone(changed['value'])
        self.assertEqual(changed['value']['calories'],300)
        self.assertEqual(record['calories'],300)
        self.assertIsNone(rescore_workout(record|dict(source='whoop'),samples,profile,190)['value'])

    def test_merge_literal_active_time_weighted_hr_and_import_gate(self):
        a=dict(id='a',start=1000,end=4600,sport='Running',source='manual',avg_hr=150,calories=600,distance_m=10000,peak_hr=178,steps=6000)
        b=dict(id='b',start=5000,end=7400,sport='Running',source='manual',avg_hr=120,calories=300,distance_m=5000,peak_hr=150,steps=3300)
        got=merge_workouts([a,b])['value']
        self.assertEqual((got['start'],got['end'],got['duration_seconds']),(1000,7400,6000))
        self.assertEqual((got['calories'],got['distance_m'],got['avg_hr'],got['peak_hr'],got['steps']),(900,15000,138,178,9300))
        self.assertIsNone(got['effort']);self.assertIsNone(got['zone_percent'])
        self.assertIsNone(merge_workouts([a,b|dict(source='whoop')])['value'])
        unknown=merge_workouts([a|dict(calories=None,distance_m=None,steps=None),b|dict(calories=None,distance_m=None,steps=None)])['value']
        self.assertIsNone(unknown['calories']);self.assertIsNone(unknown['distance_m'])

    def test_edit_proposals_bind_and_never_mutate_input(self):
        row=dict(id='x',device='d',start_ms=1000000,end_ms=4600000,sport='detected',source='d-noop')
        dismissed=workout_edit_plan('dismiss',[row])
        self.assertEqual(dismissed['value']['retire'][0]['dismiss_token'],'1000:4600')
        relabeled=workout_edit_plan('relabel',[row],'Running')
        self.assertEqual(relabeled['value']['write']['source'],'manual')
        self.assertEqual(relabeled['value']['write']['sport'],'Running')
        self.assertNotIn('id',relabeled['value']['write'])
        self.assertEqual(relabeled['value']['retire'][0]['id'],'x')
        self.assertEqual(row['sport'],'detected')
        self.assertFalse(dismissed['persisted'])
        self.assertIsNone(workout_edit_plan('dismiss',[row|dict(source='apple-health')])['value'])

    def plan(self):
        return [dict(exercise='Incline dumbbell press',target_sets=2,rest_seconds=90),dict(exercise='Lat pulldown',target_sets=1,rest_seconds=60)]

    def test_lift_literal_absolute_rest_and_measured_rest(self):
        e=LiftSession(self.plan(),1700000000)
        self.assertEqual(e.next_pending(),(0,1));e.advance(1700000000);e.advance(1700000010)
        self.assertEqual(e.ends_at,1700000100)
        self.assertEqual(e.rest_remaining(1700000055),45)
        self.assertEqual(e.rest_remaining(1700005000),0)
        self.assertEqual(e.stage,'resting')
        e.advance(1700000210)
        self.assertEqual(e.sets[0]['rest_seconds'],200)
        self.assertEqual(e.slot,(0,2))
        self.assertEqual(rest_event_times(1700000090,1700000000),dict(warning=1700000085,end=1700000090))
        self.assertEqual(rest_event_times(1700000003,1700000000),dict(warning=1700000001,end=1700000003))
        json.dumps(e.snapshot(),allow_nan=False)
        restored=LiftSession.from_snapshot(json.loads(json.dumps(e.snapshot())))
        self.assertEqual(restored.slot,e.slot)
        self.assertEqual(restored.sets,e.sets)
        self.assertFalse(restored.history)

    def test_lift_occupied_machine_stays_then_returns_to_skipped_exercise(self):
        plan=[dict(exercise='Leg press',target_sets=3,rest_seconds=90),dict(exercise='Leg curl',target_sets=3,rest_seconds=90),dict(exercise='Leg extension',target_sets=2,rest_seconds=60)]
        e=LiftSession(plan,1700000000);e.start((2,1),1700000060);e.advance(1700000100);e.advance(1700000160)
        self.assertEqual(e.slot,(2,2));e.advance(1700000200);e.advance(1700000260)
        self.assertEqual(e.slot,(0,1))
        e.undo();self.assertEqual(e.stage,'resting');self.assertEqual(e.slot,(2,2))
        e.undo();self.assertEqual(len(e.sets),1)

    def test_lift_grey_carry_finish_explicit_unstarted_choice_and_rpe_provenance(self):
        plan=[dict(exercise='Leg press',target_sets=3,rest_seconds=60,target_weight_kg=50,target_reps_low=10,target_rpe=8)]
        e=LiftSession(plan,1700000000)
        self.assertEqual(e.carry((0,1),{1:dict(weight_kg=52.5,reps=9)}),dict(weight_kg=52.5,reps=9))
        e.advance(1700000010);e.advance(1700000070)
        self.assertIsNone(e.sets[0]['weight_kg']);self.assertIsNone(e.sets[0]['rpe'])
        e.update_set((0,1),45,8,9);self.assertEqual(e.carry((0,2)),dict(weight_kg=45,reps=8))
        e.advance(1700000130);e.advance(1700000190)
        self.assertIsNone(e.sets[1]['rpe'])
        e.finish(1700000220)
        saved=e.finish_sets(False)
        self.assertEqual([s['weight_kg'] for s in saved],[45,45,0])
        self.assertEqual([s['reps'] for s in saved],[8,8,0])
        self.assertEqual(saved[0]['rpe'],9);self.assertFalse(saved[0]['rpe_is_planned'])
        self.assertEqual(saved[1]['rpe'],8);self.assertTrue(saved[1]['rpe_is_planned'])
        self.assertIsNone(saved[2]['rpe'])
        self.assertIsNone(e.finish_sets(True)[2]['start_ts'])
        self.assertEqual(e.finish_sets(True)[2]['reps'],8)

    def test_lift_plan_bounds_undo_and_heaviest_actual_program_progression(self):
        e=LiftSession([dict(exercise='Bench',target_sets=2,program_item_id='b')],1000)
        self.assertEqual(e.plan[0]['rest_seconds'],120)
        self.assertTrue(e.add_set(0));self.assertEqual(e.plan[0]['target_sets'],3)
        e.undo();self.assertEqual(e.plan[0]['target_sets'],2)
        e.start((0,2),1001);self.assertFalse(e.remove_set(0))
        program=[dict(id='b',target_weight_kg=50,target_reps=10,target_reps_low=10,target_reps_high=12,target_sets=2,note='keep')]
        sets=[dict(exercise_index=0,start_ts=1000,weight_kg=55,reps=8,is_warmup=False),dict(exercise_index=0,start_ts=1100,weight_kg=55,reps=13,is_warmup=False),dict(exercise_index=0,start_ts=None,weight_kg=100,reps=5),dict(exercise_index=0,start_ts=1200,weight_kg=200,reps=1,is_warmup=True)]
        updated=after_session_program(sets,e.plan,program)
        self.assertEqual(updated[0]['target_weight_kg'],55)
        self.assertEqual(updated[0]['target_reps_low'],13)
        self.assertEqual(updated[0]['target_reps'],13)
        self.assertIsNone(updated[0]['target_reps_high'])
        self.assertEqual(updated[0]['note'],'keep')
        self.assertEqual(program[0]['target_weight_kg'],50)


if __name__=='__main__':unittest.main()

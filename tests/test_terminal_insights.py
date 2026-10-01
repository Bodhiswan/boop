import unittest
from terminal_insights import personal_baselines,clock_profile,day_rhythm,motion_profile
from coach_evidence import evidence_catalog,referenced_evidence

class TerminalInsightTests(unittest.TestCase):
 def test_scatter_only_pairs_coobserved_minutes(self):
  result=day_rhythm([(0,60),(61,100)],[(60,0,0,1),(61,0,1,1)],0,900)
  point=result['points'][0]
  self.assertEqual(point['hr'],80)
  self.assertEqual(point['paired_hr'],100)
  self.assertEqual(point['paired_motion'],1)
  self.assertEqual(point['paired_minutes'],1)
  unpaired=day_rhythm([(0,60)],[(60,0,0,1),(61,0,1,1)],0,900)['points'][0]
  self.assertIsNone(unpaired['paired_hr'])
 def test_live_motion_bins_preserve_zero_and_recording_gaps(self):
  points=motion_profile([(0,0,0,1),(1,0,0,1),(20,0,1,1),(21,0,2,1)],0,60,10)['points']
  self.assertEqual([p['t'] for p in points],[5000,25000])
  self.assertEqual([p['motion'] for p in points],[0,1])
  self.assertEqual(points[0]['coverage'],.1)
  self.assertEqual(motion_profile([(0,0,0,1)],0,60,10)['points'],[])
 def test_rhythm_weights_minutes_equally_and_leaves_gaps(self):
  hr=[(i,60) for i in range(60)]+[(61,100),(1800,80),(-1,200),(86400,200)]
  result=day_rhythm(hr,[],0,86400)
  self.assertEqual(len(result['points']),2)
  self.assertEqual(result['points'][0]['hr'],80)
  self.assertEqual(result['points'][0]['hr_minutes'],2)
  self.assertEqual(result['hr_minutes'],3)
  self.assertEqual(result['motion_minutes'],0)
  self.assertIsNone(result['points'][0]['motion'])
 def test_rhythm_does_not_invent_motion_across_recording_gaps(self):
  gravity=[(0,0,0,1),(1,0,0,1),(120,1,0,0),(1800,0,0,1),(1801,0,1,1)]
  result=day_rhythm([],gravity,0,86400)
  self.assertEqual([p['motion'] for p in result['points']],[0,1])
  self.assertEqual(result['motion_minutes'],2)
  self.assertTrue(all(p['hr'] is None for p in result['points']))
 def test_rhythm_empty_and_partial_day(self):
  self.assertEqual(day_rhythm([],[],0,0)['points'],[])
  result=day_rhythm([(10,65),(21,100)],[],0,20)
  self.assertEqual(result['points'][0]['hr'],65)
  self.assertEqual(result['observed_until'],20000)
 def test_baseline_excludes_today_and_missing_days(self):
  bundle={'days':[{'day':f'2026-09-{i:02d}','rest':{'value':i}} for i in range(1,8)]+[{'day':'2026-09-08','rest':{'value':None}},{'day':'2026-10-01','rest':{'value':100}}]}
  result=personal_baselines(bundle,'2026-10-01')['metrics']
  self.assertEqual(result['sleep']['n'],7);self.assertEqual(result['sleep']['median'],4)
  self.assertTrue(result['sleep']['ready']);self.assertEqual(result['rhr']['n'],0)
  self.assertIsNone(result['rhr']['median'])
 def test_clock_bins_vote_once_per_day_and_need_three_days(self):
  points=[{'t':day*86400000,'hr':value} for day,value in [(1,60),(1,60),(2,70),(3,80)]]
  result=clock_profile(points);self.assertEqual(len(result),1)
  self.assertEqual(result[0]['median'],70);self.assertEqual(result[0]['n'],3)
  self.assertEqual(clock_profile(points[:3]),[])
 def test_monthly_mean_gives_each_observed_day_one_vote(self):
  points=[{'t':day*86400000,'hr':value} for day,value in [(1,60),(1,60),(2,70),(3,100)]]
  row=clock_profile(points,min_days=1)[0]
  self.assertAlmostEqual(row['mean'],230/3);self.assertEqual(row['median'],70)
  self.assertEqual(clock_profile(points[:1],min_days=1)[0]['n'],1)
 def test_evidence_rejects_invented_ids(self):
  snapshots=[{'topics':['sleep'],'data':{'recent_days_oldest_first':[{'day':'2026-10-01','rest':{'value':84,'source':'local estimate'}}]}}]
  catalog=evidence_catalog(snapshots);self.assertEqual(catalog,evidence_catalog(snapshots))
  key=catalog[0]['id'];clean,cited,unknown=referenced_evidence(f'Sleep [{key}]. Fake [Edeadbeef].',catalog)
  self.assertEqual(cited,catalog);self.assertEqual(unknown,['Edeadbeef']);self.assertNotIn('Edeadbeef',clean)

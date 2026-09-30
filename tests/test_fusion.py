"""Literal Swift/Kotlin NOOP FusionResolver fixture vectors plus BOOP adapter gates."""
import unittest
from fusion import resolve, fuse_day, day_owner, metric_kind, tier, TOLERANCES


class FusionTests(unittest.TestCase):
    def test_pinned_source_vectors(self):
        vectors=[
            ('steps',[('whoopImport',6000),('xiaomiBand',8420)],'xiaomiBand',8420,'minorDelta'),
            ('sleep_total_min',[('appleHealth',400),('whoopImport',432)],'whoopImport',432,'minorDelta'),
            ('rhr',[('appleHealth',55),('whoopImport',52)],'whoopImport',52,'agree'),
            ('steps',[('xiaomiBand',8000),('appleHealth',8100)],'appleHealth',8100,'agree'),
            ('rhr',[('whoopImport',52),('appleHealth',56)],'whoopImport',52,'minorDelta'),
            ('rhr',[('whoopImport',52),('appleHealth',62)],'whoopImport',52,'conflict'),
            ('sleep_total_min',[('whoopImport',432),('appleHealth',120)],'whoopImport',432,'conflict'),
            ('steps',[('xiaomiBand',8000),('whoopImport',8500)],'xiaomiBand',8000,'agree'),
            ('steps',[('xiaomiBand',8000),('whoopImport',14000)],'xiaomiBand',8000,'conflict'),
            ('hrv',[('whoopImport',68)],'whoopImport',68,'single'),
        ]
        for key,values,source,value,state in vectors:
            with self.subTest(key=key,values=values):
                got=resolve(key,[dict(source=s,value=v) for s,v in values])
                self.assertEqual((got['winning_source'],got['value'],got['agreement']),(source,value,state))
                self.assertEqual(len(got['contributors']),len(values))
        self.assertIsNone(resolve('hrv',[]))

    def test_all_tolerance_edges_and_zero_percent(self):
        keys=dict(restingHR='rhr',heartRate='avg_hr',hrv='hrv',spo2='spo2',skinTemp='skin_temp',steps='steps',sleep='sleep_total_min',calories='active_kcal',other='unknown')
        for kind,(agree,minor,percent) in TOLERANCES.items():
            for delta,state in ((agree,'agree'),(minor,'minorDelta'),(minor+1,'conflict')):
                edge=delta*100 if percent else delta
                got=resolve(keys[kind],[dict(source='whoopImport',value=100),dict(source='localCache',value=100+edge)])
                self.assertEqual(got['agreement'],state,(kind,delta))
        self.assertEqual(resolve('steps',[dict(source='appleHealth',value=0),dict(source='whoopImport',value=1)])['agreement'],'conflict')

    def test_policy_families(self):
        self.assertEqual(metric_kind('asleep_min'),'sleep')
        self.assertEqual(metric_kind('skinTemp'),'skinTemp')
        self.assertEqual(metric_kind('recovery'),'other')
        self.assertEqual(tier('steps','whoopImport'),3)
        self.assertEqual(tier('sleep','xiaomiBand'),1)
        self.assertEqual(tier('calories','appleHealth'),2)

    def test_aliases_official_scores_and_date_gate(self):
        computed=dict(day='2026-09-30',charge={'value':66},effort={'value':50},hrv={'value':60},resting_hr={'value':52})
        rows=[dict(id='w',day='2026-09-30',source='whoop-cycle',original={'Heart rate variability (ms)':'68','Recovery score %':'80','Day Strain':'12','Resting heart rate (bpm)':'54'}),dict(date='2026-09-29',source='whoop-cycle',hrv_ms=999)]
        got=fuse_day(computed,rows)
        self.assertEqual(got['winners']['hrv']['value'],68)
        self.assertEqual(got['winners']['hrv']['source'],'whoop-cycle')
        self.assertEqual(got['winners']['recovery']['value'],80)
        self.assertEqual(got['winners']['charge']['value'],66)
        self.assertEqual(got['winners']['strain']['value'],12)
        self.assertEqual(got['winners']['effort']['value'],50)
        self.assertEqual(got['coverage']['skipped_other_days'],1)
        self.assertEqual(got['agreements']['hrv'],'agree')

    def test_incomplete_sdnn_and_temperature_identity(self):
        got=fuse_day({'day':'2026-09-30','hrv':{'value':None},'sleep':{'main':{'total_sleep_min':432,'stage_seconds':{'deep':3600}}}},[dict(date='2026-09-30',source='apple-health',hrv_ms=40,hrv_method='SDNN',skin_temp_deviation_c=.2),dict(date='2026-09-30',source='whoop-cycle',skin_temp_c=33)])
        self.assertNotIn('hrv',got['winners'])
        self.assertEqual(got['winners']['hrv_sdnn']['value'],40)
        self.assertEqual(got['winners']['sleep_deep_min']['value'],60)
        self.assertEqual(got['winners']['skin_temp']['agreement'],'single')
        self.assertEqual(got['winners']['skin_temp_deviation_c']['value'],.2)
        self.assertEqual(fuse_day({},[])['coverage']['metrics'],0)

    def test_owner_exact_lock_and_priorities(self):
        candidates=[dict(device_id='import',priority=2,has_data=True),dict(device_id='active',priority=0,has_data=False),dict(device_id='other',priority=1,has_data=True)]
        self.assertEqual(day_owner('2026-09-30',None,candidates),'other')
        self.assertEqual(day_owner('2026-09-30','empty',candidates),'empty')
        self.assertIsNone(day_owner('2026-09-30',None,[]))
        self.assertEqual(fuse_day({},[],{'day_owner_lock':'locked'})['day_owner'],'locked')

    def test_computed_daily_energy_keeps_total_and_active_distinct(self):
        computed=dict(day='2026-09-30',calories=dict(value=115.781092473494,active_kcal=103.105766084605,resting_kcal=12.675326388889,source='WorkoutDetector.swift Calories.estimateDayEnergy',estimated=True,unit='kcal'))
        got=fuse_day(computed,[])
        self.assertEqual(got['winners']['energy_kcal']['value'],115.781092473494)
        self.assertEqual(got['winners']['active_kcal']['value'],103.105766084605)
        self.assertEqual(got['winners']['active_kcal']['source'],'WorkoutDetector.swift Calories.estimateDayEnergy')
        self.assertNotIn('active_kcal',fuse_day({'calories':{'value':10}},[])['winners'])

    def test_no_mutation_nan_or_boolean_observation(self):
        record=dict(source='whoop-cycle',hrv_ms=68)
        got=fuse_day({},[record])
        self.assertEqual(record,dict(source='whoop-cycle',hrv_ms=68))
        self.assertEqual(got['winners']['hrv']['value'],68)
        self.assertIsNone(resolve('steps',[dict(source='whoopImport',value=True),dict(source='whoopImport',value=float('nan'))]))


if __name__=='__main__': unittest.main()

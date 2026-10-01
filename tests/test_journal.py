from pathlib import Path
import tempfile
import unittest
from features import FeatureStore
from journal import JournalService,compare_observations,observed_value

class FakeAnalytics:
    def trends(self,*args):
        return {'days':[{'day':f'2026-09-{i:02d}','hrv':{'value':40+i,'source':'fixture'},'sleep':{'main':{'total_sleep_min':400+i,'start':i*86400,'end':i*86400+25000}},'effort':{'value':i}} for i in range(1,31)]}

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=FeatureStore(Path(self.temp.name)/'journal.sqlite');self.service=JournalService(self.store,FakeAnalytics())
    def tearDown(self):self.temp.cleanup()
    def save(self,item,**values):return self.service.save({'day':'2026-09-20','entries':[dict(item_id=item,**values)]})['entries'][0]
    def test_catalog_defaults_sensitive_opt_in_and_custom_order(self):
        items=self.service.catalog();self.assertEqual(len({i['category'] for i in items}),9)
        self.assertFalse(any(i['enabled'] for i in items if i['category'] in ('Cycle','Sexual wellbeing')))
        self.service.configure({'items':[{'id':'coffee','enabled':False,'order':99}], 'custom':{'name':'My supplement','type':'supplement'}})
        items=self.service.catalog();self.assertFalse(next(i for i in items if i['id']=='coffee')['enabled'])
        self.assertTrue(next(i for i in items if i['name']=='My supplement')['enabled'])
    def test_unknown_is_not_no_and_zero_is_observed(self):
        self.save('coffee',value=0);self.save('late_eating',answer=False)
        check=self.service.checkin('2026-09-20');self.assertEqual(check['logged'],2)
        rows=check['entries'];self.assertEqual(len(rows),2)
        self.assertFalse(next(r for r in rows if r['item_id']=='late_eating')['answer'])
    def test_backdating_events_edits_and_created_time_are_separate(self):
        a=self.save('coffee',mode='event',value=1,event_time='09:30');b=self.save('coffee',mode='event',value=2,event_time='14:30')
        self.assertNotEqual(a['id'],b['id']);self.assertLess(a['timestamp_ms'],a['created_ms'])
        fixed=self.save('coffee',id=a['id'],mode='event',value=3,event_time='10:00')
        self.assertEqual(fixed['created_ms'],a['created_ms']);self.assertNotEqual(fixed['timestamp_ms'],a['timestamp_ms'])
        self.assertEqual(len(self.service.checkin('2026-09-20')['entries']),2)
        spec=next(i for i in self.service.catalog() if i['id']=='coffee')
        self.assertEqual(observed_value(self.service.checkin('2026-09-20')['entries'],spec),5)
    def test_atomic_validation_and_clear_with_undo(self):
        with self.assertRaises(ValueError):self.service.save({'day':'2026-09-20','entries':[{'item_id':'coffee','value':2},{'item_id':'stress','value':8}]})
        self.assertEqual(self.service.checkin('2026-09-20')['entries'],[])
        row=self.save('late_eating',answer=True)
        self.service.save({'day':'2026-09-20','entries':[],'clear_ids':[row['id']]})
        self.assertEqual(self.service.checkin('2026-09-20')['entries'],[])
        self.store.undo_record('journal',row['id']);self.assertEqual(len(self.service.checkin('2026-09-20')['entries']),1)
    def test_backdated_daily_answer_cannot_be_overwritten_from_its_original_day(self):
        row=self.save('coffee',value=2)
        self.service.save({'day':'2026-09-19','entries':[dict(id=row['id'],item_id='coffee',value=2)]})
        self.save('coffee',value=3)
        self.assertEqual(self.service.checkin('2026-09-19')['entries'][0]['value'],2)
        self.assertEqual(self.service.checkin('2026-09-20')['entries'][0]['value'],3)
    def test_daily_quantity_total_overrides_event_sum(self):
        self.save('coffee',mode='event',value=1,event_time='09:30')
        self.save('coffee',value=2)
        spec=next(i for i in self.service.catalog() if i['id']=='coffee')
        self.assertEqual(observed_value(self.service.checkin('2026-09-20')['entries'],spec),2)
    def test_supplement_requires_unit_but_amount_is_optional(self):
        self.save('zinc',answer=True)
        with self.assertRaises(ValueError):self.save('zinc',answer=True,value=2)
        self.save('zinc',answer=True,value=2,unit='tablets',product='Test product')
    def test_previous_answers_are_drafts_until_explicit_submission(self):
        self.save('late_eating',answer=False);self.save('coffee',value=2)
        next_day=self.service.checkin('2026-09-21')
        self.assertEqual(next_day['entries'],[]);self.assertEqual(next_day['logged'],0)
        self.assertEqual({r['item_id'] for r in next_day['carried']},{'late_eating','coffee'})
        self.service.save({'day':'2026-09-21','entries':[{'item_id':'late_eating','answer':False},{'item_id':'coffee','value':2}]})
        self.assertEqual(self.service.checkin('2026-09-21')['logged'],2)
        self.assertEqual(self.service.checkin('2026-09-21')['carried'],[])
    def test_pairing_uses_explicit_controls_and_following_day(self):
        rows=[{'id':str(i),'day':f'2026-09-{i:02d}','answer':i<=5} for i in range(1,11)]
        days=[{'day':f'2026-09-{i:02d}','hrv':i} for i in range(1,31)]
        result=compare_observations(rows,{'type':'boolean'},days,'hrv',1)
        self.assertEqual((result['yes_n'],result['no_n']),(5,5));self.assertEqual(result['status'],'observed')
        self.assertEqual(result['pairs'][0]['outcome_day'],'2026-09-02');self.assertEqual(result['pairs'][0]['value'],2)
        result=compare_observations(rows[:5],{'type':'boolean'},days,'hrv',1)
        self.assertEqual(result['no_n'],0);self.assertEqual(result['status'],'learning');self.assertIsNone(result['delta'])
    def test_mixed_quantity_units_do_not_create_a_false_comparison(self):
        rows=[{'id':'a','day':'2026-09-01','value':2,'unit':'cups'},{'id':'b','day':'2026-09-02','value':50,'unit':'mg'}]
        result=compare_observations(rows,{'type':'quantity','unit':'cups'},[{'day':'2026-09-02','hrv':50},{'day':'2026-09-03','hrv':60}],'hrv',1,True)
        self.assertEqual(result['pairs'],[]);self.assertEqual(result['status'],'learning')
    def test_legacy_history_and_new_cycle_entries_are_visible(self):
        self.store.save_record('journal',{'date':'2026-09-19','question':'My old habit','answer':True,'source':'import'})
        self.assertTrue(any(i['name']=='My old habit' for i in self.service.catalog()))
        self.save('period_start',answer=True);self.save('bleeding',text='Light')
        result=self.service.summary(None,'2026-09-30',30,'coffee','hrv',1)
        self.assertEqual(result['cycle']['day'],11)
        self.assertEqual(len(result['cycle']['entries']),2)
        self.assertTrue(any(r['question']=='My old habit' for r in result['entries']))

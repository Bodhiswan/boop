import unittest
from stress_onset import evaluate,INITIAL_STATE

ON=dict(enabled=True,auto_nudge=True)
HIGH=[900+(60 if i%2==0 else -60) for i in range(60)]
LOW=[900+(5 if i%2==0 else -5) for i in range(60)]

class StressOnsetTests(unittest.TestCase):
    def tick(self,rr,state=None,now=10000,**kwargs):
        args=dict(current_hr=70,recent_motion_g=0,session_active=False,config=ON)
        args.update(kwargs)
        return evaluate(rr,state=state,now_sec=now,**args)
    def armed(self,**kwargs):
        seeded=self.tick(HIGH)
        return self.tick(LOW,seeded['next_state'],10060,**kwargs)
    def test_literal_seed_arm_sustain_fire_once(self):
        seed=self.tick(HIGH)
        self.assertEqual(seed['fast_rmssd'],120)
        self.assertEqual(seed['baseline_rmssd'],120)
        self.assertEqual(seed['reason'],'noDip')
        arm=self.armed()
        self.assertEqual(arm['reason'],'awaitingSustain')
        self.assertAlmostEqual(arm['baseline_rmssd'],117.8)
        early=self.tick(LOW,arm['next_state'],10119)
        self.assertFalse(early['should_nudge'])
        fire=self.tick(LOW,arm['next_state'],10120)
        self.assertTrue(fire['should_nudge'])
        self.assertEqual(fire['next_state']['last_fire_at'],10120)
        again=self.tick(LOW,fire['next_state'],10200)
        self.assertEqual(again['reason'],'notAnEdge')
    def test_offline_replay_disabled_untouched(self):
        state=self.armed()['next_state']
        for kwargs in ({'live':False},{'replay':True},{'config':{}},{'config':{'enabled':True}}):
            result=self.tick(LOW,state,10120,**kwargs)
            self.assertFalse(result['should_nudge'])
            self.assertEqual(result['next_state'],state)
        self.assertEqual(INITIAL_STATE['baseline_rmssd'],0)
    def test_missing_beats_flat_and_clean_first(self):
        for data in (LOW[:19],[900]*60,[]):
            result=self.tick(data)
            self.assertEqual(result['reason'],'insufficientData')
        result=self.tick([10,5000,*HIGH])
        self.assertEqual(result['fast_rmssd'],120)
    def test_resting_hr_motion_and_consumed_suppression(self):
        arm=self.armed()
        for kwargs in ({'current_hr':None},{'current_hr':110},{'current_hr':54},{'recent_motion_g':.15}):
            result=self.tick(LOW,arm['next_state'],10120,**kwargs)
            self.assertEqual(result['reason'],'exerciseGated')
            self.assertEqual(result['next_state']['pending_edge_at'],0)
        self.assertTrue(self.tick(LOW,arm['next_state'],10120,recent_motion_g=None)['should_nudge'])
        for hr in (55,100):self.assertTrue(self.tick(LOW,arm['next_state'],10120,current_hr=hr)['should_nudge'])
    def test_manual_cooldown_and_quiet(self):
        arm=self.armed()
        self.assertEqual(self.tick(LOW,arm['next_state'],10120,session_active=True)['reason'],'suppressed')
        state={**arm['next_state'],'last_fire_at':10000}
        self.assertEqual(self.tick(LOW,state,10120)['reason'],'suppressed')
        cfg={**ON,'quiet_hours_enabled':True}
        state={**arm['next_state'],'pending_edge_at':23*3600-60}
        self.assertEqual(self.tick(LOW,state,23*3600,config=cfg)['reason'],'suppressed')
        # Offset makes 21:00 UTC land inside local 23:00 quiet hours.
        state['pending_edge_at']=21*3600-60
        self.assertEqual(self.tick(LOW,state,21*3600,config=cfg,tz_offset_sec=7200)['reason'],'suppressed')
    def test_wobble_recovers_and_no_new_crossing_rearm(self):
        arm=self.armed()
        recovery=self.tick(HIGH,arm['next_state'],10090)
        self.assertEqual(recovery['next_state']['pending_edge_at'],0)
        self.assertEqual(recovery['reason'],'noDip')
        restored={**arm['next_state'],'pending_edge_at':0}
        self.assertEqual(self.tick(LOW,restored,10120)['reason'],'notAnEdge')

if __name__=='__main__':unittest.main()

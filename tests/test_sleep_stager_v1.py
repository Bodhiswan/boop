"""Pinned NOOP V1 source fixtures and transparent analytical window vectors.

Classifier/merge expected literals come from SleepStagerTests.swift and
SleepStagerRespEvidenceTests.swift at 7f396e98. No user samples or hardware.
"""
import itertools
import math
import unittest
import sleep_stager_v1 as v1


def feature(**changes):
    return dict(dict(index=0,mid_ts=0,count=0,move_fraction=0,ck_sleep=True,hr=50,
                     hr_var=0,rmssd=60,sdnn=0,resp_rate=14,rrv=float("nan"),clock=.5),**changes)


BARS = dict(hr_low=55,hr_high=90,rmssd_high=50,hr_var_high=10,rrv_high=1,rrv_low=.5,cardiac_sparse=False)


def expand(runs):
    return [stage for stage,n in runs for _ in range(n)]


class SourceV1Tests(unittest.TestCase):
    def test_cole_kripke_literal_constants(self):
        self.assertEqual(v1.rescale_counts([200,50000]),[2.,300.])
        self.assertEqual(v1.cole_kripke([0.]*20),[True]*20)
        counts=[0.]*9; counts[4]=300.
        self.assertFalse(v1.cole_kripke(counts)[4])
        # Equality SI=1 is wake; center coefficient is exactly .230.
        self.assertFalse(v1.cole_kripke([0.,0.,0.,0.,1000/230,0.,0.])[4])

    def test_grid_closed_end_and_silence_is_moving(self):
        grid=v1.build_epoch_grid(0,61,[(0,50),(61,70)],[(0,0,0,1),(61,0,0,1)],[(61,1000)],[(61,123)])
        self.assertEqual(grid["edges"],[0,30,60,90])
        self.assertEqual(grid["hr"][0],50)
        self.assertTrue(math.isnan(grid["hr"][1]))
        self.assertEqual(grid["hr"][2],70)
        self.assertEqual(grid["move_fraction"],[0.,1.,0.])
        self.assertEqual(grid["rr"],[[],[],[1000.]])

    def test_onset_persistence_three_and_last_sleep(self):
        self.assertEqual(v1.onset_and_final_wake([False,True,True,False,True,True,True,False]),(4,6))
        self.assertEqual(v1.onset_and_final_wake([False]*5),(0,4))

    def test_dog_interpolation_reflect_and_flat_signal(self):
        self.assertEqual(v1.convolve_reflect([1.,2.,3.,4.],[.25,.5,.25]),[1.5,2.,3.,3.5])
        self.assertEqual(v1.dog_hr_variability([float("nan")]*8),[0.]*8)
        self.assertTrue(all(abs(v)<1e-10 for v in v1.dog_hr_variability([50.]*120)))
        a=v1.dog_hr_variability([40.,float("nan"),60.])
        b=v1.dog_hr_variability([40.,50.,60.])
        self.assertEqual(a,b)
        self.assertAlmostEqual(sum(v1.gaussian_kernel(120)),1.)

    def test_raw_resp_peaks_and_rrv(self):
        self.assertEqual(v1.find_peaks([0,1,1,0,0,1,1,0],5,.5),[1])
        self.assertEqual(v1.find_peaks([0,0,1,0,0,0,1,0],5,.5),[2]) # source tie oracle
        raw=[math.sin(2*math.pi*i/4) for i in range(40)]
        self.assertEqual(v1.resp_rate_and_rrv(raw),(15.,0.))
        rate,rrv=v1.resp_rate_and_rrv([1.]*30)
        self.assertTrue(math.isnan(rate) and math.isnan(rrv))
        # Vary actual peak intervals, not breaths/min values: intervals 4,6,4.
        raw=[0.]*20
        for i in (2,6,12,16): raw[i]=1.
        rate,rrv=v1.resp_rate_and_rrv(raw)
        self.assertEqual(rate,15.)
        self.assertAlmostEqual(rrv,math.sqrt(8/9))

    def test_feature_raw_rr_range_only_and_sample_sdnn(self):
        grid=v1.build_epoch_grid(0,30,[(0,50)],[(0,0,0,1),(1,0,0,1)],
                                 [(i,r) for i,r in enumerate([299,300,400,500,600,2000,2001])],[])
        f=v1.extract_features(grid,[True],[0.],0,0)[0]
        self.assertAlmostEqual(f["rmssd"],math.sqrt((100**2*3+1400**2)/4))
        self.assertAlmostEqual(f["sdnn"],math.sqrt(sum((r-760)**2 for r in [300,400,500,600,2000])/4))
        self.assertTrue(math.isnan(f["rrv"]))

    def test_resp_evidence_literal_source_cases(self):
        for value,low,high,expected in [(float("nan"),.5,1,"unmeasured"),(.2,.5,1,"regular"),
             (.5,.5,1,"regular"),(1,.5,1,"irregular"),(.75,.5,1,"measured_mid_band"),
             (.75,.75,.75,"bars_degenerate"),(.7,None,None,"measured_mid_band")]:
            self.assertEqual(v1.resp_evidence(value,low,high),expected)
        self.assertEqual(v1.classify_one(feature(),BARS),"deep")
        self.assertEqual(v1.classify_one(feature(rmssd=10),BARS),"light")
        self.assertEqual(v1.classify_one(feature(hr=95,hr_var=20,rmssd=10,rrv=.75),dict(BARS,rrv_high=.75,rrv_low=.75)),"rem")

    def test_sparse_cardiac_source_fixture(self):
        f=feature(move_fraction=.16,hr=60,hr_var=200,rmssd=float("nan"))
        self.assertEqual(v1.classify_one(f,dict(BARS,hr_var_high=100)),"wake")
        self.assertEqual(v1.classify_one(f,dict(BARS,hr_var_high=100,cardiac_sparse=True)),"light")
        missing=feature(rmssd=float("nan")); present=feature(rmssd=40)
        self.assertTrue(v1.reference_bars([missing,missing,present,present])["cardiac_sparse"])
        self.assertFalse(v1.reference_bars([missing,present,present,present])["cardiac_sparse"])

    def test_classifier_exhaustive_source_boolean_equivalence(self):
        # Independent pre-representation-fix equations pinned by upstream test.
        for low,high in [(.5,1),(.75,.75),(1,.5),(None,1),(.5,None),(None,None)]:
            for move,hr,hv,rmssd,rrv,sparse in itertools.product([0,.05,.12,.2],[float("nan"),45,60,95],
                    [float("nan"),1,20],[float("nan"),10,80],[float("nan"),.2,.5,.75,1,2],[False,True]):
                has=math.isfinite(hr); hr_low=has and hr<=55; hr_high=has and hr>=90
                para=not math.isfinite(rmssd) or rmssd>=50
                var_high=math.isfinite(hv) and hv>=10
                irregular=math.isfinite(rrv) and high is not None and rrv>=high
                regular=not math.isfinite(rrv) or low is not None and rrv<=low
                expected=("wake" if move>=.15 and ((hr_high if sparse else hr_high or var_high) or not has)
                          else "deep" if move<=.1 and para and hr_low and regular
                          else "rem" if move<=.1 and (hr_high or var_high) and irregular
                          else "rem" if move<=.1 and hr_high and var_high and not math.isfinite(rrv) else "light")
                f=feature(move_fraction=move,hr=hr,hr_var=hv,rmssd=rmssd,rrv=rrv)
                self.assertEqual(v1.classify_one(f,dict(BARS,rrv_low=low,rrv_high=high,cardiac_sparse=sparse)),expected)

    def test_smoothing_tie_retains_current_and_first_insertion(self):
        self.assertEqual(v1.smooth_labels(["light","wake","deep"],3),["light","wake","deep"])
        self.assertEqual(v1.smooth_labels(["deep","light","light","deep","rem"],5),["light","light","light","light","rem"])
        self.assertEqual(v1.smooth_labels(["light","deep","wake","deep","light"],5)[2],"light")

    def test_physiology_literal_source_deep_and_rem_guards(self):
        labels=["deep"]*4
        self.assertEqual(v1.reimpose_physiology(labels,[feature(clock=c) for c in [.2,.5,.7,.9]],0,3),["deep","light","light","light"])
        self.assertEqual(v1.reimpose_physiology(labels,[feature(clock=c) for c in [.5,.6,.7,.9]],0,3),labels)
        self.assertEqual(v1.reimpose_physiology(["rem"]*31,[feature()]*31,0,30),["light"]*30+["rem"])

    def test_literal_source_fragment_run_vectors(self):
        for before,after in [([("light",8),("deep",2),("light",8)],[("light",18)]),
             ([("light",10),("deep",10),("rem",10)],[("light",10),("deep",10),("rem",10)]),
             ([("light",8),("deep",3),("rem",8)],[("light",11),("rem",8)]),
             ([("light",8),("rem",2),("deep",4)],[("light",14)]),
             ([("deep",2),("light",10),("rem",2)],[("light",14)]),
             ([("light",10),("deep",6),("light",10)],[("light",10),("deep",6),("light",10)])]:
            self.assertEqual(v1.merge_fragments(expand(before)),expand(after))

    def test_end_to_end_flat_still_active_and_unknown(self):
        # Flat still HR has all percentile bars at50, raw deep throughout; front-load prior
        # keeps exactly40/120 epochs deep. 1/3 of final119 is39.666...
        hr=[(t,50) for t in range(3600)]
        g=[(t,0,0,1) for t in range(3600)]
        result=v1.stage_sleep(0,3600,hr,g,[])
        self.assertEqual(result["value"],[dict(start=0,end=1200,stage="deep"),dict(start=1200,end=3600,stage="light")])
        self.assertEqual(result["source"],"SleepStager.swift V1")
        active=[(t,(t%2)*.5,0,1) for t in range(3600)]
        self.assertEqual(v1.stage_sleep(0,3600,hr,active,[])["value"],[dict(start=0,end=3600,stage="wake")])
        self.assertEqual(v1.stage_sleep(0,3600,[],g,[])["value"],[dict(start=0,end=3600,stage="light")])
        self.assertFalse(result["rem_funnel"]["resp_channel_present"])

    def test_missing_coverage_and_source_fallback(self):
        result=v1.stage_sleep(0,120,[],[(0,0,0,1)],[])
        self.assertEqual(result["value"],[dict(start=0,end=120,stage="light")])
        self.assertTrue(result["coverage"]["source_fallback"])
        self.assertIn("fewer than two",result["reason"])
        result=v1.stage_sleep(0,61,[],[(0,0,0,1),(61,0,0,1)],[])
        self.assertEqual(result["coverage"]["gravity_covered_epochs"],2)
        self.assertEqual(result["coverage"]["expected_epochs"],3)
        self.assertEqual(result["value"][-1]["end"],61)
        self.assertIsNone(v1.stage_sleep(10,10,[],[],[])["value"])

    def test_rem_rejection_classifier_precedence(self):
        for f,reason in [(feature(),"won_other_stage"),(feature(move_fraction=.12),"not_still"),
            (feature(hr=60,hr_var=0,rmssd=10),"no_cardiac_activation"),
            (feature(hr=95,hr_var=0,rmssd=10,rrv=.75),"resp_regular"),
            (feature(hr=95,hr_var=0,rmssd=10),"no_resp_fallback_bar"),
            (feature(hr=95,hr_var=20,rmssd=10),"rem_eligible")]:
            self.assertEqual(v1.rem_reject_reason(f,BARS),reason)


if __name__=="__main__":
    unittest.main()

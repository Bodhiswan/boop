"""NOOP synthetic LCG rhythm fixtures, VitalBands vectors and LabBook projections."""
import math
import unittest
from health_projections import (rhythm_window,rhythm_summary,rhythm_night,vital_band,
                               vital_bands,calendar_series,project_labs,pair_marker_wearable,
                               lab_book,marker_catalog,health_projections)

def sinus(count=240):
    state=1; out=[]
    for i in range(count):
        state=(state*1664525+1013904223)&0xffffffff
        phase=i%8; tri=phase/4 if phase<4 else (8-phase)/4
        out.append(1000+(tri*2-1)*30+state%5-2)
    return out

def varied(count=240):
    state=7; out=[]
    for i in range(count):
        state=(state*1664525+1013904223)&0xffffffff
        out.append(1000+state%361-180)
    return out

class RhythmTests(unittest.TestCase):
    def test_pinned_lcg_fixture_labels_and_points(self):
        plain=sinus(); ect=sinus()
        for i in range(20,len(ect)-1,40): ect[i:i+2]=[650,1350]
        for data,label in ((plain,'steady'),(varied(),'varied'),(ect,'occasionalEctopy')):
            result=rhythm_window(data,True,60)
            self.assertEqual(result['label'],label)
            self.assertEqual(result['confidence'],'solid')
            self.assertEqual(len(result['poincare']),239)
            self.assertEqual(result['poincare'][0],dict(x=data[0],y=data[1]))
            rmssd=math.sqrt(sum((b-a)**2 for a,b in zip(data,data[1:]))/239)
            self.assertAlmostEqual(result['sd1'],rmssd/math.sqrt(2))

    def test_gates_and_actual_timestamp_integrity(self):
        data=sinus()
        for args in ((data,False,60),(data[:59],True,60),(data,True,111),(data,True,39)):
            r=rhythm_window(*args); self.assertEqual(r['label'],'unreadable'); self.assertEqual(r['poincare'],[])
        honest=rhythm_window(data,True,60,list(range(240)))
        self.assertEqual(honest['label'],'steady')
        banked=rhythm_window(data,True,60,[(i//6)*5 for i in range(240)])
        self.assertEqual(banked['label'],'unreadable'); self.assertIsNone(banked['sd2'])
        # A banked decomposition can have plausible total coverage and still lack beat accuracy.
        banked=rhythm_window(data,True,60,[(i//6)*6 for i in range(240)])
        self.assertEqual(banked['label'],'unreadable')
        self.assertTrue(rhythm_window(data,True,60,ppg_ibi=data)['agreed_across_sources'])
        self.assertFalse(rhythm_window(data,True,60)['agreed_across_sources'])

    def test_actual_host_windows_and_empty_states(self):
        day={'day':'2026-10-01','sleep':{'main':{'start':0,'end':900}}}
        rr=[(t,sinus()[t%240]) for t in range(900)]
        gravity=[(t,0,0,1) for t in range(0,900,10)]
        result=rhythm_night(day,rr,gravity)
        self.assertEqual(result['night']['readable_windows'],3)
        self.assertEqual(result['empty_state'],'none')
        self.assertEqual(rhythm_night(day,rr,[])['empty_state'],'deviceNoMotion')
        banked=[((i//6)*6,sinus()[i%240]) for i in range(900)]
        self.assertEqual(rhythm_night(day,banked,[])['empty_state'],'deviceBanksBeats')
        self.assertEqual(rhythm_night(day,banked,gravity)['empty_state'],'gatheringData')
        self.assertEqual(rhythm_night({},[],[])['night']['overall'],'unreadable')

    def test_source_night_count_not_unimplemented_span(self):
        result=rhythm_summary([{'label':'varied'}]*3)
        self.assertTrue(result['variation_recurred'])
        self.assertEqual(result['overall'],'varied')
        self.assertEqual(rhythm_summary([{'label':'varied'}])['overall'],'occasionalEctopy')
        self.assertEqual(rhythm_summary([])['overall'],'unreadable')

class VitalTests(unittest.TestCase):
    def test_pinned_personal_hrv_vectors(self):
        cfg=(5,250,5); pop=(40,120)
        self.assertEqual(vital_band(None,[50],pop,cfg)['band'],'noData')
        self.assertEqual(vital_band(35,[35]*10,pop,cfg),dict(band='outOfRange',basis='population',nights=10))
        self.assertEqual(vital_band(35,[35]*14,pop,cfg)['band'],'inRange')
        self.assertEqual(vital_band(35,[35]*14,pop,cfg)['basis'],'personal')
        self.assertEqual(vital_band(70,[35]*30,pop,cfg)['band'],'outOfRange')
        self.assertEqual(vital_band(35+1.99*1.253*5,[35]*30,pop,cfg)['band'],'inRange')
        self.assertEqual(vital_band(300,[35]*30,pop,cfg)['basis'],'population')
        self.assertEqual(vital_band(35,[35]*30+[None]*15,pop,cfg)['basis'],'population')
        self.assertEqual(vital_band(93,[],(95,100))['band'],'outOfRange')

    def test_calendar_duplicates_malformed_and_missing(self):
        self.assertEqual(calendar_series([('2026-01-01',40),('bad',999),('2026-01-03',50),('2026-01-03',55)]),[40,None,55])
        self.assertEqual(calendar_series([('2026-01-01',40)],'2026-01-03'),[40,None,None])

    def test_skin_scale_separation_and_day_exclusion(self):
        day={'day':'2026-01-30','skin_temperature':{'value':.2},'hrv':{'value':35}}
        history=[{'day':f'2026-01-{i:02d}','skin_temperature':{'value':33 if i%2 else .2},'hrv':{'value':35}} for i in range(1,30)]
        result=vital_bands(day,history)
        self.assertEqual(result['hrv']['basis'],'personal')
        self.assertEqual(result['skin_temperature']['nights'],14)
        self.assertEqual(result['skin_temperature']['population_range'],[-.6,.6])
        self.assertEqual(result['spo2']['band'],'noData')

class LabTests(unittest.TestCase):
    def test_pinned_latest_mean_equal_time_and_ordering(self):
        readings=[dict(markerKey='ldl',day='2026-01-10',value=3.4,takenAtEpoch=1736500000),dict(markerKey='ldl',day='2026-01-10',value=3.0,takenAtEpoch=1736590000),dict(markerKey='ldl',day='2026-03-10',value=2.8,takenAtEpoch=1741600000)]
        self.assertEqual([r['value'] for r in project_labs(readings)],[3,2.8])
        readings=[dict(marker='bp_systolic',date='2026-02-01',value=120,timestamp_ms=1000),dict(marker='bp_systolic',date='2026-02-01',value=130,timestamp_ms=1000)]
        self.assertEqual(project_labs(readings)[0]['value'],130)
        self.assertEqual(project_labs(readings,'mean')[0]['value'],125)
        self.assertEqual(project_labs([dict(marker='note',date='2026-01-01',value='text')]),[])

    def test_inclusive_windows_duplicates_and_coverage(self):
        pairs=pair_marker_wearable([('2026-01-14',3),('2026-03-10',2)], [('2025-12-31',999),('2026-01-01',10),('2026-01-14',20),('2026-01-14',30)])
        self.assertEqual(pairs,[dict(day='2026-01-14',marker_value=3,wearable_mean=20,wearable_n=2)])
        self.assertEqual(pair_marker_wearable([('2026-01-14',3)],[('2026-01-13',10),('2026-01-14',30)],0)[0]['wearable_mean'],30)

    def test_catalog_no_range_invention_and_units_remain_separate(self):
        catalog=marker_catalog()
        self.assertEqual(len(catalog),30)
        self.assertTrue(all(r['higher_is_better'] is None for r in catalog))
        self.assertEqual(next(r for r in catalog if r['key']=='ldl')['canonical_unit'],'mmol/L')
        records=[dict(marker='LDL cholesterol',date='2026-01-01',value=3,unit='mmol/L',low=1,high=4),dict(marker='ldl',date='2026-01-01',value=120,unit='mg/dL'),dict(marker='custom',date='2026-01-01',value=.27,unit='x')]
        result=lab_book(records)
        self.assertEqual(len(result['markers']),3)
        self.assertEqual({r['unit'] for r in result['markers'] if r['marker_key']=='ldl'},{'mmol/L','mg/dL'})
        self.assertEqual(next(r for r in result['markers'] if r['unit']=='mmol/L')['report_references'][0]['high'],4)
        self.assertTrue(all(r['associations']['hrv']['correlation']['value'] is None for r in result['markers']))

    def test_paired_count_four_and_bundle_missing(self):
        records=[dict(marker='ldl',date=f'2026-01-{i:02d}',value=i) for i in (1,8,15,22)]
        history=[dict(day=f'2026-01-{i:02d}',hrv={'value':i*2}) for i in range(1,23)]
        result=lab_book(records,history)
        association=result['markers'][0]['associations']['hrv']
        self.assertEqual(len(association['pairs']),4)
        self.assertIsNotNone(association['correlation']['value'])
        empty=health_projections({'day':'2026-10-01'})
        self.assertEqual(empty['lab_book']['projected'],[])
        self.assertEqual(empty['vital_bands']['hrv']['band'],'noData')
        self.assertEqual(empty['rhythm']['empty_state'],'gatheringData')

if __name__=='__main__':unittest.main()

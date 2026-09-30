"""Independent direct-DFT oracle for the exact pinned SleepStagerV2 RSA bins."""
import math
import statistics
import unittest

from analytics import resp_regularity,stage_sleep,interpolate
from unittest.mock import patch


def direct_dft(beats):
    if len(beats)<12 or beats[-1][0]<=beats[0][0]:return None
    n=math.ceil((beats[-1][0]-beats[0][0])/.25-1e-9)
    if n<16:return None
    y=interpolate([t for t,v in beats],[v for t,v in beats],[beats[0][0]+i*.25 for i in range(n)])
    mean=statistics.mean(y);centered=[v-mean for v in y];powers=[]
    for k in range(math.ceil(.15*.25*n),math.floor(.40*.25*n)+1):
        re=sum(v*math.cos(-2*math.pi*k*j/n) for j,v in enumerate(centered))
        im=sum(v*math.sin(-2*math.pi*k*j/n) for j,v in enumerate(centered))
        powers.append(re*re+im*im)
    return max(powers)/sum(powers) if powers and sum(powers)>0 else None


class SpectralTests(unittest.TestCase):
    def test_literal_single_bin_and_equal_two_bin_peakedness(self):
        # 20s/80 bins:0.20Hz and0.30Hz are exact retained source bins4/6.
        one=[(i*.25,800+30*math.sin(2*math.pi*.2*i*.25)) for i in range(81)]
        two=[(i*.25,800+30*math.sin(2*math.pi*.2*i*.25)+30*math.sin(2*math.pi*.3*i*.25)) for i in range(81)]
        self.assertAlmostEqual(resp_regularity(one),1.,places=12)
        self.assertAlmostEqual(resp_regularity(two),.5,places=12)
        self.assertAlmostEqual(resp_regularity(two),direct_dft(two),places=12)

    def test_constant_sparse_and_unequal_time_gates(self):
        self.assertIsNone(resp_regularity([(i,800) for i in range(30)]))
        self.assertIsNone(resp_regularity([(i,800) for i in range(11)]))
        self.assertIsNone(resp_regularity([(1,800)]*30))
        self.assertIsNone(resp_regularity([(i*.1,800) for i in range(30)]))

    def test_irregular_sampling_and_band_edges_match_independent_dft(self):
        for length in (16,79,80,81,839,840):
            beats=[(i*.25,800+20*math.sin(i*.391)+11*math.cos(i*.073)) for i in range(length+1)]
            self.assertAlmostEqual(resp_regularity(beats),direct_dft(beats),places=11)
        beats=[(i*.79+(i%3)*.013,800+35*math.sin(i*.987)+10*math.cos(i*.174)) for i in range(265)]
        self.assertAlmostEqual(resp_regularity(beats),direct_dft(beats),places=11)

    def test_covered_staging_is_exactly_equal_to_direct_source_math(self):
        start=10000;end=start+1800
        hr=[(t,58+2*math.sin((t-start)/41)) for t in range(start-330,end+390)]
        gravity=[(t,.1*math.sin((t-start)/211),0,1) for t in range(start-330,end+390)]
        rr=[(t,1000+30*math.sin(t*1.43)+12*math.cos(t*.27)) for t in range(start-330,end+390)]
        optimized=stage_sleep(start,end,hr,gravity,rr)
        with patch('analytics.resp_regularity',direct_dft):oracle=stage_sleep(start,end,hr,gravity,rr)
        self.assertEqual(optimized,oracle)
        self.assertEqual(optimized['coverage']['observed_epochs'],60)


if __name__=='__main__':unittest.main()

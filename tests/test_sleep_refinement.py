"""Literal synthetic fixtures from NOOP WakeMotionRefinementTests and boundary gates."""
import unittest
from sleep_refinement import apply, _variance

START = 1_700_000_000 // 60 * 60


def night(n, bursts=(), walks=(), ticks=25):
    grav, steps, counter = [], [], 0
    for m in range(n):
        for k in range(4):
            x, z = (1, 0) if m in bursts and k % 2 else (0, 1)
            grav.append((START + m * 60 + k * 15, x, 0, z))
        counter += ticks if m in walks else 0
        steps.append(dict(ts=START + m * 60, counter=counter & 65535, activity_class=1 if m in walks else 0))
    return grav, steps


def segment(n, stage="wake"):
    return [{"start": START, "end": START + n * 60, "stage": stage}]


class RefinementTests(unittest.TestCase):
    def test_literal_source_hot_still_fixture(self):
        g, s = night(180, {45, 135})
        got = apply(segment(180), g, s, True)
        self.assertEqual([(x["start"] - START, x["end"] - START, x["stage"]) for x in got["value"]],
                         [(0,2640,"light"),(2640,2820,"wake"),(2820,8040,"light"),(8040,8220,"wake"),(8220,10800,"light")])
        self.assertTrue(got["applied"])

    def test_off_and_whoop4_identity(self):
        original = segment(180)
        g, s = night(180, {45,135})
        self.assertIs(apply(original, g, s)["value"], original)
        self.assertIs(apply(original, g, [], True)["value"], original)
        self.assertIs(apply(original, g, [{"ts":START,"steps":1000}], True)["value"], original)

    def test_density_point_eight_and_sample_floor(self):
        g, s = night(10)
        g = [r for r in g if r[0] < START + 8*60 and r[0] % 60 in (0,15)]
        s = s[:8]
        got = apply(segment(10), g, s, True)
        self.assertEqual(got["coverage"], {"dense_gravity_fraction":.8,"dense_step_fraction":.8})
        self.assertTrue(got["applied"])
        self.assertEqual(got["value"][-1]["start"], START + 7*60) # missing minutes plus pad
        self.assertFalse(apply(segment(10),g[:-1],s,True)["applied"])
        self.assertFalse(apply(segment(10),g,s[:-1],True)["applied"])

    def test_five_minute_and_stage_alias(self):
        g, s = night(5)
        self.assertTrue(apply(segment(5," AwAkE "),g,s,True)["applied"])
        self.assertFalse(apply(segment(4),g,s,True)["applied"])
        self.assertFalse(apply(segment(5,"rem"),g,s,True)["applied"])

    def test_locomotion_thresholds(self):
        for walk, ticks, changed in [({2,3},10,False),({2},40,False),({2},39,True),({2,4},10,True)]:
            g, s = night(10, walks=walk,ticks=ticks)
            self.assertEqual(apply(segment(10),g,s,True)["applied"],changed)
        g, s = night(10,walks={2,3},ticks=100)
        for row in s: row["activity_class"] = None
        self.assertTrue(apply(segment(10),g,s,True)["applied"])

    def test_wrap_run_class_and_later_minute_attribution(self):
        g, s = night(10)
        for row in s: row["counter"] = 65530
        for m in range(2,10): s[m]["counter"] = 14
        s[2]["activity_class"] = 2 # 20 ticks across wrap alone does not sustain
        self.assertTrue(apply(segment(10),g,s,True)["applied"])
        s[3].update(counter=24,activity_class=2) # second consecutive10
        self.assertFalse(apply(segment(10),g,s,True)["applied"])

    def test_stability_boundary_and_burst_union(self):
        for n, bursts, wake in [(40,{20},3),(120,{20,80},6),(90,{10,40,70},9),(60,{5,6},4),(50,{0,49},4)]:
            g,s=night(n,bursts)
            out=apply(segment(n),g,s,True)["value"]
            self.assertEqual(sum(x["end"]-x["start"] for x in out),n*60)
            self.assertEqual(sum(x["end"]-x["start"] for x in out if x["stage"]=="wake"),wake*60)
        g,s=night(10,{2,6})
        self.assertTrue(apply(segment(10),g,s,True)["applied"]) # stable exactly .8
        g,s=night(10,{2,6,9})
        self.assertFalse(apply(segment(10),g,s,True)["applied"])

    def test_partial_edges_and_inputs_unmodified(self):
        g,s=night(6)
        original=[dict(start=START+10,end=START+350,stage="wake")]
        out=apply(original,g,s,True)["value"]
        self.assertEqual(out,[dict(start=START+10,end=START+350,stage="light")])
        self.assertEqual(original[0]["stage"],"wake")
        self.assertEqual(apply([],g,s,True)["reason"],"degenerate_window")

    def test_variance_trace_and_strict_threshold(self):
        # Exact trace: x variance .04 + y variance .01 = .05; threshold is strict.
        rows=[(0,0,0,0),(15,.4,.2,0)]
        self.assertAlmostEqual(_variance(rows),.05)
        self.assertIsNone(_variance(rows[:1]))
        g,s=night(10)
        # Three .05-or-higher minutes makes stable coverage .7, preventing refinement.
        for m in (2,5,8):
            g=[r for r in g if r[0]//60 != START//60+m]
            g.extend((START+m*60+t,x,y,z) for t,x,y,z in rows)
        self.assertFalse(apply(segment(10),g,s,True)["applied"])

if __name__ == "__main__":
    unittest.main()

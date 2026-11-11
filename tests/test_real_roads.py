"""Opt-in, offline integration tests using a locally downloaded Overpass JSON.

Set DRAWRUN_REAL_OSM to the Seoul Forest cache/JSON path. No server, API key,
network requests or new OSM database copies are needed for these tests.
"""
import json
import math
import os
from pathlib import Path
import unittest

from routing import distance, generate, graph_from_osm, length, shape_policy, shape_details, shape_metrics, road_retrace_stats, near_retrace_stats

CENTER = [127.0374, 37.5445]


@unittest.skipUnless(os.environ.get("DRAWRUN_REAL_OSM"), "set DRAWRUN_REAL_OSM to a Seoul Forest Overpass JSON")
class RealRoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads(Path(os.environ["DRAWRUN_REAL_OSM"]).read_text(encoding="utf-8"))
        cls.graph = graph_from_osm(data, CENTER, 2500)

    def check_shape(self, drawing, closed):
        result = generate(self.graph, {
            "drawing": drawing, "distance_m": 5000, "approach_m": 1000,
            "allow_rotation": True, "route_type": "auto", "tolerance": .2,
            "location": CENTER,
        })
        self.assertEqual(result["source"], "osm")
        # A star is not guaranteed to fit these real roads without prohibited
        # spurs. Corner remains a positive integration fixture; star must
        # either return clean candidates or explicitly report no route.
        if not closed:
            self.assertTrue(result["candidates"], result["message"])
        self.assertEqual(result["found"], any(c["acceptable"] for c in result["candidates"]))
        self.assertEqual(result["approach_bands_tested"], [0, 1, 2, 3])
        for candidate in result["candidates"]:
            path = candidate["node_ids"]
            self.assertLessEqual(abs(candidate["distance_m"] - 5000), 1000)
            self.assertLessEqual(candidate["approach_m"], 1000)
            self.assertEqual(candidate["closed"], closed)
            if closed:
                self.assertEqual(path[0], path[-1])
            actual = [self.graph.nodes[n] for n in path]
            self.assertAlmostEqual(length(actual), candidate["distance_m"], delta=.06)
            for u, v in zip(path, path[1:]):
                self.assertTrue(any(edge[0] == v for edge in self.graph.adj[u]), (u, v))
            for node, geo in zip(actual, candidate["geometry"]["coordinates"]):
                self.assertLess(distance(node, self.graph.project(*geo)), .001)
            reference = [self.graph.project(*p) for p in candidate["reference"]]
            repeated, unwanted = road_retrace_stats(self.graph, path, reference)
            self.assertAlmostEqual(repeated, 0)
            self.assertAlmostEqual(unwanted, 0)
            self.assertEqual(candidate["unwanted_retrace_m"], 0)
            near = near_retrace_stats(actual, reference)
            self.assertAlmostEqual(candidate["unwanted_near_retrace_m"], near, delta=.002)
            self.assertEqual(candidate['narrow_return_warning'], near > .001)
            if near > .001:
                self.assertTrue(candidate['acceptable'])
            actual_policy, _ = shape_policy(*shape_metrics(reference, actual), shape_details(reference, actual), reference)
            self.assertEqual(candidate["acceptable"], actual_policy)
            if candidate["vertex_adjusted"]:
                info = candidate["vertex_adjustment"]
                self.assertLessEqual(info["vertex_max_shift_m"], info["vertex_limit_m"] + .002)
                self.assertEqual(len(candidate["vertex_moves"]), len(candidate["adjusted_reference"]))
                for move in candidate["vertex_moves"]:
                    self.assertLessEqual(move["distance_m"], info["vertex_limit_m"] + .002)
                if closed:
                    self.assertEqual(candidate["adjusted_reference"][0], candidate["adjusted_reference"][-1])
        if not result["candidates"]:
            self.assertFalse(result["found"])
            self.assertIn("왕복 없이", result["message"])
            print(json.dumps({"shape": "star", "found": False, "candidate_count": 0,
                              "retrace_rejected": result["diagnostics"]["retrace_rejected"]}))
            return
        best = result["candidates"][0]
        print(json.dumps({"shape": "star" if closed else "corner", "elapsed_s": result["elapsed_s"],
                          "found": result["found"], "road_retrace_m": best["road_retrace_m"],
                          "candidate_count": len(result["candidates"]),
                          "elastic_completed": result["elastic_routes_completed"],
                          "vertex_adjusted": best["vertex_adjusted"],
                          **{k: best[k] for k in ("distance_m", "approach_m", "mean_error_m", "frechet_m")}}))

    def test_seoul_forest_star_5km(self):
        drawing = []
        for i in range(11):
            angle = -math.pi / 2 + i * math.pi / 5
            radius = 220 if i % 2 == 0 else 95
            drawing.append([300 + math.cos(angle) * radius, 300 + math.sin(angle) * radius])
        self.check_shape(drawing, True)

    def test_seoul_forest_corner_5km(self):
        self.check_shape([[100, 100], [100, 450], [450, 450]], False)


if __name__ == "__main__":
    unittest.main()

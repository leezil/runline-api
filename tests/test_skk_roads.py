"""Opt-in cached Suwon regression; no network or guessed pedestrian links.

Set DRAWRUN_SKK_OSM to the existing Suwon Overpass JSON. The test saves actual
route results to artifacts/skk_improved_results.json for repeatable comparison.
"""
import json
import math
import os
from pathlib import Path
import unittest

from routing import (generate, graph_from_osm, length, distance, shape_policy,
                     shape_details, shape_metrics, road_retrace_stats, near_retrace_stats)

CENTER = [126.9662925, 37.3033769]


@unittest.skipUnless(os.environ.get('DRAWRUN_SKK_OSM'), 'set DRAWRUN_SKK_OSM to the Suwon cache')
class SuwonRoadTests(unittest.TestCase):
    def test_star_multiple_candidates_and_real_connections(self):
        data = json.loads(Path(os.environ['DRAWRUN_SKK_OSM']).read_text(encoding='utf-8'))
        graph = graph_from_osm(data, CENTER, 4000)
        drawing = [[300 + math.cos(-math.pi / 2 + i * math.pi / 5) * (220 if i % 2 == 0 else 95),
                    300 + math.sin(-math.pi / 2 + i * math.pi / 5) * (220 if i % 2 == 0 else 95)] for i in range(11)]
        result = generate(graph, dict(drawing=drawing, distance_m=5000, approach_m=1000,
                                      location=CENTER, candidate_limit=30))
        output = Path(__file__).resolve().parents[1] / 'artifacts/skk_improved_results.json'
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertTrue(result['found'])
        self.assertGreater(len(result['candidates']), 3)
        self.assertLessEqual(len(result['candidates']), 30)
        self.assertGreater(len({c['node_ids'][0] for c in result['candidates']}), 1)
        self.assertNotIn('70812138', {eid.split(':')[0] for _, _, eid in graph.edges})
        road_sets = set()
        for candidate in result['candidates']:
            path = candidate['node_ids']
            route = [graph.nodes[n] for n in path]
            reference = [graph.project(*p) for p in candidate['reference']]
            self.assertLessEqual(abs(candidate['distance_m'] - 5000), 1000)
            self.assertLessEqual(candidate['approach_m'], 1000)
            self.assertEqual(path[0], path[-1])
            self.assertAlmostEqual(length(route), candidate['distance_m'], delta=.06)
            for u, v in zip(path, path[1:]):
                self.assertTrue(any(edge[0] == v for edge in graph.adj[u]))
            for point, geo in zip(route, candidate['geometry']['coordinates']):
                self.assertLess(distance(point, graph.project(*geo)), .001)
            repeated, unwanted = road_retrace_stats(graph, path, reference)
            self.assertEqual(unwanted, 0)
            self.assertAlmostEqual(candidate['road_retrace_m'], repeated, delta=.06)
            near = near_retrace_stats(route, reference)
            self.assertAlmostEqual(candidate['unwanted_near_retrace_m'], near, delta=.002)
            self.assertEqual(candidate['narrow_return_warning'], near > .001)
            policy, _ = shape_policy(*shape_metrics(reference, route), shape_details(reference, route), reference)
            self.assertEqual(candidate['acceptable'], policy)
            signature = frozenset(tuple(sorted((u, v))) for u, v in zip(path, path[1:]))
            self.assertNotIn(signature, road_sets)
            road_sets.add(signature)
        print(json.dumps({'elapsed_s': result['elapsed_s'], 'candidates': len(result['candidates']),
                          'recommended': result['recommended_count'],
                          'alternative_completed': result['alternative_routes_completed'],
                          'elastic_completed': result['elastic_routes_completed'],
                          'approach_m': [c['approach_m'] for c in result['candidates']]}))

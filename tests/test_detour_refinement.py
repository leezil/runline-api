import time
import unittest
from routing import (Graph, refine_road_detours, road_connector,
                     search_checkpoints, shape_metrics, extra_road_bends)


class DetourRefinementTests(unittest.TestCase):
    def fixture(self, shortcut=True, reverse=False):
        graph = Graph([127, 37], 1000, 'synthetic')
        # A 500m open line with a 200m rectangular detour, no repeated node.
        points = [(0, 0), (100, 0), (100, 100), (300, 100), (300, 0), (500, 0)]
        for i, point in enumerate(points):
            graph.node(i, point)
        for i in range(5):
            graph.edge(i, i + 1, i)
        if shortcut:
            graph.edge(1, 4, 100, direction=-1 if reverse else 1)
        return graph, list(range(6)), [(0, 0), (500, 0)]

    def test_replaces_different_junction_detour_using_actual_edges(self):
        graph, path, reference = self.fixture()
        result, stats = refine_road_detours(graph, path, reference, 600, .2, time.monotonic() + 3)
        self.assertEqual(result, [0, 1, 4, 5])
        self.assertEqual(stats['removed_m'], 200)
        self.assertEqual(stats['replacements'], 1)
        self.assertEqual((result[0], result[-1]), (path[0], path[-1]))
        for a, b in zip(result, result[1:]):
            self.assertTrue(any(edge[0] == b for edge in graph.adj[a]))
        self.assertLess(extra_road_bends(reference, [graph.nodes[n] for n in result]),
                        extra_road_bends(reference, [graph.nodes[n] for n in path]))

    def test_missing_or_wrong_direction_road_never_invents_connection(self):
        for kwargs in ({'shortcut': False}, {'reverse': True}):
            graph, path, reference = self.fixture(**kwargs)
            result, stats = refine_road_detours(graph, path, reference, 600, .2, time.monotonic() + 3)
            self.assertEqual(result, path)
            self.assertEqual(stats['replacements'], 0)

    def test_preserves_required_distance_without_padding(self):
        graph, path, reference = self.fixture()
        result, stats = refine_road_detours(graph, path, reference, 700, .05, time.monotonic() + 3)
        self.assertEqual(result, path)
        self.assertGreater(stats['distance_rejected'], 0)

    def test_preserves_intended_corner_not_just_shortest_distance(self):
        graph, path, _ = self.fixture()
        reference = [graph.nodes[n] for n in path]
        result, stats = refine_road_detours(graph, path, reference, 600, .5, time.monotonic() + 3)
        self.assertEqual(result, path)
        self.assertEqual(stats['replacements'], 0)

    def test_preserves_intentional_retrace(self):
        graph, path, reference = self.fixture()
        result, stats = refine_road_detours(graph, path, reference + [(0, 0)], 600, .5, time.monotonic() + 3)
        self.assertEqual(result, path)
        self.assertEqual(stats['attempts'], 0)

    def test_expired_deadline_returns_original(self):
        graph, path, reference = self.fixture()
        result, stats = refine_road_detours(graph, path, reference, 600, .2, time.monotonic() - 1)
        self.assertEqual(result, path)
        self.assertEqual(stats['attempts'], 0)

    def test_connector_obeys_external_path_exclusions(self):
        graph, _, _ = self.fixture()
        self.assertIsNone(road_connector(graph, 1, 4, 300, {2}, {(1, 4)}, time.monotonic() + 1))

    def test_relaxed_checkpoints_keep_corners_and_order(self):
        points = [(0, 0), (1000, 0), (1000, 1000)]
        normal = search_checkpoints(points, 2000)
        relaxed = search_checkpoints(points, 2000, relaxed=True)
        self.assertLess(len(relaxed), len(normal))
        self.assertIn((1000, 0), relaxed)
        self.assertEqual((relaxed[0], relaxed[-1]), (points[0], points[-1]))
        self.assertEqual(points, [(0, 0), (1000, 0), (1000, 1000)])

    def test_closed_route_keeps_start_end_and_real_connections(self):
        graph, path, _ = self.fixture()
        graph.node(6, (500, -300))
        graph.node(7, (0, -300))
        for a, b in ((5, 6), (6, 7), (7, 0)):
            graph.edge(a, b, 200 + a)
        path += [6, 7, 0]
        reference = [(0, 0), (500, 0), (500, -300), (0, -300), (0, 0)]
        result, stats = refine_road_detours(graph, path, reference, 1700, .2, time.monotonic() + 3)
        self.assertGreater(stats['replacements'], 0)
        self.assertEqual(result[0], result[-1])
        self.assertAlmostEqual(shape_metrics(reference, [graph.nodes[n] for n in result])[0], 0)


if __name__ == '__main__':
    unittest.main()

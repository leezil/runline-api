import math
import time
import unittest
from unittest.mock import patch

from routing import (Graph, InputError, demo_graph, distance, frechet, generate,
                     graph_from_osm, length, prepare_drawing, reachable,
                     resample, shape_metrics, walkable, guided_path,
                     guided_choices, search_checkpoints, shape_details, shape_policy,
                     distributed_starts, select_poses, diverse_candidates, location_representatives,
                     elastic_references, match_anchor_pose, crossing_signature,
                     retrace_corridors, road_retrace_stats, road_key, match_pose, near_retrace_stats,
                     trim_corner_loops, excess_turn_cost)

CENTER = [127.0374, 37.5445]


def osm_fixture():
    local = Graph(CENTER, 1000)
    nodes = []
    for i, p in enumerate(((0, 0), (100, 0), (100, -100), (0, -100)), 1):
        lng, lat = local.unproject(p)
        nodes.append({"type": "node", "id": i, "lon": lng, "lat": lat})
    return {"elements": nodes + [{"type": "way", "id": 100,
            "nodes": [1, 2, 3, 4, 1], "tags": {"highway": "footway"}}]}


class DrawingTests(unittest.TestCase):
    def test_corner_sample_is_stable_under_coordinate_round_trip(self):
        reference = [(0, 0), (100, 0), (100, 100)]
        route = [(0, 0), (80, 0), (100, 10), (100, 100)]
        expected = shape_details(reference, route)
        graph = Graph(CENTER, 1000)
        variants = [[graph.project(*graph.unproject(p)) for p in reference],
                    [(0, 0), (100 - 1e-8, 0), (100, 100)],
                    [(0, 0), (100 + 1e-8, 0), (100, 100)]]
        for variant in variants:
            details = shape_details(variant, route)
            self.assertAlmostEqual(details['corner_error_deg'], expected['corner_error_deg'], delta=.00001)
            self.assertEqual(details['missing_corners'], expected['missing_corners'])

    def test_identical_shape_has_correct_features_including_loop_seam(self):
        square = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]
        details = shape_details(square, square)
        self.assertEqual(details["corner_count"], 4)
        self.assertEqual(details["missing_corners"], 0)
        self.assertAlmostEqual(details["direction_error"], 0)
        self.assertAlmostEqual(details["detour_ratio"], 1)
        self.assertTrue(shape_policy(*shape_metrics(square, square), details, square)[0])

    def test_missing_drawn_features_are_not_accepted_by_mean_distance_alone(self):
        raw = [[300 + math.cos(-math.pi / 2 + i * math.pi / 5) * (220 if i % 2 == 0 else 95),
                300 + math.sin(-math.pi / 2 + i * math.pi / 5) * (220 if i % 2 == 0 else 95)] for i in range(11)]
        reference, _ = prepare_drawing(raw, 5000)
        # Compare the actual drawing against its convex outline; inward turns vanish.
        factor = 5000 / length([(x, -y) for x, y in raw])
        hull = [((raw[i][0] - raw[0][0]) * factor, -(raw[i][1] - raw[0][1]) * factor) for i in range(0, 11, 2)]
        details = shape_details(reference, hull)
        self.assertGreater(details["missing_corners"], 0)
        self.assertFalse(shape_policy(*shape_metrics(reference, hull), details, reference)[0])

    def test_intentional_retracing_is_preserved(self):
        line = [(0, 0), (200, 0), (0, 0), (200, 0)]
        self.assertTrue(shape_policy(*shape_metrics(line, line), shape_details(line, line), line)[0])
        prepared, closed = prepare_drawing([[0, 0], [200, 0], [0, 0], [200, 0]], 600, "open")
        self.assertFalse(closed)
        self.assertAlmostEqual(length(prepared), 600)
        self.assertTrue(any(p == (200, 0) for p in prepared))
        self.assertGreater(sum(distance(a, b) for a, b in zip(prepared, prepared[1:])), distance(prepared[0], prepared[-1]) * 2)

    def test_shape_is_not_replaced_and_uniformly_scaled(self):
        raw = [[0, 0], [200, 0], [230, 70], [400, 160]]
        result, closed = prepare_drawing(raw, 1200, "open")
        self.assertFalse(closed)
        self.assertAlmostEqual(length(result), 1200, places=5)
        self.assertTrue(any(y < -300 for x, y in result))
        self.assertAlmostEqual(result[-1][0] / -result[-1][1], 400 / 160)

    def test_closed_and_open_controls(self):
        raw = [[0, 0], [100, 0], [100, 100], [2, 2]]
        loop, closed = prepare_drawing(raw, 1000, "auto")
        self.assertTrue(closed)
        self.assertEqual(loop[0], loop[-1])
        opened, closed = prepare_drawing(raw, 1000, "open")
        self.assertFalse(closed)
        self.assertNotEqual(opened[0], opened[-1])

    def test_self_crossing_preserved(self):
        raw = [[0, 0], [100, 100], [0, 100], [100, 0], [0, 0]]
        points, closed = prepare_drawing(raw, 1000)
        self.assertTrue(closed)
        self.assertGreater(len(points), 10)
        self.assertAlmostEqual(length(points), 1000, places=5)

    def test_bad_drawing_rejected(self):
        for raw in ([], [[0, 0]], [[0, 0], [0, 0]], [[0, 0], [math.nan, 100]], [[0, 0], ["bad", 100]]):
            with self.subTest(raw=raw), self.assertRaises(InputError):
                prepare_drawing(raw, 1000)

    def test_ordered_score_sees_backtracking(self):
        a = [(0, 0), (100, 0), (200, 0)]
        b = [(0, 0), (200, 0), (0, 0), (200, 0)]
        mean, ordered = shape_metrics(a, b)
        self.assertAlmostEqual(mean, 0)
        self.assertGreater(ordered, 50)
        self.assertEqual(frechet(resample(a), resample(a)), 0)


class RetraceTests(unittest.TestCase):
    def block_with_spur(self):
        graph = Graph(CENTER, 1000)
        for node, point in (("a", (0, 0)), ("b", (100, 0)), ("c", (100, 100)),
                            ("d", (0, 100)), ("tip", (100, 105))):
            graph.node(node, point)
        for u, v in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "a"), ("c", "tip")):
            graph.edge(u, v, u + v)
        return graph

    def test_sharp_tip_does_not_unlock_retracing(self):
        self.assertEqual(retrace_corridors([(0, 0), (100, 0), (0, 10)]), [])
        star = [(math.cos(i * math.pi / 5) * (100 if i % 2 == 0 else 15),
                 math.sin(i * math.pi / 5) * (100 if i % 2 == 0 else 15)) for i in range(11)]
        self.assertEqual(retrace_corridors(star), [])
        self.assertTrue(retrace_corridors([(0, 0), (100, 0), (0, 0)]))

    def test_previous_road_is_blocked_in_both_directions(self):
        graph = self.block_with_spur()
        result = guided_path(graph, "b", "a", (100, 0), (0, 0), time.monotonic() + 2,
                             400, {road_key("a", "b")})
        self.assertEqual(result[0], ["b", "c", "d", "a"])
        self.assertIsNone(guided_path(graph, "tip", "d", (100, 105), (0, 100),
                                     time.monotonic() + 2, 500, {road_key("c", "tip")}))

    def test_fixed_spur_vertex_cannot_force_a_return(self):
        graph = self.block_with_spur()
        nodes = ["a", "b", "tip", "d", "a"]
        reference = [graph.nodes[n] for n in nodes]
        self.assertEqual(match_anchor_pose(graph, reference, nodes, True, 410, time.monotonic() + 2), [])

    def test_adjusted_vertex_skirts_spur_on_connected_block(self):
        graph = self.block_with_spur()
        reference = [graph.nodes[n] for n in ("a", "b", "tip", "d", "a")]
        variants = elastic_references(graph, reference, "a", True, set(graph.nodes), time.monotonic() + 2)
        self.assertTrue(variants)
        guide, nodes, _ = variants[0]
        self.assertEqual(nodes, ["a", "b", "c", "d", "a"])
        paths = match_anchor_pose(graph, guide, nodes, True, 410, time.monotonic() + 2, reference=reference)
        self.assertTrue(paths)
        self.assertEqual(paths[0][2], nodes)
        self.assertEqual(road_retrace_stats(graph, nodes, reference), (0, 0))

    def test_intentional_return_stays_local(self):
        graph = self.block_with_spur()
        reference = [(0, 0), (100, 0), (0, 0), (0, 100), (100, 100)]
        path = ["a", "b", "a", "d", "c", "tip", "c"]
        repeated, unwanted = road_retrace_stats(graph, path, reference)
        self.assertAlmostEqual(repeated, 105)
        self.assertAlmostEqual(unwanted, 5)
        intended_nodes = ["a", "b", "a", "d", "c"]
        paths = match_anchor_pose(graph, reference, intended_nodes, False, 400, time.monotonic() + 2)
        self.assertEqual(paths[0][2], intended_nodes)

    def test_crossing_can_share_node_without_reusing_road(self):
        graph = Graph(CENTER, 1000)
        for node, p in (("x", (0, 0)), ("a", (100, 0)), ("b", (50, 100)),
                         ("c", (-100, 0)), ("d", (-50, -100))):
            graph.node(node, p)
        nodes = ["x", "a", "b", "x", "c", "d", "x"]
        for u, v in zip(nodes, nodes[1:]):
            graph.edge(u, v, u + v)
        reference = [graph.nodes[n] for n in nodes]
        paths = match_anchor_pose(graph, reference, nodes, True, length(reference), time.monotonic() + 2)
        self.assertEqual(paths[0][2], nodes)
        self.assertEqual(road_retrace_stats(graph, nodes, reference), (0, 0))

    def test_checkpoint_search_does_not_retrace_square(self):
        graph = self.block_with_spur()
        reference = [graph.nodes[n] for n in ("a", "b", "tip", "d", "a")]
        # Real OSM ingestion subdivides long roads. Include the same interior
        # snap points; a sparse four-node fixture has no snap within 45m at
        # the halfway checkpoints and would fail independently of retracing.
        elements = [{"type": "node", "id": i, "lon": graph.unproject(p)[0],
                     "lat": graph.unproject(p)[1]} for i, p in enumerate(graph.nodes.values(), 1)]
        elements += [{"type": "way", "id": 1, "nodes": [1, 2, 3, 4, 1], "tags": {"highway": "footway"}},
                     {"type": "way", "id": 2, "nodes": [3, 5], "tags": {"highway": "footway"}}]
        graph = graph_from_osm({"elements": elements}, CENTER, 1000)
        start = graph.nearest((0, 0), 1)[0][1]
        paths = match_pose(graph, reference, start, True, 410, time.monotonic() + 2)
        self.assertTrue(paths)
        for _, _, path in paths:
            self.assertEqual(road_retrace_stats(graph, path, reference)[1], 0)

    def test_final_cleanup_removes_spur_even_if_search_returns_it(self):
        graph = self.block_with_spur()
        with patch('routing.match_pose', return_value=[(0, 410, ["a", "b", "c", "tip", "c", "d", "a"])]):
            result = generate(graph, {"drawing": [[0, 0], [100, 0], [100, -105], [0, -100], [0, 0]],
                                     "distance_m": 410, "approach_m": 0, "allow_rotation": False,
                                     "allow_vertex_adjustment": False}, seconds=2)
        self.assertGreater(result["diagnostics"]["corner_loops_trimmed"], 0)
        self.assertTrue(result["candidates"])
        for candidate in result["candidates"]:
            self.assertNotIn("tip", candidate["node_ids"])
            self.assertEqual(candidate["unwanted_retrace_m"], 0)
            self.assertEqual(candidate["distance_m"], 400)

    def test_near_parallel_spur_is_rejected_without_shared_edges(self):
        square = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]
        spur = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 200), (8, 200), (8, 100), (0, 0)]
        self.assertGreaterEqual(near_retrace_stats(spur, square), 100)
        self.assertEqual(near_retrace_stats(square, square), 0)
        wide_loop = [(0, 0), (100, 0), (100, 30), (0, 30), (0, 0)]
        self.assertEqual(near_retrace_stats(wide_loop, square), 0)
        self.assertEqual(near_retrace_stats(spur, spur), 0)
        curved = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 120), (2, 140),
                  (0, 160), (2, 180), (0, 200), (8, 200), (10, 180), (8, 160),
                  (10, 140), (8, 120), (8, 100), (0, 0)]
        self.assertGreater(near_retrace_stats(curved, square), 30)

    def test_final_guard_rejects_distinct_road_thin_return(self):
        graph = Graph(CENTER, 1000)
        route = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 200), (8, 200), (8, 100), (0, 0)]
        nodes = [str(i) for i in range(len(route) - 1)] + ['0']
        for node, point in zip(nodes, route):
            graph.node(node, point)
        for u, v in zip(nodes, nodes[1:]):
            graph.edge(u, v, u + v)
        self.assertEqual(road_retrace_stats(graph, nodes, route)[0], 0)
        with patch('routing.match_pose', return_value=[(0, length(route), nodes)]):
            result = generate(graph, {"drawing": [[0, 0], [100, 0], [100, -100], [0, -100], [0, 0]],
                                     "distance_m": 600, "approach_m": 0, "allow_rotation": False,
                                     "allow_vertex_adjustment": False}, seconds=2)
        self.assertGreater(result['diagnostics']['near_retrace_rejected'], 0)
        self.assertEqual(result['candidates'], [])

    def test_unwanted_near_return_is_rejected_even_if_global_shape_fits(self):
        graph = graph_from_osm(osm_fixture(), CENTER, 1000)
        # Global shape acceptance must not override a local unwanted return.
        # The heuristic still preserves locally intended returns (tested above).
        with patch('routing.near_retrace_stats', return_value=40):
            result = generate(graph, {"drawing": [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
                                     "distance_m": 400, "approach_m": 0, "allow_rotation": False,
                                     "allow_vertex_adjustment": False}, seconds=2)
        self.assertFalse(result['found'])
        self.assertEqual(result['candidates'], [])
        self.assertGreater(result['diagnostics']['near_retrace_rejected'], 0)

    def test_small_distinct_edge_corner_loop_is_trimmed_on_real_edges(self):
        graph = self.block_with_spur()
        for node, p in (("e", (140, 0)), ("f", (140, -30)), ("g", (100, -30))):
            graph.node(node, p)
        for u, v in (("b", "e"), ("e", "f"), ("f", "g"), ("g", "b")):
            graph.edge(u, v, u + v)
        reference = [graph.nodes[n] for n in ("a", "b", "c", "d", "a")]
        path = ["a", "b", "e", "f", "g", "b", "c", "d", "a"]
        cleaned, meters, count = trim_corner_loops(graph, path, reference)
        self.assertEqual(cleaned, ["a", "b", "c", "d", "a"])
        self.assertEqual((meters, count), (140, 1))
        self.assertEqual(path[2], "e")  # Original path not mutated.
        for u, v in zip(cleaned, cleaned[1:]):
            self.assertTrue(any(edge[0] == v for edge in graph.adj[u]))

    def test_cleaning_preserves_drawn_figure_eight_and_intentional_return(self):
        graph = self.block_with_spur()
        path = ["a", "b", "a", "d", "c"]
        reference = [graph.nodes[n] for n in path]
        self.assertEqual(trim_corner_loops(graph, path, reference), (path, 0, 0))
        for node, p in (("x", (200, 0)), ("y", (200, 100))):
            graph.node(node, p)
        path = ["b", "a", "d", "c", "b", "x", "y", "b"]
        for u, v in zip(path, path[1:]):
            graph.edge(u, v, u + v)
        reference = [graph.nodes[n] for n in path]
        self.assertEqual(trim_corner_loops(graph, path, reference), (path, 0, 0))

    def test_cleaning_does_not_keep_loops_to_fill_requested_distance(self):
        graph = self.block_with_spur()
        path = ["a", "b", "c", "tip", "c", "d", "a"]
        with patch('routing.match_pose', return_value=[(0, 410, path)]):
            result = generate(graph, {"drawing": [[0, 0], [100, 0], [100, -100], [0, -100], [0, 0]],
                                     "distance_m": 450, "tolerance": .05, "approach_m": 0,
                                     "allow_rotation": False, "allow_vertex_adjustment": False}, seconds=2)
        self.assertEqual(result["candidates"], [])
        self.assertGreater(result["diagnostics"]["length_rejected"], 0)

    def test_extra_reversal_penalty_preserves_expected_drawn_turn(self):
        a, b, c = (0, 0), (100, 0), (0, 10)
        self.assertGreater(excess_turn_cost(a, b, c), 40)
        self.assertEqual(excess_turn_cost(a, b, c, math.pi), 0)
        self.assertEqual(excess_turn_cost(a, b, (200, 0)), 0)


class GraphTests(unittest.TestCase):
    def test_access_rules(self):
        self.assertTrue(walkable({"highway": "footway"}))
        self.assertFalse(walkable({"highway": "footway", "foot": "no"}))
        self.assertFalse(walkable({"highway": "service", "access": "private"}))
        self.assertTrue(walkable({"highway": "service", "access": "private", "foot": "yes"}))
        self.assertFalse(walkable({"highway": "motorway", "foot": "yes"}))
        self.assertFalse(walkable({"highway": "cycleway"}))
        self.assertFalse(walkable({"highway": "path", "foot:conditional": "yes @ daytime"}))
        elevated = {"highway": "tertiary", "bridge": "yes", "sidewalk": "no"}
        self.assertFalse(walkable(elevated))
        self.assertTrue(walkable({**elevated, "foot": "yes"}))
        self.assertTrue(walkable({"highway": "service", "sidewalk": "no"}))

    def test_preserves_geometry_and_length(self):
        graph = graph_from_osm(osm_fixture(), CENTER, 1000)
        self.assertGreater(len(graph.nodes), 4)
        self.assertAlmostEqual(sum(distance(graph.nodes[u], graph.nodes[v]) for u, v, _ in graph.edges), 400, places=5)
        for p in graph.nodes.values():
            projected = graph.project(*graph.unproject(p))
            self.assertLess(distance(p, projected), .00001)

    def test_geometric_crossing_is_not_connection(self):
        local = Graph(CENTER, 1000)
        pts = [(-100, 0), (100, 0), (0, -100), (0, 100)]
        data = {"elements": [{"type": "node", "id": i, "lon": local.unproject(p)[0], "lat": local.unproject(p)[1]} for i, p in enumerate(pts, 1)] + [
            {"type": "way", "id": 1, "nodes": [1, 2], "tags": {"highway": "footway", "bridge": "yes"}},
            {"type": "way", "id": 2, "nodes": [3, 4], "tags": {"highway": "footway"}}]}
        graph = graph_from_osm(data, CENTER, 1000)
        reach, _ = reachable(graph, "1", 2000)
        self.assertIn("2", reach)
        self.assertNotIn("3", reach)

    def test_foot_direction_only(self):
        data = osm_fixture()
        data["elements"][-1]["tags"]["oneway"] = "yes"
        graph = graph_from_osm(data, CENTER, 1000)
        self.assertEqual(len(graph.adj["1"]), 2)
        data["elements"][-1]["tags"]["oneway:foot"] = "yes"
        graph = graph_from_osm(data, CENTER, 1000)
        self.assertEqual(len(graph.adj["1"]), 1)

    def test_private_node_blocks_path(self):
        data = osm_fixture()
        data["elements"][1]["tags"] = {"access": "private"}
        graph = graph_from_osm(data, CENTER, 1000)
        self.assertNotIn("2", graph.nodes)


class ElasticTests(unittest.TestCase):
    def fixture(self):
        graph = Graph(CENTER, 1000)
        for key, point in (("a", (0, 0)), ("b", (200, 0)), ("c", (200, 200)), ("d", (0, 200))):
            graph.node(key, point)
        for u, v in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")):
            graph.edge(u, v, u + v)
        reference = [(0, 0), (190, 5), (190, 190), (5, 190), (0, 0)]
        return graph, reference

    def test_joint_vertex_adjustment_is_bounded_and_original_is_unchanged(self):
        graph, reference = self.fixture()
        original = reference[:]
        variants = elastic_references(graph, reference, "a", True, set(graph.nodes), time.monotonic() + 3)
        self.assertTrue(variants)
        guide, nodes, info = variants[0]
        self.assertEqual(reference, original)
        self.assertEqual(nodes, ["a", "b", "c", "d", "a"])
        self.assertEqual(guide[0], guide[-1])
        self.assertEqual(info["vertices_moved"], 3)
        self.assertLessEqual(info["vertex_max_shift_m"], info["vertex_limit_m"])
        path = match_anchor_pose(graph, guide, nodes, True, 800, time.monotonic() + 3)
        self.assertTrue(path)
        self.assertAlmostEqual(path[0][1], 800)
        for u, v in zip(path[0][2], path[0][2][1:]):
            self.assertTrue(any(edge[0] == v for edge in graph.adj[u]))

    def test_excessive_deformation_has_no_variant(self):
        graph, _ = self.fixture()
        reference = [(0, 0), (130, 40), (130, 130), (40, 130), (0, 0)]
        self.assertEqual(elastic_references(graph, reference, "a", True, set(graph.nodes), time.monotonic() + 3), [])

    def test_adjusted_straight_guide_does_not_invent_a_road(self):
        graph, reference = self.fixture()
        graph.adj.clear()
        variants = elastic_references(graph, reference, "a", True, set(graph.nodes), time.monotonic() + 3)
        self.assertEqual(variants, [])
        guide = [graph.nodes[n] for n in ("a", "b", "c", "d", "a")]
        nodes = ["a", "b", "c", "d", "a"]
        self.assertEqual(match_anchor_pose(graph, guide, nodes, True, 800, time.monotonic() + 3), [])

    def test_original_intentional_retracing_shares_adjusted_anchor(self):
        graph = Graph(CENTER, 1000)
        graph.node("a", (0, 0)); graph.node("b", (100, 0)); graph.edge("a", "b", "ab")
        reference = [(0, 0), (97, 2), (0, 0), (97, 2)]
        variants = elastic_references(graph, reference, "a", False, set(graph.nodes), time.monotonic() + 3)
        self.assertTrue(variants)
        self.assertEqual(variants[0][1], ["a", "b", "a", "b"])

    def test_crossings_are_tracked_without_changing_road_topology(self):
        crossing = [(0, 0), (100, 100), (0, 100), (100, 0)]
        self.assertEqual(crossing_signature(crossing), {(0, 2)})
        self.assertEqual(crossing_signature([(0, 0), (100, 0), (100, 100), (0, 100)]), set())

    def test_feature_can_be_disabled_and_invalid_option_is_rejected(self):
        graph = graph_from_osm(osm_fixture(), CENTER, 1000)
        payload = {"drawing": [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
                   "distance_m": 400, "approach_m": 100, "allow_rotation": False,
                   "allow_vertex_adjustment": False}
        result = generate(graph, payload, seconds=5)
        self.assertTrue(result["found"])
        self.assertFalse(result["vertex_adjustment_enabled"])
        self.assertEqual(result["elastic_poses_tested"], 0)
        self.assertTrue(all(not c["vertex_adjusted"] for c in result["candidates"]))
        with self.assertRaises(InputError):
            generate(graph, {**payload, "allow_vertex_adjustment": "true"})


class EngineTests(unittest.TestCase):
    def test_start_and_pose_selection_covers_all_approach_bands(self):
        graph = Graph(CENTER, 2000)
        reach = {}
        for i, meters in enumerate((0, 280, 560, 840)):
            graph.node(str(i), (meters, 0))
            reach[str(i)] = meters
        starts = distributed_starts(graph, reach, (0, 0), 1000)
        self.assertEqual(set(starts), set(reach))
        poses = [(float(i * 100 + angle), str(i), angle, 1, [(i, 0), (i, 1)])
                 for i in range(4) for angle in range(12)]
        selected = select_poses(poses, graph, reach, (0, 0), 1000, limit=16)
        self.assertEqual({pose[1] for pose in selected}, set(reach))
        self.assertLessEqual(max(sum(p[1] == start for p in selected) for start in starts), 4)

    def test_candidate_diversity_does_not_recommend_bad_fillers(self):
        graph = Graph(CENTER, 2000)
        for key, p in (("a", (0, 0)), ("b", (100, 0)), ("c", (500, 0)), ("d", (600, 0))):
            graph.node(key, p)
        candidates = [{"score": 1, "acceptable": True, "node_ids": ["a", "b"]},
                      {"score": 1.1, "acceptable": True, "node_ids": ["b", "a"]},
                      {"score": .1, "acceptable": False, "node_ids": ["c", "d"]}]
        self.assertEqual(diverse_candidates(candidates, graph), candidates[:1])
        self.assertEqual(diverse_candidates(candidates, graph, 20, include_comparison=True), [candidates[0], candidates[2]])

    def test_connection_gap_counts_against_approach_limit(self):
        graph = Graph(CENTER, 1000)
        graph.node("a", (50, 0)); graph.node("b", (100, 0)); graph.edge("a", "b", "ab")
        with self.assertRaises(InputError):
            generate(graph, {"drawing": [[0, 0], [100, 0]], "distance_m": 500, "approach_m": 25})

    def test_exact_arbitrary_outline_is_recommended(self):
        raw = [[0, 0], [110, 30], [70, 150], [-40, 80], [0, 0]]
        reference, _ = prepare_drawing(raw, 1000)
        graph = Graph(CENTER, 2000)
        for i, point in enumerate(reference[:-1]):
            graph.node(str(i), point)
        for i in range(len(reference) - 1):
            graph.edge(str(i), str((i + 1) % (len(reference) - 1)), str(i))
        result = generate(graph, {"drawing": raw, "distance_m": 1000,
                                 "approach_m": 0, "allow_rotation": False}, seconds=6)
        self.assertTrue(result["found"], result)
        self.assertLess(result["candidates"][0]["mean_error_m"], 1)

    def test_short_stroke_can_use_detour_within_whole_route_budget(self):
        graph = Graph(CENTER, 1000)
        for key, p in (("a", (0, 0)), ("p", (0, 300)), ("q", (60, 300)), ("b", (60, 0))):
            graph.node(key, p)
        for u, v in (("a", "p"), ("p", "q"), ("q", "b")):
            graph.edge(u, v, u + v)
        # Old segment policy rejected 660m because 60*3+250 == 430m.
        path = guided_path(graph, "a", "b", (0, 0), (60, 0), time.monotonic() + 2, 800)
        self.assertEqual(path[0], ["a", "p", "q", "b"])
        self.assertAlmostEqual(path[1], 660)
        self.assertIsNone(guided_path(graph, "a", "b", (0, 0), (60, 0), time.monotonic() + 2, 600))

    def test_snaps_consider_connectivity_not_only_nearest_three(self):
        graph = Graph(CENTER, 1000)
        for key, p in (("start", (0, 0)), ("good", (100, 0)),
                       ("isolated1", (100, 9)), ("isolated2", (100, 10)), ("isolated3", (100, 11))):
            graph.node(key, p)
        graph.edge("start", "good", "main")
        graph.edge("isolated1", "isolated2", "other1")
        graph.edge("isolated2", "isolated3", "other2")
        b = (100, 10)
        self.assertTrue(all(node.startswith("isolated") for _, node in graph.nearest(b, 3)))
        paths = guided_choices(graph, "start", graph.nearest(b, 128, 220), (0, 0), b,
                               time.monotonic() + 2, 300)
        self.assertEqual(paths[0][0], ["start", "good"])
        self.assertAlmostEqual(paths[0][3], 10)
        connected, _ = reachable(graph, "start", 300)
        self.assertEqual(graph.nearest(b, 1, 220, connected)[0][1], "good")

    def test_endpoint_alternatives_are_spatially_diverse(self):
        graph = Graph(CENTER, 1000)
        previous = None
        for i in range(41):
            key = str(i)
            graph.node(key, (i * 5, 0))
            if previous is not None:
                graph.edge(previous, key, key)
            previous = key
        paths = guided_choices(graph, "0", graph.nearest((150, 0), 128, 100),
                               (0, 0), (150, 0), time.monotonic() + 2, 300)
        self.assertGreater(len(paths), 1)
        ends = [graph.nodes[p[0][-1]] for p in paths]
        self.assertTrue(all(distance(a, b) >= 45 for i, a in enumerate(ends) for b in ends[i + 1:]))

    def test_same_endpoint_keeps_different_road_paths(self):
        graph = Graph(CENTER, 1000)
        for key, point in (("s", (0, 0)), ("x", (50, 0)), ("y", (50, 20)), ("e", (100, 0))):
            graph.node(key, point)
        for u, v in (("s", "x"), ("x", "e"), ("s", "y"), ("y", "e")):
            graph.edge(u, v, u + v)
        options = guided_choices(graph, 's', [(0, 'e')], (0, 0), (100, 0), time.monotonic() + 2, 200)
        self.assertEqual({tuple(p[0]) for p in options}, {('s', 'x', 'e'), ('s', 'y', 'e')})
        blocked = frozenset({road_key('s', 'y')})
        options = guided_choices(graph, 's', [(0, 'e')], (0, 0), (100, 0), time.monotonic() + 2, 200,
                                 forbidden_edges=blocked)
        self.assertEqual([p[0] for p in options], [['s', 'x', 'e']])

    def test_anchor_beam_recovers_when_best_prefix_blocks_next_leg(self):
        graph = Graph(CENTER, 1000)
        for key, point in (("s", (0, 0)), ("x", (80, 0)), ("y", (50, -25)),
                           ("a", (100, 0)), ("b", (100, 100)), ("c", (0, 100))):
            graph.node(key, point)
        for u, v in (("s", "x"), ("x", "a"), ("s", "y"), ("y", "a"),
                     ("x", "b"), ("b", "c"), ("c", "s")):
            graph.edge(u, v, u + v)
        nodes = ['s', 'a', 'b', 'c', 's']
        reference = [graph.nodes[n] for n in nodes]
        paths = match_anchor_pose(graph, reference, nodes, True, 440, time.monotonic() + 3, tolerance=.2)
        self.assertTrue(paths)
        self.assertTrue(any(path[:3] == ['s', 'y', 'a'] for _, _, path in paths))
        for _, _, path in paths:
            self.assertEqual(road_retrace_stats(graph, path, reference)[1], 0)

    def test_candidate_limit_validation(self):
        graph = demo_graph(CENTER)
        for value in (0, 31, 2.5, float('nan')):
            with self.assertRaises(InputError):
                generate(graph, {'candidate_limit': value})

    def test_location_representatives_do_not_chain_or_fill_from_one_start(self):
        graph = Graph(CENTER, 2000)
        for key, point in [('a', (0, 0)), ('b', (200, 0)), ('c', (400, 0)), ('d', (900, 0))]:
            graph.node(key, point)
        candidates = [{"score": score, "acceptable": accepted, "node_ids": [node]}
                      for node, score, accepted in [('a', 1, True), ('a', 1.1, True), ('b', 1.2, True),
                                                   ('c', 2, True), ('d', .5, False)]]
        result = location_representatives(candidates, graph)
        self.assertEqual([c['node_ids'][0] for c in result], ['a', 'c', 'd'])
        self.assertEqual([c['location_candidate_count'] for c in result], [3, 1, 1])
        self.assertEqual(len(location_representatives(candidates, graph, limit=2)), 2)

    def test_local_search_keeps_original_approach_limit_and_focus_radius(self):
        graph = demo_graph(CENTER)
        focus = graph.unproject((300, 0))
        result = generate(graph, {"drawing": [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
                                  "distance_m": 800, "approach_m": 500, "allow_rotation": False,
                                  "allow_vertex_adjustment": False, "search_mode": "local",
                                  "focus_location": focus}, seconds=5)
        self.assertTrue(result['candidates'])
        self.assertEqual(result['search_mode'], 'local')
        for candidate in result['candidates']:
            self.assertLessEqual(distance(graph.nodes[candidate['node_ids'][0]], (300, 0)), 250.001)
            self.assertLessEqual(candidate['approach_m'], 500)
        with self.assertRaises(InputError):
            generate(graph, {"drawing": [[0, 0], [100, 0]], "search_mode": "local",
                             "focus_location": graph.unproject((2000, 2000)), "approach_m": 100})

    def test_shorter_costlier_prefix_survives_global_budget(self):
        graph = Graph(CENTER, 1000)
        for key, p in (("start", (0, 0)), ("cheap", (25, 15)), ("merge", (50, 0)),
                       ("turn", (75, 30)), ("end", (100, 0))):
            graph.node(key, p)
        graph.edge("start", "merge", "stairs", penalty=3)
        for u, v in (("start", "cheap"), ("cheap", "merge"), ("merge", "turn"), ("turn", "end")):
            graph.edge(u, v, u + v)
        # Cheapest prefix to 'merge' uses too many meters. Keeping only its
        # cost would discard the more costly but shorter feasible prefix.
        result = guided_path(graph, "start", "end", (0, 0), (100, 0), time.monotonic() + 2, 130)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], ["start", "merge", "turn", "end"])
        self.assertLessEqual(result[1], 130)

    def test_checkpoint_coarsening_keeps_corners_and_crossings(self):
        raw = [[0, 0], [100, 100], [0, 100], [100, 0], [0, 0]]
        reference, _ = prepare_drawing(raw, 5000)
        checkpoints = search_checkpoints(reference, 5000)
        self.assertAlmostEqual(length(checkpoints), length(reference), places=5)
        self.assertEqual(checkpoints[0], checkpoints[-1])
        self.assertLess(len(checkpoints), len(reference))
        self.assertLess(shape_metrics(reference, checkpoints)[0], .001)

    def test_osm_square_end_to_end(self):
        graph = graph_from_osm(osm_fixture(), CENTER, 1000)
        payload = {"drawing": [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
                   "distance_m": 400, "approach_m": 100, "route_type": "loop",
                   "allow_rotation": False, "location": CENTER, "tolerance": .1}
        result = generate(graph, payload, seconds=10)
        self.assertTrue(result["found"], result)
        candidate = result["candidates"][0]
        self.assertAlmostEqual(candidate["distance_m"], 400, delta=2)
        self.assertEqual(candidate["node_ids"][0], candidate["node_ids"][-1])
        self.assertLess(candidate["mean_error_m"], 1)
        for u, v in zip(candidate["node_ids"], candidate["node_ids"][1:]):
            self.assertTrue(any(e[0] == v for e in graph.adj[u]))
        self.assertEqual(candidate["geometry"]["type"], "LineString")
        self.assertAlmostEqual(candidate["geometry"]["coordinates"][0][0], CENTER[0], places=4)

    def test_20km_loop_and_caller_specific_limit(self):
        graph = Graph(CENTER, 6000, source="synthetic")
        # A 5km-sided square with nodes every 50m, perimeter exactly 20km.
        points = [(i * 50, 0) for i in range(100)]
        points += [(5000, i * 50) for i in range(100)]
        points += [(5000 - i * 50, 5000) for i in range(100)]
        points += [(0, 5000 - i * 50) for i in range(100)]
        for index, point in enumerate(points):
            graph.node(str(index), (point[0], -point[1]))  # Canvas y grows downward.
        for index in range(len(points)):
            graph.edge(str(index), str((index + 1) % len(points)), str(index))
        payload = {"drawing": [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
                   "distance_m": 20000, "approach_m": 0, "route_type": "loop",
                   "allow_rotation": False, "allow_vertex_adjustment": False,
                   "location": CENTER, "tolerance": .1}
        with self.assertRaises(InputError):
            generate(graph, payload, seconds=3)
        with self.assertRaises(InputError):
            generate(graph, {**payload, "distance_m": 20001}, max_distance_m=20000)
        result = generate(graph, payload, seconds=5, max_distance_m=20000)
        self.assertTrue(result["found"], result)
        candidate = result["candidates"][0]
        self.assertAlmostEqual(candidate["distance_m"], 20000, delta=1)
        self.assertEqual(candidate["node_ids"][0], candidate["node_ids"][-1])
        self.assertLess(candidate["mean_error_m"], 1)
        self.assertEqual(len(set(candidate["node_ids"][:-1])), len(candidate["node_ids"]) - 1)

    def test_synthetic_route_is_explicit(self):
        graph = demo_graph(CENTER)
        result = generate(graph, {"drawing": [[0, 0], [200, 0], [200, 200], [0, 200], [0, 0]],
                      "distance_m": 800, "approach_m": 200, "allow_rotation": False}, seconds=8)
        self.assertEqual(result["source"], "synthetic")
        self.assertIn("가상", result["message"])
        self.assertTrue(result["candidates"])
        self.assertLessEqual(result["candidates"][0]["approach_m"], 200)

    def test_far_user_and_nan_rejected(self):
        graph = graph_from_osm(osm_fixture(), CENTER, 1000)
        base = {"drawing": [[0, 0], [100, 100]], "distance_m": 500}
        with self.assertRaises(InputError):
            generate(graph, {**base, "distance_m": math.nan})
        with self.assertRaises(InputError):
            generate(graph, {**base, "location": graph.unproject((2000, 2000))})

    def test_scale_alternatives_allow_grid_star_length(self):
        points = []
        for i in range(11):
            a = -math.pi / 2 + i * math.pi / 5
            r = 220 if i % 2 == 0 else 95
            points.append([300 + math.cos(a) * r, 300 + math.sin(a) * r])
        result = generate(demo_graph(CENTER), {"drawing": points, "distance_m": 3000}, seconds=12)
        self.assertTrue(result["candidates"], result)
        # Feasible length alone must no longer certify a grid-distorted drawing.
        self.assertEqual(result["found"], any(c["acceptable"] for c in result["candidates"]))
        self.assertTrue(all(abs(c["distance_m"] - 3000) <= 600 for c in result["candidates"]))

    def test_no_valid_length_returns_no_candidate(self):
        graph = Graph(CENTER, 1000)
        graph.node("a", (0, 0)); graph.node("b", (50, 0)); graph.edge("a", "b", "ab")
        result = generate(graph, {"drawing": [[0, 0], [100, 0]], "distance_m": 800,
                                 "approach_m": 0, "allow_rotation": False}, seconds=2)
        self.assertFalse(result["found"])
        self.assertEqual(result["candidates"], [])


if __name__ == "__main__":
    unittest.main()

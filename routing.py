"""Experimental ordered-stroke routing. No object recognition or routing SaaS.

OSM topology is kept by node IDs, never by geometric intersections. Long edges
are subdivided without creating new intersections. This is a bounded heuristic,
not a globally optimal shape solver or a safety-certified navigation engine.
"""
import heapq
import math
import time
from collections import defaultdict

EARTH = 6371008.8
ALLOWED = {"footway", "path", "pedestrian", "steps", "residential",
           "living_street", "service", "unclassified", "tertiary", "tertiary_link"}
FOOT_ALLOW = {"yes", "designated", "permissive"}
FOOT_DENY = {"no", "private", "use_sidepath"}
PLACEMENT_SCALES = (.55, .65, .75, .85, .95, 1, 1.1)
CHECKPOINT_OFFSET_WEIGHT = 3


class InputError(ValueError):
    pass


class SearchLimit(Exception):
    pass


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def length(points):
    return sum(distance(a, b) for a, b in zip(points, points[1:]))


def segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    t = max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den)) if den else 0
    return distance(p, (a[0] + t * dx, a[1] + t * dy))


def resample(points, count=64):
    if len(points) < 2 or length(points) < 1e-9:
        return [points[0]] * count
    total = length(points)
    result, idx, accumulated = [], 0, 0.0
    for i in range(count):
        target = total * i / (count - 1)
        while idx < len(points) - 2 and accumulated + distance(points[idx], points[idx + 1]) < target:
            accumulated += distance(points[idx], points[idx + 1])
            idx += 1
        a, b = points[idx:idx + 2]
        d = distance(a, b)
        t = min(1, (target - accumulated) / d) if d else 0
        result.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return result


def simplify(points, tolerance):
    # Iterative RDP keeps corners and endpoints without recursion limits.
    # Distance-only RDP would erase collinear out-and-back strokes. Protect
    # reversal vertices before simplifying each interval independently.
    keep = {0, len(points) - 1}
    for i in range(1, len(points) - 1):
        u = (points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1])
        v = (points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
        if u[0] * v[0] + u[1] * v[1] < -.85 * distance(points[i - 1], points[i]) * distance(points[i], points[i + 1]):
            keep.add(i)
    ordered = sorted(keep)
    pending = list(zip(ordered, ordered[1:]))
    while pending:
        start, end = pending.pop()
        if end - start < 2:
            continue
        d, idx = max((segment_distance(points[i], points[start], points[end]), i)
                     for i in range(start + 1, end))
        if d > tolerance:
            keep.add(idx)
            pending.extend(((start, idx), (idx, end)))
    return [points[i] for i in sorted(keep)]


def prepare_drawing(raw, target, route_type="auto"):
    if not isinstance(raw, list) or not 2 <= len(raw) <= 3000:
        raise InputError("연속된 선을 그려주세요. 좌표는 2~3,000개까지 가능합니다.")
    points = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise InputError("그림 좌표 형식이 올바르지 않습니다.")
        try:
            x, y = float(p[0]), float(p[1])
        except (ValueError, TypeError):
            raise InputError("그림 좌표는 숫자여야 합니다.") from None
        if not all(math.isfinite(v) and abs(v) <= 10000 for v in (x, y)):
            raise InputError("그림 좌표 범위를 확인해주세요.")
        # Canvas Y points south; metric Y points north.
        pt = (x, -y)
        if not points or distance(points[-1], pt) > .05:
            points.append(pt)
    if len(points) < 2 or length(points) < 10:
        raise InputError("조금 더 길게 선을 그려주세요.")
    span = max(max(p[i] for p in points) - min(p[i] for p in points) for i in (0, 1))
    if route_type not in {"auto", "open", "loop"}:
        raise InputError("경로 유형을 확인해주세요.")
    closed = route_type == "loop" or (route_type == "auto" and distance(points[0], points[-1]) <= span * .08)
    if closed and distance(points[-1], points[0]) > .001:
        points.append(points[0])
    # No standard star/heart templates: preserve this actual user's line.
    points = simplify(points, max(.2, span * .002))
    if len(points) > 140:
        raise InputError("선의 굴곡이 너무 많습니다. 이번 실험에서는 조금 단순한 선으로 테스트해주세요.")
    factor = target / length(points)
    first = points[0]
    normalized = [((x - first[0]) * factor, (y - first[1]) * factor) for x, y in points]
    dense = [normalized[0]]
    spacing = max(45, target / 65)
    for a, b in zip(normalized, normalized[1:]):
        n = max(1, math.ceil(distance(a, b) / spacing))
        dense.extend((a[0] + (b[0] - a[0]) * j / n,
                      a[1] + (b[1] - a[1]) * j / n) for j in range(1, n + 1))
    return dense, closed


def frechet(a, b):
    previous = []
    for i, p in enumerate(a):
        row = []
        for j, q in enumerate(b):
            d = distance(p, q)
            if i == 0 and j == 0:
                row.append(d)
            elif i == 0:
                row.append(max(d, row[j - 1]))
            elif j == 0:
                row.append(max(d, previous[j]))
            else:
                row.append(max(d, min(previous[j], previous[j - 1], row[j - 1])))
        previous = row
    return previous[-1]


def shape_metrics(reference, route):
    a, b = resample(reference), resample(route)
    def mean_to_curve(samples, curve):
        return sum(min(segment_distance(p, c, d) for c, d in zip(curve, curve[1:]))
                   for p in samples) / len(samples)
    mean = (mean_to_curve(a, route) + mean_to_curve(b, reference)) / 2
    ordered = frechet(a, b)
    return mean, ordered


def shape_details(reference, route):
    """Ordered alignment plus local features; no named-shape recognition.

    DTW aligns progress before comparing directions and turns, so a road
    staircase does not shift every subsequent corner by its extra length.
    These are prototype measurements, not a calibrated similarity percentage.
    """
    a, b = resample(reference, 96), resample(route, 96)
    dp, parent = {}, {}
    for i, p in enumerate(a):
        for j, q in enumerate(b):
            if i == j == 0:
                dp[i, j] = distance(p, q)
                continue
            options = [(dp[x, y], x, y) for x, y in ((i - 1, j - 1), (i - 1, j), (i, j - 1)) if (x, y) in dp]
            cost, x, y = min(options)
            dp[i, j] = cost + distance(p, q)
            parent[i, j] = (x, y)
    matches = defaultdict(list)
    cursor = (95, 95)
    while True:
        matches[cursor[0]].append(cursor[1])
        if cursor == (0, 0):
            break
        cursor = parent[cursor]
    aligned = [round(sum(matches[i]) / len(matches[i])) for i in range(96)]

    def vector(p, q):
        d = distance(p, q)
        return ((q[0] - p[0]) / d, (q[1] - p[1]) / d) if d > 1e-6 else None

    def turn(p, q, r):
        u, v = vector(p, q), vector(q, r)
        return math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1]) if u and v else None

    directions = []
    for i in range(4, 92, 3):
        u, v = vector(a[i - 4], a[i + 4]), vector(b[aligned[i - 4]], b[aligned[i + 4]])
        directions.append(1 - max(-1, min(1, u[0] * v[0] + u[1] * v[1])) if u and v else 1)
    span = max(max(p[k] for p in reference) - min(p[k] for p in reference) for k in (0, 1))
    corners = simplify(reference, max(2, span * .015))
    corner_errors, missing = [], 0
    progress = 0
    for index in range(1, len(corners) - 1):
        progress += distance(corners[index - 1], corners[index])
        expected = turn(*corners[index - 1:index + 2])
        if expected is None or abs(expected) < math.radians(35):
            continue
        # Geographic round trips introduce sub-micrometre noise. Without this
        # stable tie rule, a half-sample corner jumps from 47 to 48 and can
        # change the recommendation despite an identical visible line.
        i = max(4, min(91, round(round(progress / max(length(reference), 1) * 95, 8))))
        actual = turn(b[aligned[i - 4]], b[aligned[i]], b[aligned[i + 4]])
        error = math.pi if actual is None else abs(math.atan2(math.sin(actual - expected), math.cos(actual - expected)))
        corner_errors.append(math.degrees(error))
        if actual is None or actual * expected <= 0 or abs(actual) < abs(expected) * .4:
            missing += 1
    if distance(reference[0], reference[-1]) < .001 and len(corners) > 3:
        expected = turn(corners[-2], corners[0], corners[1])
        if expected is not None and abs(expected) >= math.radians(35):
            actual = turn(b[aligned[91]], b[0], b[aligned[4]])
            error = math.pi if actual is None else abs(math.atan2(math.sin(actual - expected), math.cos(actual - expected)))
            corner_errors.append(math.degrees(error))
            if actual is None or actual * expected <= 0 or abs(actual) < abs(expected) * .4:
                missing += 1
    errors = sorted([min(segment_distance(p, c, d) for c, d in zip(route, route[1:])) for p in a] +
                    [min(segment_distance(p, c, d) for c, d in zip(reference, reference[1:])) for p in b])
    return {"p95_error_m": errors[math.ceil(len(errors) * .95) - 1],
            "direction_error": sum(directions) / max(len(directions), 1),
            "corner_error_deg": sum(corner_errors) / max(len(corner_errors), 1),
            "missing_corners": missing, "corner_count": len(corner_errors),
            "detour_ratio": length(route) / max(length(reference), 1)}


def shape_policy(mean, ordered, details, reference):
    # Bound errors by the drawn footprint, not only the requested run length.
    span = max(max(p[k] for p in reference) - min(p[k] for p in reference) for k in (0, 1))
    unit = max(25, span * .07)
    acceptable = (mean <= unit and ordered <= unit * 3 and details["p95_error_m"] <= unit * 2 and
                  details["direction_error"] <= .4 and details["corner_error_deg"] <= 50 and
                  details["missing_corners"] <= math.floor(details["corner_count"] * .2) and
                  details["detour_ratio"] <= 1.65)
    score = (.3 * mean + .4 * ordered + .3 * details["p95_error_m"]) / unit
    score += details["direction_error"] * 2 + details["corner_error_deg"] / 60
    score += details["missing_corners"] / max(details["corner_count"], 1) * 2
    score += max(0, details["detour_ratio"] - 1.3)
    return acceptable, score


def distributed_starts(graph, reach, user, approach_limit, limit=160):
    """Round-robin road-distance bands and bearings, then 90m spatial cells."""
    buckets, cells = defaultdict(list), set()
    for node in sorted(reach, key=lambda n: (reach[n], n)):
        x, y = graph.nodes[node]
        cell = (math.floor(x / 90), math.floor(y / 90))
        if cell in cells:
            continue
        cells.add(cell)
        band = min(3, int(reach[node] / max(approach_limit, 1) * 4))
        sector = int((math.atan2(y - user[1], x - user[0]) + math.pi) / (math.pi / 4)) % 8
        buckets[band, sector].append(node)
    result = []
    for index in range(limit):
        added = False
        for key in sorted(buckets):
            if index < len(buckets[key]):
                result.append(buckets[key][index])
                added = True
                if len(result) == limit:
                    return result
        if not added:
            break
    return result


def select_poses(poses, graph, reach, user, approach_limit, limit=48):
    """Reserve search opportunities for each band/sector and scale."""
    buckets = defaultdict(list)
    for pose in sorted(poses, key=lambda p: p[0]):
        point = graph.nodes[pose[1]]
        band = min(3, int(reach[pose[1]] / max(approach_limit, 1) * 4))
        sector = int((math.atan2(point[1] - user[1], point[0] - user[0]) + math.pi) / (math.pi / 2)) % 4
        buckets[band, sector].append(pose)
    selected, used, counts, scales = [], set(), defaultdict(int), set()

    def add(pose):
        key = pose[1:4]
        if key in used or counts[pose[1]] >= 4:
            return False
        selected.append(pose)
        used.add(key)
        counts[pose[1]] += 1
        scales.add(pose[3])
        return True

    # Quality remains important; coverage does not imply forced distant results.
    for pose in sorted(poses, key=lambda p: p[0])[:4]:
        add(pose)
    while len(selected) < limit:
        added = False
        for key in sorted(buckets):
            options = [p for p in buckets[key] if p[1:4] not in used and counts[p[1]] < 4]
            if not options:
                continue
            pose = next((p for p in options if p[3] not in scales), options[0])
            added |= add(pose)
            if len(selected) >= limit:
                break
        if not added:
            break
    return selected


def diverse_candidates(candidates, graph, limit=3, include_comparison=False):
    """Prefer different locations among similarly good paths, not bad fillers."""
    ranked = sorted(candidates, key=lambda c: (not c["acceptable"], c["score"]))
    if not ranked:
        return []
    pool = ranked if include_comparison else [c for c in ranked if c["acceptable"] == ranked[0]["acceptable"]]
    result, sets = [], []
    while pool and len(result) < limit:
        eligible = [c for c in pool if c["acceptable"] == pool[0]["acceptable"]]
        best_score = min(c["score"] for c in eligible)
        comparable = [c for c in eligible if c["score"] <= best_score + .5]

        def edges(c):
            return {tuple(sorted((u, v))) for u, v in zip(c["node_ids"], c["node_ids"][1:])}

        def rank(c):
            road_set = edges(c)
            overlap = max((len(road_set & s) / max(1, len(road_set | s)) for s in sets), default=0)
            separation = min((distance(graph.nodes[c["node_ids"][0]], graph.nodes[r["node_ids"][0]]) for r in result), default=200)
            return c["score"] + overlap * .8 + max(0, 200 - separation) / 200 * .4

        chosen = min(comparable, key=rank)
        road_set = edges(chosen)
        result.append(chosen)
        sets.append(road_set)
        threshold = .95 if include_comparison else .7
        pool = [c for c in pool if len(edges(c) & road_set) / max(1, len(edges(c) | road_set)) <= threshold]
    return result


def location_representatives(candidates, graph, limit=30, radius=250):
    """One best route per fixed spatial anchor; never chain nearby groups."""
    groups = []
    for candidate in sorted(candidates, key=lambda c: (not c["acceptable"], c["score"])):
        point = graph.nodes[candidate["node_ids"][0]]
        nearby = [(distance(point, anchor), index) for index, (anchor, _) in enumerate(groups)
                  if distance(point, anchor) < radius]
        if nearby:
            groups[min(nearby)[1]][1].append(candidate)
        else:
            groups.append((point, [candidate]))
    return [{**members[0], "location_center": graph.unproject(anchor),
             "location_candidate_count": len(members), "location_radius_m": radius}
            for anchor, members in groups[:limit]]


def walkable(tags):
    highway = tags.get("highway")
    foot = tags.get("foot")
    if foot in FOOT_DENY or tags.get("area") == "yes":
        return False
    if highway in {"construction", "proposed", "motorway", "motorway_link", "raceway"}:
        return False
    if tags.get("access") in {"no", "private"} and foot not in FOOT_ALLOW:
        return False
    # Conditional permissions need a time-aware policy; conservatively exclude.
    if tags.get("foot:conditional") or (tags.get("access:conditional") and foot not in FOOT_ALLOW):
        return False
    # An elevated vehicle road with explicitly no sidewalk is not evidence of
    # a walkable connection through the parking area underneath it.
    if tags.get("bridge") == "yes" and tags.get("sidewalk") == "no" and highway in {
            "tertiary", "tertiary_link", "secondary", "secondary_link", "primary", "primary_link"} and foot not in FOOT_ALLOW:
        return False
    if highway in ALLOWED:
        return True
    return highway in {"cycleway", "secondary", "secondary_link", "primary", "primary_link", "trunk", "trunk_link", "track"} and foot in FOOT_ALLOW


class Graph:
    def __init__(self, center, radius, source="osm"):
        self.center = center  # [lng, lat]
        self.radius = radius
        self.source = source
        self.nodes = {}
        self.adj = defaultdict(list)
        self.cells = defaultdict(list)
        self.edges = []
        self.metadata = {}

    def project(self, lng, lat):
        return (math.radians(lng - self.center[0]) * EARTH * math.cos(math.radians(self.center[1])),
                math.radians(lat - self.center[1]) * EARTH)

    def unproject(self, p):
        return [self.center[0] + math.degrees(p[0] / (EARTH * math.cos(math.radians(self.center[1])))),
                self.center[1] + math.degrees(p[1] / EARTH)]

    def node(self, key, p):
        if key not in self.nodes:
            self.nodes[key] = p
            self.cells[(math.floor(p[0] / 100), math.floor(p[1] / 100))].append(key)

    def edge(self, u, v, eid, penalty=1, direction=0):
        d = distance(self.nodes[u], self.nodes[v])
        if d < .01:
            return
        if direction != -1:
            self.adj[u].append((v, d, eid, penalty))
        if direction != 1:
            self.adj[v].append((u, d, eid, penalty))
        self.edges.append((u, v, eid))

    def nearest(self, p, count=1, limit=180, allowed=None):
        cx, cy = math.floor(p[0] / 100), math.floor(p[1] / 100)
        r = math.ceil(limit / 100)
        if count == 1:
            best, best_squared = None, limit * limit
            for x in range(cx - r, cx + r + 1):
                for y in range(cy - r, cy + r + 1):
                    for key in self.cells.get((x, y), ()):
                        if allowed is not None and key not in allowed:
                            continue
                        q = self.nodes[key]
                        squared = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
                        if squared < best_squared or (squared == best_squared and (best is None or key < best)):
                            best, best_squared = key, squared
            return [(math.sqrt(best_squared), best)] if best is not None else []
        nearby = [(distance(p, self.nodes[k]), k)
                  for x in range(cx - r, cx + r + 1)
                  for y in range(cy - r, cy + r + 1)
                  for k in self.cells.get((x, y), []) if allowed is None or k in allowed]
        return heapq.nsmallest(count, ((d, k) for d, k in nearby if d <= limit))

    def preview(self):
        return [[self.unproject(self.nodes[u]), self.unproject(self.nodes[v])] for u, v, _ in self.edges]


def graph_from_osm(data, center, radius):
    elements = data.get("elements")
    if not isinstance(elements, list) or len(elements) > 180000:
        raise InputError("OSM JSON 형식을 확인하거나 더 작은 지역을 사용해주세요.")
    graph = Graph(center, radius)
    original = {}
    blocked = set()
    for e in elements:
        if e.get("type") == "node":
            p = graph.project(float(e["lon"]), float(e["lat"]))
            if not all(math.isfinite(v) for v in p):
                raise InputError("OSM 좌표가 올바르지 않습니다.")
            original[str(e["id"])] = p
            tags = e.get("tags", {})
            if tags.get("foot") in FOOT_DENY or (tags.get("access") in {"private", "no"} and tags.get("foot") not in FOOT_ALLOW):
                blocked.add(str(e["id"]))
    retained = 0
    for e in elements:
        tags = e.get("tags", {})
        if e.get("type") != "way" or not walkable(tags):
            continue
        ids = [str(n) for n in e.get("nodes", [])]
        penalty = 1.2 if tags.get("highway") == "steps" else 1
        direction = {"yes": 1, "1": 1, "-1": -1}.get(tags.get("oneway:foot"), 0)
        used = False
        for idx, (u, v) in enumerate(zip(ids, ids[1:])):
            if u not in original or v not in original or u in blocked or v in blocked:
                continue
            a, b = original[u], original[v]
            if any(abs(q) > radius + 100 for p in (a, b) for q in p):
                continue
            graph.node(u, a)
            graph.node(v, b)
            n = max(1, math.ceil(distance(a, b) / 35))
            previous = u
            for j in range(1, n + 1):
                key = v if j == n else f"w{e['id']}:{idx}:{j}"
                graph.node(key, (a[0] + (b[0] - a[0]) * j / n, a[1] + (b[1] - a[1]) * j / n))
                graph.edge(previous, key, f"{e['id']}:{idx}:{j}", penalty, direction)
                previous = key
            used = True
        retained += used
    graph.metadata = {"ways": retained, "osm_timestamp": data.get("osm3s", {}).get("timestamp_osm_base")}
    if not graph.edges:
        raise InputError("이 지역에서 사용할 보행 도로를 찾지 못했습니다. 위치 또는 데이터 범위를 바꿔주세요.")
    if len(graph.nodes) > 100000:
        raise InputError("도로가 너무 많습니다. 데이터 범위를 줄여주세요.")
    return graph


def demo_graph(center):
    """Explicit synthetic fixture; never labeled or substituted as real roads."""
    graph = Graph(center, 2000, "synthetic")
    for x in range(-1200, 1201, 100):
        for y in range(-1200, 1201, 100):
            graph.node(f"{x},{y}", (x, y))
    for x in range(-1200, 1201, 100):
        for y in range(-1200, 1201, 100):
            u = f"{x},{y}"
            for dx, dy in ((100, 0), (0, 100)):
                v = f"{x + dx},{y + dy}"
                if v in graph.nodes:
                    # Subdivide for comparable snapping density.
                    mid = f"mid:{u}:{v}"
                    graph.node(mid, (x + dx / 2, y + dy / 2))
                    graph.edge(u, mid, mid + ":a")
                    graph.edge(mid, v, mid + ":b")
    return graph


def reachable(graph, start, budget, forbidden_edges=frozenset(), deadline=math.inf):
    costs, parents, queue = {start: 0}, {}, [(0, start)]
    visits = 0
    while queue:
        visits += 1
        if visits % 100 == 0 and time.monotonic() >= deadline:
            raise SearchLimit()
        d, u = heapq.heappop(queue)
        if d != costs[u]:
            continue
        for v, meters, _, _ in graph.adj[u]:
            if road_key(u, v) in forbidden_edges:
                continue
            nd = d + meters
            if nd <= budget and nd < costs.get(v, math.inf):
                costs[v], parents[v] = nd, u
                heapq.heappush(queue, (nd, v))
    return costs, parents


def reconstruct(parents, start, end):
    result = [end]
    while result[-1] != start:
        result.append(parents[result[-1]])
    return result[::-1]


def guided_path(graph, start, end, a, b, deadline, max_length=math.inf, forbidden_edges=frozenset()):
    choices = [(distance(graph.nodes[end], b), end)]
    matches = guided_choices(graph, start, choices, a, b, deadline, max_length, count=1,
                             forbidden_edges=forbidden_edges)
    return matches[0][:3] if matches else None


def search_checkpoints(reference, target, relaxed=False):
    # Coarsen straight sections only; retain corners, crossings and stroke order.
    # The full reference remains unchanged for display and final shape scoring.
    corners = simplify(reference, .01)
    points = [corners[0]]
    for a, b in zip(corners, corners[1:]):
        spacing = max(180, target / 10) if relaxed else max(70, target / 24)
        count = max(1, math.ceil(distance(a, b) / spacing))
        points.extend((a[0] + (b[0] - a[0]) * j / count,
                       a[1] + (b[1] - a[1]) * j / count) for j in range(1, count + 1))
    return points


def turning_angle(a, b, c):
    u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    return math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1])


def road_key(u, v):
    # Block either direction of an already-used road, not shared intersections.
    return tuple(sorted((u, v)))


def retrace_corridors(reference, epsilon=None, minimum_overlap=None, alignment_limit=-.995, clip_overlap=False, with_width=False):
    """Actual opposite overlapping strokes; a sharp star/V tip is not retracing."""
    span = max(max(p[k] for p in reference) - min(p[k] for p in reference) for k in (0, 1))
    points = simplify(reference, max(.1, span * .001))
    epsilon = max(.5, min(3, span * .002)) if epsilon is None else epsilon
    minimum_overlap = max(2, span * .01) if minimum_overlap is None else minimum_overlap
    corridors = []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        size = distance(a, b)
        if size < 1:
            continue
        ux, uy = (b[0] - a[0]) / size, (b[1] - a[1]) / size
        for c, d in zip(points[i + 1:], points[i + 2:]):
            other = distance(c, d)
            if other < 1 or ((d[0] - c[0]) * ux + (d[1] - c[1]) * uy) / other > alignment_limit:
                continue
            # Nearly parallel legs alone do not qualify: they must overlap on
            # the same drawn line, rather than form a thin loop or sharp tip.
            if not clip_overlap and max(abs((p[0] - a[0]) * uy - (p[1] - a[1]) * ux) for p in (c, d)) > epsilon:
                continue
            tc, td = ((p[0] - a[0]) * ux + (p[1] - a[1]) * uy for p in (c, d))
            lo, hi = max(0, min(tc, td)), min(size, max(tc, td))
            gap = 0.0
            if clip_overlap and hi > lo:
                # Compare only the overlapping part of unequal/curved road
                # legs; distant endpoints outside that part are irrelevant.
                yc, yd = ((p[0] - a[0]) * uy - (p[1] - a[1]) * ux for p in (c, d))
                slope = (yd - yc) / (td - tc)
                if abs(slope) < 1e-9:
                    if abs(yc) > epsilon:
                        continue
                else:
                    t1, t2 = tc + (-epsilon - yc) / slope, tc + (epsilon - yc) / slope
                    lo, hi = max(lo, min(t1, t2)), min(hi, max(t1, t2))
                gap = max(abs(yc + slope * (lo - tc)), abs(yc + slope * (hi - tc)))
            if hi - lo >= minimum_overlap:
                corridor = ((a[0] + lo * ux, a[1] + lo * uy), (a[0] + hi * ux, a[1] + hi * uy))
                corridors.append((*corridor, gap) if with_width else corridor)
    return corridors


def retrace_edge_allowed(graph, edge, corridors, width):
    a, b = (graph.nodes[n] for n in edge)
    size = distance(a, b)
    if size < 1e-6:
        return False
    for c, d in corridors:
        alignment = abs((b[0] - a[0]) * (d[0] - c[0]) + (b[1] - a[1]) * (d[1] - c[1])) / max(size * distance(c, d), 1e-6)
        if alignment >= .9 and max(segment_distance(p, c, d) for p in (a, b)) <= width:
            return True
    return False


def retrace_width(reference):
    span = max(max(p[k] for p in reference) - min(p[k] for p in reference) for k in (0, 1))
    return max(15, min(75, span * .05))


def forbidden_retraces(graph, used, corridors, width, a, b):
    # Only the leg currently following an explicitly retraced stroke may reuse
    # its local roads. A return drawn elsewhere never unlocks the entire route.
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    local = [(c, d) for c, d in corridors if segment_distance(mid, c, d) <= width]
    return frozenset(edge for edge in used if not retrace_edge_allowed(graph, edge, local, width))


def road_retrace_stats(graph, path, reference):
    corridors, width = retrace_corridors(reference), retrace_width(reference)
    used, repeated, unwanted = set(), 0.0, 0.0
    for u, v in zip(path, path[1:]):
        edge = road_key(u, v)
        if edge in used:
            meters = distance(graph.nodes[u], graph.nodes[v])
            repeated += meters
            if not retrace_edge_allowed(graph, edge, corridors, width):
                unwanted += meters
        used.add(edge)
    return repeated, unwanted


def near_return_corridors(points):
    # Curved roads may be split into many short legs. Collect nearby parallel
    # overlaps first, then require a connected return of at least 30m; counting
    # only individual long straight legs misses these real-world spurs.
    corridors = retrace_corridors(points, epsilon=30, minimum_overlap=5,
                                  alignment_limit=-.97, clip_overlap=True, with_width=True)
    groups = []
    pending = set(range(len(corridors)))
    while pending:
        group = [pending.pop()]
        for idx in group:
            a, b = corridors[idx][:2]
            for other in list(pending):
                c, d = corridors[other][:2]
                alignment = abs((b[0] - a[0]) * (d[0] - c[0]) + (b[1] - a[1]) * (d[1] - c[1])) / max(distance(a, b) * distance(c, d), 1e-6)
                if alignment >= .85 and min(distance(p, q) for p in (a, b) for q in (c, d)) <= 20:
                    pending.remove(other)
                    group.append(other)
        # Union projected intervals to avoid counting overlapping pairs twice.
        a, b = corridors[group[0]][:2]
        size = distance(a, b)
        ux, uy = (b[0] - a[0]) / size, (b[1] - a[1]) / size
        intervals = []
        for idx in group:
            projections = [(p[0] - a[0]) * ux + (p[1] - a[1]) * uy for p in corridors[idx][:2]]
            intervals.append((min(projections), max(projections)))
        covered, end = 0.0, -math.inf
        for lo, hi in sorted(intervals):
            covered += max(0, hi - max(lo, end))
            end = max(end, hi)
        # A 30m-wide, 100m-long block is not a one-dimensional spur. A
        # 20m-wide, 200m-long return is. Require length >= five times width.
        if covered >= max(30, 5 * max(corridors[idx][2] for idx in group)):
            groups.extend(corridors[idx][:2] for idx in group)
    return groups


def near_retrace_stats(route, reference):
    """Catch thin out-and-back spurs on distinct, almost-parallel OSM roads.

    Exact edge reuse is checked separately. Here only long opposite strokes
    within 30m and at least five times as long as wide qualify. Normal blocks,
    crossings and ordinary star tips do not.
    Deliberately drawn thin returns are allowed in their own local corridor.
    This is a geometric heuristic, not a claim that the roads are identical.
    """
    corridors = near_return_corridors(route)
    intended = near_return_corridors(reference)
    width = retrace_width(reference)
    unwanted = 0.0
    for a, b in corridors:
        size = distance(a, b)
        permitted = False
        for c, d in intended:
            alignment = abs((b[0] - a[0]) * (d[0] - c[0]) + (b[1] - a[1]) * (d[1] - c[1])) / max(size * distance(c, d), 1e-6)
            if alignment >= .9 and max(segment_distance(p, c, d) for p in (a, b)) <= width:
                permitted = True
                break
        if not permitted:
            unwanted += size
    return unwanted


def crossing_signature(points):
    """Proper geometric crossings only, never invent graph connectivity."""
    def side(a, b, p):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    return {(i, j) for i, (a, b) in enumerate(zip(points, points[1:]))
            for j in range(i + 2, len(points) - 1)
            if side(a, b, points[j]) * side(a, b, points[j + 1]) < -1e-6 and
            side(points[j], points[j + 1], a) * side(points[j], points[j + 1], b) < -1e-6}


def drawn_loop_anchors(reference):
    """Locations where an explicit loop/crossing in the drawing must survive."""
    points = simplify(reference, .5)
    anchors = []
    for i, p in enumerate(points):
        for j in range(i + 2, len(points)):
            if i == 0 and j == len(points) - 1:
                continue  # Whole-route closure is not a local detour.
            if distance(p, points[j]) <= 1:
                anchors.append(p)
    for i, j in crossing_signature(points):
        a, b, c, d = points[i], points[i + 1], points[j], points[j + 1]
        ux, uy, vx, vy = b[0] - a[0], b[1] - a[1], d[0] - c[0], d[1] - c[1]
        t = ((c[0] - a[0]) * vy - (c[1] - a[1]) * vx) / (ux * vy - uy * vx)
        anchors.append((a[0] + t * ux, a[1] + t * uy))
    return anchors


def trim_corner_loops(graph, path, reference):
    """Remove only small graph-connected cycles absent from the drawing.

    No straight-line shortcuts are invented: a splice joins the SAME node.
    Deliberate crossings/retracing and the full route closure are protected.
    A bounded ordered-shape check prevents erasing a significant drawn tip.
    Distance constraints are checked again by the caller after trimming.
    """
    cleaned, removed, count = list(path), 0.0, 0
    span = max(max(p[k] for p in reference) - min(p[k] for p in reference) for k in (0, 1))
    protection = max(20, min(100, span * .025))
    maximum = min(1000, max(150, length(reference) * .08))
    anchors = drawn_loop_anchors(reference)
    corridors, width = retrace_corridors(reference), retrace_width(reference)
    # At most four shape comparisons; long routes must retain their time bound.
    checked = 0
    while checked < 4:
        first, progress, candidates = {}, [0.0], []
        for u, v in zip(cleaned, cleaned[1:]):
            progress.append(progress[-1] + distance(graph.nodes[u], graph.nodes[v]))
        for j, node in enumerate(cleaned):
            i = first.get(node)
            first[node] = j
            if i is None or j - i < 2 or (i == 0 and j == len(cleaned) - 1):
                continue
            meters = progress[j] - progress[i]
            if meters > maximum or any(distance(graph.nodes[node], p) <= protection for p in anchors):
                continue
            edges = [road_key(u, v) for u, v in zip(cleaned[i:j], cleaned[i + 1:j + 1])]
            if edges and all(retrace_edge_allowed(graph, edge, corridors, width) for edge in edges):
                continue
            candidates.append((meters, i, j))
        if not candidates:
            break
        before = shape_metrics(reference, [graph.nodes[n] for n in cleaned])
        changed = False
        for meters, i, j in sorted(candidates):
            if checked >= 4:
                break
            checked += 1
            trial = cleaned[:i + 1] + cleaned[j + 1:]
            if len(trial) < 2:
                continue
            after = shape_metrics(reference, [graph.nodes[n] for n in trial])
            if after[0] <= before[0] + 12 and after[1] <= before[1] + max(20, min(120, span * .03)):
                cleaned, removed, count = trial, removed + meters, count + 1
                changed = True
                break
        if not changed:
            break
    return cleaned, removed, count


def excess_turn_cost(a, b, c, expected=0):
    """Prefer through-roads over sharp extra reversals, not over drawn turns."""
    excess = max(0, abs(turning_angle(a, b, c)) - abs(expected) - math.radians(45))
    return 80 * excess / math.pi


def elastic_references(graph, reference, start, closed, allowed, deadline, limit=2, max_length=math.inf):
    """Joint, bounded road-aware vertex adjustment; keep stroke order/features.

    The beam couples neighboring displacements, edge proportions and signed
    turns. Its straight guide segments are NOT traversable road connections.
    Actual graph paths must subsequently be computed by match_anchor_pose.
    """
    span = max(max(p[k] for p in reference) - min(p[k] for p in reference) for k in (0, 1))
    anchors = simplify(reference, max(.5, span * .003))
    if len(anchors) > 64 or len(anchors) < 2:
        return []
    cap = max(8, min(120, span * .075))
    corridors, width = retrace_corridors(reference), retrace_width(reference)
    states = [(0.0, [graph.nodes[start]], [start], [(0.0, 0.0)], 0.0, frozenset())]
    connection_cache = {}
    for i in range(1, len(anchors)):
        if time.monotonic() >= deadline:
            raise SearchLimit()
        adjacent = [distance(anchors[i - 1], anchors[i])]
        if i < len(anchors) - 1:
            adjacent.append(distance(anchors[i], anchors[i + 1]))
        local_cap = min(cap, min(adjacent) * .3)
        options = [(0, start)] if closed and i == len(anchors) - 1 else graph.nearest(anchors[i], 48, local_cap, allowed)
        # Prefer junctions/bends a little, but a degree-two interior point is
        # valid and straight road vertices must remain available.
        def option_score(option):
            offset, node = option
            neighbors = {e[0] for e in graph.adj[node]}
            junction = len(neighbors) >= 3
            bend = False
            if len(neighbors) == 2:
                a, b = sorted(neighbors)
                bend = abs(turning_angle(graph.nodes[a], graph.nodes[node], graph.nodes[b])) > .35
            return offset / max(local_cap, 1) - (.12 if junction or bend else 0)
        kept = []
        for option in sorted(options, key=option_score):
            # Internal tips of dead ends force an out-and-back. Open endpoints
            # and genuinely retraced strokes can still use them.
            if i < len(anchors) - 1 and len({e[0] for e in graph.adj[option[1]]}) < 2:
                if not any(segment_distance(anchors[i], c, d) <= width for c, d in corridors):
                    continue
            if all(distance(graph.nodes[option[1]], graph.nodes[n]) >= max(8, local_cap * .18) for _, n in kept):
                kept.append(option)
            if len(kept) == 8:
                break
        if not kept:
            return []
        next_states = []
        for score, points, nodes, shifts, traveled, used in states:
            if time.monotonic() >= deadline:
                raise SearchLimit()
            # This stage chooses vertex locations, NOT the connecting roads.
            # Committing to one shortest connector here wrongly blocks later
            # anchors. Actual edge reuse is enforced by the multi-path matcher.
            blocked = frozenset()
            key = (i, nodes[-1])
            if key not in connection_cache:
                # Shortest actual road distances couple anchor choices before
                # the shape-guided final routing. Nearby parallel roads may
                # otherwise require a very long detour or be one-way blocked.
                budget = distance(anchors[i - 1], anchors[i]) * 2 + cap * 2
                connection_cache[key] = reachable(graph, nodes[-1], max(0, budget), blocked, deadline)
            connections, _ = connection_cache[key]
            for offset, node in kept:
                if node not in connections:
                    continue
                total = traveled + connections[node]
                floor = distance(graph.nodes[node], graph.nodes[start]) if closed else 0
                if total + floor > max_length:
                    continue
                point = graph.nodes[node]
                # Identical anchor visits (including retracing) must stay
                # identical. Nonadjacent visits may still share graph nodes.
                repeated = next((j for j in range(i) if distance(anchors[j], anchors[i]) < .001), None)
                if repeated is not None and node != nodes[repeated]:
                    continue
                old_edge = distance(anchors[i - 1], anchors[i])
                new_edge = distance(points[-1], point)
                ratio = new_edge / max(old_edge, 1)
                if not .55 <= ratio <= 1.5:
                    continue
                shift = (point[0] - anchors[i][0], point[1] - anchors[i][1])
                ux, uy = anchors[i][0] - anchors[i - 1][0], anchors[i][1] - anchors[i - 1][1]
                vx, vy = point[0] - points[-1][0], point[1] - points[-1][1]
                alignment = (ux * vx + uy * vy) / max(old_edge * new_edge, 1)
                cost = score + .65 * offset / max(local_cap, 1) + abs(ratio - 1)
                cost += 3 * (1 - max(-1, min(1, alignment))) + .35 * distance(shift, shifts[-1]) / cap
                cost += max(0, connections[node] / max(new_edge, 1) - 1) * 1.5
                if i >= 2:
                    expected = turning_angle(*anchors[i - 2:i + 1])
                    actual = turning_angle(points[-2], points[-1], point)
                    change = abs(math.atan2(math.sin(actual - expected), math.cos(actual - expected)))
                    if abs(expected) > .6 and (expected * actual <= 0 or abs(actual) < abs(expected) * .45):
                        continue
                    if change > math.radians(65):
                        continue
                    cost += change * 1.5
                support = []
                for t in (.25, .5, .75):
                    p = (points[-1][0] + t * vx, points[-1][1] + t * vy)
                    near = graph.nearest(p, 1, 160, allowed)
                    support.append(near[0][0] if near else 160)
                cost += sum(support) / (3 * cap) * 2
                next_states.append((cost, points + [point], nodes + [node], shifts + [shift], total, used))
        states, keys = [], set()
        for state in sorted(next_states, key=lambda s: s[0]):
            key = tuple(state[2])
            if key not in keys:
                states.append(state)
                keys.add(key)
            if len(states) == 10:
                break
        if not states:
            return []
    result = []
    original_crossings = crossing_signature(anchors)
    for _, points, nodes, _, _, _ in states:
        if crossing_signature(points) != original_crossings:
            continue
        if closed:
            expected = turning_angle(anchors[-2], anchors[0], anchors[1])
            actual = turning_angle(points[-2], points[0], points[1])
            if abs(expected) > .6 and (expected * actual <= 0 or abs(actual) < abs(expected) * .45):
                continue
        shifts = [distance(a, b) for a, b in zip(anchors, points)]
        if max(shifts) < .5:
            continue
        if any(sum(distance(a, b) for a, b in zip(points, old[0])) / len(points) < 8 for old in result):
            continue
        result.append((points, nodes, {"vertex_limit_m": cap, "vertex_max_shift_m": max(shifts),
                       "vertex_mean_shift_m": sum(shifts) / len(shifts),
                       "vertices_moved": sum(d > .5 for d in shifts), "vertex_count": len(anchors),
                       "original_anchors": anchors}))
        if len(result) == limit:
            break
    return result


def match_anchor_pose(graph, anchors, nodes, closed, target, deadline, tolerance=.2, reference=None):
    states = [(0.0, 0.0, [nodes[0]])]
    original = reference if reference is not None else anchors
    corridors, width = retrace_corridors(original), retrace_width(original)
    upper = target * (1 + tolerance)
    for i, (a, b) in enumerate(zip(anchors, anchors[1:]), 1):
        if time.monotonic() >= deadline:
            raise SearchLimit()
        floor = distance(graph.nodes[nodes[i]], graph.nodes[nodes[0]]) if closed else 0
        next_states = []
        for cost, total, path in states:
            used = {road_key(u, v) for u, v in zip(path, path[1:])}
            blocked = forbidden_retraces(graph, used, corridors, width, a, b)
            options = guided_choices(graph, path[-1], [(distance(graph.nodes[nodes[i]], b), nodes[i])],
                                     a, b, deadline, upper - total - floor, count=4, forbidden_edges=blocked)
            for part, meters, score, _ in options:
                expected = turning_angle(anchors[i - 2], a, b) if i > 1 else 0
                bend = excess_turn_cost(graph.nodes[path[-2]], graph.nodes[path[-1]], graph.nodes[part[1]], expected) if len(path) > 1 and len(part) > 1 else 0
                next_states.append((cost + score + bend, total + meters, path + part[1:]))
        states = path_beam(next_states, 8)
        if not states:
            return []
    return states


def _guided_choices(graph, start, choices, a, b, deadline, max_length, count=4, forbidden_edges=frozenset()):
    """Choose road snaps jointly with their actual connecting paths.

    A nearer parallel road may require a huge detour; its geometric offset
    alone must not decide the next checkpoint. Virtual goal costs combine
    road travel, deviation and endpoint offset. Cost/length Pareto labels
    retain a shorter alternative when a cheaper shape-following path is long.
    """
    endpoints = {node: offset for offset, node in choices}
    if not endpoints:
        return []
    width = max(35, min(85, distance(a, b) * .3))
    vx, vy = b[0] - a[0], b[1] - a[1]
    reference_length = max(distance(a, b), 1)
    radius = max(endpoints.values())
    labels = [(start, 0.0, 0.0, None)]  # node, cost, meters, parent label
    active = {start: [0]}
    queue = [(distance(graph.nodes[start], b), 0, False)]
    found, visits = [], 0
    while queue:
        _, label, terminal = heapq.heappop(queue)
        u, cost, meters, parent = labels[label]
        if label not in active.get(u, ()):
            continue
        if terminal:
            if any(distance(graph.nodes[u], graph.nodes[path[-1]]) < 45 for path, _, _, _ in found):
                continue
            path, cursor = [], label
            while cursor is not None:
                path.append(labels[cursor][0])
                cursor = labels[cursor][3]
            found.append((path[::-1], meters, cost, endpoints[u]))
            if len(found) >= count:
                break
            continue
        visits += 1
        if visits % 100 == 0 and time.monotonic() > deadline:
            raise SearchLimit()
        if visits > 8000:
            break
        if u in endpoints:
            heapq.heappush(queue, (cost + CHECKPOINT_OFFSET_WEIGHT * endpoints[u], label, True))
        for v, step, _, penalty in graph.adj[u]:
            if road_key(u, v) in forbidden_edges:
                continue
            # Positive-cost cycles never help a single guide leg. In
            # particular, do not reverse to the previous node to hit a tip.
            if parent is not None and v == labels[parent][0]:
                continue
            total = meters + step
            if total + max(0, distance(graph.nodes[v], b) - radius) > max_length:
                continue
            mid = tuple((graph.nodes[u][i] + graph.nodes[v][i]) / 2 for i in (0, 1))
            deviation = segment_distance(mid, a, b) / width
            dx, dy = graph.nodes[v][0] - graph.nodes[u][0], graph.nodes[v][1] - graph.nodes[u][1]
            alignment = (dx * vx + dy * vy) / max(step * reference_length, 1)
            new_cost = cost + step * (penalty + 1.5 * deviation + .5 * (1 - max(-1, min(1, alignment))))
            if parent is not None:
                new_cost += excess_turn_cost(graph.nodes[labels[parent][0]], graph.nodes[u], graph.nodes[v])
            existing = active.get(v, [])
            if any(labels[k][1] <= new_cost and labels[k][2] <= total for k in existing):
                continue
            keep = [k for k in existing if not (new_cost <= labels[k][1] and total <= labels[k][2])]
            new_label = len(labels)
            labels.append((v, new_cost, total, label))
            keep.append(new_label)
            # Bounded heuristic: retain cheapest, shortest and one compromise.
            if len(keep) > 3:
                cheapest = min(keep, key=lambda k: labels[k][1])
                shortest = min(keep, key=lambda k: labels[k][2])
                rest = [k for k in keep if k not in {cheapest, shortest}]
                keep = list(dict.fromkeys([cheapest, shortest]))
                keep.extend(sorted(rest, key=lambda k: labels[k][1])[:3 - len(keep)])
            active[v] = keep
            if new_label in keep:
                heapq.heappush(queue, (new_cost + distance(graph.nodes[v], b), new_label, False))
    return found


def guided_choices(graph, start, choices, a, b, deadline, max_length, count=4, forbidden_edges=frozenset(), alternatives=True):
    """Spatially diverse snaps PLUS bounded different-road alternatives.

    Deviation searches block a branch edge of a previous path. Pareto pruning
    inside each A* stays efficient without discarding every costlier route to
    the same endpoint. This is bounded replacement-path search, not exact Yen.
    """
    found = _guided_choices(graph, start, choices, a, b, deadline, max_length, count, forbidden_edges)
    if not found or count <= 1 or not alternatives:
        return found
    primary = list(found)
    signatures = {tuple(p[0]) for p in found}
    # Reserve most of the remaining budget for subsequent checkpoints.
    local_deadline = min(deadline, time.monotonic() + min(.08, max(0, deadline - time.monotonic()) * .12))
    attempts = 0
    for path, _, _, offset in primary[:2]:
        branches = [road_key(u, v) for u, v in zip(path, path[1:])
                    if len({e[0] for e in graph.adj[u]}) >= 3 or u == start]
        # Sample throughout a leg, not all 35m subdivision points.
        if len(branches) > 4:
            branches = [branches[round(i * (len(branches) - 1) / 3)] for i in range(4)]
        for edge in branches:
            if attempts >= 6 or time.monotonic() >= local_deadline:
                return found
            attempts += 1
            try:
                alternatives = _guided_choices(graph, start, [(offset, path[-1])], a, b,
                                                local_deadline, max_length, 1, forbidden_edges | {edge})
            except SearchLimit:
                return found
            for option in alternatives:
                # No cycles inside one leg, and no duplicates. Intersections
                # may still be revisited across ordered legs of the drawing.
                signature = tuple(option[0])
                if signature not in signatures and len(set(signature)) == len(signature):
                    signatures.add(signature)
                    found.append(option)
                    if len(found) >= len(primary) + min(2, count - 1):
                        return found
    return found


def path_beam(states, limit):
    """Keep distinct road histories, including paths to identical endpoints."""
    result, keys = [], set()
    for state in sorted(states, key=lambda s: s[0]):
        key = (state[2][-1], frozenset(road_key(u, v) for u, v in zip(state[2], state[2][1:])))
        if key not in keys:
            result.append(state)
            keys.add(key)
        if len(result) == limit:
            break
    return result


def match_pose(graph, reference, start, closed, target, deadline, tolerance=.2, allowed=None, alternatives=True, relaxed=False):
    # Keep alternative accumulated lengths at each reference progress layer.
    # Vertices may be revisited: crossings and retracing are valid stroke input.
    states = [(0.0, 0.0, [start])]
    cache = {}
    corridors, width = retrace_corridors(reference), retrace_width(reference)
    progress = 0
    reference = search_checkpoints(reference, target, relaxed=relaxed)
    max_total = target * (1 + tolerance)
    span = max(max(p[k] for p in reference) - min(p[k] for p in reference) for k in (0, 1))
    snap_limit = max(75, min(300, span * .15)) if relaxed else max(45, min(150, span * .1))
    for i, (a, b) in enumerate(zip(reference, reference[1:]), 1):
        if time.monotonic() > deadline:
            raise SearchLimit()
        progress += distance(a, b)
        choices = [(0, start)] if closed and i == len(reference) - 1 else graph.nearest(b, 128, snap_limit, allowed)
        if i < len(reference) - 1:
            # A dead-end snap can crowd out a nearby through-junction in the
            # spatially diverse endpoint beam. Remove it before searching,
            # unless this point belongs to a deliberately retraced stroke.
            retraced_tip = any(segment_distance(b, c, d) <= width for c, d in corridors)
            if not retraced_tip:
                choices = [(offset, node) for offset, node in choices
                           if len({e[0] for e in graph.adj[node]}) >= 2]
        if not choices:
            return []
        next_states = []
        for score, traveled, path in states:
            remaining = max_total - traveled
            used = {road_key(u, v) for u, v in zip(path, path[1:])}
            blocked = forbidden_retraces(graph, used, corridors, width, a, b)
            key = (i, path[-1], round(remaining, 6), blocked)
            if key not in cache:
                cache[key] = guided_choices(graph, path[-1], choices, a, b, deadline, remaining,
                                            forbidden_edges=blocked, alternatives=alternatives)
            for segment, meters, cost, offset in cache[key]:
                end = segment[-1]
                total = traveled + meters
                # Only a whole-route budget constrains detours. For a loop the
                # straight-line return distance is an admissible lower bound.
                return_floor = distance(graph.nodes[end], graph.nodes[start]) if closed else 0
                if total + return_floor > max_total:
                    continue
                # Reference progress is ordered; no nearest-branch jumps at crossings.
                expected = turning_angle(reference[i - 2], a, b) if i > 1 else 0
                bend = excess_turn_cost(graph.nodes[path[-2]], graph.nodes[path[-1]], graph.nodes[segment[1]], expected) if len(path) > 1 and len(segment) > 1 else 0
                rank = score + cost + offset * CHECKPOINT_OFFSET_WEIGHT + bend + abs(total - progress) * .15
                next_states.append((rank, total, path + segment[1:]))
        if not next_states:
            return []
        states = path_beam(next_states, 8 if alternatives else 4)
    return states


def extra_road_bends(reference, route):
    """Count surplus road turns without hiding block-sized jogs on long runs."""
    def bends(points):
        return sum(abs(turning_angle(a, b, c)) > math.radians(65)
                   for a, b, c in zip(points, points[1:], points[2:]))
    span = max(max(p[k] for p in reference) - min(p[k] for p in reference) for k in (0, 1))
    return max(0, bends(simplify(route, max(8, min(20, span * .006)))) -
               bends(simplify(reference, max(1, span * .003))))


def road_connector(graph, start, end, limit, blocked_nodes, blocked_edges, deadline):
    """Bounded directed A*: only existing edges, no geometric intersection joins."""
    queue = [(distance(graph.nodes[start], graph.nodes[end]), 0.0, start)]
    costs, parents = {start: 0.0}, {}
    visits = 0
    while queue and visits < 8000:
        if time.monotonic() >= deadline:
            break
        _, meters, node = heapq.heappop(queue)
        if meters != costs.get(node):
            continue
        if node == end:
            return reconstruct(parents, start, end)
        visits += 1
        for nxt, step, _, _penalty in graph.adj[node]:
            if nxt in blocked_nodes or road_key(node, nxt) in blocked_edges:
                continue
            total = meters + step
            lower = total + distance(graph.nodes[nxt], graph.nodes[end])
            if lower > limit or total >= costs.get(nxt, math.inf):
                continue
            costs[nxt], parents[nxt] = total, node
            heapq.heappush(queue, (lower, total, nxt))
    return None


def refine_road_detours(graph, path, reference, target, tolerance, deadline):
    """Replace different-junction detours, then validate the entire original stroke.

    A bounded local improvement, not a global shortest-path/shape guarantee.
    Keep endpoints, intended crossings/retracing and target distance constraints.
    """
    path = list(path)
    stats = {"attempts": 0, "replacements": 0, "removed_m": 0.0,
             "distance_rejected": 0, "shape_rejected": 0, "no_connector": 0}
    if len(path) < 4 or time.monotonic() >= deadline:
        return path, stats
    # A deliberate out-and-back must not be optimized away as a road detour.
    if retrace_corridors(reference):
        return path, stats
    original = [graph.nodes[n] for n in path]
    original_meters = length(original)
    unit = max(25, max(max(p[k] for p in reference) - min(p[k] for p in reference) for k in (0, 1)) * .07)

    def assess(points):
        mean, ordered = shape_metrics(reference, points)
        details = shape_details(reference, points)
        acceptable, score = shape_policy(mean, ordered, details, reference)
        score += .5 * abs(length(points) - target) / target + .12 * extra_road_bends(reference, points)
        return mean, ordered, details, acceptable, score

    baseline = assess(original)
    current = baseline
    tried = set()
    anchors = drawn_loop_anchors(reference)
    outline = simplify(reference, .5)
    while stats["attempts"] < 24 and stats["replacements"] < 4 and time.monotonic() < deadline:
        route = [graph.nodes[n] for n in path]
        cumulative = [0.0]
        for a, b in zip(route, route[1:]):
            cumulative.append(cumulative[-1] + distance(a, b))
        # At most ~100 endpoints: evenly spaced nodes plus distinct road bends.
        stride = max(40, cumulative[-1] / 80)
        indices, last = [0], 0
        for i in range(1, len(path) - 1):
            if cumulative[i] - cumulative[last] >= stride:
                indices.append(i)
                last = i
        indices.append(len(path) - 1)
        # Small graphs should expose every junction for deterministic tests.
        if len(path) <= 100:
            indices = list(range(len(path)))
        pairs = []
        for pos, i in enumerate(indices):
            for j in indices[pos + 1:]:
                arc = cumulative[j] - cumulative[i]
                chord = distance(route[i], route[j])
                if j <= i + 1 or arc > max(500, min(2200, target * .15)) or chord < 20:
                    continue
                if arc - chord < max(20, arc * .12) or (path[i], path[j]) in tried:
                    continue
                # Screen out chords cutting across the body of the drawing.
                # Otherwise the attempt budget is spent on many overlapping
                # shortcuts that erase the same intended star tip/corner.
                probes = [(route[i][0] + (route[j][0] - route[i][0]) * t,
                           route[i][1] + (route[j][1] - route[i][1]) * t) for t in (.25, .5, .75)]
                if any(min(segment_distance(p, a, b) for a, b in zip(outline, outline[1:])) >
                       max(unit, baseline[0] + 20) for p in probes):
                    continue
                # Do not cut through a deliberately drawn crossing/loop anchor.
                if any(min(distance(p, q) for p in route[i + 1:j]) < max(15, unit * .3) for q in anchors):
                    continue
                pairs.append((arc - chord, i, j, arc))
        improved = False
        attempted_ranges = []
        for _, i, j, arc in sorted(pairs, reverse=True):
            if stats["attempts"] >= 24 or time.monotonic() >= deadline:
                break
            # Try distinct subpaths before near-duplicates of a failed splice.
            if any(abs(cumulative[i] - a) + abs(cumulative[j] - b) < stride * 2
                   for a, b in attempted_ranges):
                continue
            attempted_ranges.append((cumulative[i], cumulative[j]))
            tried.add((path[i], path[j]))
            stats["attempts"] += 1
            outside = path[:i + 1] + path[j:]
            blocked_nodes = set(outside) - {path[i], path[j]}
            blocked_edges = {road_key(a, b) for part in (path[:i + 1], path[j:])
                             for a, b in zip(part, part[1:])}
            connector = road_connector(graph, path[i], path[j], arc - 10,
                                       blocked_nodes, blocked_edges, min(deadline, time.monotonic() + .2))
            if not connector:
                stats["no_connector"] += 1
                continue
            trial = path[:i] + connector + path[j + 1:]
            points = [graph.nodes[n] for n in trial]
            meters = length(points)
            if abs(meters - target) > target * tolerance:
                stats["distance_rejected"] += 1
                continue
            if time.monotonic() >= deadline:
                break
            after = assess(points)
            # Limits are relative to the original route, not accumulated drift.
            valid = (not current[3] or after[3]) and after[4] < current[4] - .01
            valid = valid and after[0] <= baseline[0] + unit * .1 and after[1] <= baseline[1] + unit * .25
            valid = valid and after[2]["missing_corners"] <= baseline[2]["missing_corners"]
            valid = valid and after[2]["p95_error_m"] <= baseline[2]["p95_error_m"] + unit * .25
            valid = valid and after[2]["corner_error_deg"] <= baseline[2]["corner_error_deg"] + 5
            if not valid or road_retrace_stats(graph, trial, reference)[1] > .001 or near_retrace_stats(points, reference) > .001:
                stats["shape_rejected"] += 1
                continue
            path, current = trial, after
            stats["replacements"] += 1
            improved = True
            break
        if not improved:
            break
    stats["removed_m"] = round(original_meters - length([graph.nodes[n] for n in path]), 1)
    return path, stats


def generate(graph, payload, seconds=45, *, max_distance_m=8000):
    started = time.monotonic()
    # The caller sets its own limit; request JSON cannot raise the limit.
    target = finite_number(payload.get("distance_m", 3000), 300, max_distance_m, "러닝 거리")
    approach_limit = finite_number(payload.get("approach_m", 1000), 0, 1500, "출발점 이동 거리")
    tolerance = finite_number(payload.get("tolerance", .2), .05, .5, "거리 허용 오차")
    candidate_limit = finite_number(payload.get("candidate_limit", 20), 1, 30, "후보 개수")
    if candidate_limit != int(candidate_limit):
        raise InputError("후보 개수는 정수여야 합니다.")
    candidate_limit = int(candidate_limit)
    search_mode = payload.get("search_mode", "all")
    if search_mode not in {"all", "overview", "local"}:
        raise InputError("탐색 방식이 올바르지 않습니다.")
    focus = valid_center(payload.get("focus_location")) if search_mode == "local" else None
    elastic_enabled = payload.get("allow_vertex_adjustment", True)
    if not isinstance(elastic_enabled, bool):
        raise InputError("꼭짓점 조정 여부는 true 또는 false여야 합니다.")
    drawing, closed = prepare_drawing(payload.get("drawing"), target, payload.get("route_type", "auto"))
    location = valid_center(payload.get("location", graph.center))
    user = graph.project(*location)
    near = graph.nearest(user, 1, 100)
    if not near:
        raise InputError("기준 위치 100m 이내에 연결 가능한 보행 도로가 없습니다. 지도에서 위치를 바꿔주세요.")
    snap_gap, user_node = near[0]
    if snap_gap > approach_limit + .001:
        raise InputError("첫 보행 도로까지의 연결 추정 거리가 최대 이동 거리를 넘습니다. 기준 위치나 이동 거리를 바꿔주세요.")
    reach, parents = reachable(graph, user_node, max(0, approach_limit - snap_gap))
    # Exclude disconnected roads from snapping, without inventing connections.
    allowed, _ = reachable(graph, user_node, target * (1 + tolerance) + approach_limit)
    starts = distributed_starts(graph, reach, user, approach_limit)
    if focus is not None:
        focus_point = graph.project(*focus)
        local_reach = {n: meters for n, meters in reach.items() if distance(graph.nodes[n], focus_point) <= 250}
        if not local_reach:
            raise InputError("선택한 위치 주변에 이동 거리 조건을 만족하는 출발점이 없습니다.")
        starts = distributed_starts(graph, local_reach, focus_point, approach_limit, limit=100)
        closest = min(local_reach, key=lambda n: distance(graph.nodes[n], focus_point))
        starts = [closest] + [n for n in starts if n != closest]
    angles = range(0, 360, 30) if payload.get("allow_rotation", True) else (0,)
    poses = []
    samples = resample(drawing, 20)
    screening_deadline = started + min(8, seconds * .3)
    screened_starts = 0
    for start in starts:
        if time.monotonic() > screening_deadline:
            break
        screened_starts += 1
        origin = graph.nodes[start]
        for degrees in angles:
            rad = math.radians(degrees)
            co, si = math.cos(rad), math.sin(rad)
            for scale in PLACEMENT_SCALES:
                def transform(p):
                    return (origin[0] + scale * (p[0] * co - p[1] * si),
                            origin[1] + scale * (p[0] * si + p[1] * co))
                transformed = [transform(p) for p in samples]
                offsets = []
                for p in transformed:
                    nearest = graph.nearest(p, 1, 150, allowed)
                    if not nearest:
                        break
                    offsets.append(nearest[0][0])
                if len(offsets) == len(transformed):
                    cost = sum(offsets) / len(offsets) + max(offsets) * .15
                    cost += reach[start] * .002
                    reference = [transform(p) for p in drawing]
                    poses.append((cost, start, degrees, scale, reference))
    selected = select_poses(poses, graph, reach, user, approach_limit)
    screening_seconds = time.monotonic() - started
    deadline = started + seconds
    search_deadline = deadline - min(8, seconds * .2)
    rigid_deadline = search_deadline - min(12, seconds * .27) if elastic_enabled else search_deadline
    candidates, seen = [], set()
    repair_seeds = []
    diagnostics = {"completed_routes": 0, "length_rejected": 0, "shape_rejected": 0,
                   "retrace_rejected": 0, "near_retrace_rejected": 0,
                   "corner_loops_trimmed": 0, "corner_loop_m_removed": 0.0,
                   "detour_attempts": 0, "detour_replacements": 0, "detour_removed_m": 0.0,
                   "detour_distance_rejected": 0, "detour_shape_rejected": 0, "detour_no_connector": 0}
    limited, tested, refined = False, 0, False
    refined_count = 0
    tested_starts, tested_bands = set(), set()

    def add_candidate(reference, start, rotation, scale, meters, path, adjusted=None, adjustment=None,
                      refinement=None, relaxed=False):
        diagnostics["completed_routes"] += 1
        path, removed_loop_m, trimmed_loops = trim_corner_loops(graph, path, reference)
        diagnostics["corner_loops_trimmed"] += trimmed_loops
        diagnostics["corner_loop_m_removed"] += round(removed_loop_m, 1)
        # Never pad a cleaned route back to the requested length with a loop.
        meters = length([graph.nodes[n] for n in path])
        repeated_meters, unwanted_retrace = road_retrace_stats(graph, path, reference)
        route = [graph.nodes[n] for n in path]
        near_retrace = near_retrace_stats(route, reference)
        # Keep a bounded repair pool even for routes rejected by the final
        # retracing gate. Repair never bypasses that gate on its second pass.
        if refinement is None and len(repair_seeds) < 32 and abs(meters - target) <= target * tolerance:
            repair_seeds.append((reference, start, rotation, scale, meters, path, adjusted, adjustment, relaxed))
        # Defense in depth: never display a prohibited retracing candidate,
        # even as a comparison-only route or if future search code regresses.
        if unwanted_retrace > .001:
            diagnostics["retrace_rejected"] += 1
            return
        if meters < 100 or abs(meters - target) > target * tolerance:
            diagnostics["length_rejected"] += 1
            return
        signature = (tuple(path), rotation, scale, adjusted is not None)
        if signature in seen:
            return
        seen.add(signature)
        # Recommendation criteria ALWAYS measure against the original drawing.
        mean, ordered = shape_metrics(reference, route)
        details = shape_details(reference, route)
        acceptable, shape_score = shape_policy(mean, ordered, details, reference)
        # Local unwanted returns must not disappear inside a long route's
        # good average score. Returns explicitly present in the original
        # drawing were already excluded by near_retrace_stats.
        if near_retrace > .001:
            diagnostics["retrace_rejected"] += 1
            diagnostics["near_retrace_rejected"] += 1
            return
        if not acceptable:
            diagnostics["shape_rejected"] += 1
        score = shape_score + .5 * abs(meters - target) / target + .02 * reach[start] / max(approach_limit, 1)
        score += .5 * near_retrace / max(meters, 1)
        extra_bends = extra_road_bends(reference, route)
        score += extra_bends * .12
        guide = adjusted if adjusted is not None else reference
        if adjustment:
            score += .15 * adjustment["vertex_mean_shift_m"] / max(adjustment["vertex_limit_m"], 1)
        approach_path = reconstruct(parents, user_node, start)
        candidates.append({"score": round(score, 4), "acceptable": acceptable,
            "distance_m": round(meters, 1), "approach_m": round(reach[start] + snap_gap, 1),
            "approach_road_m": round(reach[start], 1), "snap_gap_m": round(snap_gap, 1),
            "mean_error_m": round(mean, 1), "frechet_m": round(ordered, 1),
            **{k: round(v, 3) for k, v in details.items()},
            "rotation": rotation, "scale": scale, "closed": closed,
            "geometry": {"type": "LineString", "coordinates": [graph.unproject(p) for p in route]},
            "reference": [graph.unproject(p) for p in reference],
            "adjusted_reference": [graph.unproject(p) for p in guide],
            "vertex_adjusted": adjusted is not None,
            "extra_road_bends": extra_bends, "road_retrace_m": round(repeated_meters, 1),
            "trimmed_corner_loops": trimmed_loops, "trimmed_corner_loop_m": round(removed_loop_m, 1),
            "detour_refinement": refinement or {}, "relaxed_checkpoints": relaxed,
            "unwanted_retrace_m": round(unwanted_retrace, 3),
            "unwanted_near_retrace_m": round(near_retrace, 3),
            "narrow_return_warning": near_retrace > .001,
            "vertex_adjustment": {k: round(v, 3) for k, v in (adjustment or {}).items() if k != "original_anchors"},
            "vertex_moves": [{"from": graph.unproject(a), "to": graph.unproject(b), "distance_m": round(distance(a, b), 3)}
                             for a, b in zip(adjustment["original_anchors"], guide)] if adjustment else [],
            "adjusted_mean_error_m": round(shape_metrics(guide, route)[0], 1) if adjusted is not None else round(mean, 1),
            "approach": [graph.unproject(graph.nodes[n]) for n in approach_path], "node_ids": path})
    try:
        for _, start, rotation, scale, reference in selected:
            tested += 1
            tested_starts.add(start)
            tested_bands.add(min(3, int(reach[start] / max(approach_limit, 1) * 4)))
            # One expensive placement must not consume the whole area search.
            pose_deadline = min(rigid_deadline, time.monotonic() + max(.5, seconds / max(len(selected), 1) * 2))
            try:
                paths = match_pose(graph, reference, start, closed, target, pose_deadline, tolerance, allowed,
                                   alternatives=False)
            except SearchLimit:
                limited = True
                if time.monotonic() >= rigid_deadline:
                    break
                continue
            for _, meters, path in paths:
                add_candidate(reference, start, rotation, scale, meters, path)
            if search_mode != "overview" and tested == len(selected) and not refined and candidates and time.monotonic() < rigid_deadline - .5:
                refined = True
                # Fine rotation/scale refinement around the best discovered
                # locations. Never distort axes independently or replace input.
                seeds = diverse_candidates(candidates, graph)
                for seed in seeds:
                    start_node = seed["node_ids"][0]
                    origin = graph.nodes[start_node]
                    variants = [(seed["rotation"] + delta, seed["scale"]) for delta in (-10, 10)] if payload.get("allow_rotation", True) else []
                    variants += [(seed["rotation"], seed["scale"] + delta) for delta in (-.05, .05)]
                    for angle, factor in variants:
                        if not .55 <= factor <= 1.1:
                            continue
                        rad = math.radians(angle)
                        co, si = math.cos(rad), math.sin(rad)
                        curve = [(origin[0] + factor * (p[0] * co - p[1] * si),
                                  origin[1] + factor * (p[0] * si + p[1] * co)) for p in drawing]
                        selected.append((0, start_node, angle % 360, factor, curve))
                        refined_count += 1
    except SearchLimit:
        limited = True
    # First cover many locations cheaply. Only then spend extra time retaining
    # different road histories around promising, spatially diverse placements.
    # Expanding every coarse pose starves distant starts and vertex adjustment.
    alternative_tested, alternative_completed = 0, 0
    alternative_seeds = [] if search_mode == "overview" else diverse_candidates(candidates, graph, 8, include_comparison=True)
    alternative_deadline = min(rigid_deadline, time.monotonic() + min(10, seconds * .25))
    alternative_keys = set()
    for seed in alternative_seeds:
        if time.monotonic() >= alternative_deadline:
            break
        key = (seed["node_ids"][0], seed["rotation"], seed["scale"])
        if key in alternative_keys:
            continue
        alternative_keys.add(key)
        alternative_tested += 1
        start, rotation, scale = key
        reference = [graph.project(*p) for p in seed["reference"]]
        local_deadline = min(alternative_deadline, time.monotonic() + max(.5, seconds * .06))
        try:
            paths = match_pose(graph, reference, start, closed, target, local_deadline, tolerance, allowed)
            for _, meters, path in paths:
                alternative_completed += 1
                add_candidate(reference, start, rotation, scale, meters, path)
        except SearchLimit:
            limited = True
    elastic_tested, elastic_completed = 0, 0
    if elastic_enabled and time.monotonic() < search_deadline:
        seeds = location_representatives(candidates, graph, 6) if search_mode == "overview" else diverse_candidates(candidates, graph)
        seed_poses = [(0, c["node_ids"][0], c["rotation"], c["scale"],
                       [graph.project(*p) for p in c["reference"]]) for c in seeds]
        # Also try a few promising placements without a completed rigid route.
        keys = {p[1:4] for p in seed_poses}
        for pose in sorted(selected, key=lambda p: p[0]):
            if pose[1:4] not in keys:
                seed_poses.append(pose)
                keys.add(pose[1:4])
            if len(seed_poses) >= 6:
                break
        for _, start, rotation, scale, reference in seed_poses:
            if time.monotonic() >= search_deadline:
                limited = True
                break
            elastic_tested += 1
            local_deadline = min(search_deadline, time.monotonic() + max(.5, (search_deadline - time.monotonic()) / max(1, len(seed_poses) - elastic_tested + 1)))
            try:
                variants = elastic_references(graph, reference, start, closed, allowed, local_deadline,
                                               max_length=target * (1 + tolerance))
                for guide, nodes, adjustment in variants:
                    for _, meters, path in match_anchor_pose(graph, guide, nodes, closed, target, local_deadline, tolerance,
                                                             reference=reference):
                        elastic_completed += 1
                        add_candidate(reference, start, rotation, scale, meters, path, guide, adjustment)
            except SearchLimit:
                limited = True
    # Coarser straight-section checkpoints and wider snapping are an additional
    # proposal, never a replacement for the unmodified drawing's final score.
    relaxed_tested, relaxed_completed = 0, 0
    relaxed_seeds = [(0, c["node_ids"][0], c["rotation"], c["scale"],
                      [graph.project(*p) for p in c["reference"]])
                     for c in diverse_candidates(candidates, graph, 2, include_comparison=True)]
    relaxed_keys = {p[1:4] for p in relaxed_seeds}
    for pose in selected:
        if len(relaxed_seeds) >= 2:
            break
        if pose[1:4] not in relaxed_keys:
            relaxed_seeds.append(pose)
            relaxed_keys.add(pose[1:4])
    for _, start, rotation, scale, reference in relaxed_seeds:
        if time.monotonic() >= deadline - min(4, seconds * .1):
            break
        relaxed_tested += 1
        try:
            paths = match_pose(graph, reference, start, closed, target,
                               min(deadline, time.monotonic() + 1), tolerance, allowed,
                               alternatives=False, relaxed=True)
            for _, meters, path in paths:
                relaxed_completed += 1
                add_candidate(reference, start, rotation, scale, meters, path, relaxed=True)
        except SearchLimit:
            limited = True
    # Optimize good, spatially diverse routes first; then try rejected routes.
    preferred = diverse_candidates(candidates, graph, 4, include_comparison=True)
    pool = [([graph.project(*p) for p in c["reference"]], c["node_ids"][0], c["rotation"],
             c["scale"], c["distance_m"], c["node_ids"], None, None, c["relaxed_checkpoints"])
            for c in preferred if not c["vertex_adjusted"]]
    pool.extend(repair_seeds)
    repaired_keys, repair_tested = set(), 0
    for reference, start, rotation, scale, meters, path, adjusted, adjustment, relaxed in pool:
        if repair_tested >= 6 or time.monotonic() >= deadline:
            break
        key = (tuple(path), rotation, scale)
        if key in repaired_keys:
            continue
        repaired_keys.add(key)
        repair_tested += 1
        refined_path, stats = refine_road_detours(graph, path, reference, target, tolerance,
                                                min(deadline, time.monotonic() + 1.5))
        for name in ("attempts", "replacements", "removed_m", "distance_rejected", "shape_rejected", "no_connector"):
            diagnostics["detour_" + name] += stats[name]
        if stats["replacements"]:
            add_candidate(reference, start, rotation, scale, meters, refined_path, adjusted, adjustment,
                          refinement=stats, relaxed=relaxed)
    diverse = (location_representatives(candidates, graph, candidate_limit) if search_mode == "overview"
               else diverse_candidates(candidates, graph, candidate_limit, include_comparison=True))
    found = any(c["acceptable"] for c in diverse)
    message = "조건에 맞는 후보를 찾았습니다. 실제 통행 가능 여부를 확인해주세요." if found else "이번 탐색에서 충분히 비슷한 경로를 찾지 못했습니다. 아래 후보는 비교용이며 추천 경로가 아닙니다."
    if not diverse:
        message = "불필요한 도로 왕복 없이 거리·연결 조건을 만족하는 후보를 찾지 못했습니다. 위치, 거리 또는 허용 오차를 바꿔주세요."
    if graph.source == "synthetic":
        message = "가상 도로 실험 결과입니다. 실제로 달릴 수 있는 경로가 아닙니다. " + message
    return {"candidates": diverse, "found": found, "message": message,
            "source": graph.source, "elapsed_s": round(time.monotonic() - started, 2),
            "poses_screened": len(poses), "poses_tested": tested, "search_limited": limited,
            "starts_available": len(starts), "starts_screened": screened_starts, "starts_tested": len(tested_starts),
            "poses_refined": refined_count,
            "vertex_adjustment_enabled": elastic_enabled,
            "elastic_poses_tested": elastic_tested, "elastic_routes_completed": elastic_completed,
            "alternative_poses_tested": alternative_tested, "alternative_routes_completed": alternative_completed,
            "relaxed_poses_tested": relaxed_tested, "relaxed_routes_completed": relaxed_completed,
            "detour_routes_tested": repair_tested,
            "screening_s": round(screening_seconds, 2),
            "approach_bands_tested": sorted(tested_bands), "approach_limit_m": approach_limit,
            "diagnostics": diagnostics,
            "candidate_limit": candidate_limit, "candidates_available": len(candidates),
            "search_mode": search_mode, "focus_location": focus, "location_radius_m": 250,
            "recommended_count": sum(c["acceptable"] for c in diverse),
            "warnings": ["경로 생성은 제한된 후보를 탐색하는 실험용 근사 알고리즘입니다.",
                         "OSM의 누락·접근 제한·공사·횡단 안전을 모두 판단하지 못합니다.",
                         "기준 위치에서 첫 도로까지의 연결 거리는 직선 추정이며 통행 가능성을 검증하지 않았습니다."]}


def finite_number(value, low, high, label):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InputError(f"{label}는 숫자여야 합니다.") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise InputError(f"{label}는 {low}~{high} 범위여야 합니다.")
    return number


def valid_center(center):
    if not isinstance(center, (list, tuple)) or len(center) != 2:
        raise InputError("위치는 [경도, 위도] 형식이어야 합니다.")
    # This prototype intentionally supports South Korea / nearby bounds only.
    return [finite_number(center[0], 124, 132, "경도"), finite_number(center[1], 32, 39, "위도")]

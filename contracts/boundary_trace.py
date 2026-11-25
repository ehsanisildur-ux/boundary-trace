# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from fractions import Fraction
import hashlib
import json
import re

from genlayer import *


SOURCE = "https://tigerweb.geo.census.gov/arcgis/rest/services/Generalized_ACS2025/State_County/MapServer/9/query?where=STATE%3D%2708%27&outFields=STATE%2CNAME&returnGeometry=true&outSR=4326&geometryPrecision=5&f=geojson"
SCALE = 100_000
MAX_VERTICES = 160


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def subtract(left, right):
    return left[0] - right[0], left[1] - right[1]


def cross(left, right):
    return left[0] * right[1] - left[1] * right[0]


def edges(ring):
    return zip(ring, ring[1:] + ring[:1])


def grid_point(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Expected longitude and latitude")
    if any(type(coord) not in (int, str) or not re.fullmatch(r"-?[0-9]{1,3}(?:\.[0-9]{1,10})?", str(coord))
           for coord in value):
        raise ValueError("Invalid coordinate")
    x, y = (Fraction(coord) for coord in value)
    if abs(x) > 180 or abs(y) > 90:
        raise ValueError("Coordinate outside WGS84")
    return round(x * SCALE), round(y * SCALE)


def normalize_ring(raw):
    if not isinstance(raw, list) or not 4 <= len(raw) <= MAX_VERTICES + 1:
        raise ValueError("Invalid ring length")
    ring = [grid_point(vertex) for vertex in raw]
    if ring[0] != ring[-1]:
        raise ValueError("Unclosed ring")
    points = []
    for point in ring[:-1]:
        if not points or point != points[-1]:
            points.append(point)
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    if len(points) < 3 or len(set(points)) != len(points) or sum(cross(a, b) for a, b in edges(points)) == 0:
        raise ValueError("Degenerate ring")
    first = points.index(min(points))
    ordered = points[first:] + points[:first]
    return min(ordered, ordered[:1] + ordered[:0:-1])


def parse_snapshot(body):
    data = json.loads(body.decode("utf-8"), parse_float=str)
    if data["type"] != "FeatureCollection" or len(data["features"]) != 1:
        raise ValueError("Expected one Census feature")
    feature = data["features"][0]
    if feature["properties"]["STATE"] != "08" or feature["properties"]["NAME"] != "Colorado":
        raise ValueError("Unexpected Census region")
    geometry = feature["geometry"]
    if geometry["type"] != "Polygon":
        raise ValueError("Expected one polygon")
    raw = geometry["coordinates"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise ValueError("Invalid ring count")
    rings = [normalize_ring(ring) for ring in raw]
    if sum(map(len, rings)) > MAX_VERTICES:
        raise ValueError("Too many vertices")
    return {
        "source": SOURCE,
        "state_fips": "08",
        "scale": SCALE,
        "rings": [rings[0]] + sorted(rings[1:]),
    }


def acquire():
    def observe():
        try:
            response = gl.nondet.web.get(SOURCE)
            if response.status != 200 or not 1 <= len(response.body) <= 20_000:
                raise ValueError("Invalid Census response")
            return canonical(parse_snapshot(response.body))
        except Exception:
            raise gl.vm.UserError("Colorado boundary acquisition failed")

    def validate(leader):
        if not isinstance(leader, gl.vm.Return):
            return False
        try:
            return observe() == leader.calldata
        except Exception:
            return False

    return gl.vm.run_nondet_unsafe(observe, validate)


def locate_ring(point, ring):
    inside = False
    x, y = point
    for a, b in edges(ring):
        side = cross(subtract(b, a), subtract(point, a))
        if side == 0 and min(a[0], b[0]) <= x <= max(a[0], b[0]) and min(a[1], b[1]) <= y <= max(a[1], b[1]):
            return "BOUNDARY"
        if (a[1] > y) != (b[1] > y) and (side > 0) == (b[1] > a[1]):
            inside = not inside
    return "INSIDE" if inside else "OUTSIDE"


def locate(point, rings):
    location = locate_ring(point, rings[0])
    if location != "INSIDE":
        return location
    for ring in rings[1:]:
        hole_location = locate_ring(point, ring)
        if hole_location == "BOUNDARY":
            return "BOUNDARY"
        if hole_location == "INSIDE":
            return "OUTSIDE"
    return "INSIDE"


def edge_events(start, vector, a, b):
    edge = subtract(b, a)
    relative = subtract(a, start)
    determinant = cross(vector, edge)
    if determinant:
        t = Fraction(cross(relative, edge), determinant)
        u = Fraction(cross(relative, vector), determinant)
        return [t] if 0 <= t <= 1 and 0 <= u <= 1 else []
    if cross(relative, vector):
        return []
    axis = 0 if vector[0] else 1
    ends = sorted(Fraction(point[axis] - start[axis], vector[axis]) for point in (a, b))
    lo, hi = max(Fraction(0), ends[0]), min(Fraction(1), ends[1])
    return [lo, hi] if lo <= hi else []


def ratio(value):
    return [value.numerator, value.denominator]


def trace_segment(start, end, rings):
    vector = subtract(end, start)
    events = {Fraction(0), Fraction(1)}
    for ring in rings:
        for a, b in edges(ring):
            events.update(edge_events(start, vector, a, b))
    ordered = sorted(events)

    def at(t):
        return start[0] + t * vector[0], start[1] + t * vector[1]

    intervals = [
        {"from": ratio(lo), "to": ratio(hi), "kind": locate(at((lo + hi) / 2), rings)}
        for lo, hi in zip(ordered, ordered[1:])
    ]
    contacts = [ratio(t) for t in ordered if locate(at(t), rings) == "BOUNDARY"]
    return {"intervals": intervals, "contacts": contacts}


def parse_route(encoded):
    if len(encoded) > 512:
        raise ValueError("Route data too large")
    route = json.loads(encoded.decode("utf-8"))
    if not isinstance(route, list) or not 2 <= len(route) <= 6:
        raise ValueError("Expected two to six route points")
    for point in route:
        if (
            not isinstance(point, list) or len(point) != 2 or any(type(value) is not int for value in point)
            or abs(point[0]) > 180 * SCALE or abs(point[1]) > 90 * SCALE
        ):
            raise ValueError("Expected grid longitude and latitude")
    if any(a == b for a, b in zip(route, route[1:])):
        raise ValueError("Route has a zero-length segment")
    return route


class BoundaryTrace(gl.Contract):
    snapshots: TreeMap[str, str]

    def __init__(self):
        pass

    @gl.public.write
    def observe(self) -> str:
        snapshot = acquire()
        snapshot_id = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        if snapshot_id not in self.snapshots:
            self.snapshots[snapshot_id] = snapshot
        return snapshot_id

    @gl.public.view
    def get_snapshot(self, snapshot_id: str) -> dict | None:
        return json.loads(self.snapshots[snapshot_id]) if snapshot_id in self.snapshots else None

    @gl.public.view
    def trace(self, snapshot_id: str, route_json: bytes) -> dict:
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            raise gl.vm.UserError("Unknown boundary snapshot")
        try:
            route = parse_route(route_json)
        except (ValueError, UnicodeError) as error:
            raise gl.vm.UserError(str(error))
        return {
            "snapshot_id": snapshot_id,
            "route": route,
            "segments": [trace_segment(a, b, snapshot["rings"]) for a, b in zip(route, route[1:])],
        }

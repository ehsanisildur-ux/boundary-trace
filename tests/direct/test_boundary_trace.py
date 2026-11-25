import hashlib
import json
from pathlib import Path
import re

import pytest


CONTRACT = "contracts/boundary_trace.py"
SOURCE = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/Generalized_ACS2025/State_County/MapServer/9/query"
    "?where=STATE%3D%2708%27&outFields=STATE%2CNAME&returnGeometry=true&outSR=4326&geometryPrecision=5&f=geojson"
)
REAL = Path("fixtures/colorado.geojson").read_bytes()
ROUTE = [[-11000000, 3900000], [-10500000, 3900000], [-10100000, 3900000]]
WEST = [5927555893, 31303500000]
EAST = [688117091, 931920000]
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
HOLE = [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]


@pytest.fixture
def contract(direct_deploy):
    return direct_deploy(CONTRACT, sdk_version="v0.2.16")


def mock(vm, body):
    vm.mock_web(re.escape(SOURCE), {"status": 200, "body": body})


def geojson(rings, state="08"):
    return json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"STATE": state, "NAME": "Colorado"},
        "geometry": {"type": "Polygon", "coordinates": rings},
    }]}).encode("utf-8")


def trace_local(contract, snapshot_id, *points):
    route = [[coord * 100_000 for coord in point] for point in points]
    return contract.trace(snapshot_id, json.dumps(route).encode("utf-8"))["segments"][0]


def test_real_census_geometry_and_exact_crossings(direct_vm, contract, direct_bob):
    mock(direct_vm, REAL)
    with direct_vm.prank(direct_bob):
        snapshot_id = contract.observe()
    assert direct_vm.run_validator() is True
    snapshot = contract.get_snapshot(snapshot_id)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert snapshot_id == hashlib.sha256(encoded.encode()).hexdigest()
    assert len(snapshot["rings"][0]) == 96
    assert contract.observe() == snapshot_id
    direct_vm.clear_mocks()
    segments = contract.trace(snapshot_id, json.dumps(ROUTE).encode())["segments"]
    assert segments == [
        {"intervals": [
            {"from": [0, 1], "to": WEST, "kind": "OUTSIDE"},
            {"from": WEST, "to": [1, 1], "kind": "INSIDE"},
        ], "contacts": [WEST]},
        {"intervals": [
            {"from": [0, 1], "to": EAST, "kind": "INSIDE"},
            {"from": EAST, "to": [1, 1], "kind": "OUTSIDE"},
        ], "contacts": [EAST]},
    ]


def test_geometric_disagreement_changes_same_route(direct_vm, contract):
    mock(direct_vm, geojson([SQUARE]))
    first = contract.observe()
    shifted = [[x + 2, y] for x, y in SQUARE]
    direct_vm.clear_mocks()
    mock(direct_vm, geojson([shifted]))
    assert direct_vm.run_validator() is False
    second = contract.observe()
    assert first != second
    assert trace_local(contract, first, (1, 5), (2, 5))["intervals"][0]["kind"] == "INSIDE"
    assert trace_local(contract, second, (1, 5), (2, 5))["intervals"][0]["kind"] == "OUTSIDE"


def test_ring_rotation_and_reversal_agree(direct_vm, contract):
    mock(direct_vm, geojson([SQUARE]))
    first = contract.observe()
    direct_vm.clear_mocks()
    rotated = SQUARE[2:-1] + SQUARE[:2]
    reverse = list(reversed(rotated))
    mock(direct_vm, geojson([reverse + reverse[:1]]))
    assert direct_vm.run_validator() is True
    assert contract.observe() == first


def test_hole_splits_interior_and_preserves_boundary_contacts(direct_vm, contract):
    mock(direct_vm, geojson([SQUARE, HOLE]))
    observed = contract.observe()
    traced = trace_local(contract, observed, (-1, 5), (11, 5))
    assert [leg["kind"] for leg in traced["intervals"]] == ["OUTSIDE", "INSIDE", "OUTSIDE", "INSIDE", "OUTSIDE"]
    assert traced["contacts"] == [[1, 12], [5, 12], [7, 12], [11, 12]]


def test_collinear_overlap_and_isolated_tangent(direct_vm, contract):
    mock(direct_vm, geojson([SQUARE]))
    observed = contract.observe()
    overlap = trace_local(contract, observed, (-1, 0), (11, 0))
    assert [interval["kind"] for interval in overlap["intervals"]] == ["OUTSIDE", "BOUNDARY", "OUTSIDE"]
    assert overlap["contacts"] == [[1, 12], [11, 12]]
    tangent = trace_local(contract, observed, (-2, 2), (2, -2))
    assert [interval["kind"] for interval in tangent["intervals"]] == ["OUTSIDE", "OUTSIDE"]
    assert tangent["contacts"] == [[1, 2]]


def test_rejects_wrong_region_and_invalid_route(direct_vm, contract):
    mock(direct_vm, geojson([SQUARE], "09"))
    with direct_vm.expect_revert("Colorado boundary acquisition failed"):
        contract.observe()
    assert contract.get_snapshot("0" * 64) is None
    with direct_vm.expect_revert("Unknown boundary snapshot"):
        contract.trace("0" * 64, b"[[0,0],[1,1]]")
    direct_vm.clear_mocks()
    mock(direct_vm, geojson([SQUARE]))
    observed = contract.observe()
    with direct_vm.expect_revert("Route has a zero-length segment"):
        contract.trace(observed, b"[[0,0],[0,0]]")
    with direct_vm.expect_revert("Expected grid longitude and latitude"):
        contract.trace(observed, b"[[0.1,0],[1,1]]")

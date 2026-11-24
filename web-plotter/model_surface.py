"""Rebuild editor faces from straight cuts on original SketchUp faces."""

from collections import defaultdict
from hashlib import sha256
from itertools import pairwise
import json
import math

import numpy as np
from shapely import constrained_delaunay_triangles, get_parts, polygonize, set_precision, union_all
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient


MAX_MODEL_EDGES = 512
MAX_FACE_EDGES = 128
FACE_TOLERANCE = 0.01  # Millimetres
GRID = 1e-6


def _identifier(value):
    return isinstance(value, str) and bool(value.strip()) and all(ord(char) >= 32 for char in value)


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3 or any(type(n) not in (int, float) for n in value):
        return False
    try:
        return all(map(math.isfinite, value))
    except OverflowError:
        return False


def validate_model_edges(edges):
    if not isinstance(edges, list) or len(edges) > MAX_MODEL_EDGES:
        raise ValueError(f"В модели может быть не более {MAX_MODEL_EDGES} добавленных рёбер.")
    identifiers = set()
    counts = defaultdict(int)
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {"id", "nodeId", "faceId", "points"}:
            raise ValueError("Для ребра укажите исходную грань и две точки.")
        identifier, node_id, face_id, points = (edge[key] for key in ("id", "nodeId", "faceId", "points"))
        if not _identifier(identifier) or len(identifier) > 128 or identifier in identifiers:
            raise ValueError("Идентификаторы добавленных рёбер должны быть уникальными и непустыми.")
        if not _identifier(node_id) or not _identifier(face_id):
            raise ValueError("Укажите исходный компонент и грань ребра.")
        if not isinstance(points, list) or len(points) != 2 or any(not _point(point) for point in points):
            raise ValueError("Для ребра нужны две точки с конечными координатами.")
        identifiers.add(identifier)
        counts[node_id, face_id] += 1
        if counts[node_id, face_id] > MAX_FACE_EDGES:
            raise ValueError(f"На одной исходной грани может быть не более {MAX_FACE_EDGES} добавленных рёбер.")


def _source_point(vertices, index):
    if type(index) is not int or not 0 <= index < len(vertices) or not _point(vertices[index]):
        raise ValueError("В исходной грани SketchUp повреждена вершина.")
    return np.asarray(vertices[index], dtype=float)


class _FacePlane:
    def __init__(self, vertices, triangles):
        self.points = {}
        for triangle in triangles:
            if not isinstance(triangle, (list, tuple)) or len(triangle) != 3:
                raise ValueError("В исходной грани SketchUp повреждён треугольник.")
            for index in triangle:
                if type(index) is not int or index not in self.points:
                    self.points[index] = _source_point(vertices, index)
        for triangle in triangles:
            a, b, c = (self.points[index] for index in triangle)
            axis = b - a
            normal = np.cross(axis, c - a)
            area = np.linalg.norm(normal)
            if math.isfinite(area) and area > GRID**2:
                self.origin = a
                self.u = axis / np.linalg.norm(axis)
                self.normal = normal / area
                self.v = np.cross(self.normal, self.u)
                break
        else:
            raise ValueError("У исходной грани SketchUp нет плоской поверхности.")

    def project(self, point):
        offset = point - self.origin
        height = float(offset @ self.normal)
        if not math.isfinite(height) or abs(height) > FACE_TOLERANCE:
            raise ValueError("Обе точки ребра должны лежать в одной плоскости исходной грани.")
        xy = float(offset @ self.u), float(offset @ self.v)
        if not all(map(math.isfinite, xy)):
            raise ValueError("Некорректные координаты в плоскости ребра.")
        return xy

    def lift(self, point):
        x, y = point
        return (self.origin + self.u * x + self.v * y).tolist()


def _face_polygon(node, face, triangles, plane):
    projected = {index: plane.project(point) for index, point in plane.points.items()}
    loops = (node.get("faceLoops") or {}).get(face)
    if loops:
        rings = []
        for loop in loops:
            if not isinstance(loop, (list, tuple)) or len(loop) < 3:
                raise ValueError("В исходной грани SketchUp повреждён контур.")
            ring = []
            for index in loop:
                if type(index) is not int or index not in projected:
                    projected[index] = plane.project(_source_point(node["vertices"], index))
                ring.append(projected[index])
            rings.append(ring)
        polygon = Polygon(rings[0], rings[1:])
    else:
        polygons = [Polygon([projected[index] for index in triangle]) for triangle in triangles]
        polygon = union_all([part for part in polygons if part.area > GRID**2], grid_size=GRID)
    if polygon.geom_type != "Polygon" or not polygon.is_valid or polygon.area <= GRID**2:
        raise ValueError("Не удалось восстановить контур исходной грани SketchUp.")
    polygon = set_precision(polygon, GRID)
    if polygon.geom_type != "Polygon" or polygon.is_empty:
        raise ValueError("Не удалось восстановить контур исходной грани SketchUp.")
    return polygon


def _line_on_face(points, plane, boundary, allowed):
    ends = []
    for point in points:
        local = Point(plane.project(np.asarray(point, dtype=float)))
        if boundary.distance(local) <= FACE_TOLERANCE:
            local = boundary.interpolate(boundary.project(local))
        ends.append(local)
    line = LineString(ends)
    if line.length <= FACE_TOLERANCE:
        raise ValueError("Укажите две разные точки ребра на исходной грани.")
    if not allowed.covers(line):
        raise ValueError("Ребро должно целиком лежать внутри одной исходной грани.")
    return line


def _digest(value):
    return sha256(value).hexdigest()[:32]


def surface_node_id(node_id, face_id):
    key = json.dumps([node_id, face_id], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return "editor-face:" + _digest(key)


def _derive_face(node, face, triangles, drawn):
    plane = _FacePlane(node["vertices"], triangles)
    source = _face_polygon(node, face, triangles, plane)
    boundary = source.boundary
    allowed = source.buffer(2 * GRID)
    # Sort the lines and canonicalize polygons so anchors survive rebuilding the same state.
    lines = sorted((_line_on_face(points, plane, boundary, allowed).normalize() for points in drawn), key=lambda x: x.wkb)
    network = union_all([boundary, *lines], grid_size=GRID)
    paths = get_parts(network)
    regions = sorted((part.normalize() for part in get_parts(polygonize(paths))
                      if source.covers(part.representative_point())), key=lambda x: x.wkb)
    if not regions or abs(sum(part.area for part in regions) - source.area) > source.length * 4 * GRID:
        raise ValueError("Не удалось разделить исходную грань по добавленным рёбрам.")

    result = {"id": surface_node_id(node["id"], face), "parent": node["id"], "name": node.get("name", "Грань SketchUp"),
              "visible": True,
              "editorSourceNode": node["id"], "editorSourceFace": face,
              "vertices": [], "triangles": [], "faces": [], "edges": [],
              "faceLoops": {}, "faceFlags": {}, "edgeFlags": []}
    indices = {}

    def vertex(xy):
        if xy not in indices:
            indices[xy] = len(result["vertices"])
            result["vertices"].append(plane.lift(xy))
        return indices[xy]

    flags = {**node.get("faceFlags", {}).get(face, {}), "visible": True}
    for region in regions:
        face_id = "region:" + _digest(region.simplify(0).normalize().wkb)
        result["faceLoops"][face_id] = [[vertex(xy) for xy in list(ring.coords)[:-1]]
                                        for ring in (region.exterior, *region.interiors)]
        result["faceFlags"][face_id] = flags
        for triangle in sorted(constrained_delaunay_triangles(region).geoms, key=lambda x: x.normalize().wkb):
            result["triangles"].append([vertex(xy) for xy in list(orient(triangle).exterior.coords)[:3]])
            result["faces"].append(face_id)

    inherited = boundary.buffer(2 * GRID)
    edges = {tuple(sorted((a, b))) for path in paths for a, b in pairwise(path.coords)
             if a != b and not inherited.covers(LineString((a, b)))}
    for a, b in sorted(edges):
        result["edges"].append([vertex(a), vertex(b)])
        result["edgeFlags"].append({"visible": True, "soft": False, "smooth": False})
    return result


def build_surface_model(model, edges):
    """Return a working model while leaving the imported model and its indices intact."""
    validate_model_edges(edges)
    if not edges:
        return model
    groups = defaultdict(lambda: defaultdict(list))
    for edge in edges:
        groups[edge["nodeId"]][edge["faceId"]].append(edge["points"])
    nodes, derived = [], []
    for node in model["nodes"]:
        drawn = groups.pop(node["id"], None)
        if not drawn:
            nodes.append(node)
            continue
        by_face = defaultdict(list)
        for triangle, face in zip(node["triangles"], node["faces"], strict=True):
            if face in drawn:
                by_face[face].append(triangle)
        flags = node.get("faceFlags", {})
        for face, points in sorted(drawn.items()):
            if face not in by_face or node.get("visible") is False or flags.get(face, {}).get("visible") is False:
                raise ValueError("Исходная грань ребра отсутствует или скрыта в модели SketchUp.")
            try:
                derived.append(_derive_face(node, face, by_face[face], points))
            except GEOSException as error:
                raise ValueError("Не удалось построить грани по добавленным рёбрам.") from error
        nodes.append({**node, "editorHiddenFaces": sorted(drawn)})
    if groups:
        raise ValueError("Исходный компонент ребра отсутствует в модели SketchUp.")
    return {**model, "nodes": [*nodes, *derived]}

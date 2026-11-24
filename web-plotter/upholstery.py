"""Unfold triangulated SketchUp panels in millimetres and prepare editable patterns."""

from collections import defaultdict, deque
from dataclasses import dataclass
from functools import cached_property
import hashlib
from heapq import heappop, heappush
from itertools import pairwise
import json
import math
from uuid import uuid4

import numpy as np
from scipy.sparse import coo_matrix, diags, eye, kron
from scipy.sparse.linalg import factorized, spsolve
from shapely import STRtree, constrained_delaunay_triangles, union_all
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.polygon import orient

from cad import MAX_ENTITIES, MAX_POINTS, POINT_EPSILON_MM
from designer_cad import CadDesignError, edit_design, validate_design


EPS = 1e-7
FIT_TOLERANCE = 1e-5


class UpholsteryError(ValueError):
    """The requested surface or pattern cannot be constructed."""


class _TransferIssue(UpholsteryError):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _edge(a, b):
    return min(a, b), max(a, b)


def _sides(triangle):
    a, b, c = triangle
    return ((a, b), (b, c), (c, a))


def _edge_faces(triangles, selected=None):
    edges = defaultdict(list)
    for index in range(len(triangles)) if selected is None else selected:
        for a, b in _sides(triangles[index]):
            edges[_edge(a, b)].append((index, a, b))
    return edges


def _cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def _loops(edges):
    outgoing = dict(edges)
    if len(outgoing) != len(edges) or set(outgoing) != {b for _, b in edges}:
        raise UpholsteryError("У границы есть разрыв или развилка.")
    loops = []
    while outgoing:
        start = next(iter(outgoing))
        loop = [start]
        vertex = outgoing.pop(start)
        while vertex != start:
            loop.append(vertex)
            vertex = outgoing.pop(vertex)
        if len(loop) < 3:
            raise UpholsteryError("Граница содержит вырожденный контур.")
        loops.append(loop)
    return loops


def _weights(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise UpholsteryError("Точке нужны три барицентрические координаты.")
    if any(isinstance(weight, bool) or not isinstance(weight, (int, float)) for weight in value):
        raise UpholsteryError("Координаты точки на грани должны быть числами.")
    if any(not -EPS <= weight <= 1 + EPS for weight in value):
        raise UpholsteryError("Точка находится за пределами треугольника.")
    values = np.asarray(value, dtype=float)
    if abs(values.sum() - 1) > EPS:
        raise UpholsteryError("Точка находится за пределами треугольника.")
    values[values <= EPS] = 0
    return values / values.sum()


def _index(value, size, message):
    if type(value) is not int or not 0 <= value < size:
        raise UpholsteryError(message)
    return value


def _ring_triangles(ring, coords):
    polygon = Polygon([coords[index] for index in ring])
    if not polygon.is_valid or polygon.area <= EPS**2:
        raise UpholsteryError("Не удалось провести шов по поверхности без наложения.")
    indices = {tuple(coords[index]): index for index in ring}
    triangles = []
    for item in constrained_delaunay_triangles(polygon).geoms:
        a, b, c = (indices[xy] for xy in list(item.exterior.coords)[:3])
        if _cross(coords[b] - coords[a], coords[c] - coords[a]) < 0:
            b, c = c, b
        triangles.append((a, b, c))
    return triangles


def _fabric_cell(coordinate):
    scaled = coordinate * 100
    lower = math.floor(scaled)
    return lower + (scaled - lower >= 0.5)


def _has_triangle_area(points):
    a, b, c = points
    ab_x, ab_y, ab_z = (end - start for end, start in zip(b, a))
    ac_x, ac_y, ac_z = (end - start for end, start in zip(c, a))
    x = ab_y * ac_z - ab_z * ac_y
    y = ab_z * ac_x - ab_x * ac_z
    z = ab_x * ac_y - ab_y * ac_x
    return x * x + y * y + z * z != 0


def _fabric_node(layer, model_nodes):
    vertices, triangles, faces = [], [], []
    vertex_indices, seen_triangles = {}, set()
    # Match fabricSurface in the editor. Saved anchors use this vertex and triangle order.
    for surface_index, source in enumerate(layer["surfaces"]):
        node = model_nodes.get(source["nodeId"])
        if node is None:
            continue
        included = set(source["faces"])
        flags = node.get("faceFlags", {})
        for triangle, face in zip(node["triangles"], node["faces"]):
            if face not in included or flags.get(face, {}).get("visible") is False:
                continue
            points = [node["vertices"][index] for index in triangle]
            cells = [tuple(_fabric_cell(coordinate) for coordinate in point) for point in points]
            triangle_key = tuple(sorted(cells))
            if len(set(cells)) != 3 or triangle_key in seen_triangles:
                continue
            welded = [vertices[vertex_indices[cell]] if cell in vertex_indices else point for cell, point in zip(cells, points)]
            if not _has_triangle_area(welded):
                continue
            for cell, point in zip(cells, points):
                if cell not in vertex_indices:
                    vertex_indices[cell] = len(vertices)
                    vertices.append(point)
            triangles.append([vertex_indices[cell] for cell in cells])
            faces.append(f"{surface_index}:{face}")
            seen_triangles.add(triangle_key)
    return {"vertices": vertices, "triangles": triangles, "faces": faces}


class _Surface:
    def __init__(self, node, *, join_coplanar=False, join_folded=False):
        self.vertices = [np.asarray(vertex, dtype=float) for vertex in node["vertices"]]
        self.original_count = len(self.vertices)
        self.original_triangles = [tuple(triangle) for triangle in node["triangles"]]
        self.faces = node["faces"]
        self.edge_faces = _edge_faces(self.original_triangles)
        self.support = [set() for _ in self.vertices]
        self.graph = [{} for _ in self.vertices]
        self.split_edges = defaultdict(list)
        self.boundary_matches = defaultdict(list)
        self.face_points = defaultdict(list)
        self.chords = defaultdict(list)
        for index, triangle in enumerate(self.original_triangles):
            for vertex in triangle:
                self.support[vertex].add(index)
        for a, b in self.edge_faces:
            self._connect(a, b)
        if join_coplanar or join_folded:
            self._match_boundary_edges(join_folded)

    def _connect(self, a, b):
        length = math.dist(self.vertices[a], self.vertices[b])
        self.graph[a][b] = self.graph[b][a] = length

    def _split_at_vertex(self, edge, vertex):
        if vertex in edge or vertex in self.split_edges[edge]:
            return
        self.split_edges[edge].append(vertex)
        self.support[vertex].update(face for face, _, _ in self.edge_faces[edge])
        for corner in edge:
            self._connect(vertex, corner)

    def _match_boundary_edges(self, join_folded):
        # Keep source indices stable for saved anchors. Subdivide only the refined mesh,
        # where a long boundary edge and shorter edges share a real interface.
        boundaries, projections = [], []
        for edge, refs in self.edge_faces.items():
            if len(refs) != 1:
                continue
            a, b = (self.vertices[i] for i in edge)
            axis = b - a
            length = np.linalg.norm(axis)
            if length <= EPS:
                continue
            axis /= length
            third = next(i for i in self.original_triangles[refs[0][0]] if i not in edge)
            normal = np.cross(axis, self.vertices[third] - a)
            width = np.linalg.norm(normal)
            if width <= EPS:
                continue
            boundaries.append((edge, axis, length, normal / width, third))
            projections.append(LineString([a[:2], b[:2]]) if math.dist(a[:2], b[:2]) > 0 else Point(a[:2]))
        tree = STRtree(projections)
        for index, (edge, axis, length, normal, _) in enumerate(boundaries):
            a = self.vertices[edge[0]]
            for other_index in tree.query(projections[index], predicate="dwithin", distance=EPS):
                if other_index <= index:
                    continue
                other, _, _, _, other_third = boundaries[other_index]
                offsets = [self.vertices[i] - a for i in other]
                positions = [float(offset @ axis) for offset in offsets]
                if min(length, max(positions)) - max(0, min(positions)) <= EPS:
                    continue
                if any(np.linalg.norm(offset - position * axis) > EPS for offset, position in zip(offsets, positions)):
                    continue
                opposite = self.vertices[other_third] - a
                coplanar = abs(normal @ opposite) <= EPS
                if ((coplanar and normal @ np.cross(axis, opposite) >= -EPS)
                        or (not coplanar and not join_folded)):
                    continue
                self.boundary_matches[edge].append(other)
                self.boundary_matches[other].append(edge)
                for vertex, position in zip(other, positions):
                    if EPS < position < length - EPS:
                        self._split_at_vertex(edge, vertex)
                lower, upper = sorted(positions)
                for vertex, position in zip(edge, (0, length)):
                    if lower + EPS < position < upper - EPS:
                        self._split_at_vertex(other, vertex)

    def _matching_edges(self, edge, point):
        for match in self.boundary_matches.get(edge, ()):
            a, b = (self.vertices[index] for index in match)
            axis = b - a
            fraction = (point - a) @ axis / (axis @ axis)
            tolerance = EPS / np.linalg.norm(axis)
            if -tolerance <= fraction <= 1 + tolerance:
                yield match

    def _add_point(self, point, adjacent, points, corners):
        for vertex in points:
            if math.dist(self.vertices[vertex], point) <= EPS:
                return vertex
        index = len(self.vertices)
        self.vertices.append(point)
        self.support.append(set(adjacent))
        self.graph.append({})
        for vertex in corners:
            self._connect(index, vertex)
        points.append(index)
        return index

    def _edge_point(self, a, b, fraction):
        point = (1 - fraction) * self.vertices[a] + fraction * self.vertices[b]
        for vertex in (a, b):
            if math.dist(self.vertices[vertex], point) <= EPS:
                return vertex
        key = _edge(a, b)
        vertex = self._add_point(point, (face for face, _, _ in self.edge_faces[key]), self.split_edges[key], (a, b))
        for match in self._matching_edges(key, point):
            self._split_at_vertex(match, vertex)
        return vertex

    def anchor(self, anchor):
        if not isinstance(anchor, dict):
            raise UpholsteryError("Некорректная точка шва.")
        if "vertex" in anchor:
            index = _index(anchor["vertex"], self.original_count, "Вершина шва отсутствует в модели.")
            if not self.support[index]:
                raise UpholsteryError("Вершина шва не принадлежит поверхности.")
            return index
        index = _index(anchor.get("triangle"), len(self.original_triangles), "Грань шва отсутствует в модели.")
        weights = _weights(anchor.get("barycentric"))
        vertices = self.original_triangles[index]
        occupied = np.flatnonzero(weights)
        if len(occupied) == 1:
            return vertices[occupied[0]]
        if len(occupied) == 2:
            a, b = occupied
            return self._edge_point(vertices[a], vertices[b], weights[b])
        point = weights @ np.asarray([self.vertices[vertex] for vertex in vertices])
        return self._add_point(point, (index,), self.face_points[index], vertices)

    def _neighbor_crossing(self, a, b):
        for first in sorted(self.support[a]):
            for second in sorted(self.support[b]):
                common = sorted(set(self.original_triangles[first]) & set(self.original_triangles[second]))
                if len(common) != 2:
                    continue
                left, right = common
                axis = self.vertices[right] - self.vertices[left]
                length = np.linalg.norm(axis)
                if length <= EPS:
                    continue
                axis /= length
                u, v = self.vertices[a] - self.vertices[left], self.vertices[b] - self.vertices[left]
                x1, x2 = u @ axis, v @ axis
                h1, h2 = np.linalg.norm(u - x1 * axis), np.linalg.norm(v - x2 * axis)
                if h1 + h2 <= EPS:
                    continue
                fraction = (h2 * x1 + h1 * x2) / ((h1 + h2) * length)
                if 0 <= fraction <= 1:
                    return self._edge_point(left, right, fraction)
        return None

    def _coplanar_crossings(self, a, b):
        """Trace a straight segment through coplanar facets before routing along mesh edges."""
        traced = self._trace_coplanar(self.vertices[a], self.vertices[b], self.support[a], self.support[b])
        if traced is not None:
            return self._crossing_vertices(traced[0])

    def _crossing_vertices(self, crossings):
        return [self._edge_point(*point) if isinstance(point, tuple) else point for point in crossings]

    def _trace_coplanar(self, start_point, end_point, starts, ends=None):
        queue = deque(sorted(starts))
        entered = {source: (None, 0., None) for source in queue}

        def result(source, end_weights):
            path = []
            face = source
            while (entry := entered[source])[0] is not None:
                path.append(entry[2])
                source = entry[0]
            return path[::-1], face, end_weights

        while queue:
            source = queue.popleft()
            if ends is not None and source in ends:
                return result(source, None)
            indices = self.original_triangles[source]
            p, q, r = (self.vertices[index] for index in indices)
            x, y = q - p, r - p
            normal = np.cross(x, y)
            area = normal @ normal
            if area <= EPS**4:
                continue
            offsets = np.asarray([start_point - p, end_point - p])
            if np.max(abs(offsets @ normal)) > EPS * math.sqrt(area):
                continue
            u = np.cross(offsets, y) @ normal / area
            v = np.cross(x, offsets) @ normal / area
            start, end = np.column_stack((1 - u - v, u, v))
            start_t = entered[source][1]
            weights = start + start_t * (end - start)
            if min(weights) < -EPS:
                continue
            if ends is None and min(end) >= -EPS:
                return result(source, end)
            exits = [weight / (weight - tail) for weight, tail in zip(start, end) if tail < -EPS]
            if not exits or (end_t := min(exits)) <= start_t + EPS:
                continue
            weights = start + end_t * (end - start)
            inside = np.flatnonzero(weights > EPS)
            if len(inside) == 1:
                step = indices[inside[0]]
                neighbors = self.support[step]
            elif len(inside) == 2:
                left, right = inside
                step = (indices[left], indices[right], weights[right] / (weights[left] + weights[right]))
                a_edge, b_edge, fraction = step
                edge = _edge(a_edge, b_edge)
                point = (1 - fraction) * self.vertices[a_edge] + fraction * self.vertices[b_edge]
                neighbors = [face for key in (edge, *self._matching_edges(edge, point)) for face, _, _ in self.edge_faces[key]]
            else:
                continue
            for neighbor in sorted(neighbors):
                if neighbor not in entered:
                    entered[neighbor] = (source, end_t, step)
                    queue.append(neighbor)
        return None

    def _fold_edge_candidates(self, face, weights, axis):
        triangle = self.original_triangles[face]
        corners = [vertex for vertex, weight in zip(triangle, weights) if weight > EPS]
        if len(corners) == 2:
            return [_edge(*corners)]
        if len(corners) != 1:
            return []
        # A crossing can land exactly on a corner of the tessellation. Find the fold
        # edges there so touching only at one vertex still doesn't merge surfaces.
        vertex = corners[0]
        candidates = set()
        for adjacent in self.support[vertex]:
            for neighbor in self.original_triangles[adjacent]:
                direction = self.vertices[neighbor] - self.vertices[vertex]
                if neighbor != vertex and np.linalg.norm(np.cross(axis, direction)) <= EPS:
                    edge = _edge(vertex, neighbor)
                    if edge in self.edge_faces:
                        candidates.add(edge)
        return sorted(candidates)

    def _fold_crossings(self, a, b):
        start, end = self.vertices[a], self.vertices[b]

        def normals(anchor):
            directions = []
            for index in sorted(self.support[anchor]):
                p, q, r = (self.vertices[vertex] for vertex in self.original_triangles[index])
                normal = np.cross(q - p, r - p)
                length = np.linalg.norm(normal)
                if length > EPS**2:
                    normal /= length
                    if not any(abs(other @ normal) > 1 - EPS for other in directions):
                        directions.append(normal)
            return directions

        first_normals, second_normals = normals(a), normals(b)
        for first in first_normals:
            for second in second_normals:
                axis = np.cross(first, second)
                length = np.linalg.norm(axis)
                if length <= EPS:
                    continue
                axis /= length
                across = np.cross(axis, first)
                origin = start + across * ((second @ (end - start)) / (second @ across))
                along = (end - origin) @ axis
                distance = np.linalg.norm(start - origin)
                total = distance + np.linalg.norm(end - origin - along * axis)
                if total <= EPS or distance <= EPS or total - distance <= EPS:
                    continue
                pivot = origin + (along * distance / total) * axis
                left = self._trace_coplanar(start, pivot, self.support[a])
                right = self._trace_coplanar(end, pivot, self.support[b])
                if left is None or right is None:
                    continue
                left_edges = self._fold_edge_candidates(left[1], left[2], axis)
                right_edges = self._fold_edge_candidates(right[1], right[2], axis)
                for edge in left_edges:
                    if not any(other == edge or other in self.boundary_matches.get(edge, ()) for other in right_edges):
                        continue
                    u, v = (self.vertices[vertex] for vertex in edge)
                    delta = v - u
                    fraction = float(np.clip(((pivot - u) @ delta) / (delta @ delta), 0, 1))
                    crossing = self._edge_point(*edge, fraction)
                    return [*self._crossing_vertices(left[0]), crossing, *self._crossing_vertices(right[0][::-1])]
        return None

    @cached_property
    def face_components(self):
        parents = list(range(len(self.original_triangles)))

        def root(face):
            while parents[face] != face:
                parents[face] = parents[parents[face]]
                face = parents[face]
            return face

        for edge, refs in self.edge_faces.items():
            if len(refs) == 2:
                parents[root(refs[1][0])] = root(refs[0][0])
            for match in self.boundary_matches.get(edge, ()):
                parents[root(self.edge_faces[match][0][0])] = root(refs[0][0])
        return [root(index) for index in range(len(parents))]

    def _route(self, a, b, *, surface_route=False):
        components = None
        if surface_route:
            groups = self.face_components
            components = {groups[face] for face in self.support[a]} & {groups[face] for face in self.support[b]}
            if not components:
                raise UpholsteryError("Точки контура принадлежат несвязанным участкам модели.")
        if self.support[a] & self.support[b]:
            return [a, b]
        crossing = self._neighbor_crossing(a, b)
        if crossing is not None:
            return [a, crossing, b]
        crossings = self._coplanar_crossings(a, b)
        if crossings is not None:
            return [a, *crossings, b]
        if surface_route and (crossings := self._fold_crossings(a, b)) is not None:
            return [a, *crossings, b]
        distances, previous, queue = {a: 0}, {}, [(0, a)]
        while queue:
            distance, vertex = heappop(queue)
            if vertex == b:
                break
            if distance > distances[vertex]:
                continue
            for neighbor, length in self.graph[vertex].items():
                if components is not None and not any(groups[face] in components for face in self.support[vertex] & self.support[neighbor]):
                    continue
                candidate = distance + length
                if candidate < distances.get(neighbor, math.inf):
                    distances[neighbor], previous[neighbor] = candidate, vertex
                    heappush(queue, (candidate, neighbor))
        if b not in previous:
            raise UpholsteryError("Точки шва принадлежат несвязанным участкам модели.")
        path = [b]
        while path[-1] != a:
            path.append(previous[path[-1]])
        return path[::-1]

    def seam(self, seam):
        anchors = seam.get("anchors")
        if not isinstance(anchors, list) or len(anchors) < 2:
            raise UpholsteryError("Укажите не менее двух точек шва.")
        anchors = [self.anchor(anchor) for anchor in anchors]
        if seam.get("closed") and anchors[0] != anchors[-1]:
            anchors.append(anchors[0])
        path = [anchors[0]]
        for a, b in pairwise(anchors):
            if a != b:
                path.extend(self._route(a, b, surface_route=seam.get("surfaceRoute") is True)[1:])
        closed = seam.get("closed", False)
        distinct = path[:-1] if closed else path
        if len(distinct) < (3 if closed else 2) or len(set(distinct)) != len(distinct):
            raise UpholsteryError("Точки шва повторяются или не образуют линию.")
        for a, b in pairwise(path):
            face = min(self.support[a] & self.support[b])
            self.chords[face].append((a, b))
        return path

    def _triangulate(self, index, triangle):
        boundary = []
        for a, b in _sides(triangle):
            edge_points = sorted(self.split_edges.get(_edge(a, b), ()),
                                 key=lambda vertex: math.dist(self.vertices[a], self.vertices[vertex]))
            boundary.extend([a, *edge_points])
        interior, chords = self.face_points.get(index, ()), self.chords.get(index, ())
        if len(boundary) == 3 and not interior:
            return [triangle], {edge: [*edge] for edge in chords}

        p, q, r = (self.vertices[vertex] for vertex in triangle)
        x = q - p
        normal = np.cross(x, r - p)
        if np.linalg.norm(normal) <= EPS**2:
            raise UpholsteryError("Поверхность содержит вырожденный треугольник.")
        x /= np.linalg.norm(x)
        y = np.cross(normal / np.linalg.norm(normal), x)
        coords = {vertex: np.array([(self.vertices[vertex] - p) @ x, (self.vertices[vertex] - p) @ y])
                  for vertex in (*boundary, *interior)}
        triangles = [triangle]
        for vertex in [*(vertex for vertex in boundary if vertex not in triangle), *interior]:
            triangles = _insert_point(triangles, coords, vertex)
        routes, installed = {}, set()
        for a, b in chords:
            points = _segment_points(coords, a, b)
            for u, v in pairwise(points):
                key = _edge(u, v)
                if key not in installed:
                    triangles = _insert_constraint(triangles, coords, u, v, installed)
                    installed.add(key)
            routes[(a, b)] = points
        return triangles, routes

    def triangulate(self):
        triangles, sources, routes = [], [], {}
        for source, triangle in enumerate(self.original_triangles):
            items, segments = self._triangulate(source, triangle)
            triangles.extend(items)
            sources.extend([source] * len(items))
            routes.update(segments)
        return triangles, sources, routes


def _insert_point(triangles, coords, vertex):
    point = coords[vertex]
    result = []
    inserted = False
    for triangle in triangles:
        areas = [_cross(coords[b] - coords[a], point - coords[a]) for a, b in _sides(triangle)]
        scales = [math.dist(coords[a], coords[b]) * EPS for a, b in _sides(triangle)]
        if any(area < -scale for area, scale in zip(areas, scales)):
            result.append(triangle)
            continue
        inserted = True
        result.extend((a, b, vertex) for (a, b), area, scale in zip(_sides(triangle), areas, scales) if area > scale)
    if not inserted:
        raise UpholsteryError("Опорная точка вышла за границу грани.")
    return result


def _segment_points(coords, a, b):
    vector = coords[b] - coords[a]
    length = np.linalg.norm(vector)
    interior = []
    for vertex, point in coords.items():
        if vertex not in (a, b):
            offset = point - coords[a]
            along = (offset @ vector) / length
            if EPS < along < length - EPS and abs(_cross(vector, offset)) <= EPS * length:
                interior.append((along, vertex))
    return [a, *(vertex for _, vertex in sorted(interior)), b]


def _insert_constraint(triangles, coords, a, b, installed):
    if _edge(a, b) in _edge_faces(triangles):
        return triangles
    chord = LineString([coords[a], coords[b]])
    if any(chord.crosses(LineString([coords[u], coords[v]])) for u, v in installed):
        raise UpholsteryError("Пересекающиеся швы должны иметь общую опорную точку.")
    cavity, kept = [], []
    for triangle in triangles:
        group = cavity if chord.relate_pattern(Polygon([coords[v] for v in triangle]), "T********") else kept
        group.append(triangle)
    boundary = [(start, end) for incidences in _edge_faces(cavity).values() if len(incidences) == 1
                for _, start, end in incidences]
    rings = _loops(boundary)
    if len(rings) != 1 or a not in rings[0] or b not in rings[0]:
        raise UpholsteryError("Не удалось построить ребро шва на грани.")
    ring = rings[0]
    start = ring.index(a)
    ring = ring[start:] + ring[:start]
    end = ring.index(b)
    return [*kept, *_ring_triangles(ring[:end + 1], coords), *_ring_triangles(ring[end:] + [a], coords)]


def _components(selected, neighbors):
    unseen = set(selected)
    components = []
    for seed in selected:
        if seed not in unseen:
            continue
        unseen.remove(seed)
        sign = {seed: False}
        queue = deque([seed])
        while queue:
            face = queue.popleft()
            for neighbor, reverse, _ in neighbors[face]:
                desired = sign[face] ^ reverse
                if neighbor in unseen:
                    sign[neighbor] = desired
                    queue.append(neighbor)
                    unseen.remove(neighbor)
                elif neighbor in sign and sign[neighbor] != desired:
                    raise UpholsteryError("Не удалось согласовать стороны поверхности.")
        components.append(sign)
    return components


def _seed_component(components, surface, triangles, sources, panel):
    seed = panel.get("seed")
    if seed is None and len(components) == 1:
        return components[0]
    seed = _index(seed, len(surface.original_triangles), "Укажите точку внутри нужной детали после разреза.")
    point = None
    if "seedBarycentric" in panel:
        point = _weights(panel["seedBarycentric"]) @ [surface.vertices[i] for i in surface.original_triangles[seed]]
    candidates = []
    for component in components:
        for face in component:
            if sources[face] != seed:
                continue
            if point is not None:
                a, b, c = [surface.vertices[i] for i in triangles[face]]
                normal = np.cross(b - a, c - a)
                sides = [(a, b), (b, c), (c, a)]
                if any(normal @ np.cross(end - start, point - start) < -EPS * np.linalg.norm(normal) * math.dist(start, end)
                       for start, end in sides):
                    continue
            candidates.append(component)
            break
    if len(candidates) != 1:
        raise UpholsteryError("Укажите точку внутри нужной детали, вдали от её шва.")
    return candidates[0]


def _selected_facets(surface, sources, panel):
    faces = panel.get("faces", [])
    if not isinstance(faces, list) or any(not isinstance(face, str) for face in faces):
        raise UpholsteryError("Некорректный список граней детали.")
    face_ids = set(faces)
    if face_ids.difference(surface.faces):
        raise UpholsteryError("Часть выбранных граней отсутствует в модели.")
    selected = [index for index, source in enumerate(sources) if not face_ids or surface.faces[source] in face_ids]
    if not selected:
        raise UpholsteryError("В детали нет выбранных граней.")
    return selected


def _panel_neighbors(incidence, cuts):
    neighbors = defaultdict(list)
    for edge, references in incidence.items():
        if len(references) > 2:
            raise UpholsteryError("К одному ребру примыкают более двух граней.")
        if len(references) == 2 and edge not in cuts:
            (face_a, a, _), (face_b, b, _) = references
            neighbors[face_a].append((face_b, a == b, edge))
            neighbors[face_b].append((face_a, a == b, edge))
    return neighbors


def _cut_panel(surface, triangles, sources, panel, cuts, resolved=None, placement=None):
    selected = _selected_facets(surface, sources, panel)
    incidence = _edge_faces(triangles, selected)
    neighbors = _panel_neighbors(incidence, cuts)
    components = _components(selected, neighbors)
    component = _seed_component(components, surface, triangles, sources, panel)
    if resolved is not None:
        resolved.clear()
        resolved.update(component)
    return _cut_component(surface, triangles, sources, cuts, incidence, neighbors, component, placement)


def _cut_component(surface, triangles, sources, cuts, incidence, neighbors, component, placement=None):
    source_faces = sorted(component)
    facets = {face: triangles[face][::-1] if component[face] else triangles[face] for face in source_faces}
    corners = {(face, vertex): (face, vertex) for face, vertices in facets.items() for vertex in vertices}

    def root(key):
        while key != corners[key]:
            corners[key] = corners[corners[key]]
            key = corners[key]
        return key

    for face in source_faces:
        for neighbor, _, edge in neighbors[face]:
            if face < neighbor and neighbor in component:
                for vertex in edge:
                    corners[root((neighbor, vertex))] = root((face, vertex))
    origin_ids, local_ids = [], {}
    local_triangles = []
    for face in source_faces:
        row = []
        for vertex in facets[face]:
            key = root((face, vertex))
            if key not in local_ids:
                local_ids[key] = len(origin_ids)
                origin_ids.append(vertex)
            row.append(local_ids[key])
        local_triangles.append(row)
    boundary = []
    cut_edges = defaultdict(list)
    for face, triangle in zip(source_faces, local_triangles):
        for a, b in _sides(triangle):
            key = _edge(origin_ids[a], origin_ids[b])
            refs = incidence[key]
            if key in cuts or len(refs) == 1 or any(other not in component for other, _, _ in refs):
                boundary.append((a, b))
                cut_edges[key].append((a, b))
    points = np.asarray([surface.vertices[index] for index in origin_ids])
    if placement is not None:
        placement.update(vertices=points.tolist(), triangles=local_triangles, loops=[])
    if not boundary:
        raise UpholsteryError("У замкнутой поверхности нужен разрез или выбор части граней.")
    loops = _loops(boundary)
    if placement is not None:
        placement["loops"] = loops
    return points, np.asarray(local_triangles), [sources[face] for face in source_faces], loops, cut_edges, origin_ids


def _plane_coordinates(distances):
    ab, bc, ca = distances
    if min(ab, bc, ca) <= EPS:
        raise UpholsteryError("Поверхность содержит нулевое ребро.")
    x = (ab * ab + ca * ca - bc * bc) / (2 * ab)
    height = ca * ca - x * x
    if height <= EPS**2:
        raise UpholsteryError("Поверхность содержит вырожденный треугольник.")
    return x, math.sqrt(height)


def _unfold(points, triangles):
    incidence = _edge_faces(triangles)
    facets = triangles.tolist()
    uv = np.full((len(points), 2), np.nan)
    a, b, c = facets[0]
    length = math.dist(points[a], points[b])
    uv[a], uv[b] = [0, 0], [length, 0]
    uv[c] = _plane_coordinates((length, math.dist(points[b], points[c]), math.dist(points[c], points[a])))
    examined = {0}
    queue = deque([0])
    max_error = 0
    while queue:
        for left, right in _sides(facets[queue.popleft()]):
            for neighbor, a, b in incidence[_edge(left, right)]:
                if neighbor in examined:
                    continue
                c = next(vertex for vertex in facets[neighbor] if vertex not in (a, b))
                rest = math.dist(points[a], points[b])
                x, y = _plane_coordinates((rest, math.dist(points[b], points[c]), math.dist(points[c], points[a])))
                edge = uv[b] - uv[a]
                edge_length = np.linalg.norm(edge)
                if edge_length <= EPS:
                    return uv, math.inf
                axis = edge / edge_length
                target = uv[a] + axis * x + np.array([-axis[1], axis[0]]) * y
                if np.all(np.isfinite(uv[c])):
                    max_error = max(max_error, math.dist(uv[c], target))
                else:
                    uv[c] = target
                queue.append(neighbor)
                examined.add(neighbor)
    if len(examined) != len(triangles):
        raise UpholsteryError("Поверхность детали не образует связное полотно.")
    return uv, max_error


def _signed_areas(uv, triangles):
    a, b, c = np.moveaxis(uv[triangles], 1, 0)
    return _cross(b - a, c - a) / 2


def _valid_pattern(uv, triangles, loops):
    areas = _signed_areas(uv, triangles)
    if not np.all(np.isfinite(areas)) or min(areas) <= EPS**2:
        raise UpholsteryError("На развёртке есть вырожденные или перевёрнутые треугольники.")
    rings = [uv[loop] for loop in loops]
    polygon = Polygon(rings[0], rings[1:])
    if not polygon.is_valid or abs(polygon.area - areas.sum()) > max(FIT_TOLERANCE**2, polygon.area * 1e-8):
        raise UpholsteryError("Контуры развёртки пересекаются или перекрывают часть детали.")
    return polygon


def _project(points, triangles):
    a, b, c = points[triangles[0]]
    x = b - a
    normal = np.cross(x, c - a)
    if np.linalg.norm(normal) <= EPS**2:
        raise UpholsteryError("Поверхность содержит вырожденный треугольник.")
    x /= np.linalg.norm(x)
    normal /= np.linalg.norm(normal)
    y = np.cross(normal, x)
    offsets = points - a
    return offsets @ np.stack((x, y), axis=1), max(abs(offsets @ normal))


def _harmonic(points, triangles, boundary):
    edges = np.asarray(list(_edge_faces(triangles)))
    rows, cols = np.concatenate((edges, edges[:, ::-1])).T
    adjacency = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(points), len(points))).tocsr()
    laplacian = diags(np.asarray(adjacency.sum(axis=1)).ravel()) - adjacency
    lengths = np.linalg.norm(points[np.roll(boundary, -1)] - points[boundary], axis=1)
    arc = np.append(0, np.cumsum(lengths))
    radius = arc[-1] / math.tau
    angles = arc[:-1] / radius
    uv = np.empty((len(points), 2))
    uv[boundary] = radius * np.stack((np.cos(angles), np.sin(angles)), axis=1)
    inner = np.setdiff1d(np.arange(len(points)), boundary)
    if len(inner):
        uv[inner] = spsolve(laplacian[inner][:, inner], -laplacian[inner][:, boundary] @ uv[boundary])
    return uv


def _rest_triangles(points, triangles):
    a, b, c = np.moveaxis(points[triangles], 1, 0)
    first, second = b - a, c - a
    lengths = np.linalg.norm(first, axis=1)
    heights = np.linalg.norm(np.cross(first, second), axis=1) / np.maximum(EPS, lengths)
    if min(lengths) <= EPS or min(heights) <= EPS:
        raise UpholsteryError("Поверхность содержит вырожденный треугольник.")
    inverse = np.zeros((len(triangles), 2, 2))
    inverse[:, 0, 0] = 1 / lengths
    inverse[:, 1, 1] = 1 / heights
    inverse[:, 0, 1] = -(np.sum(first * second, axis=1) / lengths**2) / heights
    return inverse, np.sqrt(lengths * heights / 2)


def _arap_gradient(triangles, rest, weights, vertex_count):
    # Each pair of rows is one triangle's gradient, weighted by its rest area.
    local = np.concatenate((-rest.sum(axis=1, keepdims=True), rest), axis=1)
    values = (local.transpose(0, 2, 1) * weights[:, None, None]).reshape(-1)
    rows = np.repeat(np.arange(2 * len(triangles)), 3)
    cols = np.repeat(triangles, 2, axis=0).ravel()
    return coo_matrix((values, (rows, cols)), shape=(len(rows) // 3, vertex_count)).tocsc()[:, 1:]


def _rotations(coordinates, weights):
    jacobian = coordinates.reshape(-1, 2, 2).transpose(0, 2, 1)
    angle = np.arctan2(jacobian[:, 1, 0] - jacobian[:, 0, 1], jacobian[:, 0, 0] + jacobian[:, 1, 1])
    cosine, sine = np.cos(angle) * weights, np.sin(angle) * weights
    return np.column_stack((cosine, sine, -sine, cosine)).reshape(-1, 2)


def _rounded(points, triangles, loops, initial, rest, weights):
    try:
        _valid_pattern(initial, triangles, loops)
    except UpholsteryError:
        initial = _harmonic(points, triangles, loops[0])
        _valid_pattern(initial, triangles, loops)
    gradient = _arap_gradient(triangles, rest, weights, len(points))
    transpose = gradient.T
    solve = factorized((transpose @ gradient).tocsc())
    uv = initial - initial[0]
    for _ in range(60):
        rotations = _rotations(gradient @ uv[1:], weights)
        solution = solve(transpose @ rotations)
        difference = np.vstack(([0, 0], solution - uv[1:]))
        for step in (1, 0.5, 0.25, 0.125, 0.0625, 0.03125):
            candidate = uv + step * difference
            try:
                _valid_pattern(candidate, triangles, loops)
            except UpholsteryError:
                continue
            uv = candidate
            break
        else:
            break
        if np.max(np.abs(step * difference)) < FIT_TOLERANCE:
            break
    return uv


def _strain(uv, triangles, rest, grain):
    a, b, c = np.moveaxis(uv[triangles], 1, 0)
    deformation = np.linalg.inv(np.stack((b - a, c - a), axis=-1) @ rest)
    major, minor = np.linalg.svd(deformation, compute_uv=False).T
    cross_grain = np.array([-grain[1], grain[0]])
    warp, weft = deformation @ grain, deformation @ cross_grain
    cosine = np.sum(warp * weft, axis=1) / (np.linalg.norm(warp, axis=1) * np.linalg.norm(weft, axis=1))
    shear = abs(90 - np.degrees(np.arccos(np.clip(cosine, -1, 1))))
    return np.stack((np.maximum(0, major - 1) * 100, np.maximum(0, 1 - minor) * 100, shear), axis=-1)


def _seam_parts(path, local_edges):
    parts = []
    for a, b in pairwise(path):
        local = local_edges.get(_edge(a, b), ())
        options = [edge if a < b else edge[::-1] for edge in local]
        if parts and (last := parts[-1][-1]) is not None:
            for start, end in options:
                if start == last:
                    parts[-1].append(end)
                    break
            else:
                parts.append(list(options[0]) if options else [None])
        elif options:
            parts.append(list(options[0]))
    return [part for part in parts if len(part) >= 2]


def _sewing_banks(path, local_edges):
    banks = []
    for index, (a, b) in enumerate(pairwise(path)):
        options = [edge if a < b else edge[::-1] for edge in local_edges.get(_edge(a, b), ())]
        if index == 0:
            banks = [list(edge) for edge in options]
        else:
            continued = []
            for bank in banks:
                next_vertices = [end for start, end in options if start == bank[-1]]
                if len(next_vertices) == 1:
                    bank.append(next_vertices[0])
                    continued.append(bank)
                else:
                    continued.extend([*bank, vertex] for vertex in next_vertices)
            banks = continued
        if not banks:
            break
    return banks


@dataclass
class _SewingBank:
    panel: dict
    vertices: np.ndarray
    fractions: np.ndarray
    edge_ids: list

    def weights(self, start, end):
        start, end = sorted((start, end))
        low, high = self.fractions[:-1], self.fractions[1:]
        lengths = np.maximum(0, np.minimum(end, high) - np.maximum(start, low))
        return lengths / (high - low)

    def point(self, fraction):
        index = self.segment(fraction)
        start, end = self.fractions[index:index + 2]
        weight = (fraction - start) / (end - start)
        a, b = (np.asarray(self.panel["vertices"][vertex]) for vertex in self.vertices[index:index + 2])
        return (a * (1 - weight) + b * weight).tolist()

    def lengths(self):
        return np.linalg.norm(np.diff([self.panel["vertices"][index] for index in self.vertices], axis=0), axis=1)

    def segment(self, fraction):
        return min(len(self.vertices) - 2, max(0, np.searchsorted(self.fractions, fraction, side="right") - 1))

    def mark(self, fraction, identifier, label, **scope):
        return {"id": identifier, "point": self.point(fraction), "label": label,
                "edgeId": self.edge_ids[self.segment(fraction)], **scope}

    def spans(self, start, end):
        start, end = sorted((start, end))
        for (a, b), (low, high) in zip(pairwise(self.vertices), pairwise(self.fractions)):
            if start >= high or end <= low:
                continue
            left = (max(start, low) - low) / (high - low)
            right = (min(end, high) - low) / (high - low)
            if a > b:
                left, right = 1 - right, 1 - left
            yield (self.panel["id"], _edge(a, b)), (left, right)


def _edge_identifier(*identity):
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:32]


def _boundary_edges(points, facets, loops):
    coordinates = [[round(float(value), 7) or 0 for value in point] for point in points]
    occurrences = defaultdict(list)
    for index, loop in enumerate(loops):
        for a, b in pairwise([*loop, loop[0]]):
            key = tuple(sorted((tuple(coordinates[a]), tuple(coordinates[b]))))
            occurrences[key].append((index, a, b))
    adjacent = _edge_faces(facets) if any(len(records) > 1 for records in occurrences.values()) else {}
    edges = []
    for key, records in occurrences.items():
        for index, a, b in records:
            side = None
            if len(records) > 1:
                face, _, _ = adjacent[_edge(a, b)][0]
                side = coordinates[next(vertex for vertex in facets[face] if vertex not in (a, b))]
            reverse = coordinates[a] > coordinates[b]
            if reverse:
                a, b = b, a
            edges.append({"id": "edge:" + _edge_identifier(key, side), "loop": index, "vertices": [a, b],
                          "sourcePoints": [points[a].tolist(), points[b].tolist()], "reverseOnLoop": reverse})
    return edges


@dataclass
class _PanelGeometry:
    panel: dict
    rest: np.ndarray
    weights: np.ndarray
    facets: np.ndarray
    grain: list | None
    guides: list
    boundary: dict
    darts: dict

    @cached_property
    def edge_ids(self):
        return {_edge(*edge["vertices"]): edge["id"] for edge in self.panel["edges"]}

    def bank(self, vertices, fractions):
        return _SewingBank(self.panel, np.asarray(vertices), fractions,
                           [self.edge_ids[_edge(a, b)] for a, b in pairwise(vertices)])

    def optimize(self, targets):
        uv = np.asarray(self.panel["vertices"])
        uv -= uv[0].copy()
        loops = self.panel["loops"]
        gradient = _arap_gradient(self.facets, self.rest, self.weights, len(uv))
        cartesian = kron(gradient, eye(2), format="csc")
        transpose = cartesian.T
        base = transpose @ cartesian
        # Both terms have millimetre units. The seam term trades length error for fabric deformation.
        seam_weight = 2.

        def constraints(candidate):
            vectors = [candidate[b] - candidate[a] for a, b, _, _ in targets]
            lengths = [np.linalg.norm(vector, axis=1) for vector in vectors]
            residual = np.array([length @ factors - target
                                 for length, (_, _, factors, target) in zip(lengths, targets)])
            return vectors, lengths, residual

        def energy(candidate):
            mesh = gradient @ candidate[1:]
            difference = mesh - _rotations(mesh, self.weights)
            seam_error = constraints(candidate)[2]
            return np.sum(difference**2) + seam_weight**2 * np.sum(seam_error**2)

        current_energy = energy(uv)
        for _ in range(16):
            vectors, lengths, residual = constraints(uv)
            rows, cols, data = [], [], []
            for index, (vector, length, (a, b, weight, _)) in enumerate(zip(vectors, lengths, targets)):
                derivative = vector * (seam_weight * weight / np.maximum(length, EPS))[:, None]
                for vertices, sign in ((a, -1), (b, 1)):
                    selected = vertices != 0
                    cols.extend((2 * (vertices[selected, None] - 1) + np.arange(2)).ravel())
                    data.extend((sign * derivative[selected]).ravel())
                    rows.extend([index] * (2 * np.count_nonzero(selected)))
            penalty = coo_matrix((data, (rows, cols)), shape=(len(targets), 2 * (len(uv) - 1))).tocsc()
            current = uv[1:].ravel()
            mismatch = _rotations(gradient @ uv[1:], self.weights).ravel() - cartesian @ current
            delta = spsolve(base + penalty.T @ penalty, transpose @ mismatch - penalty.T @ (seam_weight * residual))
            direction = np.vstack(([0, 0], delta.reshape(-1, 2)))
            if np.max(np.abs(direction)) < FIT_TOLERANCE:
                break
            for step in (1, .5, .25, .125, .0625):
                candidate = uv + step * direction
                try:
                    _valid_pattern(candidate, self.facets, loops)
                except UpholsteryError:
                    continue
                score = energy(candidate)
                if score < current_energy:
                    uv, current_energy = candidate, score
                    break
            else:
                break
        _grain_line(uv, self.grain)
        self.panel["vertices"] = uv.tolist()

    def orient(self):
        uv = np.asarray(self.panel["vertices"])
        axis, grain_line = _grain_line(uv, self.grain)
        if grain_line is None:
            axis = _outline_axis(uv[self.panel["loops"][0]])
        # Columns are orthonormal and positively oriented, so this cannot scale or mirror the pattern.
        uv = uv @ np.column_stack((axis, [-axis[1], axis[0]]))
        uv -= uv.min(axis=0)
        grain, line = _grain_line(uv, self.grain)
        self.panel["vertices"] = uv.tolist()
        for guide, indices in zip(self.panel["guides"], self.guides):
            guide["points"] = uv[indices].tolist()
        for edge in self.panel["edges"]:
            edge["points"] = uv[edge["vertices"]].tolist()
        if line is not None:
            self.panel["grain"] = line
        self.panel["strain"] = _strain_records(_strain(uv, self.facets, self.rest, grain))


def _rightward(axis):
    if axis[0] < -EPS or abs(axis[0]) <= EPS and axis[1] < 0:
        return -axis
    return axis


def _outline_axis(outline):
    edges = np.roll(outline, -1, axis=0) - outline
    lengths = np.linalg.norm(edges, axis=1)
    perimeter = lengths.sum()
    # Coalesce sampled portions of a straight edge without changing any output vertex.
    simplified = Polygon(outline).simplify(min(.02, perimeter * 1e-5), preserve_topology=True)
    candidates = np.diff(np.asarray(simplified.exterior.coords), axis=0)
    candidate_lengths = np.linalg.norm(candidates, axis=1)
    longest = candidate_lengths.max()
    if longest >= perimeter / 16:
        tied = np.flatnonzero(np.isclose(candidate_lengths, longest, rtol=1e-9, atol=EPS))
        axes = [_rightward(candidates[index] / candidate_lengths[index]) for index in tied]
        # Equally long sides favor the least rotation in the current frame.
        return max(axes, key=lambda axis: (round(axis[0], 12), round(axis[1], 12)))

    # The major axis of the boundary, weighted by arc length, is stable on finely sampled curves.
    midpoints = outline + edges / 2
    centered = midpoints - np.average(midpoints, axis=0, weights=lengths)
    covariance = centered.T @ (centered * lengths[:, None]) + edges.T @ (edges * lengths[:, None]) / 12
    values, axes = np.linalg.eigh(covariance)
    if values[1] <= values[0] * 1.001:
        return np.array([1., 0.])
    return _rightward(axes[:, 1])


def _grain_line(uv, anchors):
    if anchors is None:
        return np.array([1., 0.]), None
    line = [weights @ uv[indices] for indices, weights in anchors]
    delta = line[1] - line[0]
    length = np.linalg.norm(delta)
    if length <= EPS:
        raise UpholsteryError("Точки долевой линии совпадают.")
    return delta / length, [point.tolist() for point in line]


def _grain_samples(panel, origin_ids, surface, points, facets, sources):
    if panel.get("grain") is None:
        return None
    grain = panel["grain"]
    if not isinstance(grain, list) or len(grain) != 2:
        raise UpholsteryError("Долевой линии нужны две опорные точки.")
    anchors = []
    for anchor in grain:
        if not isinstance(anchor, dict) or "vertex" in anchor:
            vertex = anchor.get("vertex") if isinstance(anchor, dict) else anchor
            vertex = _index(vertex, surface.original_count, "Вершина долевой линии отсутствует в модели.")
            if vertex not in origin_ids:
                raise UpholsteryError("Точки долевой линии должны принадлежать детали.")
            anchors.append(([origin_ids.index(vertex)], np.ones(1)))
            continue

        source = _index(anchor.get("triangle"), len(surface.original_triangles), "Грань долевой линии отсутствует в модели.")
        weights = _weights(anchor.get("barycentric"))
        triangle = surface.original_triangles[source]
        point = weights @ np.asarray([surface.vertices[vertex] for vertex in triangle])
        supporting = set.intersection(*(surface.support[vertex] for vertex, weight in zip(triangle, weights) if weight > 0))
        matches = []
        for facet, triangle_id in zip(facets, sources):
            if triangle_id not in supporting:
                continue
            a, b, c = points[facet]
            x, y, offset = b - a, c - a, point - a
            normal = np.cross(x, y)
            area = normal @ normal
            u = np.cross(offset, y) @ normal / area
            v = np.cross(x, offset) @ normal / area
            barycentric = np.array([1 - u - v, u, v])
            if min(barycentric) >= -EPS:
                barycentric = np.clip(barycentric, 0, 1)
                matches.append((triangle_id == source, facet, barycentric / barycentric.sum()))
        if not matches:
            raise UpholsteryError("Точки долевой линии должны принадлежать детали после разреза.")
        _, facet, barycentric = max(matches, key=lambda match: match[0])
        anchors.append((facet, barycentric))
    return anchors


def _seam_cuts(seams, *, excluded_darts=False):
    return {_edge(a, b) for seam, path in seams
            if seam["kind"] == "seam" or (seam["kind"] == "dart" and not excluded_darts)
            for a, b in pairwise(path)}


def _flatten(panel, surface, triangles, source_triangles, seams, excluded_darts=False, resolved=None, placement=None):
    panel_cuts = _seam_cuts(seams, excluded_darts=excluded_darts)
    points, facets, sources, loops, boundary, origin_ids = _cut_panel(surface, triangles, source_triangles, panel, panel_cuts,
                                                                      resolved, placement)
    rest, weights = _rest_triangles(points, facets)
    loops.sort(key=lambda loop: sum(math.dist(points[a], points[b]) for a, b in pairwise([*loop, loop[0]])), reverse=True)
    projected, height = _project(points, facets)
    if height <= FIT_TOLERANCE:
        uv, kind = projected, "flat"
    else:
        uv, difference = _unfold(points, facets)
        if difference <= FIT_TOLERANCE:
            kind = "developable"
        else:
            uv, kind = _rounded(points, facets, loops, projected, rest, weights), "rounded"
    _valid_pattern(uv, facets, loops)
    grain_samples = _grain_samples(panel, origin_ids, surface, points, facets, sources)
    _grain_line(uv, grain_samples)

    source_to_local, source_boundary = defaultdict(list), defaultdict(list)
    for local in _edge_faces(facets):
        a, b = local
        u, v = origin_ids[a], origin_ids[b]
        if u != v:
            source_to_local[_edge(u, v)].append((a, b) if u < v else (b, a))
    for key, edges in boundary.items():
        source_boundary[key] = [(a, b) if origin_ids[a] < origin_ids[b] else (b, a) for a, b in edges]
    guides, guide_indices, sewing_lines, darts = [], [], {}, {}
    for seam, path in seams:
        if seam.get("placementFor"):
            continue
        for part in _seam_parts(path, source_to_local):
            guides.append({"id": seam["id"], "kind": seam["kind"]})
            guide_indices.append(part)
        if seam["kind"] == "slash" or (seam["kind"] == "dart" and excluded_darts):
            continue
        banks = _sewing_banks(path, source_boundary)
        if not banks:
            continue
        seam_points = [surface.vertices[vertex] for vertex in path]
        lengths = np.array([0, *[math.dist(a, b) for a, b in pairwise(seam_points)]])
        fractions = np.cumsum(lengths) / lengths.sum()
        if seam["kind"] == "seam":
            sewing_lines[seam["id"]] = [(np.asarray(bank), fractions) for bank in banks]
        elif len(banks) == 2:
            darts[seam["id"]] = [(np.asarray(bank), fractions) for bank in banks]
    edges = _boundary_edges(points, facets, loops)
    edges_by_loop = defaultdict(list)
    for edge in edges:
        edges_by_loop[edge["loop"]].append(edge["id"])
    holes = [{"id": "hole:" + _edge_identifier(sorted(edge_ids)), "loop": index, "edgeIds": edge_ids}
             for index, edge_ids in edges_by_loop.items() if index]
    result = {"id": panel["id"], "name": panel.get("name") or panel["id"], "kind": kind,
              "vertices": uv.tolist(), "triangles": placement["triangles"] if placement is not None else facets.tolist(),
              "sourceTriangles": sources, "sourceVertices": placement["vertices"] if placement is not None else points.tolist(),
              "loops": loops, "marks": [], "guides": guides, "edges": edges, "holes": holes}
    geometry = _PanelGeometry(result, rest, weights, facets, grain_samples, guide_indices, source_boundary, darts)
    return result, sewing_lines, geometry


def _strain_records(strain):
    return [dict(zip(("stretch", "compression", "shear"), row)) for row in strain.tolist()]


def _number(value, minimum, maximum, description):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not minimum <= value <= maximum:
        raise UpholsteryError(description)
    return value


def _records(state, key):
    records = state.get(key, [])
    if not isinstance(records, list) or len(records) > 1000:
        raise UpholsteryError("Некорректный список деталей, швов или пар.")
    identifiers = set()
    for record in records:
        identifier = record.get("id") if isinstance(record, dict) else None
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128 or identifier in identifiers:
            raise UpholsteryError("Идентификаторы деталей, швов и пар должны быть уникальными и непустыми.")
        identifiers.add(identifier)
    return records


def _record_marks(record, namespace):
    marks = record.get("marks", [])
    if not isinstance(marks, list) or len(marks) > 100:
        raise UpholsteryError("Некорректный список контрольных меток.")
    marks = [_number(mark, 0, 1, "Положение метки должно быть от 0 до 1.") for mark in marks]
    if len(marks) != len(set(marks)):
        raise UpholsteryError("Положения контрольных меток повторяются.")
    identifiers = record.get("markIds")
    if identifiers is None:
        identifiers = [f"{namespace}:" + _edge_identifier(record["id"], float(mark or 0).hex()) for mark in marks]
    if (not isinstance(identifiers, list) or len(identifiers) != len(marks)
            or any(not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128
                   for identifier in identifiers) or len(set(identifiers)) != len(identifiers)):
        raise UpholsteryError("Идентификаторы контрольных меток должны быть уникальными и непустыми.")
    return sorted(zip(marks, identifiers))


def _bank_id(bank):
    return "bank:" + _edge_identifier(bank.edge_ids)


def _pair(pair, sewing_lines, edge_banks, occupied):
    sides = []
    used_banks = defaultdict(int)
    explicit = False
    for side in "ab":
        endpoint = pair.get(side)
        if not isinstance(endpoint, dict):
            raise UpholsteryError("Выберите два участка на границах деталей.")
        panel_id, seam_id, edge_id = (endpoint.get(key) for key in ("panelId", "seamId", "edgeId"))
        if not isinstance(panel_id, str):
            raise UpholsteryError("Выберите два участка на границах деталей.")
        span = endpoint.get("range", [0, 1])
        if not isinstance(span, list) or len(span) != 2:
            raise UpholsteryError("Для участка шва укажите начало и конец.")
        start, end = (_number(value, 0, 1, "Границы участка должны быть от 0 до 1.") for value in span)
        if abs(end - start) <= EPS:
            raise UpholsteryError("Выберите участок шва ненулевой длины.")
        if endpoint.get("reverse"):
            start, end = end, start
        explicit |= any(key in endpoint for key in ("edgeId", "range", "bank", "bankId"))
        if isinstance(edge_id, str) and seam_id is None:
            bank = edge_banks.get((panel_id, edge_id))
            if bank is None:
                raise UpholsteryError("Граница детали изменилась. Выберите участок шва заново.")
        elif isinstance(seam_id, str) and edge_id is None:
            key = panel_id, seam_id
            banks = sewing_lines.get(key, ())
            if not banks:
                raise UpholsteryError("Указанный шов должен целиком лежать на границе выбранной детали.")
            if "bankId" in endpoint:
                bank = next((value for value in banks if _bank_id(value) == endpoint["bankId"]), None)
                if bank is None:
                    raise UpholsteryError("Сторона шва изменилась. Выберите участок заново.")
            else:
                index = endpoint.get("bank", used_banks[key])
                if type(index) is not int or not 0 <= index < len(banks):
                    raise UpholsteryError("Выберите существующую сторону разреза для сшивания.")
                bank = banks[index]
            used_banks[key] += 1
        else:
            raise UpholsteryError("Выберите два участка на границах деталей.")
        sides.append((bank, start, end))
    spans = defaultdict(list)
    for bank, start, end in sides:
        for key, (left, right) in bank.spans(start, end):
            previous = spans[key] + [(low, high) for low, high, declared in occupied.get(key, ()) if explicit or declared]
            if any(min(right, high) - max(left, low) > EPS for low, high in previous):
                raise UpholsteryError("Участки швов должны быть разными и не перекрываться с другими парами.")
            spans[key].append((left, right))
    marks = _record_marks(pair, "pair")
    fractions = sorted({0, *(fraction for fraction, _ in marks), 1})
    ease = pair.get("ease", [])
    if not isinstance(ease, list) or len(ease) > len(fractions) - 1:
        raise UpholsteryError("Для одного интервала задаётся одно значение посадки.")
    ease = [_number(value, -1e6, 1e6, "Посадку задают числом в миллиметрах.") for value in ease]
    ease += [0] * (len(fractions) - 1 - len(ease))
    intervals = [[bank.weights(start + a * (end - start), start + b * (end - start))
                  for a, b in pairwise(fractions)] for bank, start, end in sides]
    for key, segments in spans.items():
        occupied[key].extend((low, high, explicit) for low, high in segments)
    return sides, marks, ease, intervals


def _stitch(identifier, sides):
    return {"id": identifier, **{side: {"panelId": bank.panel["id"], "vertices": bank.vertices.tolist(),
                                       "fractions": bank.fractions.tolist(), "range": [start, end]}
                                 for side, (bank, start, end) in zip("ab", sides)}}


def _fit_open_darts(result, geometries, occupied):
    used = set(occupied)
    invalid = {issue.get("panelId") for issue in result["diagnostics"] if issue["severity"] == "error"}
    for panel_id, geometry in geometries.items():
        if panel_id in invalid:
            continue
        for seam_id, mappings in geometry.darts.items():
            banks = [geometry.bank(vertices, fractions) for vertices, fractions in mappings]
            a, b = ({key for key, _ in bank.spans(0, 1)} for bank in banks)
            if not a.isdisjoint(b) or not used.isdisjoint(a | b):
                continue
            used.update(a | b)
            identifier = "dart:" + _edge_identifier(panel_id, seam_id)
            result["fitStitches"].append({**_stitch(identifier, [(bank, 0, 1) for bank in banks]),
                                          "kind": "dart", "seamId": seam_id})


def _render_pair(pair, label, spec):
    sides, marks, ease, intervals = spec
    lengths = [factors @ bank.lengths() for (bank, _, _), factors in zip(sides, intervals)]
    comparisons = [{"a": float(a), "b": float(b), "delta": float(a - b), "ease": target}
                   for a, b, target in zip(*lengths, ease)]
    rendered = []
    for index, (fraction, identifier) in enumerate(marks, 1):
        name = f"{label}.{index}"
        rendered.append({"id": identifier, "fraction": fraction, "label": name})
        for bank, start, end in sides:
            mark = bank.mark(start + fraction * (end - start), identifier, name, pairId=pair["id"])
            bank.panel["marks"].append(mark)
    return {"id": pair["id"], "intervals": comparisons, "marks": rendered}


def _automatic_notches(result, pairs, fabric_layers, sewing_lines, occupied):
    paired = {end["seamId"] for pair in pairs for side in "ab"
              if isinstance(end := pair.get(side), dict) and isinstance(end.get("seamId"), str)}
    invalid = {issue.get("panelId") for issue in result["diagnostics"] if issue["severity"] == "error"}
    for index, seam in enumerate(result["seams"], 1):
        panels = seam["panelIds"]
        if seam["nodeId"] not in fabric_layers or seam["id"] in paired or len(panels) != 2 or not invalid.isdisjoint(panels):
            continue
        banks = [sewing_lines[(panel, seam["id"])] for panel in panels]
        if all(len(side) == 1 for side in banks):
            if any(occupied.get(key) for (bank,) in banks for key, _ in bank.spans(0, 1)):
                continue
            for bank, in banks:
                identifier = "seam:" + _edge_identifier(seam["id"], .5)
                bank.panel["marks"].append(bank.mark(.5, identifier, f"шва {index}", seamId=seam["id"]))


def _join_paths(edges, vertices):
    # Each endpoint contains the welded vertex and its local vertex on each bank.
    neighbors = defaultdict(set)
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    def order(point):
        return (*vertices[point[0]], *point)
    unseen = set(neighbors)
    while unseen:
        start = unseen.pop()
        component = [start]
        for point in component:
            for other in neighbors[point]:
                if other in unseen:
                    unseen.remove(other)
                    component.append(other)
        if any(len(neighbors[point]) > 2 for point in component):
            continue
        ends = [point for point in component if len(neighbors[point]) == 1]
        start = min(ends or component, key=order)
        path = [start, min(neighbors[start], key=order)]
        while path[-1] != start:
            onward = neighbors[path[-1]] - {path[-2]}
            if not onward:
                break
            path.append(next(iter(onward)))
        yield path


def _panel_token(identifier):
    return hashlib.sha256(identifier.encode()).hexdigest()[:16]


def _common_boundaries(node_id, surface, triangles, owners, valid):
    by_pair = defaultdict(list)
    for edge, sides in _edge_faces(triangles).items():
        if len(sides) != 2:
            continue
        a, b = (side[0] for side in sides)
        first, second = owners.get(a), owners.get(b)
        if first is None or second is None or first is second:
            continue
        ids = tuple(sorted((first["id"], second["id"])))
        if any(identifier not in valid for identifier in ids):
            continue
        banks = [valid[identifier].boundary.get(edge, ()) for identifier in ids]
        if any(len(bank) != 1 for bank in banks):
            continue
        # Coincident coplanar sheets on the same side of an edge are not adjoining panels.
        p, q = (surface.vertices[i] for i in edge)
        normals = [np.cross(q - p, surface.vertices[next(i for i in triangles[face] if i not in edge)] - p)
                   for face in (a, b)]
        if normals[0] @ normals[1] >= (1 - 1e-12) * np.linalg.norm(normals[0]) * np.linalg.norm(normals[1]):
            continue
        by_pair[ids].append(tuple(zip(edge, banks[0][0], banks[1][0])))

    found, ambiguous = {}, set()
    for ids, edges in by_pair.items():
        for path in _join_paths(edges, surface.vertices):
            points = [surface.vertices[step[0]].tolist() for step in path]
            identity = json.dumps([node_id, ids, points], ensure_ascii=False, separators=(",", ":"))
            key = "join:" + "".join(map(_panel_token, ids)) + hashlib.sha256(identity.encode()).hexdigest()[:32]
            if key in found:
                ambiguous.add(key)
                continue
            lengths = np.cumsum([0., *[math.dist(p, q) for p, q in pairwise(points)]])
            if any(delta <= EPS for delta in np.diff(lengths)):
                continue
            fractions = lengths / lengths[-1]
            banks = [valid[identifier].bank([step[index] for step in path], fractions)
                     for index, identifier in enumerate(ids, 1)]
            found[key] = ({"id": key, "nodeId": node_id, "panelIds": list(ids), "points": points}, banks)
    return {key: found[key] for key in sorted(found.keys() - ambiguous)}


def _apply_joins(result, saved, candidates, panels):
    for index, join in enumerate(saved, 1):
        identifier = join["id"]
        candidate = candidates.get(identifier)
        if candidate is None:
            # The owner tokens let selective transfer block the affected surviving panels.
            tokens = {identifier[5:21], identifier[21:37]} if identifier.startswith("join:") and len(identifier) == 69 else set()
            affected = [p["id"] for p in panels if _panel_token(p["id"]) in tokens]
            for panel_id in affected or [None]:
                _diagnostic(result, "error", "join_unavailable", "Общая граница изменилась. Выберите место сшивания заново.",
                            joinId=identifier, **({"panelId": panel_id} if panel_id else {}))
            continue
        record, banks = candidate
        try:
            marks = _record_marks(join, "join")
        except UpholsteryError as error:
            for panel_id in record["panelIds"]:
                _diagnostic(result, "error", "join_invalid", str(error), joinId=identifier, panelId=panel_id)
            continue
        result["fitStitches"].append(_stitch(identifier, [(bank, 0, 1) for bank in banks]))
        rendered = []
        for number, (fraction, mark_id) in enumerate(marks, 1):
            points = [bank.point(fraction) for bank in banks]
            existing = [next((mark for mark in bank.panel["marks"] if math.dist(point, mark["point"]) <= FIT_TOLERANCE), None)
                        for bank, point in zip(banks, points)]
            label = next((mark["label"] for mark in existing if mark), f"стыка {index}.{number}")
            rendered.append({"id": mark_id, "fraction": fraction, "label": label,
                             "panelMarkIds": [mark["id"] if mark else mark_id for mark in existing]})
            for bank, point, previous in zip(banks, points, existing):
                if previous is None:
                    bank.panel["marks"].append(bank.mark(fraction, mark_id, label, joinId=identifier))
        result["confirmedJoins"].append({"id": identifier, "panelIds": record["panelIds"], "marks": rendered})


def _sewing_targets(specs, rounded):
    targets = defaultdict(list)
    for _, _, (sides, _, ease, intervals) in specs:
        lengths = [factors @ bank.lengths() for (bank, _, _), factors in zip(sides, intervals)]
        mismatch = lengths[0] - lengths[1] - ease
        movable = sum(bank.panel["id"] in rounded for bank, _, _ in sides)
        if not movable:
            continue
        for ((bank, _, _), factors, current, sign) in zip(sides, intervals, lengths, (-1, 1)):
            identifier = bank.panel["id"]
            if identifier not in rounded:
                continue
            for interval, desired in zip(factors, current + sign * mismatch / movable):
                if desired > EPS:
                    indices = interval > 0
                    targets[identifier].append((bank.vertices[:-1][indices], bank.vertices[1:][indices], interval[indices], desired))
    return targets


def _diagnostic(result, severity, code, message, *, label=None, detail=None, **scope):
    result["diagnostics"].append({"severity": severity, "code": code, "message": message, **scope,
                                  **({"detail": detail} if detail else {})})
    result["warnings"].append(f"{label}: {message}" if label else message)


def _seam_path(surface, seam):
    if seam.get("kind") not in ("seam", "dart", "slash") or (seam["kind"] != "seam" and seam.get("closed")):
        raise UpholsteryError("Укажите шов, открытую вытачку или открытую рассечку.")
    return surface.seam(seam)


def _expanded_path(path, routes):
    points = [path[0]]
    for edge in pairwise(path):
        points.extend(routes[edge][1:])
    return points


def _cover_surface(model, state, node_id):
    layer = next((layer for layer in state.get("fabricLayers", []) if f"fabric:{layer['id']}" == node_id), None)
    if layer is None:
        raise UpholsteryError("Выберите существующий чехол.")
    nodes = {node["id"]: node for node in model["nodes"]}
    for part in layer["surfaces"]:
        node = nodes.get(part["nodeId"])
        if node is None or set(part["faces"]).difference(node["faces"]):
            raise UpholsteryError("В модели отсутствуют исходные грани чехла. Проверьте выбор ткани.")
    raw = _fabric_node(layer, nodes)
    if not raw["triangles"]:
        raise UpholsteryError("В чехле нет поверхности. Проверьте выбранные грани ткани.")
    return _Surface(raw, join_coplanar=layer.get("joinCoplanar") is True, join_folded=layer.get("joinFolded") is True)


class _CoverTopology:
    def __init__(self, model, state, node_id, contour=None, *, surface_route=False):
        self.surface = _cover_surface(model, state, node_id)
        try:
            paths = [(seam, _seam_path(self.surface, seam)) for seam in _records(state, "seams") if seam.get("nodeId") == node_id]
            if contour is not None:
                if len({self.surface.anchor(anchor) for anchor in contour}) != len(contour):
                    raise UpholsteryError("Опорные точки контура совпадают. Передвиньте или удалите повторяющуюся точку.")
                draft = {"kind": "seam", "closed": True, "anchors": contour, "surfaceRoute": surface_route}
                paths.append((draft, self.surface.seam(draft)))
            self.triangles, self.sources, routes = self.surface.triangulate()
        except GEOSException as error:
            raise UpholsteryError("Не удалось построить поверхность. Проверьте пересечения швов чехла.") from error
        self.seams = [(seam, _expanded_path(path, routes)) for seam, path in paths]
        self.cuts = _seam_cuts(self.seams)
        self.incidence = _edge_faces(self.triangles)
        self.dart_cuts = {_edge(a, b) for seam, path in self.seams if seam["kind"] == "dart" for a, b in pairwise(path)}

    @cached_property
    def neighbors(self):
        return _panel_neighbors(self.incidence, self.cuts)

    @cached_property
    def components(self):
        return _components(range(len(self.triangles)), self.neighbors)

    def coverage(self, panel, *, ignoring=None):
        seams = self.seams if ignoring is None else [(seam, path) for seam, path in self.seams if seam["id"] != ignoring]
        cuts = self.cuts if ignoring is None else _seam_cuts(seams)
        if panel.get("faces") == [] or "faces" not in panel:
            selected = None
            incidence = self.incidence
            components = self.components if ignoring is None else _components(range(len(self.triangles)), _panel_neighbors(incidence, cuts))
        else:
            selected = _selected_facets(self.surface, self.sources, panel)
            incidence = _edge_faces(self.triangles, selected)
            components = _components(selected, _panel_neighbors(incidence, cuts))
        chosen = _seed_component(components, self.surface, self.triangles, self.sources, panel)
        if self.dart_cuts:
            # calculate can retry without opening darts. Resolve its actual choice when a dart
            # could join this part to a different region in the same selected face mask.
            domain = set(selected) if selected is not None else None
            for edge in self.dart_cuts & cuts:
                sides = [face for face, _, _ in self.incidence[edge] if domain is None or face in domain]
                if any(face in chosen for face in sides) and any(face not in chosen for face in sides):
                    try:
                        _flatten(panel, self.surface, self.triangles, self.sources, seams)
                    except UpholsteryError:
                        seam_cuts = _seam_cuts(seams, excluded_darts=True)
                        neighbors = _panel_neighbors(incidence, seam_cuts)
                        opened = _components(range(len(self.triangles)) if selected is None else selected, neighbors)
                        chosen = _seed_component(opened, self.surface, self.triangles, self.sources, panel)
                        _flatten(panel, self.surface, self.triangles, self.sources, seams, excluded_darts=True)
                    break
        return chosen

    def interior_seed(self, component):
        seed_facet = next((i for i in component if self.triangles[i] == self.surface.original_triangles[self.sources[i]]), None)
        if seed_facet is None:
            # An interior point of a refined facet identifies its side of an intra-triangle seam.
            def area(i):
                a, b, c = (self.surface.vertices[v] for v in self.triangles[i])
                return np.linalg.norm(np.cross(b - a, c - a))
            seed_facet = max(component, key=area)
        source = self.sources[seed_facet]
        weights = [1 / 3] * 3
        if self.triangles[seed_facet] != self.surface.original_triangles[source]:
            a, b, c = (self.surface.vertices[v] for v in self.surface.original_triangles[source])
            point = sum((self.surface.vertices[v] - a for v in self.triangles[seed_facet])) / 3
            u, v = np.linalg.lstsq(np.column_stack((b - a, c - a)), point, rcond=None)[0]
            weights = [float(1 - u - v), float(u), float(v)]
        return {"triangle": source, "barycentric": weights}

    def descriptor(self, component, occupied):
        # Uncut neighbours already claimed by a manual selection must be excluded by face ID.
        touches_claim = any(neighbor in occupied for face in component for neighbor, _, _ in self.neighbors[face])
        faces = list(dict.fromkeys(self.surface.faces[self.sources[i]] for i in sorted(component))) if touches_claim else []
        seed = self.interior_seed(component)
        descriptor = {"faces": faces, "seed": seed["triangle"], "seedBarycentric": seed["barycentric"]}
        if self.coverage({"id": "remaining", **descriptor}).keys() != component.keys():
            raise UpholsteryError("Остаток пересекает уже выбранные грани. Добавьте разделительный шов или уточните границы деталей.")
        try:
            _cut_component(self.surface, self.triangles, self.sources, self.cuts, self.incidence, self.neighbors, component)
        except UpholsteryError as error:
            if not self.dart_cuts:
                raise UpholsteryError(f"{error} Проверьте границы и добавьте разделительный шов.") from error
            seam_cuts = _seam_cuts(self.seams, excluded_darts=True)
            _cut_panel(self.surface, self.triangles, self.sources, descriptor, seam_cuts)
        return descriptor


def _overlap_error(panel):
    return UpholsteryError(f"Часть чехла пересекается с деталью «{panel.get('name') or panel['id']}». Уточните выбранные грани или положение шва.")


def route_contour(model, state, node_id, anchors):
    """Resolve each open contour leg on the surface without building the final placement."""
    if not isinstance(anchors, list) or not 2 <= len(anchors) <= 2048:
        raise UpholsteryError("Укажите от 2 до 2048 опорных точек контура.")
    surface = _cover_surface(model, state, node_id)
    for seam in _records(state, "seams"):
        if seam.get("nodeId") == node_id:
            _seam_path(surface, seam)
    vertices = [surface.anchor(anchor) for anchor in anchors]
    paths = [[surface.vertices[vertex].tolist() for vertex in surface._route(a, b, surface_route=True)]
             for a, b in pairwise(vertices)]
    return {"paths": paths}


def preview_contour(model, state, node_id, anchors, seed=None, *, surface_route=False):
    if not isinstance(anchors, list) or len(anchors) < 3:
        raise UpholsteryError("Для замкнутого контура нужны не менее трёх разных точек.")
    topology = _CoverTopology(model, state, node_id, contour=anchors, surface_route=surface_route)
    path = topology.seams[-1][1]
    if path[0] != path[-1] or len(set(path[:-1])) != len(path) - 1:
        raise UpholsteryError("Контур пересекает или повторяет сам себя. Передвиньте опорные точки.")
    border = {_edge(a, b) for a, b in pairwise(path)}
    components = topology.components
    owner = {facet: i for i, component in enumerate(components) for facet in component}
    touching = defaultdict(int)
    for edge in border:
        refs = topology.incidence[edge]
        sides = {owner[facet] for facet, _, _ in refs}
        if not refs or len(sides) != len(refs):
            raise UpholsteryError("Контур не отделяет область ткани. Измените его положение или добавьте разделительный шов.")
        for side in sides:
            touching[side] += 1
    if seed is None:
        candidates = [i for i in touching if touching[i] == len(border)]
        if not candidates:
            raise UpholsteryError("Швы делят область контура на части. Укажите точку внутри нужной части.")
        # A patch surrounded only by the new border wins even when the outside strip is smaller.
        extra_boundary = {owner[facet] for edge, refs in topology.incidence.items()
                          if edge not in border and (len(refs) == 1 or edge in topology.cuts)
                          for facet, _, _ in refs}
        candidates = sorted(set(candidates) - extra_boundary) or sorted(candidates)
        if len(candidates) > 1:
            vertices = np.asarray(topology.surface.vertices)
            triangles = np.asarray(topology.triangles)

            def area(i):
                points = vertices[triangles[list(components[i])]]
                return np.linalg.norm(np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0]), axis=1).sum()

            side = min(candidates, key=area)
        else:
            side = candidates[0]
        seed = topology.interior_seed(components[side])
    elif not isinstance(seed, dict):
        raise UpholsteryError("Укажите точку на поверхности ткани.")
    source = _index(seed.get("triangle"), len(topology.surface.original_triangles), "Точка выбора отсутствует на ткани.")
    weights = _weights(seed.get("barycentric"))
    point = weights @ [topology.surface.vertices[v] for v in topology.surface.original_triangles[source]]
    for edge in border:
        if not any(topology.sources[facet] == source for facet, _, _ in topology.incidence[edge]):
            continue
        a, b = (topology.surface.vertices[v] for v in edge)
        delta = b - a
        t = np.clip((point - a) @ delta / (delta @ delta), 0, 1)
        if np.linalg.norm(point - a - t * delta) <= EPS:
            raise UpholsteryError("Точка лежит на контуре. Укажите точку внутри нужной области.")
    chosen = _seed_component([components[i] for i in touching], topology.surface, topology.triangles, topology.sources,
                             {"seed": source, "seedBarycentric": seed["barycentric"]})
    placement = {"seed": {"triangle": source, "barycentric": seed["barycentric"]}}
    if surface_route:
        placement["outline"] = [topology.surface.vertices[vertex].tolist() for vertex in path]
    _cut_component(topology.surface, topology.triangles, topology.sources, topology.cuts,
                   topology.incidence, topology.neighbors, chosen, placement)
    return placement


def selected_panel_regions(model, state, node_id, faces):
    if not isinstance(faces, list) or not faces:
        raise UpholsteryError("Выберите поверхности для ткани.")
    topology = _CoverTopology(model, state, node_id)
    selected = _selected_facets(topology.surface, topology.sources, {"faces": faces})
    incidence = _edge_faces(topology.triangles, selected)
    components = _components(selected, _panel_neighbors(incidence, topology.cuts))
    if len(_records(state, "panels")) + len(components) > 120:
        raise UpholsteryError("В разметке может быть не более 120 деталей. Уточните выбор ткани.")
    regions = []
    for component in components:
        face_ids = list(dict.fromkeys(topology.surface.faces[topology.sources[i]] for i in sorted(component)))
        seed = topology.interior_seed(component)
        regions.append({"faces": face_ids, "seed": seed["triangle"], "seedBarycentric": seed["barycentric"]})
    return regions


def check_panel_placement(result, panel_ids):
    """Reject area overlap within a new batch or with previously assigned cloth on another layer."""
    if not isinstance(panel_ids, list) or len(panel_ids) > 120 or any(not isinstance(item, str) for item in panel_ids):
        raise UpholsteryError("Некорректный выбор деталей для проверки.")
    selected = set(panel_ids)
    placements = result["placements"]
    targets = [item for item in placements if item["id"] in selected]
    if not targets:
        return
    indexed, owners, bounds = [], [], []
    for item in placements:
        if len(targets) == 1 and item["id"] in selected:
            continue
        for triangle in np.asarray(item["vertices"], dtype=float)[item["triangles"]]:
            indexed.append(triangle)
            owners.append(item)
            lo, hi = triangle[:, :2].min(axis=0) - EPS, triangle[:, :2].max(axis=0) + EPS
            bounds.append(box(*lo, *hi))
    if not indexed:
        return
    tree = STRtree(bounds)
    for item in targets:
        for triangle in np.asarray(item["vertices"], dtype=float)[item["triangles"]]:
            lo, hi = triangle[:, :2].min(axis=0) - EPS, triangle[:, :2].max(axis=0) + EPS
            candidates = []
            for index in tree.query(box(*lo, *hi)):
                owner = owners[index]
                if owner["id"] in selected:
                    if owner["id"] <= item["id"]:
                        continue
                elif owner["nodeId"] == item["nodeId"]:
                    continue
                candidates.append(index)
            if not candidates:
                continue
            origin, b, c = triangle
            normal = np.cross(b - origin, c - origin)
            magnitude = np.linalg.norm(normal)
            if magnitude <= EPS:
                continue
            normal /= magnitude
            axes = [axis for axis in range(3) if axis != np.argmax(np.abs(normal))]
            shape = Polygon(triangle[:, axes])
            for index in candidates:
                other = indexed[index]
                if (np.max(np.abs((other - origin) @ normal)) <= FIT_TOLERANCE
                        and shape.intersection(Polygon(other[:, axes])).area > FIT_TOLERANCE**2):
                    if owners[index]["id"] in selected:
                        raise UpholsteryError("Выбранные участки ткани пересекаются. Уточните выделение.")
                    raise UpholsteryError("На выбранных гранях уже лежит ткань. Уточните выделение.")


def remaining_panels(model, state, node_id):
    topology = _CoverTopology(model, state, node_id)
    panels = _records(state, "panels")
    occupied = set()
    for panel in panels:
        if panel.get("nodeId") == node_id:
            region = topology.coverage(panel)
            if not occupied.isdisjoint(region):
                raise _overlap_error(panel)
            occupied.update(region)
    free = [i for i in range(len(topology.triangles)) if i not in occupied]
    components = _components(free, topology.neighbors)
    if len(panels) + len(components) > 120:
        raise UpholsteryError("В разметке может быть не более 120 деталей. Объедините части или измените швы чехла.")
    return [topology.descriptor(component, occupied) for component in components]


def check_cover_panel(model, state, node_id, panel_id):
    topology = _CoverTopology(model, state, node_id)
    panels = [panel for panel in _records(state, "panels") if panel.get("nodeId") == node_id]
    candidate = next((panel for panel in panels if panel["id"] == panel_id), None)
    if candidate is None:
        raise UpholsteryError("Выберите деталь этого чехла.")
    selected = topology.coverage(candidate).keys()
    for panel in panels:
        if panel is not candidate and not selected.isdisjoint(topology.coverage(panel)):
            raise _overlap_error(panel)


def _check_cut_links(state, before, after):
    old = {part["id"]: part for part in before["panels"]}
    new = {part["id"]: part for part in after["panels"]}
    lost_links = any({item["id"] for item in before[key]} - {item["id"] for item in after[key]}
                     for key in ("pairs", "confirmedJoins"))
    if lost_links:
        raise UpholsteryError("Линия изменяет сохранённый стык или пару швов. Выберите другую линию.")
    if old.keys() - new.keys():
        raise UpholsteryError("Линия нарушает существующее лекало. Выберите другую линию.")
    saved_settings = (state.get("transferSettings") or {}).get("panels") or {}
    fixed_edges = (state.get("fit") or {}).get("fixedEdges") or []
    for record in _records(state, "panels"):
        identifier = record["id"]
        if identifier not in old:
            continue
        previous, current = old[identifier], new[identifier]
        old_edges = {edge["id"] for edge in previous["edges"]}
        new_edges = {edge["id"] for edge in current["edges"]}
        saved_edges = {notch["edgeId"] for notch in record.get("notches", [])}
        settings = saved_settings.get(identifier, {}) if isinstance(saved_settings, dict) else {}
        if isinstance(settings, dict):
            allowance_edges = settings.get("edgeAllowances")
            if isinstance(allowance_edges, dict):
                saved_edges.update(allowance_edges)
            allowance_holes = settings.get("holeAllowances")
            if isinstance(allowance_holes, dict):
                old_holes = {hole["id"] for hole in previous["holes"]}
                new_holes = {hole["id"] for hole in current["holes"]}
                if (allowance_holes.keys() & old_holes) - new_holes:
                    raise UpholsteryError("Линия изменяет отверстие с сохранённым припуском. Выберите другую линию.")
        if isinstance(fixed_edges, list):
            for edge in fixed_edges:
                if not isinstance(edge, dict) or edge.get("panelId") != identifier:
                    continue
                if isinstance(edge.get("edgeId"), str):
                    saved_edges.add(edge["edgeId"])
                elif _indexed_edge(previous, edge) != _indexed_edge(current, edge):
                    raise UpholsteryError("Линия изменяет сохранённую кромку крепления. Выберите другую линию.")
        if (saved_edges & old_edges) - new_edges:
            raise UpholsteryError("Линия изменяет кромку с насечкой, припуском или креплением. Выберите другую линию.")
        previous_slashes = {guide["id"] for guide in previous["guides"] if guide["kind"] == "slash"}
        current_slashes = {guide["id"] for guide in current["guides"] if guide["kind"] == "slash"}
        if previous_slashes - current_slashes:
            raise UpholsteryError("Линия переносит существующую рассечку на другую деталь. Выберите другую линию.")


def _indexed_edge(pattern, edge):
    loop, index = edge.get("loop", 0), edge.get("index")
    if type(loop) is not int or type(index) is not int or not 0 <= loop < len(pattern["loops"]):
        return None
    ring = pattern["loops"][loop]
    if not 0 <= index < len(ring):
        return None
    return sorted(pattern["sourceVertices"][ring[i % len(ring)]] for i in (index, index + 1))


def edit_cover_cut(model, state, panel_id, anchors, kind):
    if kind not in ("seam", "slash") or not isinstance(anchors, list) or not 2 <= len(anchors) <= 2048:
        raise UpholsteryError("Укажите открытую линию шва или рассечки из 2–2048 точек.")
    panels, seams = (_records(state, key) for key in ("panels", "seams"))
    panel = next((record for record in panels if record["id"] == panel_id), None)
    if panel is None or not str(panel.get("nodeId", "")).startswith("fabric:"):
        raise UpholsteryError("Выберите назначенную деталь ткани.")
    if len(seams) >= 400 or (kind == "seam" and len(panels) >= 120):
        raise UpholsteryError("В разметке может быть до 120 деталей и 400 линий.")
    node_id = panel["nodeId"]
    cut = {"id": uuid4().hex, "nodeId": node_id, "kind": kind, "anchors": anchors, "coverCut": True}
    next_state = {**state, "seams": [*seams, cut]}
    topology = _CoverTopology(model, next_state, node_id)
    previous = topology.coverage(panel, ignoring=cut["id"])
    incidence = _edge_faces(topology.triangles, previous)
    previous_cuts = _seam_cuts(topology.seams[:-1])
    _, path = topology.seams[-1]
    path_edges = {_edge(a, b) for a, b in pairwise(path)}
    perimeter = {vertex for edge, refs in incidence.items() if edge in previous_cuts or len(refs) == 1 for vertex in edge}
    other_slash = {vertex for seam, points in topology.seams[:-1] if seam["kind"] == "slash" for vertex in points}
    if (len(set(path)) != len(path) or path_edges & previous_cuts or set(path) & other_slash or
            any(len(incidence.get(edge, ())) != 2 for edge in path_edges) or set(path[1:-1]) & perimeter):
        raise UpholsteryError("Линия должна проходить внутри выбранной детали, без пересечений с её границами или разрезами.")
    if kind == "seam" and (path[0] not in perimeter or path[-1] not in perimeter):
        raise UpholsteryError("Для разделительного шва поставьте оба конца на кромку детали.")
    if kind == "slash" and (path[0] not in perimeter or path[-1] in perimeter):
        raise UpholsteryError("Начните рассечку на кромке и закончите внутри детали.")

    panel_ids = [panel_id]
    if kind == "seam":
        components = _components(previous, _panel_neighbors(incidence, topology.cuts))
        if len(components) != 2:
            raise UpholsteryError("Этот шов не делит ткань на две отдельные детали. Измените положение линии.")
        try:
            retained = _seed_component(components, topology.surface, topology.triangles, topology.sources, panel)
        except UpholsteryError:
            def area(component):
                points = np.asarray([topology.surface.vertices[i] for i in range(len(topology.surface.vertices))])
                a, b, c = points[np.asarray([topology.triangles[i] for i in component])].transpose(1, 0, 2)
                return np.linalg.norm(np.cross(b - a, c - a), axis=1).sum()
            retained = max(components, key=area)
        separated = next(component for component in components if component is not retained)
        first_seed, second_seed = (topology.interior_seed(component) for component in (retained, separated))
        number = max(state.get("nextPanelNumber", 1), 1 + max((int(p["number"][1:]) for p in panels if p.get("number")), default=0))
        first = {**panel, "seed": first_seed["triangle"], "seedBarycentric": first_seed["barycentric"]}
        if not first.get("number"):
            first["number"] = f"Д{number:02d}"
            number += 1
        second = {key: value for key, value in panel.items() if key not in ("grain", "notches")}
        second.update(id=uuid4().hex, number=f"Д{number:02d}", seed=second_seed["triangle"], seedBarycentric=second_seed["barycentric"])
        panel_ids.append(second["id"])
        next_state.update(panels=[new for record in panels for new in ([first, second] if record is panel else [record])],
                          nextPanelNumber=number + 1)
        for piece, component in ((first, retained), (second, separated)):
            if topology.coverage(piece).keys() != component.keys():
                raise UpholsteryError("Шов пересекает существующую границу или вытачку. Выберите другую линию.")

    calculated = calculate(model, next_state)
    problem = next((issue for issue in calculated["diagnostics"] if issue.get("panelId") in panel_ids and
                    issue["severity"] == "error"), None)
    resolved = {part["id"] for part in calculated["panels"]}
    if problem or not set(panel_ids) <= resolved:
        raise UpholsteryError(problem["message"] if problem else "По линии не удалось построить лекало.")
    display = next((record for record in calculated["seams"] if record["id"] == cut["id"]), None)
    if not display or set(display["panelIds"]) != set(panel_ids):
        raise UpholsteryError("Линия не образует нужную кромку на лекале. Проверьте её положение.")
    _check_cut_links(state, calculate(model, state), calculated)
    return {"state": next_state, "calculated": calculated, "panelIds": panel_ids, "cutId": cut["id"]}


def project_cad_contour(model, state, panel_id, previous, points):
    """Map a changed planar CAD sewing line back to the same authored piece, without modifying the drawing."""
    record = next((p for p in _records(state, "panels") if p["id"] == panel_id), None)
    contour = next((s for s in _records(state, "seams") if s.get("placementFor") == panel_id), None)
    if not record or not contour:
        raise UpholsteryError("В 3D можно передать линию шва детали, уложенной замкнутым контуром.")
    node_id = record["nodeId"]
    layer = next((layer for layer in state.get("fabricLayers", []) if f"fabric:{layer['id']}" == node_id), None)
    if not layer:
        raise UpholsteryError("Поверхность уложенной детали отсутствует.")
    for ring in (previous, points):
        if (not isinstance(ring, list) or not 3 <= len(ring) <= 2048 or any(
            not isinstance(p, list) or len(p) != 2 or any(type(n) not in (float, int) or not abs(n) <= 1e9 for n in p)
            for p in ring
        )):
            raise UpholsteryError("Передайте замкнутую ломаную от 3 до 2048 точек.")
    original = calculate(model, state)
    panel = next((p for p in original["panels"] if p["id"] == panel_id), None)
    if panel is None or panel["kind"] != "flat" or len(panel["loops"]) != 1:
        raise UpholsteryError("В 3D можно передать только внешний контур плоской детали без отверстий.")
    boundary = panel["loops"][0]
    if len(boundary) != len(previous):
        raise UpholsteryError("Исходная линия шва изменилась. Сверьте чертёж с деталью в 3D.")
    old = np.asarray(previous, dtype=float)
    center = old.mean(axis=0)
    world = np.asarray(panel["sourceVertices"])
    origin = world[boundary].mean(axis=0)
    # A rigid isometry also rejects mirrored, stale, stretched or otherwise edited baseline geometry.
    axes, _, rank, _ = np.linalg.lstsq(old - center, world[boundary] - origin, rcond=None)
    local = (world - origin) @ axes.T + center
    if (rank != 2 or not np.allclose(axes @ axes.T, np.eye(2), atol=1e-7, rtol=0)
            or not np.allclose(local[boundary], old, atol=FIT_TOLERANCE, rtol=0)
            or not np.allclose((local - center) @ axes + origin, world, atol=FIT_TOLERANCE, rtol=0)
            or Polygon(old).exterior.is_ccw != Polygon(np.asarray(panel["vertices"])[boundary]).exterior.is_ccw):
        raise UpholsteryError("Исходную линию нельзя точно совместить с плоской деталью в 3D.")
    shape = Polygon(points)
    if not shape.is_valid or shape.is_empty or shape.area <= POINT_EPSILON_MM**2:
        raise UpholsteryError("Изменённая линия шва вырождена или пересекает себя.")
    raw = _fabric_node(layer, {node["id"]: node for node in model["nodes"]})
    vertices, faces = np.asarray(raw["vertices"]), np.asarray(raw["triangles"])
    flat = (vertices - origin) @ axes.T + center
    on_plane = np.linalg.norm((flat - center) @ axes + origin - vertices, axis=1) <= FIT_TOLERANCE
    a, b, c = flat[faces].transpose(1, 0, 2)
    den = _cross(b - a, c - a)
    usable = on_plane[faces].all(axis=1) & (abs(den) > EPS**2)
    den = np.where(usable, den, 1)

    def anchor(point):
        u, v = _cross(point - a, c - a) / den, _cross(b - a, point - a) / den
        weights = np.column_stack((1 - u - v, u, v))
        hits = np.flatnonzero(usable & (weights >= -EPS).all(axis=1))
        if not len(hits):
            raise UpholsteryError("Линия выходит за границы плоской ткани в 3D.")
        supports = {tuple(sorted(faces[i][weights[i] > EPS])) for i in hits}
        if len(supports) != 1:
            raise UpholsteryError("В этой точке накладываются поверхности. Исправьте контур в 3D.")
        index = int(hits[0])
        bary = np.clip(weights[index], 0, 1)
        return {"triangle": index, "barycentric": (bary / bary.sum()).tolist()}

    corners = list(shape.simplify(EPS, preserve_topology=True).exterior.coords)[:-1]
    anchors = [anchor(np.asarray(p)) for p in corners]
    seed = anchor(np.asarray(shape.representative_point().coords[0]))
    base = {**state, "seams": [s for s in state["seams"] if s is not contour]}
    preview_contour(model, base, node_id, anchors, seed, surface_route=contour.get("surfaceRoute") is True)
    proposed = {**state, "panels": [{**p, "seed": seed["triangle"], "seedBarycentric": seed["barycentric"]}
                                     if p is record else p for p in state["panels"]],
                "seams": [{**s, "anchors": anchors} if s is contour else s for s in state["seams"]]}
    check_cover_panel(model, proposed, node_id, panel_id)
    checked = calculate(model, proposed)
    piece = next((p for p in checked["panels"] if p["id"] == panel_id), None)
    if piece is None or piece["kind"] != "flat" or len(piece["loops"]) != 1:
        raise UpholsteryError("Изменённая деталь не образует один плоский контур в 3D.")
    new_world = np.asarray(piece["sourceVertices"])
    landed = (new_world - origin) @ axes.T + center
    ring = landed[piece["loops"][0]]
    if (Polygon(ring).exterior.is_ccw != Polygon(np.asarray(piece["vertices"])[piece["loops"][0]]).exterior.is_ccw
            or not np.allclose((landed - center) @ axes + origin, new_world, atol=FIT_TOLERANCE, rtol=0)
            or shape.exterior.hausdorff_distance(Polygon(ring).exterior) > FIT_TOLERANCE):
        raise UpholsteryError("На ткани линия проходит иначе. Отредактируйте этот участок в 3D.")
    first, second = ring[:2]
    return {"anchors": anchors, "seed": seed, "frame": [*first.tolist(), math.atan2(*(second - first)[::-1])],
            "calculated": checked}


def calculate(model, state):
    if not isinstance(state, dict):
        raise UpholsteryError("Некорректные настройки лекал.")
    panels, seams, pairs, joins = (_records(state, key) for key in ("panels", "seams", "pairs", "joins"))
    nodes = {node["id"]: node for node in model["nodes"]}
    fabric_layers = {f"fabric:{layer['id']}": layer for layer in state.get("fabricLayers", [])}
    material = state.get("material") or {}
    if not isinstance(material, dict):
        raise UpholsteryError("Некорректные ограничения материала.")
    metrics = (("stretch", "растяжение", 1000), ("compression", "сжатие", 100), ("shear", "сдвиг", 90))
    limits = [(key, label, _number(material[key], 0, maximum, f"Некорректный допуск на {label}."))
              for key, label, maximum in metrics if key in material]

    result = {"panels": [], "placements": [], "joins": [], "confirmedJoins": [], "seams": [], "pairs": [], "fitStitches": [],
              "warnings": [], "diagnostics": []}
    node_records = defaultdict(lambda: ([], []))
    for index, (records, field, code) in enumerate(((panels, "panelId", "geometry_unavailable"),
                                                  (seams, "seamId", "seam_invalid"))):
        for record in records:
            node_id = record.get("nodeId")
            name = record.get("name")
            if name is not None and not isinstance(name, str):
                _diagnostic(result, "error", code, "Имя должно быть строкой.", label=record["id"], **{field: record["id"]})
            elif not isinstance(node_id, str) or node_id not in nodes and node_id not in fabric_layers:
                _diagnostic(result, "error", code, "Объект отсутствует в модели. Выберите компонент заново.",
                            label=name or record["id"], **{field: record["id"]})
            else:
                node_records[node_id][index].append(record)
    sewing_lines, geometries, candidates = {}, {}, {}
    for node_id, (node_panels, node_seams) in node_records.items():
        raw = nodes[node_id] if node_id in nodes else _fabric_node(fabric_layers[node_id], nodes)
        if not raw["triangles"]:
            for panel in node_panels:
                _diagnostic(result, "error", "geometry_unavailable", "В объекте нет поверхности. Выберите компонент с гранями.",
                            label=panel.get("name") or panel["id"], panelId=panel["id"])
            for seam in node_seams:
                _diagnostic(result, "error", "seam_invalid", "В объекте нет поверхности для шва.", label=seam["id"], seamId=seam["id"])
            continue
        layer = fabric_layers.get(node_id, {})
        surface = _Surface(raw, join_coplanar=layer.get("joinCoplanar") is True,
                           join_folded=layer.get("joinFolded") is True)
        active = []
        for seam in node_seams:
            try:
                active.append((seam, _seam_path(surface, seam)))
            except UpholsteryError as error:
                _diagnostic(result, "error", "seam_invalid", str(error), label=seam["id"], seamId=seam["id"])
        try:
            triangles, sources, routes = surface.triangulate()
        except (UpholsteryError, GEOSException) as error:
            message = str(error) if isinstance(error, UpholsteryError) else "Не удалось построить сетку по размеченным швам."
            for panel in node_panels:
                _diagnostic(result, "error", "geometry_unavailable", message, label=panel.get("name") or panel["id"],
                            detail="Проверьте швы в этом компоненте и повторите расчёт.", panelId=panel["id"])
            for seam, _ in active:
                _diagnostic(result, "error", "seam_invalid", message, label=seam["id"], seamId=seam["id"])
            continue
        expanded = []
        for seam, path in active:
            points = _expanded_path(path, routes)
            expanded.append((seam, points))
            result["seams"].append({"id": seam["id"], "nodeId": node_id, "kind": seam["kind"],
                                    "points": [surface.vertices[point].tolist() for point in points],
                                    **({"coverCut": True} if seam.get("coverCut") is True else {})})
        owners, overlaps = {}, {}
        for panel in node_panels:
            name = panel.get("name") or panel["id"]
            resolved = set() if node_id in fabric_layers and node_id not in nodes else None
            placement = {} if resolved is not None else None
            try:
                try:
                    data, boundaries, geometry = _flatten(panel, surface, triangles, sources, expanded, resolved=resolved,
                                                          placement=placement)
                except UpholsteryError as original:
                    if not any(seam["kind"] == "dart" for seam, _ in expanded):
                        raise
                    try:
                        data, boundaries, geometry = _flatten(panel, surface, triangles, sources, expanded, excluded_darts=True,
                                                              resolved=resolved, placement=placement)
                    except UpholsteryError:
                        raise original from None
                    _diagnostic(result, "warning", "dart_unopened",
                                "Стороны вытачки не раскрылись. Вытачка сохранена как линия разметки.",
                                label=name, panelId=panel["id"])
                result["panels"].append(data)
                sewing_lines.update(((panel["id"], seam_id), [geometry.bank(indices, fractions) for indices, fractions in banks])
                                    for seam_id, banks in boundaries.items())
                geometries[panel["id"]] = geometry
            except (UpholsteryError, GEOSException, np.linalg.LinAlgError, RuntimeError) as error:
                message = str(error) if isinstance(error, UpholsteryError) else "Не удалось развернуть сетку детали."
                _diagnostic(result, "error", "geometry_unavailable", message, label=name, panelId=panel["id"],
                            detail="Проверьте границы детали. Для сложной формы добавьте шов или вытачку и повторите расчёт.")
            if placement:
                result["placements"].append({"id": panel["id"], "nodeId": node_id, **placement})
            for facet in resolved or ():
                other = owners.setdefault(facet, panel)
                if other is not panel:
                    overlaps.setdefault(panel["id"], other)
                    overlaps.setdefault(other["id"], panel)
        for panel in node_panels:
            other = overlaps.get(panel["id"])
            if other:
                _diagnostic(result, "error", "cover_panel_overlap",
                            f"Этот участок чехла также входит в деталь «{other.get('name') or other['id']}».",
                            label=panel.get("name") or panel["id"], panelId=panel["id"],
                            detail="Восстановите разделительный шов или уточните границы деталей.")
        if owners:
            valid = {panel["id"]: geometries[panel["id"]] for panel in node_panels
                     if panel["id"] in geometries and panel["id"] not in overlaps}
            if len(valid) >= 2:
                candidates.update(_common_boundaries(node_id, surface, triangles, owners, valid))
    result["joins"] = [candidate for candidate, _ in candidates.values()]
    panel_data = {panel["id"]: panel for panel in result["panels"]}
    requested_edges = {(endpoint["panelId"], endpoint["edgeId"]) for pair in pairs for side in "ab"
                       if isinstance(endpoint := pair.get(side), dict)
                       and all(isinstance(endpoint.get(field), str) for field in ("panelId", "edgeId"))}
    edge_banks = {(panel["id"], edge["id"]): geometries[panel["id"]].bank(edge["vertices"], np.asarray([0., 1.]))
                  for panel in panel_data.values() for edge in panel["edges"]
                  if (panel["id"], edge["id"]) in requested_edges}
    occupied = defaultdict(list)
    specs = []
    for index, pair in enumerate(pairs, 1):
        name = pair.get("name")
        label = name.strip()[:30] if isinstance(name, str) and name.strip() else str(index)
        try:
            specs.append((pair, label, _pair(pair, sewing_lines, edge_banks, occupied)))
        except UpholsteryError as error:
            _diagnostic(result, "error", "pair_invalid", str(error), label=f"Пара {label}", pairId=pair["id"])
    rounded = {identifier: geometry for identifier, geometry in geometries.items() if geometry.panel["kind"] == "rounded"}
    for identifier, targets in _sewing_targets(specs, rounded).items():
        try:
            rounded[identifier].optimize(targets)
        except (UpholsteryError, GEOSException, np.linalg.LinAlgError, RuntimeError):
            _diagnostic(result, "warning", "seam_fit_unavailable", "Подгонка длины швов не сошлась.",
                        label=panel_data[identifier]["name"], panelId=identifier)
    for geometry in geometries.values():
        geometry.orient()
    for seam in result["seams"]:
        if seam["kind"] == "slash":
            seam["panelIds"] = [panel["id"] for panel in result["panels"]
                                if any(guide["id"] == seam["id"] for guide in panel["guides"])]
            seam["banks"] = []
            continue
        seam["panelIds"] = [panel_id for panel_id in panel_data if (panel_id, seam["id"]) in sewing_lines]
        seam["banks"] = [{"id": _bank_id(bank), "panelId": panel_id, "bank": index,
                          "points": [bank.panel["vertices"][vertex] for vertex in bank.vertices],
                          "sourcePoints": [bank.panel["sourceVertices"][vertex] for vertex in bank.vertices],
                          "edgeIds": bank.edge_ids}
                         for panel_id in seam["panelIds"] for index, bank in enumerate(sewing_lines[(panel_id, seam["id"])])]
    if state.get("autoNotches") is True:
        _automatic_notches(result, pairs, fabric_layers, sewing_lines, occupied)
    for pair, label, spec in specs:
        comparison = _render_pair(pair, label, spec)
        result["pairs"].append(comparison)
        result["fitStitches"].append(_stitch(pair["id"], spec[0]))
        remaining = max(abs(interval["delta"] - interval["ease"]) for interval in comparison["intervals"])
        if remaining > .02:
            _diagnostic(result, "warning", "seam_ease_mismatch", f"Наибольшее отклонение от заданной посадки {remaining:.2f} мм.",
                        label=f"Пара {label}", pairId=pair["id"])
    _apply_joins(result, joins, candidates, panels)
    for record in panels:
        panel = panel_data.get(record["id"])
        if not panel or not record.get("notches"):
            continue
        edges = {edge["id"]: edge for edge in panel["edges"]}
        for index, notch in enumerate(record["notches"], 1):
            edge = edges.get(notch["edgeId"])
            if edge is None:
                _diagnostic(result, "warning", "edge_notch_unavailable", "Кромка насечки изменилась. Укажите её заново.",
                            label=panel["name"], panelId=panel["id"])
                continue
            a, b = edge["points"]
            t = notch["position"]
            point = [x + t * (y - x) for x, y in zip(a, b)]
            panel["marks"].append({"id": notch["id"], "point": point, "label": f"кромки {index}", "edgeId": edge["id"]})
    _fit_open_darts(result, geometries, occupied)
    for panel in result["panels"]:
        maxima = {key: max(metrics[key] for metrics in panel["strain"]) for key, _, _ in metrics}
        advice = "Перенесите шов, добавьте вытачку или разделите деталь и повторите расчёт."
        for key, label, limit in limits:
            measured = maxima[key]
            if measured > limit + FIT_TOLERANCE:
                unit = "°" if key == "shear" else "%"
                _diagnostic(result, "warning", "material_limit", f"{label.capitalize()} {measured:.2f}{unit} превышает допуск {limit:.2f}{unit}.",
                            label=panel["name"], detail=advice, panelId=panel["id"])
        if panel["kind"] == "rounded" and max(maxima.values()) >= 20:
            values = ", ".join(f"{label} {maxima[key]:.2f}{'°' if key == 'shear' else '%'}" for key, label, _ in metrics)
            _diagnostic(result, "warning", "high_deformation", f"Большая деформация развёртки: {values}.",
                        label=panel["name"], detail=advice, panelId=panel["id"])
    result["complete"] = (not any(issue["severity"] == "error" for issue in result["diagnostics"])
                          and all(len(result[key]) == len(values) for key, values in (("panels", panels), ("seams", seams), ("pairs", pairs))))
    return result


def _entity(kind, layer, group, **geometry):
    return {"id": uuid4().hex, "type": kind, "layer": layer, "groupPath": group, **geometry}


def _hole_notch(contour, material, point, depth):
    line = LineString([*contour["points"], contour["points"][0]])
    at = line.project(point)
    step = min(line.length / 4, 0.01)
    before, after = (line.interpolate((at + offset) % line.length) for offset in (-step, step))
    dx, dy = after.x - before.x, after.y - before.y
    magnitude = math.hypot(dx, dy)
    if magnitude <= EPS:
        raise UpholsteryError("Не удалось определить направление насечки на отверстии.")
    # The hole is clockwise, so its left-hand side is the surrounding material.
    base = line.interpolate(at)
    end = [base.x - dy * depth / magnitude, base.y + dx * depth / magnitude]
    if not material.buffer(EPS).covers(LineString([base, end])):
        raise UpholsteryError("Насечка на отверстии пересекает другую границу детали.")
    return {"id": uuid4().hex, "type": "line", "layer": contour["layer"], "notch": True,
            "parentId": contour["id"], "start": list(base.coords[0]), "end": end}


def _transfer_settings(allowance_mm, notch_depth_mm, hole_allowance_mm=None, edge_allowances=None, hole_allowances=None):
    try:
        allowance = _number(allowance_mm, 0, 1000, "Припуск должен быть от 0 до 1000 мм.")
    except UpholsteryError as error:
        raise _TransferIssue("allowance_invalid", str(error)) from error
    try:
        depth = _number(notch_depth_mm, 0, 1000, "Глубина насечки должна быть от 0 до 1000 мм.")
    except UpholsteryError as error:
        raise _TransferIssue("notch_invalid", str(error)) from error
    try:
        hole_allowance = allowance if hole_allowance_mm is None else _number(
            hole_allowance_mm, 0, 1000, "Припуск отверстия должен быть от 0 до 1000 мм.")
        for values in (edge_allowances, hole_allowances):
            if values is None:
                continue
            if not isinstance(values, dict) or len(values) > MAX_POINTS:
                raise UpholsteryError("Проверьте индивидуальные припуски участков контура.")
            for value in values.values():
                _number(value, 0, 1000, "Припуск участка контура должен быть от 0 до 1000 мм.")
    except UpholsteryError as error:
        raise _TransferIssue("allowance_invalid", str(error)) from error
    if depth > max(allowance, hole_allowance) and not (edge_allowances or hole_allowances):
        raise _TransferIssue("notch_invalid", "Глубина насечки не должна превышать припуск.",
                             "Уменьшите глубину насечки или задайте 0, чтобы оставить только контрольные метки.")
    return allowance, depth, hole_allowance, edge_allowances or {}, hole_allowances or {}


def _hole_closed(hole):
    return hole.is_empty or hole.buffer(-POINT_EPSILON_MM / 2).is_empty


def _closed_hole_issue(allowance):
    return _TransferIssue("allowance_closes_hole", f"Припуск {allowance:g} мм закрывает отверстие.",
                          "Уменьшите припуск отверстия или подставьте предложенный размер.")


def _edge_allowances(panel, allowance, hole_allowance, edge_overrides, hole_overrides):
    edges = {edge["id"]: edge for edge in panel.get("edges", ())}
    holes = {hole["id"]: hole["loop"] for hole in panel.get("holes", ())}
    if edge_overrides.keys() - edges.keys() or hole_overrides.keys() - holes.keys():
        raise _TransferIssue("allowance_edge_unavailable", "Граница с индивидуальным припуском изменилась.",
                             "Обновите настройки выбранной границы.")
    widths = {holes[identifier]: width for identifier, width in hole_overrides.items()}
    by_ring = [allowance, *(widths.get(index, hole_allowance) for index in range(1, len(panel["loops"])))]
    by_edge = {identifier: edge_overrides.get(identifier, by_ring[edge["loop"]]) for identifier, edge in edges.items()}
    return by_ring, by_edge


def _variable_ring(ring, widths, outer):
    base = Polygon(ring)
    if len(set(widths)) == 1:
        distance = widths[0] if outer else -widths[0]
        return base.buffer(distance, join_style="mitre") if distance else base
    starts = np.asarray(ring.coords[:-1])
    ends = np.roll(starts, -1, axis=0)
    tangents = ends - starts
    tangents /= np.linalg.norm(tangents, axis=1)[:, None]
    offsets = np.column_stack((tangents[:, 1], -tangents[:, 0])) * np.asarray(widths)[:, None]
    pieces = [Polygon([a, b, b + offset, a + offset]) for a, b, offset, width in zip(starts, ends, offsets, widths)
              if width]
    for index, (vertex, tangent) in enumerate(zip(starts, tangents)):
        previous = tangents[index - 1]
        turn = _cross(previous, tangent)
        width = max(widths[index - 1], widths[index])
        if turn <= EPS or not width:
            continue
        a, b = vertex + offsets[index - 1], vertex + offsets[index]
        along_previous = _cross(b - a, tangent) / turn
        along_next = _cross(b - a, previous) / turn
        corner = a + previous * along_previous
        points = [vertex, a, b]
        if along_previous >= -EPS and along_next <= EPS and math.dist(corner, vertex) <= 5 * width:
            points.insert(2, corner)
        wedge = Polygon(points)
        if wedge.area > EPS**2:
            pieces.append(wedge)
    if outer:
        return union_all([base, *pieces], grid_size=EPS)
    strips = union_all(pieces, grid_size=EPS)
    return base.difference(strips, grid_size=EPS)


def _cut_geometry(panel, allowance, hole_allowance, ring_widths, edge_widths, variable):
    vertices = np.asarray(panel["vertices"])
    try:
        polygon = _valid_pattern(vertices, np.asarray(panel["triangles"]), panel["loops"])
    except (UpholsteryError, GEOSException) as error:
        raise _TransferIssue("geometry_unavailable", "Контуры детали пересекаются или вырождены.",
                             "Проверьте границы и швы детали, затем повторите расчёт.") from error
    try:
        if variable:
            widths_by_vertices = {_edge(*edge["vertices"]): edge_widths[edge["id"]] for edge in panel.get("edges", ())}
            rings = [polygon.exterior, *polygon.interiors]
            widths = [[widths_by_vertices.get(_edge(a, b), ring_widths[index]) for a, b in pairwise([*loop, loop[0]])]
                      for index, loop in enumerate(panel["loops"])]
            shell, *holes = [_variable_ring(ring, ring_widths, not index)
                             for index, (ring, ring_widths) in enumerate(zip(rings, widths))]
            if any(_hole_closed(hole) for hole in holes):
                raise _closed_hole_issue(max(max(values) for values in widths[1:]))
            if (shell.geom_type != "Polygon" or shell.is_empty or shell.interiors
                    or any(hole.geom_type != "Polygon" or hole.interiors for hole in holes)):
                raise _TransferIssue("allowance_collapses", "Припуск изменяет границы детали.",
                                     "Уменьшите припуск и проверьте узкие участки.")
            cut = Polygon(shell.exterior, [hole.exterior for hole in holes])
        elif hole_allowance == allowance or not polygon.interiors:
            cut = polygon.buffer(allowance, join_style="mitre") if allowance else polygon
        else:
            shell = Polygon(polygon.exterior).buffer(allowance, join_style="mitre")
            holes = [Polygon(ring).buffer(-hole_allowance, join_style="mitre") for ring in polygon.interiors]
            if any(_hole_closed(hole) for hole in holes):
                raise _closed_hole_issue(hole_allowance)
            if (shell.geom_type != "Polygon" or shell.is_empty or shell.interiors
                    or any(hole.geom_type != "Polygon" or hole.interiors for hole in holes)):
                raise _TransferIssue("allowance_collapses", "Припуск изменяет границы детали.",
                                     "Уменьшите припуск и проверьте узкие участки.")
            cut = Polygon(shell.exterior, [hole.exterior for hole in holes])
    except GEOSException as error:
        raise _TransferIssue("allowance_collapses", "Не удалось построить срез с заданным припуском.",
                             "Уменьшите припуск или проверьте узкие участки контура.") from error
    if cut.geom_type != "Polygon" or cut.is_empty or not cut.is_valid:
        raise _TransferIssue("allowance_collapses", "Припуск меняет форму среза или сводит контур в одну линию.",
                             "Уменьшите припуск и проверьте узкие участки контура.")
    if len(cut.interiors) < len(polygon.interiors):
        raise _closed_hole_issue(hole_allowance)
    if len(cut.interiors) > len(polygon.interiors):
        raise _TransferIssue("allowance_collapses", "Припуск замыкает узкий участок среза.", "Уменьшите припуск.")
    cut = orient(cut)
    # GEOS can reorder holes. Associate each surviving cut hole with its original one.
    cut_holes = [(ring, Polygon(ring)) for ring in cut.interiors]
    if any(_hole_closed(hole) for _, hole in cut_holes):
        raise _closed_hole_issue(hole_allowance)
    cut_rings = [cut.exterior]
    for source in polygon.interiors:
        region = Polygon(source).buffer(EPS)
        matches = [ring for ring, hole in cut_holes if region.covers(hole)]
        if len(matches) != 1:
            raise _TransferIssue("allowance_collapses", "Припуск разделяет или сужает отверстие.", "Уменьшите припуск.")
        cut_rings.append(matches[0])
    return (polygon.exterior, *polygon.interiors), cut_rings, cut.bounds


def _edge_notch(contour, location, edge, width, depth):
    a, b = np.asarray(edge["points"])
    direction = a - b if edge["reverseOnLoop"] else b - a
    outward = np.asarray([direction[1], -direction[0]]) / np.linalg.norm(direction)
    expected = np.asarray(location) + width * outward
    line = LineString([*contour["points"], contour["points"][0]])
    target = Point(expected)
    if line.distance(target) > FIT_TOLERANCE:
        raise UpholsteryError("Насечка попадает на переход между участками припуска.")
    start = line.interpolate(line.project(target))
    end = expected - depth * outward
    return _entity("line", contour["layer"], contour["groupPath"], notch=True,
                   parentId=contour["id"], start=[start.x, start.y], end=end.tolist())


def _slash_cut(points, source_rings, cut_rings):
    issue = _TransferIssue("slash_unavailable", "Рассечку нельзя провести от среза до заданной точки.",
                           "Проверьте начало рассечки и припуск кромки.")
    if len(points) < 2:
        raise issue
    start = Point(points[0])
    matching = [i for i, ring in enumerate(source_rings) if ring.distance(start) <= FIT_TOLERANCE]
    if len(matching) != 1:
        raise issue
    edge = cut_rings[matching[0]]
    if edge.distance(start) > FIT_TOLERANCE:
        axis = np.subtract(points[0], points[1])
        size = np.linalg.norm(axis)
        if size <= EPS:
            raise issue
        x0, y0, x1, y1 = cut_rings[0].bounds
        ray = LineString([points[0], np.asarray(points[0]) + (2 * math.hypot(x1 - x0, y1 - y0) / size) * axis])
        hits = ray.intersection(edge)
        candidates = [p for p in getattr(hits, "geoms", (hits,)) if p.geom_type == "Point" and not p.is_empty]
        if not candidates:
            raise issue
        entry = min(candidates, key=start.distance)
        points = [[entry.x, entry.y], *points]
    path = LineString(points)
    material = Polygon(cut_rings[0], cut_rings[1:])
    if not path.is_simple or not material.buffer(POINT_EPSILON_MM).covers(path) or not material.contains(Point(points[-1])):
        raise issue
    return points


def _pattern_entities(panel, allowance, depth, hole_allowance, edge_overrides, hole_overrides, group, offset):
    ring_widths, edge_widths = _edge_allowances(panel, allowance, hole_allowance, edge_overrides, hole_overrides)
    variable = bool(edge_overrides or hole_overrides)
    source_rings, cut_rings, (x0, y0, x1, _) = _cut_geometry(panel, allowance, hole_allowance, ring_widths, edge_widths, variable)
    delta = (offset - x0, -y0)

    def translate(points):
        return (np.asarray(points) + delta).tolist()

    contours = []
    entities = []
    for index, (source_ring, cut_ring) in enumerate(zip(source_rings, cut_rings)):
        contours.append(_entity("polyline", "Срез отверстия" if index else "Срез", group,
                                points=translate(list(cut_ring.coords)[:-1]), closed=True))
        entities.append(_entity("polyline", "Линия отверстия" if index else "Линия шва", group,
                                 points=translate(list(source_ring.coords)[:-1]), closed=True, reference=True))
    guides = panel.get("guides", [])
    for guide in guides:
        if guide["kind"] == "slash":
            points = _slash_cut(guide["points"], source_rings, cut_rings)
            entities.append(_entity("polyline", "Рассечка", group, points=translate(points), closed=False, notch=False))
    entities.extend(contours)
    for guide in guides:
        if guide["kind"] == "dart":
            entities.append(_entity("polyline", "Вытачка", group, points=translate(guide["points"]),
                                     closed=False, reference=True))
    if panel.get("grain"):
        start, end = translate(panel["grain"])
        entities.append(_entity("line", "Долевая", group, start=start, end=end, reference=True))
    if depth and panel["marks"]:
        source_lines = [LineString(ring) for ring in source_rings]
        material = Polygon(contours[0]["points"], [contour["points"] for contour in contours[1:]])
        tolerated_material = material.buffer(POINT_EPSILON_MM)
    edge_data = {edge["id"]: edge for edge in panel.get("edges", ())}
    notches = []
    for mark in panel["marks"]:
        point = Point(mark["point"])
        location = translate([mark["point"]])[0]
        entities.append(_entity("point", f"Метка {mark['label']}"[:255], group, point=location))
        if not depth:
            continue
        edge = edge_data.get(mark.get("edgeId"))
        index = edge["loop"] if edge else min(range(len(source_lines)), key=lambda candidate: source_lines[candidate].distance(point))
        advice = "Уменьшите глубину насечки, измените положение метки или оставьте только метку с глубиной 0 мм."
        if source_lines[index].distance(point) > FIT_TOLERANCE:
            raise _TransferIssue("notch_invalid", f"Метка {mark['label']} находится вне линии шва.", advice)
        width = edge_widths[edge["id"]] if edge else ring_widths[index]
        if depth > width:
            raise _TransferIssue("notch_invalid", f"Насечка {mark['label']} глубже припуска участка ({width:g} мм).", advice)
        contour = contours[index]
        try:
            if variable and edge:
                notch = _edge_notch(contour, location, edge, width, depth)
            elif index:
                notch = _hole_notch(contour, material, Point(location), depth)
            else:
                edited = edit_design([contour], "notch", {"id": contour["id"], "point": location, "depth": depth})
                notch = edited["entities"][-1]
        except (UpholsteryError, CadDesignError, GEOSException) as error:
            message = str(error) if not isinstance(error, GEOSException) else "Не удалось построить насечку."
            raise _TransferIssue("notch_invalid", f"Метка {mark['label']}: {message}", advice) from error
        path = LineString([notch["start"], notch["end"]])
        if not tolerated_material.covers(path):
            raise _TransferIssue("notch_invalid", f"Насечка {mark['label']} пересекает границу детали.", advice)
        if any(path.intersects(other) or math.dist(path.coords[0], other.coords[0]) < 0.5 for other in notches):
            raise _TransferIssue("notch_invalid", "Насечки пересекаются или расположены ближе 0,5 мм.", advice)
        notches.append(path)
        entities.append(notch)
    try:
        validated = validate_design(entities)
    except CadDesignError as error:
        raise _TransferIssue("cad_invalid", str(error), "Проверьте размеры и границы детали.") from error
    cut_shapes = [shape["points"] for shape in validated if shape.get("closed") and not shape.get("reference")]
    if any(_hole_closed(Polygon(hole)) for hole in cut_shapes[1:]):
        raise _TransferIssue("allowance_closes_hole", "При заданном припуске отверстие слишком мало для переноса.",
                             "Уменьшите припуск, чтобы сохранить отверстие.")
    cut_polygon = Polygon(cut_shapes[0], cut_shapes[1:])
    if not cut_polygon.is_valid or cut_polygon.area <= POINT_EPSILON_MM**2:
        raise _TransferIssue("allowance_collapses", "Срез вырождается при переносе в плоский редактор.",
                             "Проверьте размеры детали или уменьшите припуск.")
    return validated, offset + x1 - x0 + max(20, 2 * allowance), delta


def _transfer_panels(state, panel_ids):
    panels = {panel["id"]: panel for panel in _records(state, "panels")}
    if panel_ids is None:
        return list(panels.values())
    if (not isinstance(panel_ids, list) or len(panel_ids) > len(panels)
            or any(not isinstance(identifier, str) or identifier not in panels for identifier in panel_ids)
            or len(set(panel_ids)) != len(panel_ids)):
        raise UpholsteryError("Выберите существующие детали для переноса без повторов.")
    return [panels[identifier] for identifier in panel_ids]


def _transfer_issues(result, state):
    pair_panels = {pair["id"]: [end["panelId"] for side in "ab" if isinstance(end := pair.get(side), dict)
                                and isinstance(end.get("panelId"), str)]
                   for pair in _records(state, "pairs")}
    issues = defaultdict(list)
    for diagnostic in result.get("diagnostics", []):
        if "panelId" in diagnostic:
            identifiers = [diagnostic["panelId"]]
        else:
            identifiers = pair_panels.get(diagnostic.get("pairId"), ())
        for identifier in dict.fromkeys(identifiers):
            issues[identifier].append({key: diagnostic[key] for key in ("code", "message", "detail", "severity") if key in diagnostic})
    return issues


def _settings_values(settings):
    allowance, depth, hole_allowance, edge_overrides, hole_overrides = settings
    values = {"allowanceMm": allowance, "notchDepthMm": depth, "holeAllowanceMm": hole_allowance}
    if edge_overrides:
        values["edgeAllowances"] = edge_overrides
    if hole_overrides:
        values["holeAllowances"] = hole_overrides
    return values


def _validated_overrides(state, value):
    if value is None:
        return {}
    identifiers = {panel["id"] for panel in _records(state, "panels")}
    keys = {"allowanceMm", "notchDepthMm", "holeAllowanceMm", "edgeAllowances", "holeAllowances"}
    if (not isinstance(value, dict) or any(identifier not in identifiers or not isinstance(preset, dict)
            or set(preset) - keys for identifier, preset in value.items())):
        raise UpholsteryError("Проверьте индивидуальные настройки припуска.")
    return value


def _hole_allowance_limit(panel):
    vertices = panel["vertices"]
    half_widths = []
    for loop in panel["loops"][1:]:
        box = Polygon([vertices[index] for index in loop]).minimum_rotated_rectangle
        if box.geom_type != "Polygon":
            return 0
        half_widths.append(min(math.dist(a, b) for a, b in pairwise(box.exterior.coords)) / 2)
    return min(half_widths, default=0)


def _suggest_settings(panel, settings, code, group):
    allowance, depth, hole_allowance, edge_overrides, hole_overrides = settings
    if code == "allowance_closes_hole" and len(panel["loops"]) > 1:
        limit = _hole_allowance_limit(panel)
        below_limit = max(0, math.ceil(limit - POINT_EPSILON_MM) - 1) if limit >= 1 else limit / 2
        preferred = min(hole_allowance / 2 if below_limit >= hole_allowance else hole_allowance, below_limit)
        widths = tuple(round(preferred / 2**i, 3) for i in range(3)) + (0,)
        candidates = ((allowance, suggested_depth, width)
                      for width in widths for suggested_depth in (depth, min(depth, width)))
    elif code == "allowance_collapses":
        widths = tuple(round(allowance / 2**i, 3) for i in range(1, 4)) + (0,)
        candidates = ((width, min(depth, width), min(hole_allowance, width)) for width in widths)
    elif code == "notch_invalid" and depth:
        candidates = ((allowance, proposed, hole_allowance) for proposed in (round(depth / 2, 3), 0))
    else:
        return None
    for widths in dict.fromkeys(candidates):
        candidate = (*widths, edge_overrides, hole_overrides)
        if candidate == settings:
            continue
        try:
            _pattern_entities(panel, *candidate, group, 0)
            return _settings_values(candidate)
        except _TransferIssue:
            continue
    return None


def _transfer_preview(shapes, delta):
    preview = []
    for shape in shapes:
        if shape.get("closed") and shape.get("points"):
            points = shape["points"]
        elif shape.get("notch"):
            points = [shape["start"], shape["end"]]
        else:
            continue
        item = {key: shape[key] for key in ("type", "layer", "closed", "reference", "notch") if key in shape}
        item["points"] = (np.asarray(points) - delta).tolist()
        preview.append(item)
    return preview


def _prepare_transfer(result, state, panel_ids, allowance_mm, notch_depth_mm, panel_settings, *, suggest=False):
    requested = _transfer_panels(state, panel_ids)
    available = {panel["id"]: panel for panel in result["panels"]}
    by_panel = _transfer_issues(result, state)
    statuses, entities, points, offset = [], [], 0, 0
    overrides = _validated_overrides(state, panel_settings)
    for record in requested:
        identifier = record["id"]
        name = record.get("name")
        name = name if isinstance(name, str) and name else identifier
        issues = sorted(by_panel[identifier], key=lambda issue: issue["severity"] != "error")
        status = {"id": identifier, "name": name, "ready": False, "issues": issues}
        statuses.append(status)
        settings = None
        try:
            if any(issue["severity"] == "error" for issue in issues):
                continue
            panel = available.get(identifier)
            if panel is None:
                raise _TransferIssue("geometry_unavailable", "Развёртка для детали недоступна.",
                                     "Проверьте границы и повторите расчёт.")
            preset = overrides.get(identifier, {})
            settings = _transfer_settings(preset.get("allowanceMm", allowance_mm), preset.get("notchDepthMm", notch_depth_mm),
                                          preset.get("holeAllowanceMm"), preset.get("edgeAllowances"), preset.get("holeAllowances"))
            status["settings"] = _settings_values(settings)
            panel_group = ["Лекала 3D", identifier]
            shapes, next_offset, delta = _pattern_entities(panel, *settings, panel_group, offset)
            added_points = sum(len(shape["points"]) if shape["type"] == "polyline" else 2 if shape["type"] == "line" else 1
                               for shape in shapes)
            if len(entities) + len(shapes) > MAX_ENTITIES or points + added_points > MAX_POINTS:
                raise _TransferIssue("cad_limit", "Выбранные детали превышают предел сложности чертежа.",
                                     "Перенесите меньше деталей за один раз.")
        except _TransferIssue as error:
            issue = {"severity": "error", "code": error.code, "message": str(error)}
            if error.detail:
                issue["detail"] = error.detail
            issues.insert(0, issue)
            if suggest and settings is not None:
                proposed = _suggest_settings(panel, settings, error.code, panel_group)
                if proposed is not None:
                    status["suggestion"] = proposed
            continue
        entities.extend(shapes)
        points += added_points
        offset = next_offset
        status["ready"] = True
        if suggest:
            status["preview"] = _transfer_preview(shapes, delta)
        issues.insert(0, {"code": "ready", "message": "Деталь готова к переносу."})
    return statuses, entities


def check_transfer(result, state, allowance_mm, notch_depth_mm, panel_ids=None, *, panel_settings=None):
    statuses, _ = _prepare_transfer(result, state, panel_ids, allowance_mm, notch_depth_mm, panel_settings, suggest=True)
    return {"panels": statuses}


def cad_sync_geometry(model_id, result, state, patterns):
    settings = state.get("transferSettings") or {}
    overrides = settings.get("panels", {})
    if isinstance(overrides, dict):
        overrides = {key: value for key, value in overrides.items() if key in patterns}
    try:
        statuses, entities = _prepare_transfer(result, state, None, settings.get("allowanceMm", 10),
                                              settings.get("notchDepthMm", 2), overrides)
    except UpholsteryError as error:
        return {"entities": [], "patterns": {}, "issues": [{"id": key, "message": str(error)} for key in patterns]}
    ready = {status["id"] for status in statuses if status["ready"]}
    issues = [{"id": status["id"], "message": issue["message"]} for status in statuses if not status["ready"]
              for issue in status["issues"] if issue.get("severity") == "error"]
    # IDs stay attached to the same role within a panel across calculations, including parented notches.
    by_id = {entity["id"]: entity for entity in entities}
    counts, ids = defaultdict(int), {}
    for entity in entities:
        owner = by_id[entity["parentId"]] if "parentId" in entity else entity
        role = (owner["groupPath"][1], entity["layer"], entity["type"], bool(entity.get("notch")))
        identity = json.dumps((model_id, *role, counts[role]), ensure_ascii=False, separators=(",", ":"))
        ids[entity["id"]] = hashlib.sha256(identity.encode()).hexdigest()[:32]
        counts[role] += 1
    for entity in entities:
        entity["id"] = ids[entity["id"]]
        if "parentId" in entity:
            entity["parentId"] = ids[entity["parentId"]]
    return {"entities": entities, "patterns": {key: pattern for key, pattern in patterns.items() if key in ready},
            "issues": issues}


def build_entities(result, allowance_mm, notch_depth_mm, *, state=None, panel_ids=None, panel_settings=None):
    state = state if state is not None else {"panels": result.get("panels", [])}
    statuses, entities = _prepare_transfer(result, state, panel_ids, allowance_mm, notch_depth_mm, panel_settings)
    for panel in statuses:
        if not panel["ready"]:
            issue = next(issue for issue in panel["issues"] if issue.get("severity") == "error")
            detail = f" {issue['detail']}" if issue.get("detail") else ""
            raise UpholsteryError(f"{panel['name']}: {issue['message']}{detail}")
    if panel_ids is None and result.get("complete") is False:
        issue = next((issue for issue in result.get("diagnostics", []) if issue["severity"] == "error"), None)
        raise UpholsteryError(issue["message"] if issue else "Сначала рассчитайте все детали, швы и пары без ошибок.")
    if not entities:
        raise UpholsteryError("Выберите хотя бы одну рассчитанную деталь для переноса.")
    return entities

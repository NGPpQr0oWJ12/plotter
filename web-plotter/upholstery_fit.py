"""Quasi-static upholstery fitting. Distances are mm, forces N and energies N mm."""

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from scipy.sparse import coo_matrix, csr_matrix
from scipy.spatial import cKDTree

from upholstery import UpholsteryError, calculate


_CLOTH = {
    "warpNPerMm": (12., .001, 10000.), "weftNPerMm": (8., .001, 10000.),
    "shearNPerMm": (1.5, .001, 10000.), "bendingNmm": (.05, 0., 1000.),
    "massGsm": (350., 0., 10000.), "warpReductionPct": (0., -50., 50.),
    "weftReductionPct": (0., -50., 50.),
}


def _value(settings, key, default, low, high):
    value = settings.get(key, default)
    if type(value) not in (float, int) or not low <= value <= high:
        raise UpholsteryError(f"Параметр {key} должен быть числом от {low:g} до {high:g}.")
    return float(value)


def _normalize(rows):
    return rows / np.maximum(np.linalg.norm(rows, axis=-1, keepdims=True), 1e-12)


def _dot(a, b):
    return np.einsum("...i,...i->...", a, b)


def _matrix(rows, cols, data, size):
    if not rows:
        return csr_matrix((0, size))
    return coo_matrix((data, (rows, cols)), shape=(max(rows) + 1, size)).tocsr()


@dataclass
class _Panel:
    identifier: str
    offset: int
    faces: np.ndarray
    uv: np.ndarray
    source: np.ndarray
    loops: list
    edge_vertices: dict
    material: dict


def _panels(calculation, settings):
    result = {}
    offset = 0
    overrides = settings.get("panelMaterials", {})
    if not isinstance(overrides, dict):
        raise UpholsteryError("Проверьте профили материала деталей.")
    for panel in calculation["panels"]:
        pid = panel["id"]
        override = overrides.get(pid, {})
        if not isinstance(override, dict):
            raise UpholsteryError("Некорректный профиль материала детали.")
        spec = {**settings, **override}
        material = {key: _value(spec, key, *params) for key, params in _CLOTH.items()}
        grain = np.asarray(panel.get("grain", [[0., 0.], [1., 0.]]), dtype=float)
        along = _normalize(grain[1] - grain[0])
        axes = np.array([along, [-along[1], along[0]]])
        uv = np.asarray(panel["vertices"], dtype=float) @ axes.T
        uv *= 1. - np.array([material["warpReductionPct"], material["weftReductionPct"]]) / 100.
        source = np.asarray(panel["sourceVertices"], dtype=float)
        faces = np.asarray(panel["triangles"], dtype=int)
        if len(faces) == 0 or len(source) != len(uv) or not np.isfinite(uv).all() or not np.isfinite(source).all():
            raise UpholsteryError(f"Нет корректной сетки для примерки детали {pid}.")
        edge_vertices = {edge["id"]: edge["vertices"] for edge in panel.get("edges", [])}
        result[pid] = _Panel(pid, offset, faces, uv, source, panel["loops"], edge_vertices, material)
        offset += len(uv)
    if not result:
        raise UpholsteryError("Сначала создайте и разверните детали чехла.")
    if set(overrides) - result.keys():
        raise UpholsteryError("Профиль ткани ссылается на отсутствующую деталь.")
    return result


class _Shell:
    """Orthotropic finite-strain membrane and a discrete flat-rest bending hinge."""

    def __init__(self, panels):
        self.initial = np.concatenate([p.source for p in panels.values()])
        self.faces = np.concatenate([p.faces + p.offset for p in panels.values()])
        uv = np.concatenate([p.uv for p in panels.values()])
        attrs = {key: np.concatenate([np.full(len(p.faces), p.material[key]) for p in panels.values()])
                 for key in _CLOTH}
        edges = uv[self.faces[:, 1:]] - uv[self.faces[:, :1]]
        determinant = np.linalg.det(edges)
        if np.any(abs(determinant) < 1e-9):
            raise UpholsteryError("В лекале есть вырожденные треугольники.")
        self.area = abs(determinant) / 2.
        # dX/du and dX/dv as sparse linear combinations of each triangle's vertices.
        basis = np.linalg.inv(edges)
        basis = np.concatenate((-basis.sum(axis=2, keepdims=True), basis), axis=2)
        size = len(self.initial)
        face_ids = np.repeat(np.arange(len(self.faces)), 3)
        self.warp, self.weft = [coo_matrix((basis[:, i].ravel(), (face_ids, self.faces.ravel())),
                                           shape=(len(self.faces), size)).tocsr() for i in (0, 1)]
        self.kw, self.kf, self.ks = (attrs[k] for k in ("warpNPerMm", "weftNPerMm", "shearNPerMm"))
        self.nodal_area = np.bincount(self.faces.ravel(), weights=np.repeat(self.area / 3., 3), minlength=size)
        self.weight = np.bincount(self.faces.ravel(), weights=np.repeat(
            self.area * attrs["massGsm"] * 9.80665e-9 / 3., 3), minlength=size)
        # A flat rest hinge costs zero for every affine map, including rigid rotations.
        row, col, values = [], [], []
        seen = {}
        hinge_id = 0
        for i, tri in enumerate(self.faces):
            for j in range(3):
                a, b = sorted((tri[j], tri[(j + 1) % 3]))
                previous = seen.pop((a, b), None)
                if previous is None:
                    seen[(a, b)] = i
                    continue
                edge = uv[b] - uv[a]
                length = np.linalg.norm(edge)
                normal = np.array([-edge[1], edge[0]]) / length
                dual_width = 2. * (self.area[i] + self.area[previous]) / (3. * length)
                scale = np.sqrt(attrs["bendingNmm"][i] * length / dual_width)
                for idx, sign in ((i, 1.), (previous, -1.)):
                    row.extend([hinge_id] * 3)
                    col.extend(self.faces[idx])
                    values.extend(sign * scale * (normal @ basis[idx]))
                hinge_id += 1
        self.bending = _matrix(row, col, values, size)

    def strains(self, x):
        warp, weft = self.warp @ x, self.weft @ x
        lw, lf = np.linalg.norm(warp, axis=1), np.linalg.norm(weft, axis=1)
        return warp, weft, lw, lf, _dot(warp, weft)

    def energy(self, x):
        warp, weft, lw, lf, shear = self.strains(x)
        ew, ef = lw - 1., lf - 1.
        energy = .5 * self.area @ (self.kw * ew**2 + self.kf * ef**2 + self.ks * shear**2)
        sw = (self.area * self.kw * ew / np.maximum(lw, 1e-12))[:, None]
        sf = (self.area * self.kf * ef / np.maximum(lf, 1e-12))[:, None]
        ss = (self.area * self.ks * shear)[:, None]
        grad = self.warp.T @ (sw * warp + ss * weft) + self.weft.T @ (sf * weft + ss * warp)
        bend = self.bending @ x
        energy += .5 * np.sum(bend**2) + self.weight @ (x[:, 2] - self.initial[:, 2])
        grad += self.bending.T @ bend
        grad[:, 2] += self.weight
        return energy, grad


def _edge_constraints(panels, settings):
    fixed = settings.get("fixedEdges", [])
    if not isinstance(fixed, list):
        raise UpholsteryError("Фиксацию задают списком кромок.")
    vertices = set()
    for item in fixed:
        if not isinstance(item, dict):
            raise UpholsteryError("Проверьте кромку крепления.")
        identifier = item.get("panelId")
        panel = panels.get(identifier) if isinstance(identifier, str) else None
        if "edgeId" in item:
            identifier = item["edgeId"]
            if panel is None or not isinstance(identifier, str) or identifier not in panel.edge_vertices:
                raise UpholsteryError("Сохранённая кромка крепления исчезла. Выберите её заново.")
            vertices.update(panel.offset + v for v in panel.edge_vertices[identifier])
            continue
        loop, idx = item.get("loop", 0), item.get("index")
        if (panel is None or type(loop) is not int or not 0 <= loop < len(panel.loops)
                or type(idx) is not int or not 0 <= idx < len(panel.loops[loop])):
            raise UpholsteryError("Контур или кромка крепления больше не существуют.")
        indices = panel.loops[loop]
        vertices.update(panel.offset + indices[k % len(indices)] for k in (idx, idx + 1))
    return sorted(vertices)


def _stitches(panels, stitches, size):
    rows, cols, weights = [], [], []
    row_id = 0
    for stitch in stitches:
        samples = {0., 1.}
        sides = []
        for name in ("a", "b"):
            side = stitch[name]
            panel = panels[side["panelId"]]
            indices = np.asarray(side["vertices"], dtype=int) + panel.offset
            fractions = np.asarray(side["fractions"], dtype=float)
            start, end = side.get("range", [0., 1.])
            if abs(end - start) < 1e-10 or len(fractions) < 2 or np.any(np.diff(fractions) <= 0):
                raise UpholsteryError("Участок стачивания вырожден.")
            coords = (fractions - start) / (end - start)
            samples.update(coords[(coords > 0) & (coords < 1)])
            sides.append((indices, fractions, start, end))
        for t in sorted(samples):
            for sign, (indices, fractions, start, end) in zip((1., -1.), sides):
                u = start + t * (end - start)
                i = min(len(indices) - 2, max(0, np.searchsorted(fractions, u, side="right") - 1))
                v = (u - fractions[i]) / (fractions[i + 1] - fractions[i])
                rows.extend((row_id, row_id))
                cols.extend(indices[i:i + 2])
                weights.extend((sign * (1. - v), sign * v))
            row_id += 1
    return _matrix(rows, cols, weights, size)


def _closest(points, tri):
    """Closest point on each finite triangle, including its edges."""
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    u, v, w = b - a, c - a, points - a
    uu, vv, uv = _dot(u, u), _dot(v, v), _dot(u, v)
    determinant = np.maximum(uu * vv - uv**2, 1e-30)
    s = (_dot(w, u) * vv - _dot(w, v) * uv) / determinant
    t = (_dot(w, v) * uu - _dot(w, u) * uv) / determinant
    bary = np.column_stack((1. - s - t, s, t))
    projection = np.einsum("ni,nij->nj", bary, tri)
    best = np.where(bary.min(axis=1) >= 0, np.sum((points - projection)**2, axis=1), np.inf)
    for i, j in ((0, 1), (1, 2), (2, 0)):
        edge = tri[:, j] - tri[:, i]
        h = np.clip(_dot(points - tri[:, i], edge) / np.maximum(_dot(edge, edge), 1e-30), 0., 1.)
        pos = tri[:, i] + h[:, None] * edge
        score = np.sum((pos - points)**2, axis=1)
        update = score < best
        projection[update] = pos[update]
        bary[update] = 0.
        bary[update, i], bary[update, j] = 1. - h[update], h[update]
        best = np.minimum(best, score)
    return best, projection, bary


class _FoamLaw:
    def __init__(self, settings):
        self.thickness = _value(settings, "foamThicknessMm", 50., .1, 2000.)
        modulus = _value(settings, "foamModulusKPa", 30., .01, 100000.)
        curve = settings.get("foamCurve")
        if curve is None:
            strain, pressure = [0., .9], [0., .9 * modulus * .001]
        else:
            if not isinstance(curve, list) or not 2 <= len(curve) <= 100:
                raise UpholsteryError("Нужны от 2 до 100 точек кривой ППУ.")
            if any(not isinstance(row, dict) for row in curve):
                raise UpholsteryError("Каждая точка кривой ППУ задаёт сжатие и давление.")
            strain = [_value(row, "compressionPct", None, 0., 99.9) / 100. for row in curve]
            pressure = [_value(row, "pressureKPa", None, 0., 100000.) * .001 for row in curve]
            if strain[0] != 0 or pressure[0] != 0 or np.any(np.diff(strain) <= 0) or np.any(np.diff(pressure) < 0) or pressure[-1] <= 0:
                raise UpholsteryError("Кривая ППУ начинается в (0, 0); сжатие растёт, давление не убывает.")
        self.measured_curve = curve is not None
        self.depth = np.asarray(strain) * self.thickness
        self.pressure = np.asarray(pressure)
        slopes = np.diff(self.pressure) / np.diff(self.depth)
        # An elastic backing limits extrapolation beyond the measured compression range.
        self.slopes = np.append(slopes, max(slopes[-1], self.pressure[-1] / self.depth[-1]))
        self.integral = np.r_[0., np.cumsum(.5 * (self.pressure[:-1] + self.pressure[1:]) * np.diff(self.depth))]
        self.friction = _value(settings, "friction", .35, 0., 3.)

    def response(self, depth):
        i = np.searchsorted(self.depth, depth, side="right") - 1
        d = depth - self.depth[i]
        p = self.pressure[i] + self.slopes[i] * d
        e = self.integral[i] + self.pressure[i] * d + .5 * self.slopes[i] * d**2
        # A near-bottomed-out layer stiffens sharply. A result past thickness is reported.
        bottom = np.maximum(0., depth - max(self.depth[-1], .95 * self.thickness))
        k = 1000. * self.slopes[-1]
        return e + .5 * k * bottom**2, p + k * bottom


class _Foundation:
    def __init__(self, model, settings):
        requested = settings.get("foamNodeIds")
        zones = settings.get("foamZones", {})
        if (not isinstance(requested, list) or not requested or any(not isinstance(key, str) for key in requested)
                or len(requested) != len(set(requested))):
            raise UpholsteryError("Выберите поверхности ППУ для примерки.")
        if not isinstance(zones, dict) or set(zones) - set(requested):
            raise UpholsteryError("Проверьте выбранные зоны и свойства ППУ.")
        nodes = {node["id"]: node for node in model["nodes"]}
        self.zones, triangles, normals, smooth_normals, zone_ids = [], [], [], [], []
        for i, key in enumerate(requested):
            node, override = nodes.get(key), zones.get(key, {})
            if not node or not node.get("triangles") or not isinstance(override, dict):
                raise UpholsteryError("У выбранной зоны ППУ нет поверхности или настроек.")
            law = _FoamLaw({**settings, **override})
            verts = np.asarray(node["vertices"], dtype=float)
            faces = np.asarray(node["triangles"], dtype=int)
            tris = verts[faces]
            area_normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
            valid = np.linalg.norm(area_normals, axis=1) > 1e-8
            faces, tris, area_normals = faces[valid], tris[valid], area_normals[valid]
            if not len(tris):
                raise UpholsteryError("Все треугольники опоры ППУ вырождены.")
            # Closed consistently wound skins can be oriented using signed volume.
            _, weld = np.unique(np.round(verts, 7), axis=0, return_inverse=True)
            welded = weld[faces]
            edge = np.sort(np.concatenate((welded[:, [0, 1]], welded[:, [1, 2]], welded[:, [2, 0]])), axis=1)
            closed = np.all(np.unique(edge, axis=0, return_counts=True)[1] == 2)
            reverse = closed and _dot(tris[:, 0] - verts.mean(axis=0), area_normals).sum() < 0
            sign = -1. if bool(reverse) != bool(override.get("flipNormals", False)) else 1.
            normals.append(sign * _normalize(area_normals))
            averaged = np.column_stack([np.bincount(welded.ravel(), weights=np.repeat(area_normals[:, c], 3),
                                                     minlength=int(weld.max()) + 1) for c in range(3)])
            smooth_normals.append(sign * _normalize(averaged)[welded])
            triangles.append(tris)
            zone_ids.extend([i] * len(tris))
            self.zones.append((key, law, faces, verts))
        self.tri = np.concatenate(triangles)
        self.normal = np.concatenate(normals)
        self.smooth_normals = np.concatenate(smooth_normals)
        self.zone = np.asarray(zone_ids)
        centers = self.tri.mean(axis=1)
        radii = np.linalg.norm(self.tri - centers[:, None, :], axis=2).max(axis=1)
        levels = np.floor(np.log2(np.maximum(radii, 1e-8)))
        self.trees = []
        for level in np.unique(levels):
            ids = np.flatnonzero(levels == level)
            self.trees.append((ids, cKDTree(centers[ids]), radii[ids].max()))

    def closest(self, x, previous=None):
        # Bounds by centroid radius are exact even when SKP mixes tiny and very large faces.
        if previous is not None:
            _, point, _, valid, normal = previous
            depth = np.where(valid, np.maximum(0., -_dot(x - point, normal)), 0.)
            # Search on the unloaded skin after undoing the previous normal indentation.
            # This keeps an indented cloth vertex on the same foam column at faceted crests.
            x = x + depth[:, None] * normal
        count = len(x)
        face = np.zeros(count, dtype=int)
        point, bary = np.zeros_like(x), np.zeros_like(x)
        score = np.full(count, np.inf)
        for ids, tree, radius in self.trees:
            nearest, local = tree.query(x)
            possible = np.flatnonzero(nearest - radius <= np.sqrt(score) + 1e-9)
            for begin in range(0, len(possible), 128):
                chunk = possible[begin:begin + 128]
                upper = np.sqrt(np.minimum(score[chunk], _closest(x[chunk], self.tri[ids[local[chunk]]])[0]))
                groups = tree.query_ball_point(x[chunk], upper + radius + 1e-8, return_sorted=True)
                copies = np.array([len(group) for group in groups])
                mask = copies > 0
                if not np.any(mask):
                    continue
                chunk, copies = chunk[mask], copies[mask]
                candidates = ids[np.concatenate(groups[mask]).astype(int)]
                owners = np.repeat(chunk, copies)
                distances, points, weights = _closest(x[owners], self.tri[candidates])
                starts = np.r_[0, np.cumsum(copies)[:-1]]
                mins = np.minimum.reduceat(distances, starts)
                # The first equal-distance candidate has deterministic face ordering.
                winners = np.minimum.reduceat(np.where(distances == np.repeat(mins, copies),
                                                       np.arange(len(distances)), len(distances)), starts)
                better = mins < score[chunk]
                out, take = chunk[better], winners[better]
                score[out], face[out] = distances[take], candidates[take]
                point[out], bary[out] = points[take], weights[take]
        normal = self.normal[face]
        offset = x - point
        lateral = offset - _dot(offset, normal)[:, None] * normal
        # Outside the rim of a finite foam sheet the supporting plane ends.
        valid = np.linalg.norm(lateral, axis=1) < 1e-4
        interpolated = _normalize(np.einsum("ni,nij->nj", bary, self.smooth_normals[face]))
        continuous = _dot(interpolated, normal) > .8
        normal = np.where(continuous[:, None], interpolated, normal)
        return face, point, bary, valid, normal

    def energy(self, x, contact, initial, area, friction_load):
        faces, points, _, valid, normal = contact
        signed = _dot(x - points, normal)
        depth = np.maximum(0., -signed)
        depth[~valid] = 0.
        pressure = np.zeros_like(depth)
        energy, tangential = 0., np.zeros_like(x)
        for zone, (_, law, _, _) in enumerate(self.zones):
            mask = self.zone[faces] == zone
            e, p = law.response(depth[mask])
            pressure[mask] = p
            energy += area[mask] @ e
            delta = x[mask] - initial[mask]
            slip = delta - _dot(delta, normal[mask])[:, None] * normal[mask]
            length = np.sqrt(_dot(slip, slip) + .1**2)
            load = law.friction * friction_load[mask] * area[mask] * valid[mask]
            energy += load @ (length - .1)
            tangential[mask] = (load / length)[:, None] * slip
        grad = tangential - (area * pressure)[:, None] * normal
        return energy, grad, signed, pressure


class _SelfContact:
    """Discrete vertex/triangle contact, with a frozen contact frame per load increment."""

    def __init__(self, x, shell, stitch, clearance):
        faces = shell.faces
        tri = x[faces]
        centers = tri.mean(axis=1)
        radii = np.linalg.norm(tri - centers[:, None, :], axis=2).max(axis=1)
        levels = np.floor(np.log2(np.maximum(1e-8, radii)))
        links = []
        # Do not repel a sewing line from the very triangle it is being sewn to.
        neighbors = defaultdict(set)
        for i in range(stitch.shape[0]):
            indices = stitch.indices[stitch.indptr[i]:stitch.indptr[i + 1]]
            for v in indices:
                neighbors[v].update(indices)
        for level in np.unique(levels):
            ids = np.flatnonzero(levels == level)
            tree = cKDTree(centers[ids])
            radius = radii[ids].max() + 2 * clearance
            for start in range(0, len(x), 128):
                vertices = np.arange(start, min(start + 128, len(x)))
                groups = tree.query_ball_point(x[vertices], radius, return_sorted=True)
                count = [len(g) for g in groups]
                if sum(count) == 0:
                    continue
                v, candidates = np.repeat(vertices, count), ids[np.concatenate(groups).astype(int)]
                mask = np.all(faces[candidates] != v[:, None], axis=1)
                v, candidates = v[mask], candidates[mask]
                dist, point, weights = _closest(x[v], tri[candidates])
                # Shared rims on coplanar adjoining panels have no overlapping face area.
                candidate_normal = _normalize(np.cross(tri[candidates, 1] - tri[candidates, 0],
                                                       tri[candidates, 2] - tri[candidates, 0]))
                offset = x[v] - point
                normal_offset = _dot(offset, candidate_normal)
                lateral = offset - normal_offset[:, None] * candidate_normal
                on_face = (weights.min(axis=1) > 1e-7) | (
                    (np.abs(normal_offset) > 1e-7) & (_dot(lateral, lateral) < 1e-10))
                selected = np.flatnonzero((dist < (2 * clearance)**2) & on_face)
                for i in selected:
                    owner, f = v[i], faces[candidates[i]]
                    if any(a in neighbors.get(owner, ()) for a in f):
                        continue
                    normal = candidate_normal[i]
                    # The assembled placement establishes the side for this contact pair.
                    initial_delta = shell.initial[owner] - weights[i] @ shell.initial[f]
                    if initial_delta @ normal < 0:
                        normal = -normal
                    links.append((owner, f, weights[i], normal))
        rows, cols, coefficients, normals = [], [], [], []
        for i, (v, f, weights, norm) in enumerate(links):
            rows.extend([i] * 4)
            cols.extend([v, *f])
            coefficients.extend([1., *-weights])
            normals.append(norm)
        self.matrix = _matrix(rows, cols, coefficients, len(x))
        self.normals = np.asarray(normals).reshape(-1, 3)
        self.clearance = clearance

    def energy(self, x):
        gap = _dot(self.matrix @ x, self.normals)
        penetration = np.maximum(0., self.clearance - gap)
        stiffness = 10.  # N/mm per active discretized contact
        return (.5 * stiffness * (penetration @ penetration),
                self.matrix.T @ ((-stiffness * penetration)[:, None] * self.normals),
                float(penetration.max(initial=0.)))


def simulate(model, markup, settings=None, *, calculated=None):
    settings = markup.get("fit", {}) if settings is None else settings
    if not isinstance(settings, dict):
        raise UpholsteryError("Некорректные настройки примерки.")
    foundation = _Foundation(model, settings)
    calc = calculate(model, markup) if calculated is None else calculated
    if not calc.get("complete", True):
        raise UpholsteryError("Устраните ошибки развёртки и пар швов перед примеркой.")
    panels = _panels(calc, settings)
    shell = _Shell(panels)
    x = shell.initial.copy()
    free = np.ones(len(x), dtype=bool)
    fixed = _edge_constraints(panels, settings)
    free[fixed] = False
    stitch = _stitches(panels, calc.get("fitStitches", []), len(x))
    if (markup.get("pairs") or markup.get("joins")) and stitch.shape[0] == 0:
        raise UpholsteryError("Нет пригодного для примерки соответствия участков швов.")
    stitch_weight = 80.  # N/mm per stitch sample
    # Scale optimizer coordinates to avoid poorly conditioned steps at small SKP facets.
    diagonal = (shell.warp.power(2).T @ (shell.area * shell.kw)
                + shell.weft.power(2).T @ (shell.area * shell.kf)
                + np.asarray(shell.bending.power(2).sum(axis=0)).ravel()
                + np.asarray(stitch.power(2).sum(axis=0)).ravel() * stitch_weight)
    scale = (1. / np.sqrt(np.maximum(diagonal, .01)))[free, None]
    tolerance = .003  # N maximum nodal unbalanced force
    clearance = _value(settings, "clothThicknessMm", 1., .05, 15.)
    pressure = np.zeros(len(x))
    previous_separation = stitch @ x
    sewing_force = np.zeros_like(previous_separation)
    iterations = 0
    converged = False
    warnings = []
    if not fixed:
        warnings.append("Нет явно закреплённых кромок. Положение определяется швами, опорой и трением.")
    contact = foundation.closest(x)
    stages = [.35, .7] if stitch.shape[0] else []
    for fraction in [*stages, *([1.] * 12)]:
        collision = _SelfContact(x, shell, stitch, clearance)
        friction_load = pressure.copy()
        target = previous_separation * (1. - fraction)

        def balance(current):
            energy, grad = shell.energy(current)
            delta = stitch @ current - target
            energy += .5 * stitch_weight * np.sum(delta**2) + np.sum(sewing_force * delta)
            grad += stitch.T @ (stitch_weight * delta + sewing_force)
            support, reaction, _, _ = foundation.energy(current, contact, shell.initial,
                                                        shell.nodal_area, friction_load)
            self_energy, self_grad, _ = collision.energy(current)
            return energy + support + self_energy, grad + reaction + self_grad

        if free.any():
            reference = x.copy()

            def objective(displacement):
                trial = reference.copy()
                trial[free] += displacement.reshape(-1, 3) * scale
                energy, grad = balance(trial)
                return energy, (grad[free] * scale).ravel()

            # Limit travel within one contact linearization. Then rebuild closest features.
            trust = np.maximum(2., np.sqrt(shell.nodal_area[free]) * .5)[:, None]
            # Enough travel to sew free panels that were intentionally placed apart.
            if previous_separation.size:
                trust = np.maximum(trust, .5 * np.linalg.norm(previous_separation, axis=1).max())
            bound = np.repeat((trust / scale).ravel(), 3)
            sol = minimize(objective, np.zeros(scale.size * 3), jac=True, method="L-BFGS-B",
                           bounds=np.column_stack((-bound, bound)),
                           options={"maxiter": 80, "ftol": 1e-13, "gtol": 1e-6, "maxls": 24})
            iterations += sol.nit
            if not np.isfinite(sol.fun) or not np.isfinite(sol.x).all():
                raise UpholsteryError("Численный расчёт примерки разошёлся. Проверьте сетку и крепления.")
            x[free] += sol.x.reshape(-1, 3) * scale
        # Check residuals at the recomputed contact locations with the actual normal load.
        contact = foundation.closest(x, contact)
        _, _, signed, pressure = foundation.energy(x, contact, shell.initial, shell.nodal_area, friction_load)
        if fraction == 1.:
            friction_load = pressure
            collision = _SelfContact(x, shell, stitch, clearance)
            _, grad = balance(x)
            residual = float(np.linalg.norm(grad[free], axis=1).max(initial=0.))
            seam_gap = float(np.linalg.norm(stitch @ x, axis=1).max(initial=0.))
            collision_depth = collision.energy(x)[2]
            converged = residual <= tolerance and seam_gap <= .25 and collision_depth <= .25
            if converged:
                break
            # Augmented Lagrangian sewing closes stressed joins without forcing enormous stiffness.
            sewing_force += stitch_weight * (stitch @ x)
    face, contact_points, bary, valid, normals = contact
    depth = np.where(valid, np.maximum(0., -signed), 0.)
    gap = np.linalg.norm(x - contact_points, axis=1)
    gap[valid] = np.maximum(signed[valid], 0.)
    cloth_measured = all(key in settings or all(key in settings.get("panelMaterials", {}).get(p, {})
                                                for p in panels) for key in
                         ("warpNPerMm", "weftNPerMm", "shearNPerMm", "bendingNmm", "massGsm"))
    foam_measured = True
    for i, (key, law, _, _) in enumerate(foundation.zones):
        profile = {**settings, **settings.get("foamZones", {}).get(key, {})}
        foam_measured &= "foamThicknessMm" in profile and "friction" in profile and law.measured_curve
        local_depth = depth[foundation.zone[face] == i]
        if law.measured_curve and local_depth.max(initial=0.) > law.depth[-1] + 1e-6:
            warnings.append(f"Зона {key}: сжатие выходит за измеренную кривую ППУ.")
            foam_measured = False
    calibrated = settings.get("calibrated") is True and cloth_measured and foam_measured
    if not calibrated:
        warnings.append("Расчёт на оценочных свойствах. Для количественной проверки нужны измерения ткани, ППУ и трения.")
    limits = [
        "ППУ сжимается по нормали независимо на каждом участке. Поперечное распирание и ползучесть не моделируются.",
        "Контакт ткани дискретный, вершина–треугольник; пересечения рёбер между шагами могут быть пропущены.",
        "Трение приближено к сухому трению при однократной установке. Последовательность ручных перехватов не моделируется.",
        "Жёсткости ткани заданы для рабочего диапазона нагрузки. Усилие рассчитано на единицу ширины ткани.",
    ]
    if any(np.any(depth[foundation.zone[face] == i] > law.thickness)
           for i, (_, law, _, _) in enumerate(foundation.zones)):
        warnings.append("Расчётное сжатие превысило толщину ППУ. Проверьте опору и крепления.")
        converged = False
    if not converged:
        warnings.append("Равновесие в заданных пределах не подтверждено. Показано последнее приближение.")
    warp, weft, lw, lf, shear = shell.strains(x)
    shear_angle = np.rad2deg(np.arcsin(np.clip(shear / np.maximum(lw * lf, 1e-12), -1., 1.)))
    # Principal tensile resultant in the material plane (force per original unit width).
    sw, sf, tau = shell.kw * (lw - 1.), shell.kf * (lf - 1.), shell.ks * shear
    tension = np.maximum(0., .5 * (sw + sf) + np.hypot(.5 * (sw - sf), tau))
    rendered, first = [], 0
    for panel in panels.values():
        end = first + len(panel.faces)
        region = slice(first, end)
        tri = panel.faces + panel.offset
        values = np.column_stack(((lw[region] - 1.) * 100., (lf[region] - 1.) * 100.,
                                  shear_angle[region], tension[region], depth[tri].max(axis=1),
                                  gap[tri].max(axis=1)))
        keys = ("warpPct", "weftPct", "shearDeg", "tensionNPerMm", "compressionMm", "gapMm")
        strain = [dict(zip(keys, row.tolist())) for row in np.round(values, 6)]
        peaks = values.max(axis=0)
        rendered.append({"id": panel.identifier, "triangles": panel.faces.tolist(), "strain": strain,
                         "vertices": x[panel.offset:panel.offset + len(panel.source)].round(6).tolist(),
                         "maxWarpPct": float(peaks[0]), "maxWeftPct": float(peaks[1]),
                         "maxShearDeg": float(np.abs(values[:, 2]).max()),
                         "maxTensionNPerMm": float(peaks[3]), "maxCompressionMm": float(peaks[4]),
                         "maxGapMm": float(peaks[5])})
        first = end
    foam, start = [], 0
    for zone_id, (key, law, tri, verts) in enumerate(foundation.zones):
        selected = (foundation.zone[face] == zone_id) & valid
        touching = selected & (signed <= 1e-4)
        local = face[selected] - start
        weights = (shell.nodal_area[selected, None] * bary[selected]).ravel()
        ids = tri[local].ravel()
        summed = np.bincount(ids, weights=weights, minlength=len(verts))
        disp = -depth[selected, None] * normals[selected]
        displacement = np.column_stack([np.bincount(ids, weights=weights * np.repeat(disp[:, c], 3),
                                                     minlength=len(verts)) for c in range(3)])
        displacement /= np.maximum(summed[:, None], 1e-12)
        foam.append({"nodeId": key, "vertices": (verts + displacement).round(6).tolist(),
                     "triangles": tri.tolist(), "maxCompressionMm": float(depth[selected].max(initial=0.)),
                     "contactVertices": int(np.count_nonzero(touching)),
                     "contactClothAreaMm2": float(shell.nodal_area[touching].sum())})
        if not touching.any():
            warnings.append(f"Зона {key}: ни одна вершина чехла не касается опоры.")
        start += len(tri)
    return {"status": "converged" if converged else "approximate", "converged": bool(converged),
            "calibrated": bool(calibrated), "panels": rendered, "foam": foam, "warnings": warnings, "limits": limits,
            "metrics": {"seamGapMm": seam_gap, "residualN": residual, "iterations": iterations,
                        "selfContactResidualMm": collision_depth, "fixedVertices": len(fixed),
                        "forceToleranceN": tolerance, "seamToleranceMm": .25, "selfContactToleranceMm": .25,
                        "stitchSamples": stitch.shape[0], "maxCompressionMm": float(depth.max(initial=0.)),
                        "maxGapMm": float(gap.max(initial=0.)), "unsupportedVertices": int(sum(~valid))}}

"""Read SketchUp geometry with the locally installed Windows x64 C API."""

from __future__ import annotations

import argparse
import ctypes as c
import json
import math
import os
from pathlib import Path
import sys
import threading


INCH_TO_MM = 25.4
SU_ERROR_NO_DATA = 9
IDENTITY = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
SDK_LOCK = threading.Lock()


class SkpReadError(Exception):
    """The local SketchUp reader could not import the model."""


class Ref(c.Structure):
    _fields_ = [("ptr", c.c_void_p)]


class Point(c.Structure):
    _fields_ = [(axis, c.c_double) for axis in "xyz"]


class Transformation(c.Structure):
    _fields_ = [("values", c.c_double * 16)]


class Color(c.Structure):
    _fields_ = [(channel, c.c_uint8) for channel in ("red", "green", "blue", "alpha")]


def _runtime_path() -> Path:
    override = os.environ.get("PLOTTER_SKETCHUP_DLL")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file():
            raise SkpReadError("PLOTTER_SKETCHUP_DLL does not point to an existing DLL.")
        return path

    install_root = Path(os.environ.get("ProgramW6432") or os.environ["ProgramFiles"]) / "SketchUp"
    candidates = []
    for directory in install_root.glob("SketchUp *"):
        year = directory.name.removeprefix("SketchUp ")
        if not year.isdecimal():
            continue
        for location in (directory / "SketchUp", directory):
            dll_path = location / "SketchUpAPI.dll"
            if dll_path.is_file():
                candidates.append((int(year), location != directory, dll_path))
    if not candidates:
        raise SkpReadError("Install SketchUp for Windows x64 or set PLOTTER_SKETCHUP_DLL.")
    return max(candidates)[2]


class SketchUpApi:
    """Bindings for the subset of the SketchUp C SDK used by this reader."""

    def __init__(self, dll: c.CDLL):
        refptr, sizeptr = c.POINTER(Ref), c.POINTER(c.c_size_t)
        boolptr = c.POINTER(c.c_bool)
        signatures = {
            "SUInitialize": (None,),
            "SUTerminate": (None,),
            "SUModelCreateFromBufferWithStatus": (
                c.c_int, refptr, c.POINTER(c.c_ubyte), c.c_size_t, c.POINTER(c.c_int),
            ),
            "SUModelRelease": (c.c_int, refptr),
            "SUStringCreate": (c.c_int, refptr),
            "SUStringRelease": (c.c_int, refptr),
            "SUStringGetUTF8Length": (c.c_int, Ref, sizeptr),
            "SUStringGetUTF8": (c.c_int, Ref, c.c_size_t, c.POINTER(c.c_char), sizeptr),
            "SUEntityGetPersistentID": (c.c_int, Ref, c.POINTER(c.c_int64)),
            "SUDrawingElementToEntity": (Ref, Ref),
            "SUEntitiesGetNumEdges": (c.c_int, Ref, c.c_bool, sizeptr),
            "SUEntitiesGetEdges": (c.c_int, Ref, c.c_bool, c.c_size_t, refptr, sizeptr),
            "SUGroupGetTransform": (c.c_int, Ref, c.POINTER(Transformation)),
            "SUComponentInstanceGetTransform": (c.c_int, Ref, c.POINTER(Transformation)),
            "SUVertexGetPosition": (c.c_int, Ref, c.POINTER(Point)),
            "SUMaterialGetColor": (c.c_int, Ref, c.POINTER(Color)),
            "SUMeshHelperCreate": (c.c_int, refptr, Ref),
            "SUMeshHelperRelease": (c.c_int, refptr),
            "SUMeshHelperGetNumVertices": (c.c_int, Ref, sizeptr),
            "SUMeshHelperGetVertices": (c.c_int, Ref, c.c_size_t, c.POINTER(Point), sizeptr),
            "SUMeshHelperGetNumTriangles": (c.c_int, Ref, sizeptr),
            "SUMeshHelperGetVertexIndices": (c.c_int, Ref, c.c_size_t, sizeptr, sizeptr),
        }
        for name in (
            "SUModelGetEntities", "SUGroupGetEntities", "SUGroupGetName", "SUComponentInstanceGetName",
            "SUComponentInstanceGetDefinition", "SUComponentDefinitionGetEntities",
            "SUComponentDefinitionGetName", "SUDrawingElementGetLayer", "SUDrawingElementGetMaterial",
            "SULayerGetParentLayerFolder", "SULayerFolderGetParentLayerFolder", "SUFaceGetOuterLoop",
            "SUFaceGetFrontMaterial", "SUFaceGetBackMaterial", "SUEdgeGetStartVertex", "SUEdgeGetEndVertex",
        ):
            signatures[name] = (c.c_int, Ref, refptr)
        for name in (
            "SUDrawingElementGetHidden", "SULayerGetVisibility", "SULayerFolderGetVisibility",
            "SUEdgeGetSoft", "SUEdgeGetSmooth",
        ):
            signatures[name] = (c.c_int, Ref, boolptr)
        for prefix, item in (
            ("SUEntities", "Groups"), ("SUEntities", "Instances"), ("SUEntities", "Faces"),
            ("SUFace", "InnerLoops"), ("SULoop", "Vertices"),
        ):
            signatures[f"{prefix}GetNum{item}"] = (c.c_int, Ref, sizeptr)
            signatures[f"{prefix}Get{item}"] = (c.c_int, Ref, c.c_size_t, refptr, sizeptr)
        for prefix in ("SUGroup", "SUComponentInstance", "SUFace", "SUEdge"):
            signatures[f"{prefix}ToDrawingElement"] = (Ref, Ref)
        for name, (result, *arguments) in signatures.items():
            try:
                function = getattr(dll, name)
            except AttributeError as error:
                raise SkpReadError(f"The installed SketchUp runtime lacks {name}.") from error
            function.restype = result
            function.argtypes = arguments
            setattr(self, name, function)

    def check(self, name, *args, optional=False):
        result = getattr(self, name)(*args)
        if result and (not optional or result != SU_ERROR_NO_DATA):
            raise SkpReadError(f"SketchUp API {name} failed with code {result}.")
        return result == 0

    def get(self, name, *args, output=Ref, optional=False):
        value = output()
        self.check(name, *args, c.byref(value), optional=optional)
        return value.value if hasattr(value, "value") else value

    def array(self, name, items, size, *args):
        if size == 0:
            return ()
        buffer, count = (items * size)(), c.c_size_t()
        self.check(name, *args, size, buffer, c.byref(count))
        return buffer[:count.value]

    def refs(self, prefix, items, *args):
        size = self.get(f"{prefix}GetNum{items}", *args, output=c.c_size_t)
        return self.array(f"{prefix}Get{items}", Ref, size, *args)

    def name(self, name, ref):
        string = self.get("SUStringCreate")
        try:
            self.check(name, ref, c.byref(string), optional=True)
            size = self.get("SUStringGetUTF8Length", string, output=c.c_size_t)
            buffer = c.create_string_buffer(size + 1)
            self.get("SUStringGetUTF8", string, len(buffer), buffer, output=c.c_size_t)
            return buffer.value.decode("utf-8", errors="replace")
        finally:
            self.check("SUStringRelease", c.byref(string))

    def persistent_id(self, drawing_element, fallback):
        entity = self.SUDrawingElementToEntity(drawing_element)
        value = self.get("SUEntityGetPersistentID", entity, output=c.c_int64, optional=True)
        return str(value) if value else f"index-{fallback}"


def _millimeter_transform(transform):
    # SketchUp stores matrices column-major and uses w = 1 / uniform scale.
    w = transform.values[15]
    if not w:
        raise SkpReadError("The model contains an invalid SketchUp transform.")
    values = [coefficient / w for coefficient in transform.values]
    values[12:15] = [value * INCH_TO_MM for value in values[12:15]]
    if not all(map(math.isfinite, values)):
        raise SkpReadError("The model contains an invalid SketchUp transform.")
    return values


def _compose(parent, local):
    return [
        sum(parent[row + 4 * term] * local[term + 4 * column] for term in range(4))
        for column in range(4)
        for row in range(4)
    ]


def _mirrored(matrix):
    a, d, g, _, b, e, h, _, c_, f, i, *_ = matrix
    return a * (e * i - f * h) - b * (d * i - f * g) + c_ * (d * h - e * g) < 0


class GeometryReader:
    def __init__(self, api):
        self.api = api
        self.nodes = []
        self.layer_visibility = {}

    def is_visible(self, element):
        if self.api.get("SUDrawingElementGetHidden", element, output=c.c_bool):
            return False
        layer = self.api.get("SUDrawingElementGetLayer", element, optional=True)
        if not layer.ptr:
            return True
        if layer.ptr not in self.layer_visibility:
            visible = self.api.get("SULayerGetVisibility", layer, output=c.c_bool)
            folder = self.api.get("SULayerGetParentLayerFolder", layer, optional=True)
            while visible and folder.ptr:
                visible = self.api.get("SULayerFolderGetVisibility", folder, output=c.c_bool)
                folder = self.api.get("SULayerFolderGetParentLayerFolder", folder, optional=True)
            self.layer_visibility[layer.ptr] = visible
        return self.layer_visibility[layer.ptr]

    def color(self, method, ref):
        material = self.api.get(method, ref, optional=True)
        if not material.ptr:
            return None
        color = Color()
        if self.api.check("SUMaterialGetColor", material, c.byref(color), optional=True):
            return [color.red, color.green, color.blue]
        return None

    def face_data(self, face, add_point, vertex_index):
        api = self.api
        loops = [api.get("SUFaceGetOuterLoop", face), *api.refs("SUFace", "InnerLoops", face)]
        loops = [[vertex_index(vertex) for vertex in api.refs("SULoop", "Vertices", loop)] for loop in loops]
        mesh = Ref()
        api.check("SUMeshHelperCreate", c.byref(mesh), face)
        try:
            vertex_count = api.get("SUMeshHelperGetNumVertices", mesh, output=c.c_size_t)
            triangle_count = api.get("SUMeshHelperGetNumTriangles", mesh, output=c.c_size_t)
            points = api.array("SUMeshHelperGetVertices", Point, vertex_count, mesh)
            indices = api.array("SUMeshHelperGetVertexIndices", c.c_size_t, 3 * triangle_count, mesh)
            return loops, [add_point(point) for point in points], indices
        finally:
            api.check("SUMeshHelperRelease", c.byref(mesh))

    def geometry(self, entities, node, world, inherited_color):
        api = self.api
        by_position, by_vertex = {}, {}
        vertices = node["vertices"]
        reverse = _mirrored(world)

        def add_point(point):
            position = (point.x, point.y, point.z)
            if position not in by_position:
                x, y, z = (coordinate * INCH_TO_MM for coordinate in position)
                result = [
                    world[axis] * x + world[axis + 4] * y + world[axis + 8] * z + world[axis + 12]
                    for axis in range(3)
                ]
                if not all(map(math.isfinite, result)):
                    raise SkpReadError("The model contains a non-finite vertex.")
                by_position[position] = len(vertices)
                vertices.append(result)
            return by_position[position]

        def vertex_index(vertex):
            if vertex.ptr not in by_vertex:
                by_vertex[vertex.ptr] = add_point(api.get("SUVertexGetPosition", vertex, output=Point))
            return by_vertex[vertex.ptr]

        for index, face in enumerate(api.refs("SUEntities", "Faces", entities), 1):
            drawing = api.SUFaceToDrawingElement(face)
            face_id = api.persistent_id(drawing, index)
            loops, mesh_vertices, indices = self.face_data(face, add_point, vertex_index)
            node["faceLoops"][face_id] = loops
            flags = {"visible": self.is_visible(drawing)}
            front_color = self.color("SUFaceGetFrontMaterial", face) or inherited_color
            back_color = self.color("SUFaceGetBackMaterial", face) or inherited_color
            if front_color is not None:
                flags["frontColor"] = front_color
            if back_color is not None:
                flags["backColor"] = back_color
            node["faceFlags"][face_id] = flags
            for offset in range(0, len(indices), 3):
                a, b, c_ = (mesh_vertices[vertex] for vertex in indices[offset:offset + 3])
                node["triangles"].append([a, c_, b] if reverse else [a, b, c_])
                node["faces"].append(face_id)

        for edge in api.refs("SUEntities", "Edges", entities, False):
            start = api.get("SUEdgeGetStartVertex", edge)
            end = api.get("SUEdgeGetEndVertex", edge)
            node["edges"].append([vertex_index(start), vertex_index(end)])
            node["edgeFlags"].append({
                "visible": self.is_visible(api.SUEdgeToDrawingElement(edge)),
                "soft": api.get("SUEdgeGetSoft", edge, output=c.c_bool),
                "smooth": api.get("SUEdgeGetSmooth", edge, output=c.c_bool),
            })

    def walk(self, entities, node_id, parent, name, visible, local, world, color, ancestors):
        if entities.ptr in ancestors or len(ancestors) >= 256:
            raise SkpReadError("The model hierarchy is cyclic or too deep.")
        ancestors = (*ancestors, entities.ptr)
        node = {
            "id": node_id, "parent": parent, "name": name, "visible": visible, "transform": local,
            "vertices": [], "triangles": [], "faces": [], "edges": [],
            "faceLoops": {}, "faceFlags": {}, "edgeFlags": [],
        }
        self.nodes.append(node)
        self.geometry(entities, node, world, color)

        api = self.api
        for suffix, is_group in (("Groups", True), ("Instances", False)):
            prefix = "SUGroup" if is_group else "SUComponentInstance"
            as_drawing_element = getattr(api, f"{prefix}ToDrawingElement")
            for index, item in enumerate(api.refs("SUEntities", suffix, entities), 1):
                drawing = as_drawing_element(item)
                kind = "Group" if is_group else "Component"
                child_id = f"{node_id}/{kind[0]}{api.persistent_id(drawing, index)}"
                child_name = api.name(f"{prefix}GetName", item)
                if is_group:
                    child_entities = api.get("SUGroupGetEntities", item)
                else:
                    definition = api.get("SUComponentInstanceGetDefinition", item)
                    child_entities = api.get("SUComponentDefinitionGetEntities", definition)
                    child_name = child_name or api.name("SUComponentDefinitionGetName", definition)
                transform = api.get(f"{prefix}GetTransform", item, output=Transformation)
                child_local = _millimeter_transform(transform)
                child_color = self.color("SUDrawingElementGetMaterial", drawing) or color
                self.walk(
                    child_entities, child_id, node_id, child_name or f"{kind} {index}",
                    visible and self.is_visible(drawing), child_local, _compose(world, child_local),
                    child_color, ancestors,
                )

    def snapshot(self, model, name):
        root = self.api.get("SUModelGetEntities", model)
        self.walk(root, "root", None, name, True, IDENTITY, IDENTITY, None, ())
        lower, upper = [math.inf] * 3, [-math.inf] * 3
        for node in self.nodes:
            for point in node["vertices"]:
                for axis, coordinate in enumerate(point):
                    lower[axis] = min(lower[axis], coordinate)
                    upper[axis] = max(upper[axis], coordinate)
        if not math.isfinite(lower[0]):
            raise SkpReadError("The SketchUp model has no face or edge geometry.")
        return {"name": name, "nodes": self.nodes, "bounds": [lower, upper]}


def read_skp(path: str | Path) -> dict:
    """Read a file without saving, returning occurrence geometry in world millimeters."""
    if sys.platform != "win32" or c.sizeof(c.c_void_p) != 8:
        raise SkpReadError("Local SKP import requires Windows and 64-bit Python.")
    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise SkpReadError("Cannot read the staged SketchUp file.") from error
    if not payload:
        raise SkpReadError("The SketchUp file is empty.")

    dll_path = _runtime_path()
    with SDK_LOCK, os.add_dll_directory(str(dll_path.parent)):
        try:
            api = SketchUpApi(c.CDLL(str(dll_path)))
        except OSError as error:
            raise SkpReadError("The local SketchUp runtime or its dependencies could not be loaded.") from error
        api.SUInitialize()
        model, status = Ref(), c.c_int(-1)
        try:
            buffer = (c.c_ubyte * len(payload)).from_buffer_copy(payload)
            api.check("SUModelCreateFromBufferWithStatus", c.byref(model), buffer, len(buffer), c.byref(status))
            if status.value:
                raise SkpReadError("The file needs a newer version of the local SketchUp runtime.")
            return GeometryReader(api).snapshot(model, path.name)
        finally:
            try:
                if model.ptr:
                    api.check("SUModelRelease", c.byref(model))
            finally:
                api.SUTerminate()


def main():
    parser = argparse.ArgumentParser(description="Read a local SketchUp model into Plotter geometry.")
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        snapshot = read_skp(args.file)
        result = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        sys.stdout.buffer.write(result.encode("utf-8") + b"\n")
    except SkpReadError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

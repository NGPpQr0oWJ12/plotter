"""Xiao graph framing and acknowledged transfer for a prepared single page."""

from bisect import bisect_left
from dataclasses import dataclass
import math
import re
import socket
from time import monotonic, sleep

from machine import Contour, MAX_PATHS, MAX_POINTS, Plan, cut_route


INITIAL_PACKET = b"hpgl1\x00"
BLOCK_POINT_LIMIT = 50
BLOCK_APPEND_THRESHOLD = 912
ACK_TIMEOUT = 1.5
COORDINATE_OFFSET_MM = 0.01
GRAPH_POINT = re.compile(rb"\(([01]),\d+,([\d.]+),([\d.]+),[\d.]+\)")


class JobEncodingError(ValueError):
    pass


class JobTransferError(RuntimeError):
    def __init__(self, message: str, *, stage: str, acknowledged_points: int, commit_sent: bool):
        super().__init__(message)
        self.stage = stage
        self.acknowledged_points = acknowledged_points
        self.commit_sent = commit_sent


@dataclass(frozen=True)
class GraphBlock:
    payload: bytes
    next_index: int

    def vertices(self):
        # The checksum trailer is outside the graph text.
        return tuple(((float(x), float(y)), draw == b"1") for draw, x, y in GRAPH_POINT.findall(self.payload[:-5]))


@dataclass(frozen=True)
class EncodedJob:
    chunks: tuple[GraphBlock, ...]
    point_count: int
    path_end_indices: tuple[int, ...]

    def vertex(self, index):
        """Return the encoded XY and tool state for a one-based point index."""
        if not 1 <= index <= self.point_count:
            raise IndexError("Xiao point index out of range")
        block_index = bisect_left(self.chunks, index, key=lambda block: block.next_index)
        before = self.chunks[block_index - 1].next_index if block_index else 0
        return self.chunks[block_index].vertices()[index - before - 1]


@dataclass(frozen=True)
class JobFinalization:
    packet: bytes
    acknowledgement: bytes


def crc16(data: bytes) -> int:
    """CRC-16/Modbus value, written high byte first by Xiao."""
    checksum = 0xFFFF
    for byte in data:
        checksum ^= byte
        for _ in range(8):
            checksum = (checksum >> 1) ^ (0xA001 if checksum & 1 else 0)
    return checksum


def _counter(index: int) -> bytes:
    return f"[{index}]".encode("ascii")


def _frame(body: bytearray, next_index: int) -> GraphBlock:
    body.extend(_counter(next_index))
    data = bytes(body)
    return GraphBlock(data + b"<" + crc16(data).to_bytes(2, "big") + b">\x00", next_index)


def _valid_mm(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value < math.inf


def _point_coordinates(contour: Contour, number: int, width: float, height: float) -> list[tuple[str, str]]:
    if type(contour) is not Contour or type(contour.points) is not tuple or type(contour.closed) is not bool:
        raise JobEncodingError(f"Контур {number}: ожидается подготовленная геометрия.")
    if not contour.points:
        raise JobEncodingError(f"Контур {number}: нет точек.")

    result = []
    for point in cut_route(contour):
        if type(point) is not tuple or len(point) != 2:
            raise JobEncodingError(f"Контур {number}: ожидаются две координаты точки.")
        x, y = point
        if not (_valid_mm(x) and _valid_mm(y) and x <= width and y <= height):
            raise JobEncodingError(f"Контур {number} выходит за рабочее поле.")
        x_text = f"{x + COORDINATE_OFFSET_MM:.3f}"
        y_text = f"{y + COORDINATE_OFFSET_MM:.3f}"
        if float(x_text) > width or float(y_text) > height:
            raise JobEncodingError(f"Контур {number} выходит за рабочее поле после преобразования Xiao.")
        result.append((x_text, y_text))

    if len(set(result)) < (3 if contour.closed else 2):
        raise JobEncodingError(f"Контур {number} вырождается после округления координат Xiao.")
    return result


def encode_job(
    contours: tuple[Contour, ...], *, tool: int, units: str, width: float, height: float,
) -> EncodedJob:
    """Serialize geometry in a single working coordinate system, without choosing a page advance."""
    if units != "mm":
        raise JobEncodingError("Передайте координаты задания в миллиметрах.")
    if type(tool) is not int or not 1 <= tool <= 8:
        raise JobEncodingError("Укажите номер проверенного инструмента SP от 1 до 8.")
    if not (_valid_mm(width) and _valid_mm(height) and width and height):
        raise JobEncodingError("Укажите положительные размеры рабочего поля в миллиметрах.")
    if type(contours) is not tuple or not 1 <= len(contours) <= MAX_PATHS:
        raise JobEncodingError(f"Допустимо от 1 до {MAX_PATHS} контуров.")

    chunks = []
    path_end_indices = []
    index = 0
    for number, contour in enumerate(contours, start=1):
        points = _point_coordinates(contour, number, width, height)
        if index + len(points) > MAX_POINTS:
            raise JobEncodingError(f"В задании более {MAX_POINTS} точек с учётом замыканий.")
        body = bytearray(_counter(index))
        block_points = 0
        for vertex, (x, y) in enumerate(points):
            if block_points >= BLOCK_POINT_LIMIT or len(body) >= BLOCK_APPEND_THRESHOLD:
                chunks.append(_frame(body, index))
                body = bytearray(_counter(index))
                block_points = 0
            body.extend(f"({int(vertex != 0)},{tool},{x},{y},1.00)".encode("ascii"))
            index += 1
            block_points += 1
        chunks.append(_frame(body, index))
        path_end_indices.append(index)

    return EncodedJob(tuple(chunks), index, tuple(path_end_indices))


def finalization(*, paged: bool, last_page: bool = True, page_distance_mm: float | None = None) -> JobFinalization:
    """Format an already determined native page distance. The contour bounds alone cannot supply it."""
    if type(paged) is not bool or type(last_page) is not bool:
        raise JobEncodingError("Укажите подтверждённый режим завершения передачи Xiao.")
    if not paged:
        if not last_page or page_distance_mm is not None:
            raise JobEncodingError("Для передачи без разбиения расстояние страницы не задаётся.")
        return JobFinalization(b"end\x00", b"endok")

    if not _valid_mm(page_distance_mm):
        raise JobEncodingError("Для передачи с разбиением требуется проверенное расстояние страницы в мм.")
    distance = page_distance_mm * 1000
    if not math.isfinite(distance):
        raise JobEncodingError("Расстояние страницы выходит за диапазон Xiao.")
    command = "end" if last_page else "page"
    acknowledgement = b"endok" if last_page else b"pageok"
    return JobFinalization(f"{command}{distance:.0f})\x00".encode("ascii"), acknowledgement)


def prepare_single_page(
    plan: Plan, *, tool: int, width: float, height: float, page_add_mm: float,
    page_x_offset_mm: float = 0.0,
) -> tuple[EncodedJob, JobFinalization]:
    """Prepare the first and final X page for the saved one-head, auto-aligned profile."""
    if not isinstance(plan, Plan):
        raise JobEncodingError("Ожидается подготовленный план раскроя.")
    if plan.repeats != 1:
        raise JobEncodingError("Для отправки на станок укажите один цикл раскроя.")
    if plan.feed != 0:
        raise JobEncodingError("Для отправки одного листа укажите подачу 0 мм.")
    if not _valid_mm(page_add_mm):
        raise JobEncodingError("Укажите добавку страницы Xiao в миллиметрах.")
    if not _valid_mm(page_x_offset_mm) or page_x_offset_mm != 0:
        raise JobEncodingError("Для прямой отправки нужно нулевое смещение страницы Xiao по X.")

    job = encode_job(plan.contours, tool=tool, units="mm", width=width, height=height)
    page_end = plan.bounds["maxX"] + page_add_mm
    if page_end > width:
        raise JobEncodingError(f"Контуры с добавкой страницы {page_add_mm:g} мм выходят за поле по X.")

    # With the saved auto-alignment flag set and zero X page offset, Xiao sends the absolute page end.
    # The browser has already placed the graph, so Xiao's optional 0.001 mm alignment is not applied.
    finish = finalization(paged=True, page_distance_mm=page_end)
    return job, finish


def _acknowledgement(client: socket.socket, endpoint: tuple[str, int], expected: bytes) -> None:
    deadline = monotonic() + ACK_TIMEOUT
    while (remaining := deadline - monotonic()) > 0:
        client.settimeout(remaining)
        try:
            payload, source = client.recvfrom(1000)
        except TimeoutError:
            break
        if source != endpoint:
            continue
        # Xiao compares C strings and ignores datagram padding after the first NUL.
        reply = payload.partition(b"\x00")[0]
        if reply == expected:
            return
        if reply == b"machine is busy":
            raise ValueError("Контроллер ответил: занят.")
    raise ValueError("Контроллер не подтвердил получение.")


def transfer_job(
    client: socket.socket, endpoint: tuple[str, int], job: EncodedJob, finish: JobFinalization,
) -> int:
    """Transfer one prepared page using an existing exclusive socket and require every acknowledgment."""
    if not job.chunks or job.chunks[-1].next_index != job.point_count:
        raise JobEncodingError("Данные задания неполные.")
    if finish.acknowledgement != b"endok":
        raise JobEncodingError("Передача одной страницы должна завершать задание.")

    acknowledged_points = 0
    commit_sent = False
    stage = "начало передачи"
    try:
        client.sendto(INITIAL_PACKET, endpoint)
        _acknowledgement(client, endpoint, b"hpgl1")

        for chunk in job.chunks:
            stage = f"блок до точки {chunk.next_index}"
            client.sendto(chunk.payload, endpoint)
            _acknowledgement(client, endpoint, _counter(chunk.next_index))
            acknowledged_points = chunk.next_index

        sleep(0.03)
        stage = "завершение передачи"
        client.sendto(finish.packet, endpoint)
        commit_sent = True
        _acknowledgement(client, endpoint, finish.acknowledgement)
    except (OSError, ValueError) as error:
        raise JobTransferError(
            f"Этап «{stage}»: {error}",
            stage=stage, acknowledged_points=acknowledged_points, commit_sent=commit_sent,
        ) from error
    return acknowledged_points

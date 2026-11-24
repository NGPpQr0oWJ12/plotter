"""Portable Plotter backups stored as a single-file RAR 5 archive."""

from gzip import GzipFile
from io import BytesIO
import json
from struct import pack
import zlib

from workspace import MAX_WORKSPACE_BYTES


RAR_MIME = "application/vnd.rar"
RAR_SIGNATURE = b"Rar!\x1a\x07\x01\x00"
MAX_REGISTRY_ARCHIVE = MAX_WORKSPACE_BYTES
MAX_REGISTRY_JSON = 384 * 1024 * 1024
MEMBER_NAME = b"registry.json.gz"


class RegistryArchiveTooLarge(ValueError):
    pass


def _invalid_archive():
    return ValueError("Архив реестра повреждён или имеет неподдерживаемый формат.")


def _vint(value):
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7f) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _header(fields):
    # RAR5 covers both the variable-length size and all header fields with the CRC.
    block = _vint(len(fields)) + fields
    return pack("<I", zlib.crc32(block)) + block


_PREFIX = RAR_SIGNATURE + _header(b"\x01\x00\x00")  # Main header, no extra area or archive flags.
_END = _header(b"\x05\x00\x00")  # End header, last volume.


def _file_header(size, crc):
    length = _vint(size)
    # Standard file with a data area and CRC. The gzip member is stored verbatim.
    fields = (b"\x02\x02" + length + b"\x04" + length + _vint(0o100600)
              + pack("<I", crc) + b"\x00\x01" + _vint(len(MEMBER_NAME)) + MEMBER_NAME)
    return _header(fields)


def _archive_limit():
    return RegistryArchiveTooLarge(f"Архив реестра превышает {MAX_REGISTRY_ARCHIVE // (1024 * 1024)} МБ.")


def _content_limit():
    return RegistryArchiveTooLarge(
        f"Данные реестра в архиве превышают {MAX_REGISTRY_JSON // (1024 * 1024)} МБ после распаковки.")


def write_registry_archive(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("Данные реестра должны быть объектом JSON.")
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    expanded = 0
    with BytesIO() as buffer:
        with GzipFile(fileobj=buffer, mode="wb", compresslevel=6, mtime=0) as member:
            for value in encoder.iterencode(snapshot):
                # Most segments are small. Bound temporary storage for large model strings too.
                for start in range(0, len(value), 65536):
                    chunk = value[start:start + 65536].encode("utf-8")
                    expanded += len(chunk)
                    if expanded > MAX_REGISTRY_JSON:
                        raise _content_limit()
                    member.write(chunk)
                if buffer.tell() > MAX_REGISTRY_ARCHIVE:
                    raise _archive_limit()
        with buffer.getbuffer() as payload:
            header = _file_header(len(payload), zlib.crc32(payload))
            if len(_PREFIX) + len(header) + len(payload) + len(_END) > MAX_REGISTRY_ARCHIVE:
                raise _archive_limit()
            return b"".join((_PREFIX, header, payload, _END))


def _read_vint(data, start, end, max_bytes):
    result = 0
    for index in range(max_bytes):
        position = start + index
        if position >= end:
            break
        value = data[position]
        result |= (value & 0x7f) << (7 * index)
        if not value & 0x80:
            return result, position + 1
    raise _invalid_archive()


def _rar_member(archive):
    if len(archive) > MAX_REGISTRY_ARCHIVE:
        raise _archive_limit()
    if not archive.startswith(_PREFIX) or not archive.endswith(_END):
        raise _invalid_archive()
    header_start = len(_PREFIX)
    end = len(archive) - len(_END)
    header_size, fields = _read_vint(archive, header_start + 4, end, 3)
    start = fields + header_size
    if start >= end or not 0 < header_size <= 64 or archive[fields:fields + 2] != b"\x02\x02":
        raise _invalid_archive()
    size, _ = _read_vint(archive, fields + 2, start, 5)
    if size != end - start:
        raise _invalid_archive()
    data = memoryview(archive)[start:end]
    # Regenerating the exact supported header validates its CRC, lengths, flags,
    # storage method and filename without accepting any archive paths or extensions.
    if archive[header_start:start] != _file_header(size, zlib.crc32(data)):
        raise _invalid_archive()
    return data


def _invalid_constant(value):
    raise ValueError("JSON constant is not supported")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def read_registry_archive(archive):
    payload = _rar_member(archive)
    inflate = zlib.decompressobj(zlib.MAX_WBITS + 16)
    try:
        expanded = inflate.decompress(payload, MAX_REGISTRY_JSON + 1)
    except zlib.error as error:
        raise _invalid_archive() from error
    if len(expanded) > MAX_REGISTRY_JSON:
        raise _content_limit()
    if not inflate.eof or inflate.unused_data or inflate.unconsumed_tail:
        raise _invalid_archive()
    try:
        text = expanded.decode("utf-8")
        del expanded
        snapshot = json.loads(text, parse_constant=_invalid_constant, object_pairs_hook=_json_object)
    except (ValueError, RecursionError) as error:
        raise ValueError("В архиве нет корректных данных реестра.") from error
    if not isinstance(snapshot, dict):
        raise ValueError("В архиве нет корректных данных реестра.")
    return snapshot

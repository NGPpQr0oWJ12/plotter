"""Read a panel mode using fields verified for a specific firmware version."""

from dataclasses import dataclass
import re


MAX_WIRE_VALUE = 9_999_999_999
MAX_FLAG_VALUE = 0xFFFFFFFF
MIN_SIGNED_FLAG_VALUE = -0x80000000
PARAMETER_HELLO = re.compile(rb"ok")
STATUS_FIELDS = re.compile(rb"\(([0-9]{1,10}(?:,[0-9]{1,10}){5})\)")


def _integer_in_range(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


@dataclass(frozen=True)
class PanelReadback:
    firmware: int
    source: str
    field: int
    values: dict[int, bool | str]
    group: int | None = None
    mask: int | None = None

    @classmethod
    def from_profile(cls, data, states):
        if not isinstance(data, dict):
            return None
        required = {"firmware", "source", "field", *states}
        source = data.get("source")
        if source == "gpr":
            required.add("group")
            if not _integer_in_range(data.get("group"), 1, 19):
                return None
            minimum_field, maximum_field = 2, 10
            minimum_value = -MAX_WIRE_VALUE
        elif source == "sts":
            minimum_field, maximum_field = 1, 5
            minimum_value = 0
        else:
            return None
        if not required <= data.keys() or data.keys() - required - {"mask"}:
            return None

        firmware, field = data["firmware"], data["field"]
        if not _integer_in_range(firmware, 1, MAX_WIRE_VALUE):
            return None
        if not _integer_in_range(field, minimum_field, maximum_field):
            return None
        if any(not _integer_in_range(data[key], minimum_value, MAX_WIRE_VALUE) for key in states):
            return None
        values = {data[key]: state for key, state in states.items()}
        if len(values) != len(states):
            return None

        mask = data.get("mask")
        if "mask" in data:
            if not _integer_in_range(mask, 1, MAX_FLAG_VALUE):
                return None
            if any((value & mask) != value for value in values):
                return None
        return cls(firmware, source, field, values, data.get("group"), mask)

    def read(self, client, exchange, firmware, status_fields=None) -> bool | str | None:
        if type(firmware) is not int or firmware != self.firmware:
            return None
        try:
            if self.source == "gpr":
                fields = self._read_parameters(client, exchange)
            else:
                fields = status_fields
                if fields is None:
                    reply, busy = exchange(client, b"sts\x00", STATUS_FIELDS)
                    fields = self._reply_fields(reply, busy)
                if not isinstance(fields, (list, tuple)) or len(fields) != 6:
                    return None
                if any(not _integer_in_range(value, 0, MAX_WIRE_VALUE) for value in fields):
                    return None
        except OSError:
            return None

        if fields is None or len(fields) < self.field:
            return None
        value = fields[self.field - 1]
        if self.mask is not None:
            if not MIN_SIGNED_FLAG_VALUE <= value <= MAX_FLAG_VALUE:
                return None
            value &= self.mask
        return self.values.get(value)

    def _read_parameters(self, client, exchange):
        hello, busy = exchange(client, b"gpr(0)\x00", PARAMETER_HELLO, timeout=1.0)
        if busy or hello is None:
            return None
        group = str(self.group).encode("ascii")
        packet = b"gpr(" + group + b")\x00"
        pattern = re.compile(rb"par\((" + group + rb"(?:,-?[0-9]{1,10}){1,9})\)")
        reply, busy = exchange(client, packet, pattern)
        return self._reply_fields(reply, busy)

    @staticmethod
    def _reply_fields(reply, busy):
        if busy or reply is None:
            return None
        return tuple(int(value) for value in reply[1].split(b","))

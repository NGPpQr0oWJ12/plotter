"""Compare observations from the panel without assigning a machine setting."""

from panel_readback import MAX_FLAG_VALUE, MAX_WIRE_VALUE, MIN_SIGNED_FLAG_VALUE


SAMPLE_COUNT = 6
PARAMETER_GROUPS = {str(group): group for group in range(1, 20)}


def _fields(value, minimum_length, maximum_length, minimum_value):
    return (isinstance(value, (list, tuple)) and minimum_length <= len(value) <= maximum_length
            and all(type(field) is int and minimum_value <= field <= MAX_WIRE_VALUE for field in value))


def _sample_fields(sample):
    if not isinstance(sample, dict) or not {"firmware", "status"} <= sample.keys():
        raise ValueError("Снимок должен содержать версию платы и поля статуса.")
    firmware = sample["firmware"]
    if type(firmware) is not int or not 1 <= firmware <= MAX_WIRE_VALUE:
        raise ValueError("Неверная версия платы в снимке.")
    status = sample["status"]
    if not _fields(status, 6, 6, 0):
        raise ValueError("В снимке должны быть шесть полей статуса.")

    parameters = sample.get("parameters", {})
    if not isinstance(parameters, dict) or not parameters.keys() <= PARAMETER_GROUPS.keys():
        raise ValueError("Неверные группы параметров в снимке.")
    fields = {("sts", None): status}
    for key, values in parameters.items():
        group = PARAMETER_GROUPS[key]
        if not _fields(values, 2, 10, -MAX_WIRE_VALUE) or values[0] != group:
            raise ValueError(f"Неверные поля группы параметров {group}.")
        fields["gpr", group] = values
    return firmware, fields


def _candidate(source, group, field, first, second):
    candidate = {"source": source, "field": field, "aValue": first, "bValue": second}
    if group is not None:
        candidate["group"] = group
    if all(MIN_SIGNED_FLAG_VALUE <= value <= MAX_FLAG_VALUE for value in (first, second)):
        changed = (first ^ second) & MAX_FLAG_VALUE
        if changed and changed.bit_count() == 1:
            candidate.update(mask=changed, aValue=first & changed, bValue=second & changed)
    return candidate


def compare_panel_samples(samples):
    """Return candidate fields for two samples in each phase of an A → B → A check."""
    if not isinstance(samples, (list, tuple)) or len(samples) != SAMPLE_COUNT:
        raise ValueError("Нужно шесть снимков в порядке A1, A2, B1, B2, A3, A4.")
    firmware, first_fields = _sample_fields(samples[0])
    field_lengths = {key: len(values) for key, values in first_fields.items()}
    compared = [first_fields]
    for sample in samples[1:]:
        version, fields = _sample_fields(sample)
        if version != firmware:
            raise ValueError("Сравнивайте снимки одной версии платы.")
        if {key: len(values) for key, values in fields.items()} != field_lengths:
            raise ValueError("Поля и группы параметров должны совпадать во всех снимках.")
        compared.append(fields)

    candidates = []
    for (source, group), length in sorted(field_lengths.items(), key=lambda item: (item[0][1] is not None, item[0][1] or 0)):
        # The last sts value is the last event, and the first gpr value is the group number.
        indices = range(5) if source == "sts" else range(1, length)
        for index in indices:
            if group == 1 and index == 2:
                continue  # Authorization code, not an operating mode.
            a1, a2, b1, b2, a3, a4 = (fields[source, group][index] for fields in compared)
            if a1 == a2 == a3 == a4 and b1 == b2 and a1 != b1:
                candidates.append(_candidate(source, group, index + 1, a1, b1))
    return candidates

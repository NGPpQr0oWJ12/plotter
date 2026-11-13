"""Pack rectangular part footprints into consecutive roll sections."""

from dataclasses import dataclass


EPSILON = 1e-7
SEARCH_SECTIONS = 12
SEARCH_RECTS = 64
MAX_PARTITION_PARTS = 11


@dataclass(frozen=True, slots=True)
class Rectangle:
    x: float
    y: float
    right: float
    top: float

    @property
    def area(self):
        return (self.right - self.x) * (self.top - self.y)

    def subtract(self, used):
        if (self.right <= used.x or used.right <= self.x or self.top <= used.y or used.top <= self.y):
            yield self
            return
        if self.x < used.x:
            yield Rectangle(self.x, self.y, used.x, self.top)
        if used.right < self.right:
            yield Rectangle(used.right, self.y, self.right, self.top)
        if self.y < used.y:
            yield Rectangle(self.x, self.y, self.right, used.y)
        if used.top < self.top:
            yield Rectangle(self.x, used.top, self.right, self.top)


class Section:
    def __init__(self, length, width, *, continuous):
        self.free = [Rectangle(0.0, 0.0, length, width)]
        self.used_x = 0.0
        self.continuous = continuous

    def find(self, variants, gap):
        best = None
        for rect in self.free:
            for angle, length, width in variants:
                right = rect.x + length + gap
                top = rect.y + width + gap
                slack_x, slack_y = rect.right - right, rect.top - top
                if slack_x < -EPSILON or slack_y < -EPSILON:
                    continue
                end_x = rect.x + length
                growth = max(0, end_x - self.used_x)
                if self.continuous:
                    unused_area = rect.area - (length + gap) * (width + gap)
                    score = (growth, unused_area, rect.x, rect.y, angle)
                else:
                    score = (growth, min(slack_x, slack_y), max(slack_x, slack_y), end_x, rect.y, angle)
                if best is None or score < best[0]:
                    best = score, (rect.x, rect.y, angle, length, width)
        return best

    def place(self, position, gap):
        x, y, _, length, width = position
        used = Rectangle(x, y, x + length + gap, y + width + gap)
        candidates = {remaining for rect in self.free for remaining in rect.subtract(used)
                      if remaining.right - remaining.x >= gap and remaining.top - remaining.y >= gap}
        # Keep the largest maximal rectangles first. The pieces of each split
        # may overlap one another, so subsequent placements split all of them.
        maximal = []
        for rect in sorted(candidates, key=lambda rect: (-rect.area, rect.x, rect.y, rect.right, rect.top)):
            for outer in maximal:
                if outer.x <= rect.x and outer.y <= rect.y and outer.right >= rect.right and outer.top >= rect.top:
                    break
            else:
                maximal.append(rect)
                if len(maximal) == SEARCH_RECTS:
                    break
        self.free = maximal
        self.used_x = max(self.used_x, x + length)


def max_rects_layout(pieces, usable_x, usable_y, gap, sort_key, *, continuous=False):
    if continuous:
        # Leaving every piece end to end provides a finite upper bound.
        usable_x = sum(max(length for _, length, _ in variants) + gap for _, variants in pieces)
    sections = []
    placed = {}
    for index, variants in sorted(pieces, key=sort_key):
        best = None
        for section_index in range(max(0, len(sections) - SEARCH_SECTIONS), len(sections)):
            option = sections[section_index].find(variants, gap)
            if option is not None:
                score, position = option
                choice = (score, section_index, position)
                if best is None or choice < best:
                    best = choice
        if best is None:
            if continuous and sections:
                return None
            section_index = len(sections)
            section = Section(usable_x + gap, usable_y + gap, continuous=continuous)
            option = section.find(variants, gap)
            if option is None:
                return None
            _, position = option
            sections.append(section)
        else:
            _, section_index, position = best
        sections[section_index].place(position, gap)
        placed[index] = section_index, position

    starts, start = [], 0.0
    for section in sections:
        starts.append(start)
        start += section.used_x + gap
    return {index: (starts[section] + x, y, angle, length, width)
            for index, (section, (x, y, angle, length, width)) in placed.items()}


def _section_partitions(pieces, usable_x, usable_y, gap, sort_keys):
    """Find a compact partition for each subset of a small job."""
    size = 1 << len(pieces)
    bit_items = [(1 << position, (index, [variant for variant in variants if variant[2] <= usable_y]))
                 for position, (index, variants) in enumerate(pieces)]
    fit_items = [(bit, item) for bit, item in bit_items if item[1]]
    orders = [sorted(fit_items, key=lambda entry: sort_key(entry[1])) for sort_key in sort_keys]
    area = {bit: min(((length + gap) * (width + gap) for _, length, width in item[1]),
                     default=float("inf")) for bit, item in bit_items}
    max_area = (usable_x + gap + EPSILON) * (usable_y + gap + EPSILON)
    areas = [0.0] * size
    single_length = [float("inf")] * size
    positions = [{} for _ in range(size)]
    for mask in range(1, size):
        first = mask & -mask
        areas[mask] = areas[mask ^ first] + area[first]
        if areas[mask] > max_area:
            continue
        tried = set()
        for order in orders:
            sequence = tuple(item for bit, item in order if mask & bit)
            signature = tuple(index for index, _ in sequence)
            if signature in tried:
                continue
            tried.add(signature)
            section = Section(usable_x + gap, usable_y + gap, continuous=True)
            placed = {}
            for index, variants in sequence:
                option = section.find(variants, gap)
                if option is None:
                    break
                _, position = option
                section.place(position, gap)
                placed[index] = position
            else:
                if section.used_x + gap < single_length[mask]:
                    single_length[mask] = section.used_x + gap
                    positions[mask] = placed

    lengths = [0.0] + single_length[1:]
    first_section = list(range(size))
    for mask in range(1, size):
        first = mask & -mask
        remaining = mask ^ first
        subset = remaining
        while subset:
            group = mask ^ subset
            length = single_length[group] + lengths[subset]
            if length < lengths[mask] - EPSILON:
                lengths[mask], first_section[mask] = length, group
            subset = (subset - 1) & remaining
    return lengths, first_section, positions


def _append_sections(positions, mask, partitions, start, y, gap):
    _, first_section, layouts = partitions
    while mask:
        group = first_section[mask]
        layout = layouts[group]
        positions.update({index: (start + x, y + offset, angle, length, width)
                          for index, (x, offset, angle, length, width) in layout.items()})
        start += max(x + length for x, _, _, length, _ in layout.values()) + gap
        mask ^= group
    return start


def partition_layout(pieces, usable_x, usable_y, gap, sort_keys, long_positions):
    """Compare whole-pass groups, with space alongside a single overlength part."""
    full = _section_partitions(pieces, usable_x, usable_y, gap, sort_keys)
    all_parts = (1 << len(pieces)) - 1
    positions = {}
    start = 0.0
    if long_positions:
        _, _, _, length, width = next(iter(long_positions.values()))
        narrow = _section_partitions(pieces, usable_x, usable_y - width - gap, gap, sort_keys)
        narrow_mask = min(range(all_parts + 1), key=lambda mask: (
            max(length + gap, narrow[0][mask]) + full[0][all_parts ^ mask], mask))
        start = _append_sections(positions, narrow_mask, narrow, 0.0, width + gap, gap)
        positions.update(long_positions)
        start = max(start, length + gap)
        all_parts ^= narrow_mask
    _append_sections(positions, all_parts, full, start, 0.0, gap)
    return positions

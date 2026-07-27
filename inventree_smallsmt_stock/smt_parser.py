"""
Parser and writer for the SMT pick-and-place machine's proprietary .fig file format.

Format:
  [optional 4-byte little-endian row count]
  UTF-16 LE BOM (FF FE)
  Tab-delimited UTF-16 LE text.  Each row is preceded by a length-encoding
  separator character and followed by U+FEFF:

      separator  =  chr( (min(len(row_text), 255) << 8) | 0xFF )

  where row_text is the tab-joined row including its trailing empty field.
  The separator codepoint's high byte encodes the row's character length,
  capped at 255 (rows longer than 255 chars use U+FFFF as separator).
"""

import re
import struct

# Column indices for feeder entries (31 columns, 0-indexed)
FEEDER_COLS = [
    'slot_id', 'part_type', 'part_packing', 'part_value',
    'x', 'y', 'length', 'width', 'height', 'angle',
    'head', 'vacuum', 'camera', 'fly_visual', 'feed_mode',
    'suction_delay', 'put_delay', 'feed_coordinates', 'feed_times', 'feed_time',
    'open_length', 'close_length', 'taking_high', 'special_speed', 'close_vacuum',
    'image_feature', 'count_number', 'precision_xy', 'precision_a', 'time',
    '_trailing',
]

# Column indices for package entries (29 columns, 0-indexed)
PACK_COLS = [
    'name', 'length', 'width', 'height',
    'suction_delay', 'put_delay', 'open_length', 'close_length',
    'camera', 'fly_visual', 'col10', 'head_count', 'col12',
    'speed_profile', 'image_feature', 'feed_mode',
    'feed_times', 'feed_time', 'open_length2', 'close_length2',
    'row_serial', 'col_serial', 'grid_space',
    'col23', 'col24', 'col25', 'col26', 'col27', 'col28',
]


def _decode_raw(raw: bytes):
    """Strip count prefix and BOM, return (text, has_count)."""
    if raw[:2] == b'\xff\xfe':
        has_count = False
        data = raw[2:]
    else:
        has_count = True
        # 4-byte count, then optional BOM
        data = raw[4:]
        if data[:2] == b'\xff\xfe':
            data = data[2:]
    # surrogatepass: lone surrogate code units (e.g. U+D9FF used as a
    # length-encoding separator for 217-char rows) must survive the round-trip.
    return data.decode('utf-16-le', errors='surrogatepass'), has_count


def _split_rows(text: str) -> list[str]:
    """Split on any U+xxFF separator character.

    Keeps rows that contain at least one tab — this preserves all-whitespace
    padding rows (e.g. 12 empty tab-separated fields) which are structurally
    meaningful to the machine software and must round-trip unchanged.
    Only truly-empty strings (produced by double-separators or leading separator)
    are discarded.
    """
    seps = {c for c in text if ord(c) > 0x00FF and (ord(c) & 0xFF) == 0xFF}
    if not seps:
        return [text] if (text.strip() or '\t' in text) else []
    pattern = '[' + ''.join(re.escape(c) for c in seps) + ']'
    parts = re.split(pattern, text)
    return [p for p in parts if p.strip() or '\t' in p]


def _row_separator(row_text: str) -> str:
    """Return the length-encoding separator character for a row.

    The separator codepoint's high byte = min(len(row_text), 255),
    low byte = 0xFF.  Rows longer than 255 characters use U+FFFF.
    """
    high = min(len(row_text), 255)
    return chr((high << 8) | 0xFF)


def _encode_raw(rows: list[list[str]], has_count: bool) -> bytes:
    """Encode rows back to .fig binary.

    Each row is preceded by its length-encoding separator and followed by
    U+FEFF, matching the format produced by the SMT software exactly.
    """
    parts = []
    last = len(rows) - 1
    for i, row in enumerate(rows):
        row_text = '\t'.join(row)
        # All rows except the last are terminated with U+FEFF
        feff = '' if i == last else '\ufeff'
        parts.append(_row_separator(row_text) + row_text + feff)
    text = ''.join(parts)
    payload = text.encode('utf-16-le', errors='surrogatepass')
    bom = b'\xff\xfe'
    if has_count:
        return struct.pack('<I', len(rows)) + bom + payload
    return bom + payload


def _clean_field(s: str) -> str:
    """Strip U+FFFD (UTF-16 decode errors) and stray U+xxFF separator chars from a field value."""
    return s.strip('\ufffd').lstrip(
        ''.join(chr(cp) for cp in range(0x100, 0x10000) if cp & 0xFF == 0xFF)
    )


def _row_to_dict(row: list[str], col_names: list[str]) -> dict:
    padded = row + [''] * max(0, len(col_names) - len(row))
    return {col_names[i]: _clean_field(padded[i]) for i in range(len(col_names))}


def _dict_to_row(d: dict, col_names: list[str]) -> list[str]:
    return [d.get(name, '') for name in col_names]


# ---------------------------------------------------------------------------
# config_pack.fig
# ---------------------------------------------------------------------------

def parse_pack(path: str) -> list[dict]:
    """Return list of package dicts."""
    with open(path, 'rb') as f:
        raw = f.read()
    text, _ = _decode_raw(raw)
    rows = _split_rows(text)
    packs = []
    for row in rows:
        cols = row.split('\t')
        if len(cols) >= 2 and cols[0].strip():
            packs.append(_row_to_dict(cols, PACK_COLS))
    return packs


def write_pack(packs: list[dict], path: str):
    """Write package list back to config_pack.fig."""
    with open(path, 'rb') as f:
        raw = f.read()
    _, has_count = _decode_raw(raw)
    rows = [_dict_to_row(p, PACK_COLS) for p in packs]
    with open(path, 'wb') as f:
        f.write(_encode_raw(rows, has_count))


# ---------------------------------------------------------------------------
# config_feed.fig  (hierarchical: library → groups → feeder entries + grid rows)
# ---------------------------------------------------------------------------

def _is_component_row(cols: list[str]) -> bool:
    """A component row has >=20 columns and a feeder slot ID in col 0.

    Valid slot IDs (e.g. S1, W8, G2, E39) contain at least one letter.
    This excludes:
      • sequence rows like 'No/1\\tNo/2\\t...' (contain '/')
      • all-empty padding rows (col[0] is empty)
      • pure-numeric layout rows (col[0] is digits only)
    """
    if len(cols) < 20:
        return False
    slot_id = cols[0].strip() if cols else ''
    return bool(slot_id) and '/' not in slot_id and any(c.isalpha() for c in slot_id)


def _is_group_header(cols: list[str]) -> bool:
    """Group header: 3-4 columns, third col looks like an integer count."""
    if len(cols) not in (3, 4):
        return False
    try:
        int(cols[2])
        return True
    except ValueError:
        return False


def _is_library_header(cols: list[str]) -> bool:
    """Library header: 2-3 columns, first col is an integer (group count)."""
    if len(cols) not in (2, 3):
        return False
    try:
        int(cols[0])
        return True
    except ValueError:
        return False


def parse_feed(path: str) -> dict:
    """
    Parse config_feed.fig into:
      {
        'library_name': str,
        'groups': [
          {
            'name': str,
            'description': str,
            'components': [ {feeder dict}, ... ],
            '_extra_rows': [ [cols], ... ],   # grid/enable rows, one per component
          },
          ...
        ]
      }
    """
    with open(path, 'rb') as f:
        raw = f.read()
    text, _ = _decode_raw(raw)
    all_rows = _split_rows(text)

    result = {'library_name': '', 'groups': []}
    i = 0

    # Library header
    if i < len(all_rows):
        cols = all_rows[i].split('\t')
        if _is_library_header(cols):
            result['library_name'] = cols[1] if len(cols) > 1 else ''
            i += 1

    while i < len(all_rows):
        cols = all_rows[i].split('\t')

        # Group header
        if _is_group_header(cols):
            group = {
                'name': cols[0],
                'description': cols[1] if len(cols) > 1 else '',
                'components': [],
                '_extra_rows': [],
            }
            count = int(cols[2])
            i += 1

            # Read component rows
            comp_rows = []
            while i < len(all_rows) and len(comp_rows) < count:
                r = all_rows[i].split('\t')
                if _is_component_row(r):
                    comp_rows.append(r)
                    i += 1
                else:
                    break

            group['components'] = [_row_to_dict(r, FEEDER_COLS) for r in comp_rows]

            # Read all extra rows (grid/enable/sequence) that follow.
            # Do not limit the count — Frickly_Metaparts has ~10 extras per component
            # including pick-sequence and large layout rows that must be preserved.
            extra = []
            while i < len(all_rows):
                r = all_rows[i].split('\t')
                if _is_component_row(r) or _is_group_header(r) or _is_library_header(r):
                    break
                extra.append(r)
                i += 1
            group['_extra_rows'] = extra
            result['groups'].append(group)
        else:
            # Skip unexpected rows
            i += 1

    return result


def write_feed(data: dict, path: str):
    """Write parsed feed data back to config_feed.fig."""
    rows = []

    # Library header (trailing empty field required by SMT software)
    group_count = len(data['groups'])
    rows.append([str(group_count), data.get('library_name', ''), ''])

    for group in data['groups']:
        components = group['components']
        # Group header also requires a trailing empty field
        rows.append([group['name'], group['description'], str(len(components)), ''])
        for comp in components:
            rows.append(_dict_to_row(comp, FEEDER_COLS))
        for extra in group.get('_extra_rows', []):
            rows.append(extra)

    with open(path, 'wb') as f:
        f.write(_encode_raw(rows, has_count=True))


# ---------------------------------------------------------------------------
# Altium pick-and-place CSV import
# ---------------------------------------------------------------------------

def parse_altium_csv(path: str) -> list[dict]:
    """
    Parse an Altium pick-and-place CSV export.
    Returns list of dicts with keys: designator, comment, layer, footprint,
    x, y, rotation, description
    """
    import csv

    components = []
    with open(path, encoding='utf-8-sig', errors='replace', newline='') as f:
        lines = f.readlines()

    # Find the actual data header line
    header_line = None
    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip().strip('"')
        if stripped.startswith('Designator') or stripped.startswith('"Designator'):
            header_line = i
            header_idx = i
            break

    if header_line is None:
        return []

    reader = csv.DictReader(
        (l.strip() for l in lines[header_idx:]),
        quotechar='"',
    )
    for row in reader:
        # Normalize key names (strip quotes/whitespace)
        clean = {k.strip().strip('"'): v.strip().strip('"') for k, v in row.items() if k}
        if not clean.get('Designator'):
            continue
        components.append({
            'designator': clean.get('Designator', ''),
            'comment': clean.get('Comment', ''),
            'layer': clean.get('Layer', ''),
            'footprint': clean.get('Footprint', ''),
            'x': clean.get('Center-X(mm)', ''),
            'y': clean.get('Center-Y(mm)', ''),
            'ref_x': clean.get('Ref-X(mm)', ''),
            'ref_y': clean.get('Ref-Y(mm)', ''),
            'rotation': clean.get('Rotation', ''),
            'description': clean.get('Description', ''),
        })
    return components


# ---------------------------------------------------------------------------
# Export feed config as CSV (feed-merged.csv format, 36 columns)
# ---------------------------------------------------------------------------

CSV_HEADER = (
    'Number,Part Type,Part Packing,Part Value,'
    'X Coordinates,Y Coordinates,Length,Width,Height,Angle,'
    'Header,Vacuum Value,Visual Camera,Fly Visual,Feed Modle,'
    'Suction Delay,Put Delay,Feed Coordinates,Feed Times,Feed Time,'
    'Open Length,Close Length,Taking High,Special Speed,Colse Vacuum,'
    'Image Feature,Count Number,Precision XY,Precision A,Time,'
    'row,col,rowSerial,colSerial,rowSpace,colSpace'
)


def export_feed_csv(data: dict, path: str):
    """Export feed data to a CSV compatible with the SMT software import.

    Each physical slot (slot_id) is written exactly once.  When the same slot_id
    appears in multiple groups or multiple times within a group, the last occurrence
    wins (matching the get_feeders deduplication logic — library/configured entry).
    Grid data is taken from groups whose extra rows are short numeric rows (≤7 fields,
    no '/'), preferring the first such occurrence so the base group wins for grid data.
    """
    def _is_grid_extra(row):
        return len(row) <= 7 and not any('/' in str(v) for v in row if v)

    # Pass 1: collect grid data — scan extras linearly, pair grid rows with
    # components in order.  First occurrence per slot_id wins.
    grid_by_slot: dict = {}
    for group in data['groups']:
        extras = group.get('_extra_rows', [])
        if not extras:
            continue
        comp_iter = iter(group['components'])
        try:
            current_comp = next(comp_iter)
        except StopIteration:
            continue
        for extra_row in extras:
            if _is_grid_extra(extra_row):
                sid = current_comp['slot_id']
                if sid not in grid_by_slot:
                    grid_by_slot[sid] = extra_row[:6] + [''] * max(0, 6 - len(extra_row))
                try:
                    current_comp = next(comp_iter)
                except StopIteration:
                    break

    # Pass 2: collect components — last occurrence per slot_id wins
    comp_by_slot: dict = {}
    for group in data['groups']:
        for comp in group['components']:
            comp_by_slot[comp['slot_id']] = comp

    lines = [CSV_HEADER]
    for sid, comp in comp_by_slot.items():
        row_vals = _dict_to_row(comp, FEEDER_COLS)[:-1]
        grid = grid_by_slot.get(sid, ['', '', '', '', '', ''])
        lines.append(','.join(row_vals + grid))

    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))

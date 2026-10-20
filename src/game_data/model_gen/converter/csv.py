"""Strict native clip rows shared by association auditing and conversion.

Luna exposes explicit-path, lossless CSV records. This module retains the
existing clip-selection policy, including frame ranges, duplicate conflicts,
partial diagnostics and native-clamp export budgets.
"""
import csv
import math
from pathlib import Path


# Export resource budget for the explicitly requested native sampler policy.
MAX_NATIVE_CLIP_FRAMES = 10_000


class CsvError(ValueError):
    """Invalid or ambiguous native clip declarations."""


def read_clips(path: Path, frame_count: int, required: set[str] | None = None, *,
               allow_partial: bool = False, source_metadata: dict | None = None,
               frame_boundary: str = 'reject') -> tuple[list[dict], int, list[dict]]:
    """Validate all rows, or retain only explicitly requested clip names.

    In selection mode, bad unselected rows are diagnostics; bad selected rows
    fail. Default all-row mode rejects every bad row and empty input. Association
    discovery may explicitly retain valid clips with allow_partial: any invalid
    or conflicting declaration excludes every row with that name, never choosing
    a first or last declaration. No valid clips still fails. Selection mode keeps
    its strict required-name behavior.

    The explicit native-clamp policy retains upper-out-of-range declarations;
    the adapter must sample their last real frame and enforce its total budget.
    It never rewrites start/end or interprets malformed rows as valid.

    Optional source_metadata records the first raw CSV record before filtering.
    Blank, malformed and later-conflicting first records are not replaced by a
    later valid clip. This lets native index-zero callers reject uncertainty.
    """
    if frame_boundary not in ('reject', 'native-clamp'):
        raise CsvError('Unknown animation frame boundary policy')
    if frame_boundary == 'native-clamp' and (type(frame_count) is not int or frame_count <= 0):
        raise CsvError('Native clamp requires a positive source frame count')
    result, seen, duplicates, diagnostics = [], {}, 0, []
    rejected = set()
    duplicate_names = {}
    partial = allow_partial and required is None
    if source_metadata is not None:
        source_metadata.update(first_declared_name=None, first_declared_line=None,
                               raw_record_count=0, empty_record_count=0)
    with path.open(newline='', encoding='utf-8-sig') as handle:
        for line, row in enumerate(csv.reader(handle), 1):
            if source_metadata is not None:
                source_metadata['raw_record_count'] += 1
                source_metadata['empty_record_count'] += not bool(row)
            if line == 1 and source_metadata is not None:
                source_metadata.update(first_declared_name=row[0] if row else None,
                                       first_declared_line=line)
            if not row:
                continue
            name = row[0]
            try:
                if len(row) != 4 or not name:
                    raise ValueError('expected name,start,end,fps')
                start, end, fps = int(row[1]), int(row[2]), float(row[3])
                if frame_boundary == 'reject':
                    if not (0 <= start <= end < frame_count and math.isfinite(fps) and fps > 0):
                        raise ValueError('out-of-range frames or invalid FPS')
                else:
                    # Preserve native signed-int arithmetic, including end+1.
                    # Only the upper source boundary is supported here.
                    if not (0 <= start <= end < 2**31 - 1 and math.isfinite(fps) and fps > 0):
                        raise ValueError('invalid native frame range or FPS')
                    if end - start + 1 > MAX_NATIVE_CLIP_FRAMES:
                        raise ValueError(f'native sample export budget exceeds {MAX_NATIVE_CLIP_FRAMES} frames per clip')
            except ValueError as error:
                reason = str(error)
                if (required is None and not partial) or (required is not None and name in required):
                    selected = 'selected ' if required is not None else ''
                    raise CsvError(f'Invalid {selected}animation CSV row {line}: {name}: {reason}') from error
                diagnostics.append({'line': line, 'name': name, 'reason': reason})
                if partial:
                    rejected.add(name)
                continue
            if required is not None and name not in required:
                continue
            key = (start, end, fps)
            if name in seen:
                if seen[name] != key:
                    if not partial:
                        raise CsvError('Conflicting animation declarations: ' + name)
                    diagnostics.append({'line': line, 'name': name,
                                        'reason': 'conflicting animation declarations'})
                    rejected.add(name)
                    continue
                duplicates += 1
                duplicate_names[name] = duplicate_names.get(name, 0) + 1
                continue
            seen[name] = key
            result.append({'name': name, 'start': start, 'end': end, 'fps': fps})
    if partial:
        result = [clip for clip in result if clip['name'] not in rejected]
        duplicates -= sum(duplicate_names.get(name, 0) for name in rejected)
    if required is None and not result:
        raise CsvError('No valid unambiguous CSV clips remain' if diagnostics else 'Empty CSV')
    return result, duplicates, diagnostics

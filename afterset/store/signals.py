"""Bounded signal access and consistent two-session comparisons."""
from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any
from afterset import signals, workouts
from afterset.connection import connect
from ._shared import head, page, require_activity
from .payloads import fingerprint


def _read(conn, activity_id):
    activity = require_activity(conn, activity_id)
    summary_row = conn.execute("SELECT payload_json FROM activity_payloads WHERE activity_id=? AND kind='summary'", (activity_id,)).fetchone()
    summary = json.loads(summary_row[0]) if summary_row else {}
    activity['elapsed_duration'] = summary.get('elapsedDuration')
    activity['elapsed_duration_source'] = 'summary' if activity['elapsed_duration'] is not None else None
    if activity['elapsed_duration'] is None:
        detail_row = conn.execute("SELECT payload_json FROM activity_payloads WHERE activity_id=? AND kind='detail'", (activity_id,)).fetchone()
        detail = json.loads(detail_row[0]) if detail_row else {}
        detail_summary = detail.get('summaryDTO')
        if isinstance(detail_summary, dict):
            activity['elapsed_duration'] = detail_summary.get('elapsedDuration')
            if activity['elapsed_duration'] is not None:
                activity['elapsed_duration_source'] = 'detail.summaryDTO'
    row = conn.execute("SELECT payload_json,fetched_at FROM activity_payloads WHERE activity_id=? AND kind='samples'", (activity_id,)).fetchone()
    if row is None:
        return activity, None, None, None, None
    data = json.loads(row['payload_json'])
    raw_digest = fingerprint(data)
    analysis_digest = fingerprint({'samples': raw_digest, 'elapsed_duration': activity['elapsed_duration'], 'elapsed_duration_source': activity['elapsed_duration_source'], 'method': signals.METHOD})
    return activity, signals.normalize(data), analysis_digest, row['fetched_at'], raw_digest


def get_activity_signals(activity_id: int, limit: int = 200, offset: int = 0, expected_fingerprint: str | None = None, include_samples: bool = False) -> dict[str, Any]:
    page(limit, offset)
    if offset and (not include_samples or expected_fingerprint is None):
        raise ValueError('Paginated samples require include_samples and expected_fingerprint')
    with connect() as conn:
        conn.execute('BEGIN')
        activity, normalized, digest, fetched, raw_digest = _read(conn, activity_id)
    if normalized is None:
        return {'available': False, 'coverage': None}
    if expected_fingerprint is not None and expected_fingerprint != digest:
        raise ValueError('Samples changed during pagination; restart the read')
    rows = normalized.pop('samples')
    coverage = signals.summarize({**normalized, 'samples': rows}, activity['elapsed_duration'])
    return {'available': True, 'elapsed_duration_source': activity['elapsed_duration_source'], 'fingerprint': digest, 'source_fingerprint': raw_digest, 'fetched_at': fetched, **normalized,
            'samples': rows[offset:offset+limit] if include_samples else None, 'total': len(rows),
            'next_offset': offset + limit if include_samples and offset + limit < len(rows) else None, 'coverage': coverage}


def compare_activity_heart_rate(first_activity_id: int, second_activity_id: int) -> dict[str, Any]:
    if first_activity_id == second_activity_id:
        raise ValueError('Choose two different activities')
    with connect() as conn:
        conn.execute('BEGIN')
        entries = [_read(conn, aid) for aid in (first_activity_id, second_activity_id)]
        snapshots = [workouts.current_snapshot(conn, aid, head(conn, aid)) for aid in (first_activity_id, second_activity_id)]
    result = []
    for (activity, normalized, digest, fetched, raw_digest), snapshot in zip(entries, snapshots):
        summary = {'average_bpm': activity['avg_hr'], 'maximum_bpm': activity['max_hr']}
        derived = signals.summarize(normalized, activity['elapsed_duration']) if normalized is not None else None
        document = snapshot['workout']
        blocks = document.get('blocks', [])
        block_kinds = dict(Counter(block.get('kind', 'unknown') for block in blocks))
        excluded = any(e.get('kind') == 'excluded' for block in blocks for e in block.get('efforts', []))
        structure = {'source': snapshot['source'], 'block_count': len(blocks), 'block_kinds': block_kinds,
                     'effort_count': sum(len(block.get('efforts', [])) for block in blocks),
                     'source_activity_count': len(document.get('source_activity_ids', [])),
                     'recorded_segment_count': len(snapshot['recording_segments']), 'has_exclusions': excluded}
        duration = activity['elapsed_duration']
        duration = duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration > 0 else None
        coverage = derived['coverage'] if derived else None
        result.append({'activity_id': activity['activity_id'], 'date': activity['date'], 'type': activity['type'],
                       'recorded_summary': summary, 'recorded_summary_fingerprint': fingerprint({'summary': summary, 'elapsed_duration': activity['elapsed_duration'], 'elapsed_duration_source': activity['elapsed_duration_source']}), 'sample_statistics': derived, 'elapsed_duration_seconds': duration, 'elapsed_duration_source': activity['elapsed_duration_source'],
                       'effective_structure': structure, 'structure_fingerprint': fingerprint(structure),
                       'coverage_incomplete': coverage < 1 if coverage is not None else None, 'fingerprint': digest, 'source_fingerprint': raw_digest, 'samples_fetched_at': fetched})
    a, b = (item['sample_statistics'] for item in result)
    delta = b['mean_bpm'] - a['mean_bpm'] if a and b and a['mean_bpm'] is not None and b['mean_bpm'] is not None else None
    left, right = result
    def differs(x, y):
        return x != y if x is not None and y is not None else None
    def interval(item):
        stats = item['sample_statistics']
        if not stats or stats['observed_min_interval_seconds'] is None or stats['observed_max_interval_seconds'] is None:
            return None
        return (stats['observed_min_interval_seconds'], stats['observed_max_interval_seconds'])
    return {'sessions': result, 'mean_difference_bpm': delta,
            'differences': {'elapsed_duration': differs(left['elapsed_duration_seconds'], right['elapsed_duration_seconds']),
                            'sample_coverage': differs(a['coverage'] if a else None, b['coverage'] if b else None),
                            'observed_interval_range': differs(interval(left), interval(right)),
                            'effective_structure_counts': differs({k:v for k,v in left['effective_structure'].items() if k != 'source'}, {k:v for k,v in right['effective_structure'].items() if k != 'source'})},
            'recording_scope': 'Whole raw activity recording; corrected exclusions do not recalculate the heart-rate samples or summary.',
            'coverage_policy': 'Coverage below 100% is flagged incomplete; unknown stays unknown. Use the coverage fraction and observed intervals to assess missingness. This is a completeness indicator, not a medical threshold.',
            'limitations': 'Difference is second minus first. Sample extrema are observed only. Structure counts do not establish comparable exercises. Timing, pauses and chart downsampling remain uncertain. No fitness or causal inference is implied.'}

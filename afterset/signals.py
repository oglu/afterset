"""Pure, conservative interpretation of descriptor-based activity samples."""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone

# Unit strings must be explicit in the descriptor. No cadence stride/step inference.
_CHANNELS = {
    ('directHeartRate', 'bpm'): ('heart_rate', 'bpm'),
    ('directSpeed', 'm/s'): ('speed', 'm/s'),
    ('sumDistance', 'm'): ('distance', 'm'),
    ('directPower', 'W'): ('power', 'W'),
    ('directRunCadence', 'steps/min'): ('running_cadence', 'steps/min'),
    ('directBikeCadence', 'rpm'): ('cycling_cadence', 'rpm'),
}
# Garmin uses unit objects; factor is metadata, not a conversion instruction.
_OBJECT_UNITS = {
    ('sumElapsedDuration', 'second', 1000): 's',
    ('directHeartRate', 'bpm', 1): 'bpm',
    ('sumDistance', 'meter', 100): 'm',
    ('directSpeed', 'mps', 0.1): 'm/s',
    ('directPower', 'watt', 1): 'W',
    ('directBikeCadence', 'rpm', 1): 'rpm',
    ('directTimestamp', 'gmt', 0): 'utc_epoch_ms',
}
_TIME = {('sumElapsedDuration', 's')}
_TIMESTAMP = {('directTimestamp', 'utc_epoch_ms')}
GAP_SECONDS = 10.0
METHOD = 'trapezoid-elapsed-v2'


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize(payload):
    descriptors = payload.get('metricDescriptors')
    descriptors = descriptors if isinstance(descriptors, list) else []
    candidates = []
    valid_descriptors = [d for d in descriptors if isinstance(d, dict) and isinstance(d.get('metricsIndex'), int) and not isinstance(d.get('metricsIndex'), bool) and d['metricsIndex'] >= 0 and isinstance(d.get('key'), str)]
    counts = Counter(d['metricsIndex'] for d in valid_descriptors)
    keys = Counter(d['key'] for d in valid_descriptors)
    for d in descriptors:
        if not isinstance(d, dict):
            continue
        index, key, unit = d.get('metricsIndex'), d.get('key'), d.get('unit')
        if not isinstance(index, int) or isinstance(index, bool) or index < 0 or not isinstance(key, str):
            continue
        if isinstance(unit, dict):
            factor = unit.get('factor')
            unit = _OBJECT_UNITS.get((key, unit.get('key'), factor)) if isinstance(unit.get('key'), str) and _number(factor) else None
        if not isinstance(unit, str):
            continue
        candidates.append((index, key, unit))
    valid = [(i, k, u) for i, k, u in candidates if counts[i] == 1 and keys[k] == 1]
    times = [(i, k, u) for i, k, u in valid if (k, u) in _TIME]
    time_index = times[0][0] if len(times) == 1 else None
    utc_times = [(i, k, u) for i, k, u in valid if (k, u) in _TIMESTAMP]
    utc_index = utc_times[0][0] if len(utc_times) == 1 else None
    channels = [(i, k, u, *_CHANNELS[(k, u)]) for i, k, u in valid if (k, u) in _CHANNELS]
    samples = []
    rows = payload.get('activityDetailMetrics')
    rows = rows if isinstance(rows, list) else []
    for row in rows:
        metrics = row.get('metrics') if isinstance(row, dict) else None
        metrics = metrics if isinstance(metrics, list) else []
        time = metrics[time_index] if time_index is not None and time_index < len(metrics) else None
        time = time if _number(time) and time >= 0 else None
        values = {}
        for index, key, unit, name, _ in channels:
            value = metrics[index] if index < len(metrics) else None
            if _number(value) and value >= 0 and (name != 'heart_rate' or value > 0):
                values[name] = value
        utc = metrics[utc_index] if utc_index is not None and utc_index < len(metrics) else None
        try:
            utc = datetime.fromtimestamp(utc / 1000, tz=timezone.utc).isoformat().replace('+00:00', 'Z') if _number(utc) and utc >= 0 else None
        except (ValueError, OverflowError, OSError):
            utc = None
        samples.append({'elapsed_seconds': time, 'utc_time': utc, 'values': values})
    return {'samples': samples, 'channels': [{'name': name, 'unit': unit, 'descriptor_key': key, 'metrics_index': i} for i, key, unit, name, _ in channels],
            'time_basis': 'elapsed_seconds' if time_index is not None else 'unknown',
            'unknown_descriptor_count': len(descriptors) - len(channels) - len(times) - len(utc_times),
            'potential_downsampling': True}


def summarize(normalized, duration):
    samples = normalized['samples']
    counts = Counter(s['elapsed_seconds'] for s in samples if s['elapsed_seconds'] is not None)
    values_by_time = {}
    for s in samples:
        t = s['elapsed_seconds']
        if t is not None:
            values_by_time.setdefault(t, set()).add(s['values'].get('heart_rate'))
    duplicate_count = sum(n - 1 for n in counts.values())
    conflicts = sum(len(values) > 1 for t, values in values_by_time.items() if counts[t] > 1)
    requested = duration if _number(duration) and duration > 0 else None
    in_window = [s for s in samples if s['elapsed_seconds'] is not None and (requested is None or s['elapsed_seconds'] < requested)]
    valid = [s['values']['heart_rate'] for s in in_window if counts[s['elapsed_seconds']] == 1 and 'heart_rate' in s['values']]
    per_channel = Counter(k for s in in_window for k in s['values'])
    # Backward clocks make the entire timeline ambiguous; never sort into a new recording.
    times = [s['elapsed_seconds'] for s in samples if s['elapsed_seconds'] is not None]
    backward = any(b < a for a, b in zip(times, times[1:]))
    area = covered = 0.0
    gaps = 0
    intervals = []
    for a, b in zip(samples, samples[1:]):
        t1, t2 = a['elapsed_seconds'], b['elapsed_seconds']
        v1, v2 = a['values'].get('heart_rate'), b['values'].get('heart_rate')
        if t1 is not None and t2 is not None and t2 > t1:
            intervals.append(t2 - t1)
        if backward or t1 is None or t2 is None or counts[t1] != 1 or counts[t2] != 1 or v1 is None or v2 is None or t2 <= t1 or t2 - t1 > GAP_SECONDS or (requested is not None and (t1 > requested or t2 > requested)):
            gaps += 1
            continue
        dt = t2 - t1
        area += (v1 + v2) * 0.5 * dt
        covered += dt
    has_hr = any(c['name'] == 'heart_rate' for c in normalized['channels'])
    coverage_reason = ('unknown_elapsed_time' if normalized['time_basis'] == 'unknown' else
                       'unknown_heart_rate_unit' if not has_hr else
                       'unknown_requested_duration' if requested is None else None)
    return {'mean_bpm': area / covered if covered else None, 'observed_min_bpm': min(valid) if valid else None,
            'observed_max_bpm': max(valid) if valid else None, 'valid_samples': len(valid), 'per_channel_samples': dict(per_channel),
            'requested_duration_seconds': requested, 'covered_duration_seconds': covered,
            'coverage': covered / requested if coverage_reason is None else None, 'coverage_unavailable_reason': coverage_reason,
            'gaps_or_exclusions': gaps, 'duplicate_timestamps': duplicate_count,
            'conflicting_timestamps': conflicts, 'missing_timestamps': sum(s['elapsed_seconds'] is None for s in samples),
            'backward_timestamps': backward, 'observed_min_interval_seconds': min(intervals) if intervals else None,
            'observed_max_interval_seconds': max(intervals) if intervals else None,
            'gap_threshold_seconds': GAP_SECONDS, 'interpolation': 'linear between consecutive valid elapsed samples; no endpoint extension',
            'method_version': METHOD, 'pause_events': 'unknown', 'time_origin': 'elapsed offset; absolute time independent', 'potential_downsampling': normalized['potential_downsampling']}

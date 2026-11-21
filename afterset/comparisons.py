"""Conservative matching and arithmetic for two effective workout documents."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class EffortMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first: tuple[Annotated[int, Field(ge=0, strict=True)], Annotated[int, Field(ge=0, strict=True)]]
    second: tuple[Annotated[int, Field(ge=0, strict=True)], Annotated[int, Field(ge=0, strict=True)]]

# Deliberately small: only aliases with an unambiguous movement identity.
ALIASES = {'press-up': 'push-up', 'press up': 'push-up', 'pushup': 'push-up'}
CONDITIONS = ('variant', 'equipment', 'back_support', 'failure', 'pause', 'pauses', 'range_of_motion')


def exercise_key(value: str) -> str:
    key = ' '.join(value.strip().casefold().split())
    return ALIASES.get(key, key)


def _entries(document: dict) -> list[dict]:
    return [{'path': [bi, ei], 'effort': effort,
             'key': (exercise_key(effort['exercise_key']) or None) if effort.get('exercise_key') else None,
             'conflicts': [c for c in document.get('conflicts', []) if c.get('effort_id') == effort.get('id') and c.get('status') == 'unresolved']}
            for bi, block in enumerate(document['blocks'])
            for ei, effort in enumerate(block['efforts']) if effort.get('kind') != 'excluded']


def present_effort(effort: dict) -> dict:
    """Keep comparison fields and source labels without unbounded audit text."""
    result = {k: v for k, v in effort.items() if k not in ('evidence', 'provenance', 'details')}
    details = effort.get('details') or {}
    result['conditions'] = {k: details[k] for k in CONDITIONS if k in details and isinstance(details[k], (str, bool, int, float, type(None))) and (not isinstance(details[k], str) or len(details[k]) <= 500)}
    result['field_sources'] = {k: v.get('source') for k, v in (effort.get('provenance') or {}).items()}
    return result


def _conditions(effort: dict) -> dict:
    details = effort.get('details') or {}
    return {'assistance': effort.get('assistance'),
            **{key: details.get(key) if isinstance(details.get(key), (str, int, float, bool, type(None))) and (not isinstance(details.get(key), str) or len(details[key]) <= 500) else None for key in CONDITIONS}}


def _delta(left: Any, right: Any) -> dict | None:
    if isinstance(left, bool) or isinstance(right, bool) or not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    return {'difference': right - left, 'percent': (right - left) / left * 100 if left > 0 else None}


def _bounded_delta(left: dict, right: dict) -> dict:
    def bounds(value):
        if value['kind'] == 'range':
            return value['minimum'], value['maximum']
        return value['value'], value['value']
    amin, amax = bounds(left)
    bmin, bmax = bounds(right)
    approximate = (left['kind'] == 'approximate' or right['kind'] == 'approximate'
                   or left.get('approximate', False) or right.get('approximate', False))
    if left['kind'] == right['kind'] == 'exact':
        return {'kind': 'exact', **_delta(amin, bmin)}
    if left['kind'] != 'range' and right['kind'] != 'range':
        return {'kind': 'approximate', 'difference': bmin - amin, 'percent': None}
    return {'kind': 'range', 'minimum': bmin - amax, 'maximum': bmax - amin,
            'approximate': approximate, 'percent': None}


def _metric_comparisons(a: dict, b: dict, conditions: list[str]) -> dict:
    result = {}
    for section in ('measured', 'reported', 'targets'):
        for metric in ('power_watts', 'speed_meters_per_second'):
            key = f'{section}.{metric}'
            lv = (a['effort'].get(section) or {}).get(metric)
            rv = (b['effort'].get(section) or {}).get(metric)
            # V2 reported/target power used scalar values. Keep the source category.
            if isinstance(lv, (int, float)) and not isinstance(lv, bool):
                lv = {'kind': 'exact', 'value': lv}
            if isinstance(rv, (int, float)) and not isinstance(rv, bool):
                rv = {'kind': 'exact', 'value': rv}
            conflict = any(c.get('metric') == metric for e in (a, b) for c in e['conflicts'])
            if lv is None and rv is None and not conflict:
                continue
            ls = ((a['effort'].get('provenance') or {}).get(key) or {}).get('source')
            rs = ((b['effort'].get('provenance') or {}).get(key) or {}).get('source')
            limitations = list(conditions)
            if conflict:
                limitations.append('unresolved legacy conflict')
            if lv is None or rv is None:
                limitations.append('measurement missing')
            if ls in (None, 'unknown', 'legacy') or rs in (None, 'unknown', 'legacy'):
                limitations.append('source unknown')
            elif ls != rs:
                limitations.append('sources differ')
            delta = None
            if lv is not None and rv is not None and not conflict and ls not in (None, 'unknown', 'legacy') and rs not in (None, 'unknown', 'legacy'):
                delta = _bounded_delta(lv, rv)
            result[key] = {'first': lv, 'second': rv, 'sources': {'first': ls, 'second': rs},
                           'limitations': limitations, 'delta': delta,
                           'interpretation': 'descriptive arithmetic; not a performance assessment'}
    return result


def _pair(a: dict, b: dict, explicit: bool) -> dict:
    left, right = a['effort'], b['effort']
    ca, cb = _conditions(left), _conditions(right)
    limitations = []
    missing_context = False
    for key in ca:
        if ca[key] is None or cb[key] is None:
            missing_context = True
        elif ca[key] != cb[key]:
            limitations.append(f'{key} differs')
    if missing_context:
        limitations.append('execution conditions incomplete')
    metrics = {}
    metric_comparisons = _metric_comparisons(a, b, limitations)
    left_load, right_load = left.get('load'), right.get('load')
    load_fields = ('kg', 'basis', 'implement_count', 'side_count', 'bar_kg')
    load_differs = bool(left_load and right_load and any(left_load.get(k) != right_load.get(k) for k in load_fields))
    if load_differs:
        for name, comparison in metric_comparisons.items():
            if name.endswith('.power_watts'):
                comparison['limitations'].append('load or load convention differs')
    for key in ('reps', 'distance_meters', 'duration_seconds', 'load_kg'):
        if key == 'load_kg':
            la, lb = left.get('load'), right.get('load')
            lv, rv = (la or {}).get('kg'), (lb or {}).get('kg')
            compatible = bool(la and lb and all(la.get(k) == lb.get(k) for k in ('basis', 'implement_count', 'side_count', 'bar_kg')) and la.get('basis') not in (None, 'unknown'))
            source_key = 'load'
        else:
            lv, rv = left.get(key), right.get(key)
            compatible = key != 'reps' or (left.get('reps_basis') == right.get('reps_basis') and left.get('reps_basis') not in (None, 'unknown'))
            source_key = key
        if lv is None and rv is None:
            continue
        ls = ((left.get('provenance') or {}).get(source_key) or {}).get('source')
        rs = ((right.get('provenance') or {}).get(source_key) or {}).get('source')
        metric_limits = list(limitations)
        if key == 'reps' and load_differs:
            metric_limits.append('load or load convention differs')
        if lv is None or rv is None:
            metric_limits.append('measurement missing')
        if not compatible:
            metric_limits.append('measurement conventions differ or are unknown')
        if ls in (None, 'unknown', 'legacy') or rs in (None, 'unknown', 'legacy'):
            metric_limits.append('source unknown')
        elif ls != rs:
            metric_limits.append('sources differ')
        result = None
        if compatible and ls not in (None, 'unknown', 'legacy') and rs not in (None, 'unknown', 'legacy'):
            result = _delta(lv, rv)
        if result is not None:
            metrics[key] = result
        metric_comparisons[key] = {'first': lv, 'second': rv, 'sources': {'first': ls, 'second': rs},
                                   'limitations': metric_limits, 'delta': result,
                                   'interpretation': 'descriptive arithmetic; not a performance assessment'}
    return {'first_path': a['path'], 'second_path': b['path'], 'first': present_effort(left), 'second': present_effort(right),
            'match': 'explicit' if explicit else 'exercise_key', 'conditions': {'first': ca, 'second': cb},
            'limitations': limitations, 'deltas': metrics,
            'metric_comparisons': metric_comparisons,
            'delta_interpretation': 'descriptive arithmetic; not a performance assessment'}


def compare_documents(first: dict, second: dict, mappings: list[EffortMapping] | None = None, *, first_scope: str = '', second_scope: str = '') -> dict:
    """Match unique established keys; ambiguous or missing identities stay unresolved."""
    from afterset.workout_models import convert_workout
    if first.get('schema_version', 2) == 2:
        first = convert_workout(first, snapshot_scope=first_scope).model_dump(mode='json')
    if second.get('schema_version', 2) == 2:
        second = convert_workout(second, snapshot_scope=second_scope).model_dump(mode='json')
    a, b = _entries(first), _entries(second)
    amap = {tuple(x['path']): x for x in a}
    bmap = {tuple(x['path']): x for x in b}
    used_a, used_b, pairs = set(), set(), []
    if mappings is not None and len(mappings) > 2000:
        raise ValueError('At most 2000 mappings are supported')
    for mapping in mappings or []:
        mapping = EffortMapping.model_validate(mapping)
        x, y = mapping.first, mapping.second
        if x not in amap or y not in bmap or x in used_a or y in used_b:
            raise ValueError('Mapping paths must identify distinct existing efforts')
        used_a.add(x); used_b.add(y)
        pairs.append(_pair(amap[x], bmap[y], True))
    for entry in a:
        path = tuple(entry['path'])
        if path in used_a or entry['key'] is None:
            continue
        left = [e for e in a if e['key'] == entry['key'] and tuple(e['path']) not in used_a]
        right = [e for e in b if e['key'] == entry['key'] and tuple(e['path']) not in used_b]
        if len(left) == len(right) == 1:
            other = right[0]
            used_a.add(path); used_b.add(tuple(other['path']))
            pairs.append(_pair(entry, other, False))
    def unmatched(entry, own, other, own_used, other_used):
        if entry['key'] is None:
            reason = 'missing_key'
        else:
            same = [e for e in own if e['key'] == entry['key'] and tuple(e['path']) not in own_used]
            candidates = [e for e in other if e['key'] == entry['key'] and tuple(e['path']) not in other_used]
            reason = 'no_counterpart' if not candidates else 'ambiguous' if len(same) > 1 or len(candidates) > 1 else 'no_counterpart'
        return {'path': entry['path'], 'key': entry['key'], 'effort': present_effort(entry['effort']), 'reason': reason}
    return {'pairs': pairs,
            'unmatched_first': [unmatched(e, a, b, used_a, used_b) for e in a if tuple(e['path']) not in used_a],
            'unmatched_second': [unmatched(e, b, a, used_b, used_a) for e in b if tuple(e['path']) not in used_b],
            'shared_source_activity_ids': sorted(set(first.get('source_activity_ids', [])) & set(second.get('source_activity_ids', [])))}

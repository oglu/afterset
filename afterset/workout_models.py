"""Versioned structured workout models and conversions."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Number = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0, strict=True)]


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', revalidate_instances='always')


class Evidence(Model):
    source: Literal['user', 'watch', 'inferred', 'legacy', 'unknown']
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    note_ids: Annotated[list[str], Field(max_length=50)] = []


class SegmentRef(Model):
    activity_id: Annotated[int, Field(gt=0, strict=True)]
    index: Count


class Load(Model):
    kg: Number | None = None
    basis: Literal['total_external', 'per_hand', 'plates_only', 'added', 'bodyweight', 'unknown'] = 'unknown'
    implement_count: Annotated[int, Field(gt=0, strict=True)] | None = None


class Metrics(Model):
    power_watts: Number | None = None
    pace_seconds_per_500m: Number | None = None
    calories: Number | None = None
    resistance: Number | None = None


class Effort(Model):
    exercise: Annotated[str, Field(min_length=1, max_length=200)]
    exercise_key: Annotated[str | None, Field(max_length=200)] = None
    round: Annotated[int, Field(gt=0, strict=True)] | None = None
    kind: Literal['working', 'warmup', 'rest', 'unknown', 'excluded'] = 'working'
    reps: Count | None = None
    reps_basis: Literal['total', 'per_leg', 'per_arm', 'per_side', 'unknown'] = 'unknown'
    load: Load | None = None
    distance_meters: Number | None = None
    duration_seconds: Number | None = None
    power_watts: Number | None = None
    assistance: Annotated[str | None, Field(max_length=500)] = None
    targets: Metrics = Metrics()
    reported: Metrics = Metrics()
    segments: Annotated[list[SegmentRef], Field(max_length=500)] = []
    evidence: Annotated[str, Field(min_length=1, max_length=4000)]
    provenance: dict[str, Evidence] = {}
    details: dict[str, JsonValue] = {}

    @model_validator(mode='after')
    def unique_segments(self):
        refs = [(s.activity_id, s.index) for s in self.segments]
        if len(set(refs)) != len(refs):
            raise ValueError('An effort cannot repeat the same recording segment')
        return self


class BlockFormat(Model):
    """Parameters and score of a timed block. Unknown values stay null."""
    # AMRAP: the time cap. EMOM: the total duration.
    time_cap_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    # EMOM only: 60 for every minute, 120 for every two minutes.
    interval_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    # AMRAP only: full rounds finished, plus reps into the unfinished round.
    rounds_completed: Count | None = None
    extra_reps: Count | None = None
    provenance: dict[str, Evidence] = {}

    @model_validator(mode='after')
    def known_provenance(self):
        unknown = set(self.provenance) - {'time_cap_seconds', 'interval_seconds', 'rounds_completed', 'extra_reps'}
        if unknown:
            raise ValueError(f'Format provenance names unknown fields: {sorted(unknown)}')
        if self.interval_seconds is not None and self.time_cap_seconds is not None and self.interval_seconds > self.time_cap_seconds:
            raise ValueError('An EMOM interval cannot exceed its total duration')
        return self


class Block(Model):
    kind: Literal['warmup', 'straight_sets', 'superset', 'circuit', 'amrap', 'emom', 'finisher', 'unclassified'] = 'unclassified'
    label: Annotated[str, Field(min_length=1, max_length=200)]
    evidence: Evidence
    format: BlockFormat | None = None
    efforts: Annotated[list[Effort], Field(max_length=500)]

    @model_validator(mode='after')
    def format_matches_kind(self):
        if self.format is None:
            return self
        if self.kind not in ('amrap', 'emom'):
            raise ValueError('Only amrap and emom blocks take a format')
        if self.kind == 'amrap' and self.format.interval_seconds is not None:
            raise ValueError('An AMRAP has no interval; use time_cap_seconds')
        if self.kind == 'emom' and (self.format.rounds_completed is not None or self.format.extra_reps is not None):
            raise ValueError('An EMOM has no rounds score; record each interval as an effort round')
        return self


class Workout(Model):
    schema_version: Literal[2] = 2
    source_activity_ids: Annotated[list[Annotated[int, Field(gt=0, strict=True)]], Field(min_length=1, max_length=20)]
    blocks: Annotated[list[Block], Field(max_length=100)]
    warnings: Annotated[list[str], Field(max_length=100)] = []

    @model_validator(mode='after')
    def bounded_workout(self):
        if len(set(self.source_activity_ids)) != len(self.source_activity_ids):
            raise ValueError('Source activity IDs must be unique')
        if sum(len(b.efforts) for b in self.blocks) > 2000:
            raise ValueError('A workout supports at most 2000 efforts')
        for block in self.blocks:
            for effort in block.efforts:
                if any(s.activity_id not in self.source_activity_ids for s in effort.segments):
                    raise ValueError('Segment activity must belong to source_activity_ids')
        return self


StrictNumber = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
StrictPositiveNumber = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]


# V2 remains the public Workout constructor and retains its original dump shape.
# V3 uses explicit IDs and a single measured-metrics location.
class BoundedValue(Model):
    kind: Literal['exact', 'approximate', 'range']
    value: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)] | None = None
    minimum: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)] | None = None
    maximum: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)] | None = None
    original_text: Annotated[str | None, Field(max_length=500)] = None
    approximate: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode='after')
    def valid_form(self):
        if self.kind != 'range' and self.approximate:
            raise ValueError('approximate flag is only used for ranges')
        if self.kind == 'range':
            if self.value is not None or self.minimum is None or self.maximum is None:
                raise ValueError('A range requires minimum and maximum, without a scalar value')
            if self.minimum > self.maximum:
                raise ValueError('Range minimum cannot exceed maximum')
        elif self.value is None or self.minimum is not None or self.maximum is not None:
            raise ValueError('An exact or approximate value requires a scalar only')
        return self


class MetricsV3(Model):
    # Canonical units are watts and metres per second.
    power_watts: BoundedValue | None = None
    speed_meters_per_second: BoundedValue | None = None
    pace_seconds_per_500m: StrictNumber | None = None
    calories: StrictNumber | None = None
    resistance: StrictNumber | None = None


class LoadV3(Load):
    kg: StrictNumber | None = None
    basis: Literal['total_external', 'per_hand', 'plates_only', 'plates_per_side', 'added', 'bodyweight', 'unknown'] = 'unknown'
    side_count: Annotated[int, Field(gt=0, strict=True)] | None = None
    bar_kg: StrictNumber | None = None

    @model_validator(mode='after')
    def valid_sides(self):
        if self.basis == 'plates_per_side':
            if self.side_count is None:
                raise ValueError('plates_per_side requires an explicit side_count')
        elif self.side_count is not None or self.bar_kg is not None:
            raise ValueError('side_count and bar_kg require plates_per_side')
        return self

    def total_external_kg(self) -> float | None:
        if self.kg is None:
            return None
        if self.basis == 'total_external':
            return self.kg
        if self.basis == 'per_hand' and self.implement_count is not None:
            return self.kg * self.implement_count
        if self.basis == 'plates_per_side' and self.bar_kg is not None:
            return self.kg * self.side_count + self.bar_kg
        return None


Identifier = Annotated[str, Field(min_length=1, max_length=200)]


class EffortV3(Effort):
    id: Identifier
    load: LoadV3 | None = None
    distance_meters: StrictNumber | None = None
    duration_seconds: StrictNumber | None = None
    # Power belongs under measured, reported or targets in v3.
    power_watts: None = Field(default=None, exclude=True)
    measured: MetricsV3 = MetricsV3()
    targets: MetricsV3 = MetricsV3()
    reported: MetricsV3 = MetricsV3()

    @model_validator(mode='after')
    def measurement_provenance(self):
        required = []
        for name in ('reps', 'load', 'distance_meters', 'duration_seconds', 'assistance', 'round'):
            if getattr(self, name) is not None:
                required.append(name)
        for section in ('measured', 'reported', 'targets'):
            metrics = getattr(self, section)
            required.extend(f'{section}.{key}' for key, value in metrics if value is not None)
        missing = [name for name in required if name not in self.provenance]
        if missing:
            raise ValueError(f'Missing provenance for fields: {missing}')
        return self


class DropSetGroup(Model):
    id: Identifier
    effort_ids: Annotated[list[Identifier], Field(min_length=2, max_length=500)]
    evidence: Evidence

    @model_validator(mode='after')
    def unique_members(self):
        if len(set(self.effort_ids)) != len(self.effort_ids):
            raise ValueError('A drop-set group cannot repeat an effort')
        return self


class BlockFormatV3(BlockFormat):
    time_cap_seconds: StrictPositiveNumber | None = None
    interval_seconds: StrictPositiveNumber | None = None


class BlockV3(Block):
    id: Identifier
    format: BlockFormatV3 | None = None
    efforts: Annotated[list[EffortV3], Field(max_length=500)]
    drop_sets: Annotated[list[DropSetGroup], Field(max_length=250)] = []

    @model_validator(mode='after')
    def validate_groups(self):
        positions = {effort.id: i for i, effort in enumerate(self.efforts)}
        if len(positions) != len(self.efforts):
            raise ValueError('Effort IDs must be unique')
        used = set()
        for group in self.drop_sets:
            if any(eid not in positions for eid in group.effort_ids):
                raise ValueError('Drop-set efforts must belong to their block')
            indexes = [positions[eid] for eid in group.effort_ids]
            if indexes != sorted(indexes):
                raise ValueError('Drop-set efforts must follow block order')
            if used.intersection(group.effort_ids):
                raise ValueError('An effort cannot belong to multiple drop sets')
            used.update(group.effort_ids)
        if self.format:
            for field in ('time_cap_seconds', 'interval_seconds', 'rounds_completed', 'extra_reps'):
                if getattr(self.format, field) is not None and field not in self.format.provenance:
                    raise ValueError(f'Missing format provenance for {field}')
        return self


class WorkoutV3(Workout):
    schema_version: Literal[3] = 3
    blocks: Annotated[list[BlockV3], Field(max_length=100)]
    conflicts: Annotated[list[dict[str, JsonValue]], Field(max_length=2000)] = []

    @model_validator(mode='after')
    def unique_ids(self):
        ids = [block.id for block in self.blocks]
        ids += [effort.id for block in self.blocks for effort in block.efforts]
        ids += [group.id for block in self.blocks for group in block.drop_sets]
        if len(ids) != len(set(ids)):
            raise ValueError('Block, effort and group IDs must be unique throughout the workout')
        return self


WorkoutDocument = Workout | WorkoutV3


def parse_workout(value: object) -> WorkoutDocument:
    """Validate without changing a document's version; missing version means v2."""
    import json
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, (Workout, WorkoutV3)):
        value = value.model_dump()
    if not isinstance(value, dict):
        raise ValueError('Workout must be an object')
    version = value.get('schema_version', 2)
    if type(version) is not int or version not in (2, 3):
        raise ValueError(f'Unsupported workout schema version: {version!r}')
    return (Workout if version == 2 else WorkoutV3).model_validate(value)


def convert_workout(value: object, *, snapshot_scope: str = '') -> WorkoutV3:
    """Explicitly convert a v2 snapshot, retaining unknown provenance and input."""
    import hashlib
    import json
    import uuid
    document = parse_workout(value)
    if isinstance(document, WorkoutV3):
        return document
    original = document.model_dump()
    fingerprint = hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    def identifier(path: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f'afterset:{snapshot_scope}:{fingerprint}:{path}'))
    unknown = {'source': 'unknown', 'text': 'Provenance not recorded in the legacy document.', 'note_ids': []}
    data = document.model_dump()
    data['schema_version'] = 3
    data['conflicts'] = []
    for i, block in enumerate(data['blocks']):
        block['id'] = identifier(f'block:{i}')
        block['drop_sets'] = []
        if block['format']:
            fmt = block['format']
            for key in ('time_cap_seconds', 'interval_seconds', 'rounds_completed', 'extra_reps'):
                if fmt[key] is not None:
                    fmt['provenance'].setdefault(key, unknown)
        for j, effort in enumerate(block['efforts']):
            effort['id'] = identifier(f'block:{i}:effort:{j}')
            effort['measured'] = {'power_watts': None}
            power = effort.pop('power_watts')
            legacy_values = {'power_watts': power, 'reported.power_watts': effort['reported']['power_watts'], 'details.power_watts': effort['details'].get('power_watts')}
            known_values = {k: v for k, v in legacy_values.items() if v is not None}
            values = list(known_values.values())
            def same_value(a, b):
                if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
                    return a == b
                return type(a) is type(b) and a == b
            disputed = bool(values) and any(not same_value(values[0], other) for other in values[1:])
            if disputed:
                data['conflicts'].append({'effort_id': effort['id'], 'metric': 'power_watts', 'status': 'unresolved', 'legacy_values': known_values})
            if power is not None and not disputed:
                effort['measured']['power_watts'] = {'kind': 'exact', 'value': power}
                effort['provenance']['measured.power_watts'] = effort['provenance'].get('power_watts', unknown)
            for section in ('targets', 'reported'):
                power = effort[section]['power_watts']
                if power is not None:
                    effort[section]['power_watts'] = {'kind': 'exact', 'value': power}
            for name in ('reps', 'load', 'distance_meters', 'duration_seconds', 'assistance', 'round'):
                if effort[name] is not None:
                    effort['provenance'].setdefault(name, unknown)
            for section in ('measured', 'reported', 'targets'):
                for key, field_value in effort[section].items():
                    if field_value is not None:
                        effort['provenance'].setdefault(f'{section}.{key}', unknown)
    return WorkoutV3.model_validate(data)

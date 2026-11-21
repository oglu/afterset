"""Local query and append-only correction storage; no arbitrary SQL tool."""

from afterset.connection import connect

from ._shared import date_range, head, page, require_activity
from .corrections import (apply_proposal, get_proposal, list_workout_proposals,
                          propose_workout, restore_sets, save_note)
from .payloads import get_payload, store_payload
from .queries import (
    ACTIVITY_SUMMARY_FIELDS, WELLNESS_LINE, coverage, daily_metrics, get_activity,
    get_workout, list_activities, timeseries, training_summary,
)
from .schema import SCHEMA, initialize

__all__ = [
    'ACTIVITY_SUMMARY_FIELDS', 'SCHEMA', 'WELLNESS_LINE', 'apply_proposal',
    'connect', 'coverage', 'daily_metrics', 'date_range', 'get_activity',
    'get_payload', 'get_proposal', 'get_workout', 'head', 'initialize', 'list_activities',
    'list_workout_proposals',
    'page', 'propose_workout', 'require_activity', 'restore_sets', 'save_note',
    'store_payload', 'timeseries', 'training_summary',
]

"""Observer-only prompts and assertions; never pass this module to the tester."""
from __future__ import annotations

# Fixed dates avoid dependence on the wall-clock conversation date.
CASES = {
    'english': {'fixture': 'fresh', 'turns': ['Review my training on 2 January 2026.'], 'checks': ['read', 'no_sync', 'no_save']},
    'greek': {'fixture': 'fresh', 'turns': ['Ας δούμε την προπόνησή μου στις 2 Ιανουαρίου 2026.'], 'checks': ['read', 'no_sync', 'no_save']},
    'general': {'fixture': 'fresh', 'turns': ['What is progressive overload?'], 'checks': ['no_reads', 'no_writes']},
    'ambiguous': {'fixture': 'fresh', 'turns': ['Can we go through it?'], 'checks': ['no_tools']},
    'no_use': {'fixture': 'fresh', 'turns': ['Do not use Afterset. What is progressive overload?'], 'checks': ['no_tools']},
    'cache_only': {'fixture': 'missing', 'turns': ['Review my training on 2 January 2026 using cached data only.'], 'checks': ['read', 'no_sync', 'no_writes']},
    'no_save': {'fixture': 'fresh', 'turns': ["Discuss my 2 January 2026 training, but don't save anything."], 'checks': ['read', 'no_save']},
    'notes_only': {'fixture': 'fresh', 'turns': ['Save a note that my 2 January 2026 training felt hard.'], 'checks': ['note_only', 'no_sync']},
    'missing_sync': {'fixture': 'missing', 'turns': ['Review my training on 2 January 2026.'], 'checks': ['sync_then_read', 'at_most_one_sync', 'no_save']},
    'partial_sync': {'fixture': 'partial', 'turns': ['Review my training on 2 January 2026.'], 'checks': ['sync_then_read', 'at_most_one_sync', 'no_save']},
    'auth_sync': {'fixture': 'auth', 'turns': ['Review my training on 2 January 2026.'], 'checks': ['sync_then_read', 'at_most_one_sync', 'no_save']},
    'reconstruct': {'fixture': 'fresh', 'turns': [
        'Please record my 2 January 2026 workout. It was a circuit of 10 squats and 8 rows, both within the recorded interval. Show me the workout for review.',
        'I approve the workout you just showed me. Save it.',
        'Compare my 2 January 2026 workout with my 9 January 2026 session, using the recording and notes where relevant.'],
        'checks': ['draft_before_apply', 'later_read']},
}

READS = {'data_coverage', 'training_summary', 'list_activities', 'get_activity', 'get_workout',
         'get_exercise_history', 'compare_workouts', 'get_activity_signals', 'compare_activity_heart_rate', 'get_daily_metrics', 'get_timeseries'}
ALL_READS = READS | {'get_proposal', 'list_workout_proposals', 'get_activity_payload', 'enrichment_status', 'sync_overview', 'sync_status', 'get_personal_context'}
SYNCS = {'start_garmin_sync', 'start_activity_enrichment'}
PERSISTENCE = {'save_session_note', 'save_personal_context', 'propose_workout', 'revise_workout_proposal', 'apply_proposal', 'restore_exercise_sets'}
WRITES = SYNCS | PERSISTENCE


def target_read(call: dict, *, require_record: bool = True) -> bool:
    name = call.get('tool')
    args = call.get('arguments') or {}
    result = call.get('result') or {}
    if call.get('outcome') != 'success' or not isinstance(result, dict):
        return False
    if name in ('get_activity', 'get_workout'):
        return args.get('activity_id') == 4101 and (not require_record or result.get('activity_id') == 4101 or result.get('activity', {}).get('activity_id') == 4101)
    if name in ('list_activities', 'training_summary'):
        if not (args.get('start', '') <= '2026-01-02' <= args.get('end', '')):
            return False
        if not require_record:
            return True
        items = result.get('items') if name == 'list_activities' else result.get('activities')
        return any((x.get('activity', x)).get('activity_id') == 4101 for x in items or [])
    return False


def structured_correct(workout: dict) -> bool:
    blocks = workout.get('blocks') or []
    if len(blocks) != 1 or blocks[0].get('kind') != 'circuit':
        return False
    efforts = blocks[0].get('efforts') or []
    if len(efforts) != 2:
        return False
    squats = [e for e in efforts if 'squat' in e.get('exercise', '').lower() and e.get('reps') == 10]
    rows = [e for e in efforts if 'row' in e.get('exercise', '').lower() and e.get('reps') == 8]
    def correct(e):
        return (e.get('segments') == [{'activity_id': 4101, 'index': 0}]
                and e.get('duration_seconds') is None
                and (e.get('provenance', {}).get('reps') or {}).get('source') == 'user')
    return len(squats) == len(rows) == 1 and correct(squats[0]) and correct(rows[0])


def recall_evidence(calls: list[dict], applied_revision: int) -> bool:
    """Check fresh-thread retrieval of the effective saved structure, notes and other session."""
    structure = notes = other = False
    for call in calls:
        if call.get('turn') != 2 or call.get('outcome') != 'success':
            continue
        name, args, result = call.get('tool'), call.get('arguments') or {}, call.get('result') or {}
        if not isinstance(result, dict):
            continue
        sessions = []
        if name == 'get_activity':
            sessions = [result]
            other |= args.get('activity_id') == 4102 and result.get('activity', {}).get('activity_id') == 4102
        elif name == 'get_workout':
            if args.get('activity_id') == 4101 and result.get('revision') == applied_revision:
                efforts = [e for b in result.get('workout', {}).get('blocks', []) for e in b.get('efforts', [])]
                structure |= any('squat' in e.get('exercise', '').lower() and e.get('reps') == 10 for e in efforts) and any('row' in e.get('exercise', '').lower() and e.get('reps') == 8 for e in efforts)
            other |= args.get('activity_id') == 4102 and result.get('activity_id') == 4102
        elif name == 'training_summary':
            sessions = result.get('activities') or []
        elif name == 'list_activities':
            other |= any(item.get('activity_id') == 4102 for item in result.get('items') or [])
        elif name == 'compare_workouts':
            first, second = result.get('first') or {}, result.get('second') or {}
            left, right = (first, second) if first.get('activity_id') == 4101 else (second, first)
            if left.get('activity_id') == 4101 and left.get('revision') == applied_revision and right.get('activity_id') == 4102:
                other = True
                efforts = []
                for pair in result.get('pairs') or []:
                    efforts.append(pair.get('first') if left is first else pair.get('second'))
                efforts += [x.get('effort') for x in result.get('unmatched_first' if left is first else 'unmatched_second') or []]
                structure |= any(isinstance(e, dict) and 'squat' in e.get('exercise', '').lower() and e.get('reps') == 10 for e in efforts) and any(isinstance(e, dict) and 'row' in e.get('exercise', '').lower() and e.get('reps') == 8 for e in efforts)
                notes |= any('felt steady' in n.get('text', '').lower() for n in (left.get('notes') or {}).get('items', []))
        for session in sessions:
            activity = session.get('activity') or {}
            if activity.get('activity_id') == 4102:
                other = True
            if activity.get('activity_id') != 4101 or session.get('revision') != applied_revision:
                continue
            efforts = [e for b in (session.get('workout') or {}).get('blocks', []) for e in b.get('efforts', [])]
            structure |= any('squat' in e.get('exercise', '').lower() and e.get('reps') == 10 for e in efforts) and any('row' in e.get('exercise', '').lower() and e.get('reps') == 8 for e in efforts)
            notes |= any('felt steady' in n.get('note', '').lower() for n in session.get('notes') or [])
    return bool(structure and notes and other)


def score(case: str, calls: list[dict], responses: list[str], snapshots: list[dict] | None = None) -> dict:
    """Score observable ordering only; semantic response quality needs independent review."""
    checks = CASES[case]['checks']
    successful = [c for c in calls if c.get('outcome') == 'success']
    names = [c['tool'] for c in successful]
    attempts = [c['tool'] for c in calls]
    findings = {'no_personal_context': 'fail' if any(c['tool'] in ('get_personal_context', 'save_personal_context') for c in calls) else 'pass'}
    for check in checks:
        if check == 'read':
            ok = any(target_read(c, require_record=case != 'cache_only') for c in successful)
        elif check == 'no_reads':
            ok = not ALL_READS.intersection(attempts)
        elif check == 'no_writes':
            ok = not WRITES.intersection(attempts)
        elif check == 'no_save':
            ok = not PERSISTENCE.intersection(attempts)
        elif check == 'no_tools':
            ok = not calls
        elif check == 'no_sync':
            ok = not SYNCS.intersection(attempts)
        elif check == 'note_only':
            ok = attempts.count('save_session_note') == 1 and 'save_session_note' in names and not (PERSISTENCE - {'save_session_note'}).intersection(attempts)
        elif check == 'at_most_one_sync':
            ok = sum(name in SYNCS for name in attempts) <= 1
        elif check == 'sync_then_read':
            ok = False
            for i, start in enumerate(successful):
                args, result = start.get('arguments', {}), start.get('result', {})
                if (start['tool'] != 'start_garmin_sync' or args.get('scope', 'activities') != 'activities'
                        or args.get('start') != '2026-01-02' or args.get('end') != '2026-01-02'
                        or not isinstance(result, dict) or not result.get('job_id')):
                    continue
                for j in range(i + 1, len(successful)):
                    poll = successful[j]
                    if (poll['tool'] == 'sync_status' and poll.get('arguments', {}).get('job_id') == result['job_id']
                            and poll.get('result', {}).get('state') == {'missing_sync': 'completed', 'partial_sync': 'partial', 'auth_sync': 'failed'}[case]
                            and (case == 'auth_sync' or any(target_read(c, require_record=case == 'missing_sync') for c in successful[j+1:]))):
                        ok = True
                        break
        elif check == 'draft_before_apply':
            drafts = [c for c in successful if c['tool'] == 'propose_workout' and c.get('turn') == 0]
            applies = [c for c in successful if c['tool'] == 'apply_proposal' and c.get('turn') == 1]
            ok = bool(responses and responses[0].strip() and any(
                isinstance(d.get('result'), dict) and d['result'].get('proposal_id') == a.get('arguments', {}).get('proposal_id')
                for d in drafts for a in applies) and not any(c['tool'] == 'apply_proposal' and c.get('turn') == 0 for c in calls))
        elif check == 'later_read':
            # Evaluated against the saved head after state snapshots are available below.
            ok = False
        else:
            raise ValueError('Unknown check')
        findings[check] = 'pass' if ok else 'fail'
    if snapshots and len(snapshots) >= 2:
        before, after = snapshots[0]['counts'], snapshots[-1]['counts']
        if 'no_writes' in checks or 'no_tools' in checks:
            findings['state_unchanged'] = 'pass' if before == after and not snapshots[-1]['personal_context_exists'] else 'fail'
        if 'no_sync' in checks:
            findings['sync_state_unchanged'] = 'pass' if before['sync_jobs'] == after['sync_jobs'] else 'fail'
        if 'no_save' in checks:
            findings['save_state_unchanged'] = 'pass' if all(before[k] == after[k] for k in ('session_notes', 'set_proposals', 'set_revisions')) else 'fail'
        if 'note_only' in checks:
            ok = (after['session_notes'] == before['session_notes'] + 1
                  and after['set_proposals'] == before['set_proposals']
                  and after['set_revisions'] == before['set_revisions']
                  and len([n for n in snapshots[-1]['notes'] if n not in snapshots[0]['notes']
                           and n['activity_id'] == 4101 and 'hard' in n['text'].lower()]) == 1)
            findings['note_state'] = 'pass' if ok else 'fail'
        if 'draft_before_apply' in checks:
            attempts_before = [c for c in calls if c.get('turn') == 0 and c['tool'] == 'apply_proposal']
            before_approval = snapshots[1]['counts']['set_revisions'] if len(snapshots) > 1 else None
            findings['no_early_revision'] = 'pass' if not attempts_before and before_approval == before['set_revisions'] else 'fail'
            applied_calls = [c for c in successful if c['tool'] == 'apply_proposal' and c.get('turn') == 1]
            applied = []
            for call in applied_calls:
                result = call.get('result') or {}
                for proposal in snapshots[-1]['proposals']:
                    if (proposal['proposal_id'] == call.get('arguments', {}).get('proposal_id')
                            and proposal['activity_id'] == 4101 and proposal['applied_revision'] == result.get('revision')
                            and proposal['applied_revision'] is not None
                            and snapshots[-1]['heads'].get(4101) == proposal['applied_revision']):
                        applied.extend(v for v in snapshots[-1]['versions'] if v['activity_id'] == 4101
                                       and v['revision'] == proposal['applied_revision'])
            findings['saved_structure'] = 'pass' if any(structured_correct(p['workout']) for p in applied) else 'fail'
            expected_counts = all(after[k] == before[k] + 1 for k in ('session_notes', 'set_proposals', 'set_revisions'))
            findings['authorized_write_counts'] = 'pass' if expected_counts else 'fail'
            findings['later_read'] = 'pass' if applied and recall_evidence(successful, applied[0]['revision']) else 'fail'
            findings['no_recall_writes'] = 'pass' if not any(c.get('turn') == 2 and c['tool'] in WRITES for c in calls) else 'fail'
    return {'checks': findings, 'semantic_review': 'unverified', 'overall': 'fail' if 'fail' in findings.values() else 'needs_semantic_review'}


def ready_for_approval(calls: list[dict], response: str, before: dict, after: dict) -> bool:
    """Conservative content gate; semantic review of what was shown is still required."""
    text = response.casefold()
    if not all(word in text for word in ('squat', 'row', '10', '8')):
        return False
    if before['counts']['set_revisions'] != after['counts']['set_revisions']:
        return False
    if any(c['tool'] == 'apply_proposal' and c.get('turn') == 0 for c in calls):
        return False
    for call in calls:
        if call['tool'] != 'propose_workout' or call.get('outcome') != 'success' or call.get('turn') != 0:
            continue
        result = call.get('result') or {}
        if result.get('activity_id') != 4101 or not isinstance(result.get('workout'), dict):
            continue
        if not any(p['proposal_id'] == result.get('proposal_id') and p['activity_id'] == 4101
                   and p['applied_revision'] is None and p['workout'] == result['workout'] for p in after['proposals']):
            continue
        if structured_correct(result['workout']):
            return True
    return False

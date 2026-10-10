"""Revisioned review and one-transaction human facts. No private recovery path."""
import json

from ..core_io import canonical, digest, now
from .store import require, token, exact_keys, replay, record, check_basis, state, bump, hook

ACTIONS = {'EXISTING_PERSON', 'NEW_PERSON', 'REJECT', 'UNASSIGN', 'OUTSIDER', 'IGNORE_FACE', 'CORRECTION'}


def _session(c, sid):
    row = c.execute('SELECT * FROM session WHERE session_id=?', (sid,)).fetchone()
    require(row is not None, 'NOT_FOUND', 'Unknown session')
    result = dict(row)
    for k in ('basis', 'source', 'decisions', 'applied_receipt'):
        result[k] = json.loads(result[k]) if result[k] is not None else None
    return result


def fetch_session(ws, session_id):
    with ws.read() as c:
        return _session(c, session_id)


def create_session(ws, run_id, request_id):
    payload = {'run_id': run_id}
    with ws.write() as c:
        prior = replay(c, 'session', request_id, payload)
        if prior is not None:
            return prior
        row = c.execute('SELECT * FROM machine_result WHERE run_id=?', (run_id,)).fetchone()
        require(row is not None, 'NOT_FOUND', 'Unknown machine result')
        source = json.loads(row['result'])
        check_basis(c, source['basis'])
        sid = 'review_' + digest([ws.workspace_id, request_id, run_id])
        c.execute('INSERT INTO session VALUES(?,0,?,?,?,?,NULL)',
                  (sid, 'DRAFT', row['basis'], canonical(source), '[]'))
        c.execute('INSERT INTO review_revision VALUES(?,0,?)', (sid, '[]'))
        return record(c, 'session', request_id, payload, _session(c, sid))


def _state_shape(value):
    exact_keys(value, ('state', 'person_id'))
    require(value['state'] in ('UNKNOWN', 'ASSIGNED', 'OUTSIDER', 'IGNORED', 'UNASSIGNED'), 'INVALID_DECISION', 'Invalid state')
    require((value['state'] == 'ASSIGNED' and isinstance(value['person_id'], str) and value['person_id']) or
            (value['state'] != 'ASSIGNED' and value['person_id'] is None), 'INVALID_DECISION', 'Invalid state identity')


def _person(c, pid):
    r = c.execute("SELECT * FROM person WHERE person_id=? AND status='active'", (pid,)).fetchone()
    require(r is not None, 'INVALID_DECISION', 'Target person is not active')


def validate_decisions(c, session, decisions):
    require(isinstance(decisions, list) and 0 < len(decisions) <= len(session['source']['candidates']),
            'INVALID_DECISION', 'A nonempty bounded decision list is required')
    candidates = {r['face_id']: r for r in session['source']['candidates']}
    seen, names, normalized = set(), {}, []
    for d in decisions:
        require(isinstance(d, dict) and d.get('action') in ACTIONS, 'INVALID_DECISION', 'Unknown action')
        action = d['action']
        extra = {'EXISTING_PERSON': {'person_id'}, 'NEW_PERSON': {'display_name', 'person_key'},
                 'REJECT': {'person_id'}, 'CORRECTION': {'to'}}.get(action, set())
        exact_keys(d, {'face_id', 'action', 'expected_state'} | extra, {'enroll_reference'})
        fid = d['face_id']
        require(fid in candidates and fid not in seen, 'INVALID_DECISION', 'Face outside session or duplicated')
        seen.add(fid)
        _state_shape(d['expected_state'])
        require(d['expected_state'] == {k: candidates[fid][k] for k in ('state', 'person_id')},
                'STALE_SOURCE', 'Decision does not match frozen human state')
        if 'enroll_reference' in d:
            require(type(d['enroll_reference']) is bool, 'INVALID_DECISION', 'Reference choice must be explicit boolean')
        if action in ('EXISTING_PERSON', 'REJECT'):
            _person(c, d['person_id'])
        if action == 'NEW_PERSON':
            token(d['person_key'])
            name = d['display_name']
            require(isinstance(name, str) and name == name.strip() and 0 < len(name) <= 128
                    and not any(ord(ch) < 32 for ch in name), 'INVALID_DECISION', 'Invalid display name')
            require(not c.execute('SELECT 1 FROM person WHERE display_name=?', (name,)).fetchone(),
                    'CONFLICT', 'Person display name already exists')
            require(d['person_key'] not in names or names[d['person_key']] == name, 'CONFLICT', 'Person group has conflicting names')
            require(not any(k != d['person_key'] and v == name for k, v in names.items()), 'CONFLICT', 'Different new persons share a name')
            names[d['person_key']] = name
        if action == 'CORRECTION':
            _state_shape(d['to'])
            require(d['to']['state'] != 'UNKNOWN', 'INVALID_DECISION', 'Correction cannot erase human review history')
            if d['to']['state'] == 'ASSIGNED':
                _person(c, d['to']['person_id'])
        if action in ('EXISTING_PERSON', 'NEW_PERSON'):
            require(d['expected_state']['state'] in ('UNKNOWN', 'UNASSIGNED'),
                    'CORRECTION_REQUIRED', 'Existing human terminal fact requires explicit correction')
        assigned = action in ('EXISTING_PERSON', 'NEW_PERSON') or action == 'CORRECTION' and d['to']['state'] == 'ASSIGNED'
        require(not d.get('enroll_reference') or assigned, 'INVALID_DECISION', 'Only assigned faces can become references')
        if action == 'EXISTING_PERSON':
            require(not c.execute('SELECT 1 FROM rejection WHERE face_id=? AND person_id=?', (fid, d['person_id'])).fetchone(),
                    'CORRECTION_REQUIRED', 'Explicit correction required to revoke a human rejection')
        normalized.append(json.loads(canonical(d)))
    return sorted(normalized, key=lambda d: d['face_id'])


def save_decisions(ws, session_id, revision, decisions, request_id):
    require(type(revision) is int and revision >= 0, 'INVALID_REQUEST', 'Invalid review revision')
    payload = {'session_id': session_id, 'revision': revision, 'decisions': decisions}
    with ws.write() as c:
        prior = replay(c, 'save', request_id, payload)
        if prior is not None:
            return prior
        s = _session(c, session_id)
        require(s['revision'] == revision, 'STALE_REVISION', 'Review draft advanced')
        require(s['status'] == 'DRAFT', 'FROZEN_REVISION', 'Applied session is immutable')
        check_basis(c, s['basis'])
        normalized = validate_decisions(c, s, decisions)
        rev = revision + 1
        c.execute('INSERT INTO review_revision VALUES(?,?,?)', (session_id, rev, canonical(normalized)))
        c.execute('UPDATE session SET revision=?,decisions=? WHERE session_id=?', (rev, canonical(normalized), session_id))
        result = {'session_id': session_id, 'revision': rev, 'decision_digest': digest(normalized)}
        return record(c, 'save', request_id, payload, result)


def apply(ws, session_id, revision, request_id, *, fault=None):
    require(type(revision) is int and revision >= 0, 'INVALID_REQUEST', 'Invalid review revision')
    payload = {'session_id': session_id, 'revision': revision}
    with ws.write() as c:
        previous = replay(c, 'apply', request_id, payload)
        if previous is not None:
            return previous
        s = _session(c, session_id)
        require(s['revision'] == revision, 'STALE_REVISION', 'Review revision changed')
        if s['status'] == 'APPLIED':
            return record(c, 'apply', request_id, payload, s['applied_receipt'])
        check_basis(c, s['basis'])
        decisions = validate_decisions(c, s, s['decisions'])
        dd = digest(decisions)
        people, events = {}, []
        for d in decisions:
            fid, action = d['face_id'], d['action']
            require(state(c, fid) == d['expected_state'], 'STALE_SOURCE', 'Live human state changed')
            eid = 'event_' + digest([ws.workspace_id, session_id, revision, dd, fid])
            if action == 'NEW_PERSON':
                pid = 'person_' + digest([ws.workspace_id, session_id, d['person_key']])
                if d['person_key'] not in people:
                    c.execute('INSERT INTO person VALUES(?,?,?)', (pid, d['display_name'], 'active'))
                    people[d['person_key']] = pid
                target = {'state': 'ASSIGNED', 'person_id': pid}
            elif action == 'EXISTING_PERSON':
                target = {'state': 'ASSIGNED', 'person_id': d['person_id']}
            elif action == 'CORRECTION':
                target = d['to']
                if target['state'] == 'ASSIGNED':
                    c.execute('DELETE FROM rejection WHERE face_id=? AND person_id=?', (fid, target['person_id']))
            elif action == 'REJECT':
                c.execute('INSERT INTO rejection VALUES(?,?,?) ON CONFLICT(face_id,person_id) DO UPDATE SET event_id=excluded.event_id',
                          (fid, d['person_id'], eid))
                old = state(c, fid)
                target = {'state': 'UNASSIGNED', 'person_id': None} if old['state'] == 'UNKNOWN' or old['state'] == 'ASSIGNED' and old['person_id'] == d['person_id'] else old
            else:
                target = {'state': {'UNASSIGN': 'UNASSIGNED', 'OUTSIDER': 'OUTSIDER', 'IGNORE_FACE': 'IGNORED'}[action], 'person_id': None}
            if action != 'REJECT' or state(c, fid) != target:
                c.execute('DELETE FROM reference WHERE face_id=?', (fid,))
            c.execute('INSERT INTO human_state VALUES(?,?,?,?) ON CONFLICT(face_id) DO UPDATE SET '
                      'state=excluded.state,person_id=excluded.person_id,event_id=excluded.event_id',
                      (fid, target['state'], target['person_id'], eid))
            if d.get('enroll_reference'):
                model = c.execute('SELECT model_space FROM face WHERE face_id=?', (fid,)).fetchone()[0]
                c.execute('INSERT INTO reference VALUES(?,?,?,?)', (fid, target['person_id'], model, eid))
            event = {'event_id': eid, 'session_id': session_id, 'review_revision': revision,
                     'decision_digest': dd, 'decision': d, 'after': target, 'authority': 'human'}
            c.execute('INSERT INTO event VALUES(?,?)', (eid, canonical(event)))
            events.append(eid)
            hook(fault, 'after_human_event', c)
        result = {'session_id': session_id, 'revision': revision, 'decision_digest': dd,
                  'application_id': 'apply_' + digest([ws.workspace_id, session_id, revision, dd]),
                  'domain_revision': bump(c), 'event_ids': events, 'new_persons': people, 'applied_at': now()}
        result['receipt_digest'] = digest(result)
        c.execute("UPDATE session SET status='APPLIED',applied_receipt=? WHERE session_id=?", (canonical(result), session_id))
        hook(fault, 'before_apply_commit', c)
        return record(c, 'apply', request_id, payload, result)


def person_command(ws, action, request_id, expected_basis, *, person_id=None, display_name=None):
    """Human-only fresh person CRUD; deletion is retirement, IDs are never reused."""
    require(action in ('CREATE', 'RENAME', 'RETIRE'), 'INVALID_REQUEST', 'Unsupported person action')
    if action in ('CREATE', 'RENAME'):
        require(isinstance(display_name, str) and display_name == display_name.strip() and
                0 < len(display_name) <= 128 and not any(ord(x) < 32 for x in display_name),
                'INVALID_REQUEST', 'Invalid display name')
    require(action != 'CREATE' or person_id is None, 'INVALID_REQUEST', 'Engine assigns person identity')
    require(action != 'RETIRE' or display_name is None, 'INVALID_REQUEST', 'Retirement has no new name')
    payload = {'action': action, 'person_id': person_id, 'display_name': display_name, 'basis': expected_basis}
    with ws.write() as c:
        previous = replay(c, 'person', request_id, payload)
        if previous is not None:
            return previous
        check_basis(c, expected_basis)
        if action in ('CREATE', 'RENAME'):
            require(not c.execute('SELECT 1 FROM person WHERE display_name=? AND person_id IS NOT ?',
                                  (display_name, person_id)).fetchone(), 'CONFLICT', 'Display name already exists')
        if action == 'CREATE':
            person_id = 'person_' + digest([ws.workspace_id, 'person-command', request_id])
            c.execute('INSERT INTO person VALUES(?,?,?)', (person_id, display_name, 'active'))
        else:
            _person(c, person_id)
            if action == 'RENAME':
                c.execute('UPDATE person SET display_name=? WHERE person_id=?', (display_name, person_id))
            else:
                require(not c.execute('SELECT 1 FROM human_state WHERE person_id=?', (person_id,)).fetchone(),
                        'CONFLICT', 'Unassign active human facts before retiring this person')
                require(not c.execute('SELECT 1 FROM reference WHERE person_id=?', (person_id,)).fetchone(),
                        'CONFLICT', 'Active references prevent retirement')
                c.execute("UPDATE person SET status='retired' WHERE person_id=?", (person_id,))
        event = {'event_id': 'event_' + digest([ws.workspace_id, 'person', request_id, payload]),
                 'kind': 'PERSON_' + action, 'person_id': person_id, 'display_name': display_name, 'authority': 'human'}
        c.execute('INSERT INTO event VALUES(?,?)', (event['event_id'], canonical(event)))
        result = {'person_id': person_id, 'action': action, 'domain_revision': bump(c), 'event_id': event['event_id']}
        return record(c, 'person', request_id, payload, result)

"""Trusted request scope. Model arguments can never widen these permissions.

Legacy memories are owner-private. QQ groups only see their own scoped entries;
the desktop and authenticated owner DMs can see private memories. Persona files
remain unchanged: this module governs runtime data and tool authority only.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_SCOPE = ContextVar('aipet_access_scope', default={})
PUBLIC_TOOLS = frozenset({'get_time', 'web_search', 'recall', 'group_knowledge'})


def current():
    return dict(_SCOPE.get())


@contextmanager
def bind(**values):
    scope = {**current(), **values}
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def private_allowed():
    s = current()
    return s.get('source') != 'qq' or (s.get('is_owner') is True and s.get('scene') == 'c2c')


def allows_tool(name):
    s = current()
    if s.get('source') != 'qq':
        return True
    if name == 'tavily_usage':
        return s.get('is_owner') is True
    if name == 'run_python':
        return False
    if private_allowed():
        return True
    return name in PUBLIC_TOOLS or (name == 'remember' and s.get('is_owner') is True)


def can_read(entry):
    s = current()
    if private_allowed():
        return True
    if entry.get('persona_id') and entry['persona_id'] != s.get('persona_id'):
        return False
    visibility = entry.get('visibility', 'private')
    if visibility == 'public':
        return True
    return (visibility == 'group' and s.get('scene') == 'group'
            and bool(s.get('group_id')) and entry.get('group_id') == s['group_id'])


def provenance(writer):
    s = current()
    if s.get('source') != 'qq':
        return {}
    return {key: value for key, value in {
        'source': 'qq', 'origin': 'qq', 'writer': writer,
        'speaker': 'owner' if s.get('is_owner') is True else 'guest',
        'actor_id': s.get('actor_id', ''), 'group_id': s.get('group_id', ''),
        'message_id': s.get('message_id', ''), 'persona_id': s.get('persona_id', ''),
        'visibility': 'group' if s.get('scene') == 'group' else 'private',
    }.items() if value != ''}

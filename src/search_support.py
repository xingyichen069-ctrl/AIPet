"""Small search-only safeguards; no models, credentials or paid fallback."""
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from urllib.parse import unquote, urlsplit

import atomic_store as STORE
from web_direct import WebError

RETRYABLE = frozenset({'no_results', 'low_relevance', 'site_mismatch'})
FAILURE_TTL = 30
FAILURE_LIMIT = 128
TRACE_BYTES = 256 * 1024  # Active file + one backup, independent of query volume.
_FAILURES = OrderedDict()
_LOCK = threading.Lock()
_SITE = re.compile(r'(?<!\S)(-?)site:([^\s]+)', re.I)
_NEGATIVE = re.compile(r'(?<!\S)-(?:"[^"]+"|\S+)')
_COMMON = frozenset('the and for with from this that what how official website latest news information about'.split())


def fingerprint(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:20]


def provider_error(error):
    """Classify third-party failures without returning raw URLs or proxy secrets."""
    value = (type(error).__name__ + ' ' + str(error)).casefold()
    status = getattr(error, 'code', None)
    if isinstance(error, TimeoutError) or any(s in value for s in ('timeout', 'timed out')):
        return WebError('普通搜索等待超时；未调用 Tavily。', 'timeout')
    if status in (401, 403, 429, 451, 468) or 'ratelimit' in value:
        return WebError('普通搜索被上游拒绝或限流；未调用 Tavily。', 'http_denied',
                        status=status if isinstance(status, int) else None)
    if isinstance(status, int) and 400 <= status < 600:
        return WebError(f'普通搜索返回 HTTP {status}；未调用 Tavily。', 'http_error', status=status)
    if any(s in value for s in ('no results', 'no result found')):
        return WebError('普通搜索后端报告无结果；未调用 Tavily。', 'no_results')
    if isinstance(error, OSError) or any(s in value for s in ('connection', 'dnserror', 'ssl', 'unreachable')):
        return WebError('普通搜索连接失败；未调用 Tavily。', 'network_error')
    return WebError('普通搜索后端发生异常；未调用 Tavily。', 'backend_error')


def fallback_query(query, alternative):
    """A known equivalent supplied in the same tool call; keep site/exclusion limits."""
    if not alternative or alternative.casefold() == query.casefold():
        return ''
    sites = {m.group(0).casefold() for m in _SITE.finditer(query)}
    alternate_sites = {m.group(0).casefold() for m in _SITE.finditer(alternative)}
    if alternate_sites - sites:
        raise ValueError('备用关键词不能新增或替换原查询的 site: 范围。')
    constraints = [m.group(0) for m in _SITE.finditer(query)]
    constraints.extend(m.group(0) for m in _NEGATIVE.finditer(query))
    present = {m.group(0).casefold() for m in _SITE.finditer(alternative)}
    present.update(m.group(0).casefold() for m in _NEGATIVE.finditer(alternative))
    extra = list(dict.fromkeys(x for x in constraints if x.casefold() not in present))
    candidate = ' '.join([alternative, *extra])
    return candidate if candidate.casefold() != query.casefold() else ''


def _site_matches(url, spec):
    try:
        target = urlsplit(url)
        site = urlsplit(spec if '://' in spec else 'https://' + spec)
        host = (target.hostname or '').casefold().rstrip('.')
        wanted = (site.hostname or '').casefold().rstrip('.')
        path = unquote(target.path)
        prefix = unquote(site.path).rstrip('/')
        return bool(wanted and (host == wanted or host.endswith('.' + wanted))
                    and (not prefix or path == prefix or path.startswith(prefix + '/')))
    except ValueError:
        return False


def relevant_results(query, rows):
    """Reject off-site and zero-overlap batches, not claim semantic verification."""
    if not rows:
        raise WebError('本次普通搜索没有匹配结果；不代表网络不可用，未调用 Tavily。', 'no_results')
    sites = _SITE.findall(query)
    positive = [s for neg, s in sites if not neg]
    negative = [s for neg, s in sites if neg]
    scoped = [r for r in rows if (not positive or any(_site_matches(r.get('url', ''), s) for s in positive))
              and not any(_site_matches(r.get('url', ''), s) for s in negative)]
    if not scoped:
        raise WebError('搜索结果不符合指定站点范围，未作为来源；未调用 Tavily。', 'site_mismatch')
    topic = _NEGATIVE.sub(' ', _SITE.sub(' ', query))
    topic = re.sub(r'\b\w+:\S+', ' ', topic)  # Other operators are not subject words.
    topic = unicodedata.normalize('NFKC', topic).casefold()
    topic = re.sub(r'官方网站|官网|最新|新闻|搜索|查询|相关|资料|介绍|是什么|如何', ' ', topic)
    words = set(re.findall(r'[a-z0-9_]{2,}', topic)) - _COMMON
    pairs = {chunk[i:i+2] for chunk in re.findall(r'[\u3400-\u9fff]+', topic)
             for i in range(len(chunk)-1)}
    # A synonym may not share spelling. Only reject when the entire batch has
    # no evidence, and tell the caller this is a heuristic, not a fact check.
    def overlap(row):
        text = unicodedata.normalize('NFKC', str(row.get('title', '')) + ' ' + str(row.get('snippet', ''))).casefold()
        return bool(words.intersection(re.findall(r'[a-z0-9_]{2,}', text))
                    or any(pair in text for pair in pairs))
    if (words or pairs) and not any(overlap(r) for r in scoped):
        raise WebError('结果标题和摘要未匹配关键词，相关性不足，暂不作为来源；可用已知别名，未调用 Tavily。',
                       'low_relevance')
    unique, seen = [], set()
    for row in scoped:
        key = row.get('url', '').split('#', 1)[0]
        if key and key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def failure_get(key):
    with _LOCK:
        now = time.monotonic()
        for expired in [k for k, v in _FAILURES.items() if v[0] <= now]:
            del _FAILURES[expired]
        hit = _FAILURES.get(key)
        if hit:
            return WebError(hit[2], hit[1], status=hit[3], cached=True)
    return None


def failure_put(key, error):
    # Never retain exception objects: their tracebacks hold request state alive.
    with _LOCK:
        _FAILURES[key] = (time.monotonic() + FAILURE_TTL, error.code, str(error), error.status)
        _FAILURES.move_to_end(key)
        while len(_FAILURES) > FAILURE_LIMIT:
            _FAILURES.popitem(last=False)


def trace(root, cache_dir, *, query, backend, kind, scope, outcome, count, elapsed,
          cached=False, fallback=False, request_id='', http_status=None):
    """Bounded metadata only; exclude text, results, URLs, keys and raw errors."""
    event = dict(at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                 query_id=fingerprint(query), query_chars=len(query), backend=backend,
                 kind=kind, source_scope=scope, outcome=outcome, results=count,
                 elapsed_ms=round(max(0, elapsed)*1000), cached=cached, fallback=fallback)
    if request_id:
        event['request_id'] = fingerprint(request_id)
    if http_status is not None:
        event['http_status'] = http_status
    raw = (json.dumps(event, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
    try:
        # The lock only covers the tiny append/rotation, never a network request.
        with STORE.state_lock(root):
            cache_dir.mkdir(parents=True, exist_ok=True)
            path = cache_dir / 'search-events.jsonl'
            if path.exists() and path.stat().st_size + len(raw) > TRACE_BYTES:
                os.replace(path, path.with_suffix('.jsonl.1'))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                stream.write(raw)
    except OSError:
        pass  # Diagnostics must not discard a result or trigger another search.

"""Opt-in, basic-only Tavily search with a durable local credit reservation.

The counter belongs to this installation, not to the provider account. Failed
requests keep their reservation because a timeout does not prove non-billing.
"""
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import time

import atomic_store as STORE

MAX_RESPONSE = 512 * 1024
MAX_QUERY = 500
MONTHLY_LIMIT = 400
DAILY_LIMIT = 20


class TavilyError(RuntimeError):
    """Only static, credential-free messages may leave this module."""


def _limit(config, name, default, ceiling):
    value = config.get(name, default)
    if type(value) is not int or not 0 <= value <= ceiling:
        raise TavilyError(f"Tavily 配置 {name} 应为 0–{ceiling} 的整数。")
    return value


def load_key(root, config):
    """Reload only the private key; never include file contents in errors."""
    path = Path(root) / 'data/secrets.json'
    try:
        if path.exists():
            if path.stat().st_size > 128 * 1024:
                raise ValueError()
            secrets = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(secrets, dict):
                raise ValueError()
        else:
            secrets = {}
        key = (secrets.get('tavily_api_key') or os.environ.get('TAVILY_API_KEY')
               or config.get('tavily_key') or '')
        if not isinstance(key, str):
            raise ValueError()
        key = key.strip()
        if key and (len(key) > 512 or any(not 33 <= ord(c) <= 126 for c in key)):
            raise ValueError()
    except (OSError, ValueError):
        raise TavilyError('Tavily 密钥配置无法读取，请检查 data/secrets.json。') from None
    if not key:
        raise TavilyError('境外搜索尚未启用：请在 data/secrets.json 填入 tavily_api_key。')
    return key


def _usage_state(path, day):
    month=day[:7]
    state = {'schema': 1, 'month': month, 'day': day,
             'month_reserved': 0, 'day_reserved': 0}
    if path.exists():
        if path.stat().st_size > 4096:
            raise ValueError()
        state = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('schema') != 1
                or any(type(state.get(k)) is not int or state[k] < 0
                       for k in ('month_reserved', 'day_reserved'))
                or state['day_reserved'] > state['month_reserved']):
            raise ValueError()
        old_day = datetime.strptime(state['day'], '%Y-%m-%d').strftime('%Y-%m-%d')
        if old_day != state['day'] or state['month'] != old_day[:7] or old_day > day:
            raise ValueError()
        if state['month'] != month:
            state.update(month=month, month_reserved=0)
        if state['day'] != day:
            state.update(day=day, day_reserved=0)
    return state


def reserve(root, settings, *, now=None):
    """Reserve before I/O, across threads/processes; never reset damaged state."""
    monthly = _limit(settings, 'monthly_limit', MONTHLY_LIMIT, 1000)
    daily = _limit(settings, 'daily_limit', DAILY_LIMIT, 100)
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime('%Y-%m-%d')
    month = day[:7]
    path = Path(root) / 'data/tavily_usage.json'
    try:
        with STORE.state_lock(root):
            state = _usage_state(path, day)
            if state['month_reserved'] >= monthly:
                raise TavilyError('Tavily 已达本安装的每月上限，本次不请求；下月 UTC 重置。')
            if state['day_reserved'] >= daily:
                raise TavilyError('Tavily 已达本安装的每日上限，本次不请求；次日 UTC 重置。')
            state['month_reserved'] += 1
            state['day_reserved'] += 1
            STORE.write_json(path, state)
    except (OSError, ValueError, KeyError, TypeError):
        raise TavilyError('Tavily 用量记录无法安全读写，本次未请求，请先检查用量文件。') from None


def _time_left(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TavilyError('境外搜索超时，未自动重试。')
    return remaining


def _request(method, path, payload, key, deadline):
    # Fixed HTTPS host, no proxy autodetection, redirects or automatic retries.
    # Refresh socket timeouts before headers and each bounded body read so a
    # trickling response cannot keep the QQ worker occupied indefinitely.
    connection = http.client.HTTPSConnection('api.tavily.com', timeout=_time_left(deadline))
    response = None
    try:
        connection.request(method, path, body=json.dumps(payload).encode('utf-8') if payload is not None else None,
                           headers={'Content-Type': 'application/json',
                                    'Authorization': f'Bearer {key}'})
        sock = connection.sock
        sock.settimeout(_time_left(deadline))
        response = connection.getresponse()
        if response.status != 200:
            if response.status in (401, 403):
                raise TavilyError('Tavily 密钥无效或无权访问，请检查免费账号配置。')
            if response.status in (402, 432, 433):
                raise TavilyError('Tavily 账号额度已用尽或被限制；本次停止，不升级套餐。')
            if response.status == 429:
                raise TavilyError('Tavily 请求过于频繁，本次停止，不自动重试。')
            raise TavilyError(f'Tavily 暂不可用（HTTP {response.status}），未自动重试。')
        raw = bytearray()
        while True:
            if response.isclosed():
                break
            sock.settimeout(_time_left(deadline))
            block = response.read1(min(32768, MAX_RESPONSE + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
            if len(raw) > MAX_RESPONSE:
                raise TavilyError('Tavily 响应过大，已停止读取。')
        _time_left(deadline)
        return json.loads(raw.decode('utf-8'))
    finally:
        if response is not None:
            response.close()
        connection.close()


def _post(payload, key, deadline):
    return _request('POST', '/search', payload, key, deadline)


def _provider_counts(data):
    values={
        'key_used':data['key']['usage'],
        'key_limit':data['key'].get('limit'),
        'search_credits':data['key'].get('search_usage'),
        'account_used':data['account']['plan_usage'],
        'account_limit':data['account']['plan_limit'],
    }
    if any((value is None and name not in ('key_limit','search_credits')) or
           (value is not None and (type(value) is not int or value<0))
           for name,value in values.items()):
        raise ValueError()
    return values


def usage(root, config, *, request_deadline=None):
    """Read local reservations and GET /usage; never reserve or run a search."""
    settings=config.get('tavily',{})
    if not isinstance(settings,dict):raise TavilyError('Tavily 配置格式有误。')
    day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
    try:
        with STORE.state_lock(root):
            local=_usage_state(Path(root)/'data/tavily_usage.json',day)
        local.update(day_limit=_limit(settings,'daily_limit',DAILY_LIMIT,100),
                     month_limit=_limit(settings,'monthly_limit',MONTHLY_LIMIT,1000))
    except (OSError,ValueError,KeyError,TypeError):
        raise TavilyError('本安装用量记录无法读取，未清零或改写计数。') from None
    result={'local':local,'provider':None,'provider_cached':False}
    try:key=load_key(root,config)
    except TavilyError as error:
        result['provider_error']=str(error);return result
    fingerprint=hashlib.sha256(key.encode()).hexdigest()
    cache=Path(root)/'data/cache/tavily_usage.json'
    try:
        if cache.stat().st_size>4096:raise ValueError()
        saved=json.loads(cache.read_text(encoding='utf-8'))
        if (saved['key_hash']==fingerprint and 0<=time.time()-saved['at']<60
                and saved['reservation']==[local['day'],local['month_reserved'],local['day_reserved']]):
            result.update(provider=_provider_counts(saved['counts']),provider_cached=True)
            return result
    except (OSError,ValueError,KeyError,TypeError):pass
    deadline=time.monotonic()+8
    if request_deadline is not None:deadline=min(deadline,request_deadline-20)
    try:
        counts=_request('GET','/usage',None,key,deadline)
        result['provider']=_provider_counts(counts)
        # Store only whitelisted counts, never keys, account identifiers or bodies.
        safe={'key':{'usage':result['provider']['key_used'],'limit':result['provider']['key_limit'],
                     'search_usage':result['provider']['search_credits']},
              'account':{'plan_usage':result['provider']['account_used'],
                         'plan_limit':result['provider']['account_limit']}}
        try:STORE.write_json(cache,{'key_hash':fingerprint,'at':time.time(),'counts':safe,
                    'reservation':[local['day'],local['month_reserved'],local['day_reserved']]})
        except OSError:pass
    except Exception:
        result['provider_error']='官方额度暂时查询失败；未调用搜索，也未改写本安装计数。'
    return result


def usage_text(root, config, *, request_deadline=None):
    try:state=usage(root,config,request_deadline=request_deadline)
    except TavilyError as error:return str(error)
    local=state['local'];provider=state['provider']
    lines=['Tavily 用量（credit）',
           f"本安装今日预留 {local['day_reserved']}/{local['day_limit']}，本月 {local['month_reserved']}/{local['month_limit']}（UTC）。"]
    if provider:
        remaining=max(0,provider['account_limit']-provider['account_used'])
        lines.append(f"官方当前周期：此密钥用了 {provider['key_used']}；账号 {provider['account_used']}/{provider['account_limit']}，套餐剩余 {remaining}。")
        if state['provider_cached']:lines.append('官方数据来自一分钟内缓存。')
    else:lines.append(state['provider_error'])
    lines.append('查询不消耗搜索次数；基础搜索每次1 credit，本安装预留含失败请求，官方周期可能不同。')
    return '\n'.join(lines)


def search(root, config, query, n, kind, *, request_deadline=None):
    settings = config.get('tavily', {})
    if not isinstance(settings, dict):
        raise TavilyError('Tavily 配置格式有误。')
    if settings.get('enabled', True) is not True:
        raise TavilyError('Tavily 境外搜索已关闭。')
    key = load_key(root, config)
    timeout = _limit(settings, 'timeout_seconds', 12, 20)
    deadline = time.monotonic() + timeout
    if request_deadline is not None:
        # Leave time for the model to read the sources and send its QQ answer.
        deadline = min(deadline, request_deadline - 20)
    if deadline - time.monotonic() < 1:
        raise TavilyError('本轮剩余时间不足，未发起境外搜索。')
    payload = {'query': query, 'max_results': n,
               'topic': 'news' if kind == 'news' else 'general',
               'search_depth': 'basic', 'auto_parameters': False,
               'include_answer': False, 'include_raw_content': False,
               'include_images': False, 'chunks_per_source': 1}
    reserve(root, settings)
    try:
        data = _post(payload, key, deadline)
        if not isinstance(data, dict) or not isinstance(data.get('results'), list):
            raise ValueError()
        results = []
        for item in data['results'][:n]:
            if not isinstance(item, dict):
                raise ValueError()
            def field(name, size):
                value = item.get(name) or ''
                return value[:size] if isinstance(value, str) else ''
            url = field('url', 2048)
            if not url.startswith(('https://', 'http://')):
                continue
            results.append({'title': field('title', 240), 'url': url,
                            'snippet': field('content', 900), 'source': '',
                            'date': field('published_date', 48)})
        return {'answer': '', 'results': results}
    except TavilyError:
        raise
    except (TimeoutError, OSError, http.client.HTTPException):
        raise TavilyError('Tavily 连接失败或超时，未自动重试，也未切换收费服务。') from None
    except Exception:
        # Provider bodies / exception text may contain credentials or queries.
        raise TavilyError('Tavily 返回格式异常，本次搜索未完成。') from None

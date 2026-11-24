#!/usr/bin/env python3
'''har2recipe: turn a browser HAR into a Python client and a recipe that replays with no model.

  har2recipe calls  site.har                  list the data calls in a capture
  har2recipe learn  site.har --goal '...'     your own claude writes a client and recipe.json
  har2recipe run    site_api/recipe.json      replay with plain HTTP, no model
  har2recipe verify site_api/recipe.json      stale check; --har new.har re-learns

MIT licence. Python 3.9+, standard library only.
'''
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = '0.1.0'

SECRET_HEADERS = {'cookie', 'set-cookie', 'authorization', 'proxy-authorization', 'x-csrf-token', 'x-xsrf-token'}
SECRET_NAME = re.compile(r'^(.*[_-])?(token|secret|session|sessionid|signature|sig|auth|apikey|api_key|key)$', re.I)
SKIP_HEADERS = {'host', 'content-length', 'connection', 'accept-encoding', 'priority', 'upgrade-insecure-requests'}
STATIC = re.compile(r'\.(png|jpe?g|gif|webp|avif|svg|ico|css|js|mjs|woff2?|ttf|otf|eot|mp4|webm|mp3|wasm|map)(\?|$)', re.I)
TRACKERS = re.compile(r'google-analytics|googletagmanager|doubleclick|facebook\.com/tr|segment\.(io|com)|sentry\.io|'
                      r'hotjar|mixpanel|datadoghq|clarity\.ms|amplitude|bugsnag|newrelic|cloudflareinsights|recaptcha', re.I)
TOKEN = re.compile(r'\{(env:)?([A-Za-z_][A-Za-z0-9_]*)\}')
FILE_BLOCK = re.compile(r'<file name="([^"]+)">\n?(.*?)\n?</file>', re.S)
OUTPUTS = ('recipe.json', 'api_client.py', 'example_usage.py', 'README.md')
KEEP = set()  # header or query names exempt from redaction (learn --keep)


def die(msg, code=1):
    print(f'har2recipe: {msg}', file=sys.stderr)
    raise SystemExit(code)


def note(msg):
    print(msg, file=sys.stderr)


# --- reading a HAR -------------------------------------------------------

def load_har(path):
    try:
        with open(path, encoding='utf-8') as f:
            har = json.load(f)
    except (OSError, ValueError) as e:
        die(f'cannot read {path} as a HAR file: {e}')
    if not isinstance(har, dict) or not isinstance((har.get('log') or {}).get('entries'), list):
        die(f'{path} has no log.entries; export it from DevTools > Network > Export HAR')
    return har


def body_text(part):
    part = part or {}
    text = part.get('text') or ''
    if text and part.get('encoding') == 'base64':
        try:
            return base64.b64decode(text).decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            return ''
    return text


def secret(name):
    name = name.lower()
    return name not in KEEP and (name in SECRET_HEADERS or bool(SECRET_NAME.match(name)))


def header_map(headers):
    out = {}
    for h in headers or []:
        name = str(h.get('name', '')).lower()
        if not name or name.startswith(':') or name.startswith('sec-') or name in SKIP_HEADERS:
            continue
        out[name] = '<redacted>' if secret(name) else str(h.get('value', ''))
    return out


def redact_url(url):
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = [(k, '<redacted>' if secret(k) else v)
             for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(pairs, safe='<>{}:,')))


def is_data_call(entry):
    req, resp = entry.get('request') or {}, entry.get('response') or {}
    url = str(req.get('url', ''))
    if not url.startswith(('http://', 'https://')) or STATIC.search(url) or TRACKERS.search(url):
        return False
    try:
        status = int(resp.get('status') or 0)
    except (TypeError, ValueError):
        return False
    if not 200 <= status < 300:
        return False
    mime = str((resp.get('content') or {}).get('mimeType', '')).lower()
    kind = str(entry.get('_resourceType', '')).lower()
    looks_data = 'json' in mime or 'graphql' in url.lower() or (kind in ('xhr', 'fetch') and 'html' not in mime)
    return looks_data and bool(body_text(resp.get('content')).strip())


def pick_calls(har, max_calls=40):
    seen, calls = set(), []
    for entry in har['log']['entries']:
        if not isinstance(entry, dict) or not is_data_call(entry):
            continue
        req, resp = entry['request'], entry['response']
        method = str(req.get('method', 'GET')).upper()
        sent = body_text(req.get('postData'))
        key = (method, req['url'], sent[:500])
        if key in seen:
            continue
        seen.add(key)
        answer = body_text(resp.get('content'))
        calls.append({'method': method, 'url': req['url'], 'status': resp.get('status'),
                      'mime': (resp.get('content') or {}).get('mimeType', ''),
                      'headers': header_map(req.get('headers')), 'body': sent,
                      'response': answer, 'size': len(answer)})
    if len(calls) > max_calls:  # keep the biggest answers, in capture order
        keep = sorted(sorted(range(len(calls)), key=lambda i: -calls[i]['size'])[:max_calls])
        calls = [calls[i] for i in keep]
    return calls


# --- asking claude -------------------------------------------------------

RULES = '''recipe.json is replayed by har2recipe with plain HTTP and no model, so it must be exact. Shape:
{
  "site": "example.com",
  "name": "short-kebab-name",
  "about": "one line: what the items are",
  "url": "the page the user was on, with {param} where their input goes",
  "why": "one line: which endpoint serves the goal and why",
  "needs": "nothing (public), or the environment variables the user must set",
  "params": {"query": {"default": "a value seen in the capture", "about": "what it is"}},
  "requests": [
    {"name": "search", "method": "GET", "url": "https://.../search?q={query}",
     "headers": {"accept": "application/json"},
     "body": null,
     "save": {}}
  ],
  "extract": {"from": "search", "items": "data.results", "fields": {"title": "name", "users": "stats.users"}, "limit": 50},
  "output": "array of {title, users}",
  "check": {"min_items": 5, "required": ["title"]}
}
Rules:
- "{name}" in a url, header or body string is replaced by a param; "{env:NAME}" by an environment variable. Other braces stay as they are.
- body is null, a JSON object (sent as JSON) or a string (sent as is, e.g. a form).
- "save" copies values from that response into params for the next request (tokens, ids, cursors): {"param": "path"}.
- Paths are dot-separated keys, with numbers for list items: "data.search.edges", "node.title", "0.id".
- Keep only the headers the endpoint needs. Any value shown as <redacted> must become {env:SOMETHING}, and "needs" names it.
- Keep only the requests the goal needs (usually one). Set check.min_items to about half the items you saw.
'''

PROMPT = '''You turn a browser capture (HAR) into code that fetches the same data with plain HTTP.

Goal: @GOAL@
Page(s) the user visited: @PAGES@

Below are @N@ JSON/XHR calls from the capture. Static files and trackers are removed; cookie, auth and key values read <redacted>; long bodies are cut.

Choose the smallest set of calls that serves the goal. Never invent an endpoint that is not in the capture. Do not use tools. Reply with exactly these four files and nothing else:

<file name="recipe.json">
...
</file>
<file name="api_client.py">
...
</file>
<file name="example_usage.py">
...
</file>
<file name="README.md">
...
</file>

@RULES@
api_client.py: Python 3.9+, standard library only (urllib.request, json, dataclasses, typing). One class with one typed method per endpoint, typed results, docstrings that name the endpoint. Secrets come from environment variables, never literals.
example_usage.py: imports api_client, fetches what the goal asks for and prints the first 5 items.
README.md: under 40 lines: what it fetches, each endpoint, its params, the environment variables, and one line asking the reader to review the code and respect the site's terms.

## Calls

'''


def clip(text, limit):
    return text if len(text) <= limit else text[:limit] + f' ... [cut: {len(text):,} chars in all]'


def build_prompt(goal, pages, calls, max_body):
    lines = []
    for i, c in enumerate(calls, 1):
        lines.append(f"### call {i}: {c['method']} {redact_url(c['url'])} -> {c['status']} {c['mime']}")
        lines.append('request headers: ' + json.dumps(c['headers'], ensure_ascii=False))
        if c['body']:
            lines.append('request body: ' + clip(c['body'], max_body))
        lines.append('response body: ' + clip(c['response'], max_body))
        lines.append('')
    head = (PROMPT.replace('@RULES@', RULES).replace('@GOAL@', goal)
            .replace('@PAGES@', pages).replace('@N@', str(len(calls))))
    return head + '\n'.join(lines)


def ask_claude(prompt, model=None):
    exe = shutil.which('claude')
    if not exe:
        die('learn needs Claude Code: install it, log in, and make sure `claude` is on your PATH')
    cmd = [exe, '-p', 'Follow the instructions on stdin. Reply with text only; do not use tools.',
           '--output-format', 'json']
    if model:
        cmd += ['--model', model]
    start = time.time()
    try:
        done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        die('claude took more than 15 minutes; try a narrower --goal or a smaller HAR')
    if done.returncode != 0:
        die(f'claude exited {done.returncode}: {(done.stderr or done.stdout).strip()[:400]}')
    try:
        data = json.loads(done.stdout)
    except ValueError:
        return done.stdout, None, time.time() - start
    if not isinstance(data, dict):
        return done.stdout, None, time.time() - start
    if data.get('is_error'):
        die(f"claude: {str(data.get('result'))[:400]}")
    usd = data.get('total_cost_usd', data.get('cost_usd'))
    return str(data.get('result') or ''), usd, time.time() - start


FENCE = re.compile(r'^\s*```[\w.+-]*\n(.*?)\n?```\s*$', re.S)


def parse_files(text):
    out = {}
    for name, body in FILE_BLOCK.findall(text or ''):
        m = FENCE.match(body)
        out[name.strip()] = m.group(1) if m else body
    return out


def slug(text, limit=40):
    s = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return s[:limit].rstrip('-') or 'site'


def learn(har_path, goal, out=None, name=None, model=None, max_calls=40, max_body=3000,
          retries=1, timeout=30, dry_run=False, keep=()):
    KEEP.update(k.lower() for k in keep or ())
    har = load_har(har_path)
    calls = pick_calls(har, max_calls)
    if not calls:
        die('no JSON or XHR calls with a body in this HAR. In DevTools open Network, tick Preserve log, '
            'do the action on the page, then export the HAR with content.')
    pages = ', '.join(str(p.get('title') or '') for p in (har['log'].get('pages') or []) if isinstance(p, dict))
    prompt = build_prompt(goal, (pages or calls[0]['url'])[:500], calls, max_body)
    if dry_run:
        print(prompt)
        return 0
    name = name or slug(goal)
    folder = Path(out or name.replace('-', '_') + '_api')
    note(f'learn    {len(calls)} data calls from {har_path} -> your claude')
    spent, cost, extra, problems, wrote = 0.0, 0.0, '', ['no reply'], False
    for attempt in range(retries + 1):
        reply, usd, secs = ask_claude(prompt + extra, model)
        spent, cost = spent + secs, cost + (usd or 0.0)
        files = parse_files(reply)
        try:
            recipe = json.loads(files.get('recipe.json') or 'null')
            if not isinstance(recipe, dict) or not recipe.get('requests'):
                raise ValueError('no requests in it')
        except ValueError as e:
            problems = [f'recipe.json missing or not valid ({e})']
            extra = f'\n\nYour last reply failed: {problems[0]}. Reply again with all four files.'
            continue
        recipe.setdefault('site', urllib.parse.urlsplit(calls[0]['url']).hostname or '')
        recipe.setdefault('name', name)
        recipe.update(method='http', want=goal,
                      run='har2recipe run recipe.json (plain HTTP, no model); har2recipe verify recipe.json flags a stale recipe')
        recipe['learned'] = {'at': dt.date.today().isoformat(), 'by': f'har2recipe {VERSION}',
                             'model': model or 'claude default', 'har': Path(har_path).name,
                             'seconds': round(spent, 1), 'usd': round(cost, 4)}
        recipe.pop('verified', None)
        folder.mkdir(parents=True, exist_ok=True)
        for fname in OUTPUTS[1:]:
            if files.get(fname):
                (folder / fname).write_text(files[fname].rstrip() + '\n', encoding='utf-8')
        items, seconds, problems = replay_and_check(recipe, {}, timeout)
        if not problems:
            stamp_verified(recipe, seconds)
        write_recipe(folder / 'recipe.json', recipe)
        wrote = True
        if not problems:
            note(f"learned  {recipe['name']} in {spent:.0f}s · ${cost:.2f} on your claude")
            note(f'replay   {len(items)} items in {seconds:.2f}s · no model')
            note(f'wrote    {folder}/ ' + ', '.join(f for f in OUTPUTS if (folder / f).exists()))
            return 0
        if attempt < retries:
            note('retry    replay failed: ' + '; '.join(problems))
        shown = {k: recipe.get(k) for k in ('params', 'requests', 'extract', 'check')}
        extra = ('\n\nYour last recipe.json failed when replayed with plain HTTP: ' + '; '.join(problems)
                 + '\nIt was:\n' + json.dumps(shown, indent=1) + '\nFix it and reply again with all four files.')
    note(f'learned  in {spent:.0f}s · ${cost:.2f}, but the replay check failed: ' + '; '.join(problems))
    if wrote:
        note(f'         files are in {folder}/; check the recipe, try a narrower --goal or a fresh HAR')
    return 1


# --- replaying a recipe --------------------------------------------------

def fill(value, params, quote=False):
    if isinstance(value, dict):
        return {k: fill(v, params, quote) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, params, quote) for v in value]
    if not isinstance(value, str):
        return value
    whole = TOKEN.fullmatch(value)
    if whole and not quote and not whole.group(1) and whole.group(2) in params:
        return params[whole.group(2)]

    def sub(m):
        if m.group(1):
            if m.group(2) not in os.environ:
                raise ValueError(f'this recipe needs the environment variable {m.group(2)}')
            v = os.environ[m.group(2)]
        elif m.group(2) in params:
            v = params[m.group(2)]
            v = json.dumps(v) if isinstance(v, (dict, list)) else ('' if v is None else str(v))
        else:
            return m.group(0)
        return urllib.parse.quote(v, safe='') if quote else v
    return TOKEN.sub(sub, value)


def dig(obj, path):
    if path in (None, '', '.'):
        return obj
    for part in str(path).split('.'):
        if isinstance(obj, list) and re.fullmatch(r'-?\d+', part):
            i = int(part)
            obj = obj[i] if -len(obj) <= i < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


def fetch(method, url, headers, body, timeout):
    data = None
    if body is not None and body != '':
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            if not any(k.lower() == 'content-type' for k in headers):
                headers['content-type'] = 'application/json'
        else:
            data = str(body).encode()
    if not any(k.lower() == 'user-agent' for k in headers):
        headers['user-agent'] = f'Mozilla/5.0 (compatible; har2recipe/{VERSION})'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get('Content-Encoding', '').lower() == 'gzip':
            raw = gzip.decompress(raw)
        text = raw.decode(resp.headers.get_content_charset() or 'utf-8', 'replace')
    try:
        return json.loads(text)
    except ValueError:
        return text


def replay(recipe, overrides=None, timeout=30):
    params = {}
    for key, spec in (recipe.get('params') or {}).items():
        params[key] = spec.get('default') if isinstance(spec, dict) else spec
    params.update(recipe.get('defaults') or {})
    params.update(overrides or {})
    responses, last = {}, None
    start = time.time()
    for i, rq in enumerate(recipe.get('requests') or []):
        url = fill(rq['url'], params, quote=True)
        if '<redacted>' in url:
            raise ValueError('the recipe still holds a <redacted> value; turn it into {env:NAME}')
        headers = {k: str(fill(v, params)) for k, v in (rq.get('headers') or {}).items()
                   if k.lower() not in SKIP_HEADERS and not k.startswith(':') and v != '<redacted>'}
        last = fetch(str(rq.get('method', 'GET')).upper(), url, headers, fill(rq.get('body'), params), timeout)
        responses[rq.get('name') or f'r{i}'] = last
        for key, path in (rq.get('save') or {}).items():
            params[key] = dig(last, path)
    ex = recipe.get('extract') or {}
    source = responses.get(ex['from'], last) if ex.get('from') else last
    items = dig(source, ex.get('items'))
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    fields = ex.get('fields')
    if isinstance(fields, dict) and fields:
        items = [{k: dig(it, p) for k, p in fields.items()} for it in items]
    if ex.get('limit'):
        items = items[:int(ex['limit'])]
    return items, time.time() - start


def check(recipe, items):
    rule = recipe.get('check') or {}
    need = int(rule.get('min_items') or 1)
    problems = []
    if len(items) < need:
        problems.append(f'{len(items)} items, the recipe expects at least {need}')
    for field in rule.get('required') or []:
        empty = sum(1 for it in items if not isinstance(it, dict) or it.get(field) in (None, '', [], {}))
        if items and empty > len(items) * 0.2:
            problems.append(f"'{field}' is empty in {empty} of {len(items)} items")
    return problems


def replay_and_check(recipe, overrides, timeout):
    try:
        items, seconds = replay(recipe, overrides, timeout)
    except urllib.error.HTTPError as e:
        return [], 0.0, [f"HTTP {e.code} from {getattr(e, 'filename', '')} (logged out, blocked, or the endpoint moved)"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        return [], 0.0, [f'replay error: {e}']
    return items, seconds, check(recipe, items)


def stamp_verified(recipe, seconds):
    v = recipe.get('verified') if isinstance(recipe.get('verified'), dict) else {}
    v['replays'] = int(v.get('replays') or 0) + 1
    v['seconds'] = (list(v.get('seconds') or []) + [round(seconds, 2)])[-5:]
    v['complete'] = 1.0
    v['last'] = dt.date.today().isoformat()
    recipe['verified'] = v


def write_recipe(path, recipe):
    Path(path).write_text(json.dumps(recipe, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')


def load_recipe(path):
    try:
        recipe = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        die(f'cannot read recipe {path}: {e}')
    if not isinstance(recipe, dict) or not recipe.get('requests'):
        die(f'{path} is not a har2recipe recipe (no requests)')
    return recipe


def parse_params(pairs):
    out = {}
    for pair in pairs or []:
        if '=' not in pair:
            die(f'-p wants key=value, got {pair!r}')
        key, value = pair.split('=', 1)
        out[key] = value
    return out


def show(items, fmt, rows):
    if fmt == 'jsonl':
        for it in items:
            print(json.dumps(it, ensure_ascii=False))
        return
    if fmt == 'json':
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return
    dicts = [it if isinstance(it, dict) else {'value': it} for it in items[:rows]]
    cols = list(dict.fromkeys(k for it in dicts for k in it))[:6]

    def cell(v):
        v = '' if v is None else json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        return re.sub(r'\s+', ' ', v)[:32]
    widths = {c: max([len(c)] + [len(cell(it.get(c))) for it in dicts]) for c in cols}
    print('  '.join(c.ljust(widths[c]) for c in cols))
    print('  '.join('-' * widths[c] for c in cols))
    for it in dicts:
        print('  '.join(cell(it.get(c)).ljust(widths[c]) for c in cols))
    if len(items) > rows:
        print(f'... {len(items) - rows} more (--format json prints all)')


# --- commands ------------------------------------------------------------

def cmd_calls(a):
    KEEP.update(k.lower() for k in a.keep or ())
    calls = pick_calls(load_har(a.har), a.max_calls)
    if not calls:
        die('no JSON or XHR calls with a body in this HAR')
    for i, c in enumerate(calls, 1):
        print(f"{i:>3}  {c['method']:<6} {c['status']}  {c['size']:>9,} B  {redact_url(c['url'])[:110]}")
    note(f'{len(calls)} data calls; these, redacted, are what learn sends to your claude')
    return 0


def cmd_run(a):
    recipe = load_recipe(a.recipe)
    items, seconds, problems = replay_and_check(recipe, parse_params(a.param), a.timeout)
    if problems and not items:
        die('; '.join(problems) + f'\nstale? capture a fresh HAR, then: har2recipe verify {a.recipe} --har new.har', 2)
    show(items, a.format or ('table' if sys.stdout.isatty() else 'json'), a.rows)
    note(f'replay   {len(items)} items in {seconds:.2f}s · no model'
         + (f" · check: {'; '.join(problems)}" if problems else ''))
    return 0


def cmd_verify(a):
    recipe = load_recipe(a.recipe)
    items, seconds, problems = replay_and_check(recipe, parse_params(a.param), a.timeout)
    if not problems:
        stamp_verified(recipe, seconds)
        write_recipe(a.recipe, recipe)
        note(f"ok       {len(items)} items in {seconds:.2f}s · no model · verified {recipe['verified']['replays']}x")
        return 0
    note('stale    ' + '; '.join(problems))
    goal = recipe.get('want') or recipe.get('about') or 'the same data as before'
    if a.har:
        note(f'relearn  from {a.har}')
        return learn(a.har, goal, out=str(Path(a.recipe).resolve().parent), name=recipe.get('name'),
                     model=a.model, timeout=a.timeout)
    note(f"fix      capture a fresh HAR of {recipe.get('url') or recipe.get('site')}, then:\n"
         f'         har2recipe verify {a.recipe} --har new.har')
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(prog='har2recipe',
                                 description='Turn a browser HAR into a Python client and a recipe that replays with no model.')
    ap.add_argument('--version', action='version', version=f'har2recipe {VERSION}')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('calls', help='list the data calls in a HAR (what learn would send)')
    p.add_argument('har')
    p.add_argument('--max-calls', type=int, default=40)
    p.add_argument('--keep', action='append', metavar='NAME', help='a header or query name not to redact')
    p = sub.add_parser('learn', help='write api_client.py, example_usage.py, README.md and recipe.json with your own claude')
    p.add_argument('har')
    p.add_argument('--goal', '-g', required=True, help='what you want, e.g. "top 50 results for a search word: name, author, users"')
    p.add_argument('--out', '-o', help='output folder (default: <name>_api)')
    p.add_argument('--name', help='recipe name (default: from the goal)')
    p.add_argument('--model', help='passed to claude --model')
    p.add_argument('--keep', action='append', metavar='NAME',
                   help='a header or query name not to redact, e.g. a public search key')
    p.add_argument('--max-calls', type=int, default=40)
    p.add_argument('--max-body', type=int, default=3000, help='characters kept per body')
    p.add_argument('--retries', type=int, default=1, help='fix-and-retry rounds when the replay check fails')
    p.add_argument('--timeout', type=int, default=30)
    p.add_argument('--dry-run', action='store_true', help='print the prompt instead of calling claude')
    for name, text in (('run', 'replay a recipe with plain HTTP, no model'),
                       ('verify', 'replay and check; --har new.har re-learns a stale recipe')):
        p = sub.add_parser(name, help=text)
        p.add_argument('recipe')
        p.add_argument('-p', '--param', action='append', metavar='KEY=VALUE')
        p.add_argument('--timeout', type=int, default=30)
        if name == 'run':
            p.add_argument('--format', choices=('table', 'json', 'jsonl'))
            p.add_argument('--rows', type=int, default=15)
        else:
            p.add_argument('--har', help='a fresh HAR to re-learn from when the check fails')
            p.add_argument('--model')
    a = ap.parse_args(argv)
    if a.cmd == 'calls':
        return cmd_calls(a)
    if a.cmd == 'learn':
        return learn(a.har, a.goal, a.out, a.name, a.model, a.max_calls, a.max_body,
                     a.retries, a.timeout, a.dry_run, a.keep)
    return cmd_run(a) if a.cmd == 'run' else cmd_verify(a)


if __name__ == '__main__':
    sys.exit(main())

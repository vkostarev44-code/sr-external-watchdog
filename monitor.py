import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib import request

TARGETS = {
    'steelracing.ru': ('https://steelracing.ru/', ('SteelRacing',)),
    'lk.steelracing.ru': ('https://lk.steelracing.ru/login', ('<form', 'name="email"')),
}
STATE_TITLE = 'Состояние внешнего сторожа сайта'
TEST_TITLE = 'Резерв теста внешнего сторожа сайта'
BOT = 'github-actions[bot]'
UTC = timezone.utc
OPENER = request.build_opener(request.ProxyHandler({}))


def stamp(now):
    return now.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse_state(body):
    try:
        state = json.loads(body)
        if not isinstance(state, dict) or type(state.get('schema')) is not int or state['schema'] != 1:
            raise ValueError()
        if set(state) != {'schema', 'status', 'started_at', 'notification', 'goals'}:
            raise ValueError()
        if state.get('status') not in ('up', 'down') or state.get('notification') not in ('reserved', 'sent', 'unknown'):
            raise ValueError()
        started = state.get('started_at')
        if state['status'] == 'down' and not isinstance(started, str):
            raise ValueError()
        if started is not None:
            if not isinstance(started, str) or not started.endswith('Z'):
                raise ValueError()
            datetime.fromisoformat(started.replace('Z', '+00:00'))
        if not isinstance(state['goals'], list) or any(not isinstance(x, str) or x not in TARGETS for x in state['goals']):
            raise ValueError()
        return state
    except (ValueError, TypeError, KeyError):
        raise RuntimeError('Некорректное состояние') from None


def transition(store, sender, bad, now):
    state = store.load()
    if state is None:
        state = {'schema': 1, 'status': 'up', 'started_at': None, 'notification': 'sent', 'goals': []}
        if not bad:
            store.save(state)
            return
    state = parse_state(json.dumps(state))
    if state['status'] == ('down' if bad else 'up'):
        return
    if bad:
        state = {'schema': 1, 'status': 'down', 'started_at': stamp(now), 'notification': 'reserved', 'goals': sorted(bad)}
        text = '🔴 Внешний сторож: сайт недоступен: ' + ', '.join(sorted(bad))
    else:
        start = datetime.fromisoformat(state['started_at'].replace('Z', '+00:00'))
        minutes = max(0, int((now - start).total_seconds() // 60))
        text = f'🟢 Внешний сторож: сайт снова доступен, простой ~{minutes} мин'
        state = {'schema': 1, 'status': 'up', 'started_at': state['started_at'], 'notification': 'reserved', 'goals': []}
    store.save(state.copy())
    try:
        sender.send(text)
    except Exception:
        state['notification'] = 'unknown'
        try:
            store.save(state.copy())
        except Exception:
            pass  # The earlier durable reservation still prevents a duplicate.
        raise RuntimeError('Доставка неизвестна; резерв уже сохранён, не повторяйте отправку без проверки') from None
    state['notification'] = 'sent'
    try:
        store.save(state.copy())
    except Exception:
        raise RuntimeError('Telegram подтвердил отправку, но отметка не сохранена; резерв исключает повтор') from None


def probe(url, markers, runner=subprocess.run):
    try:
        result = runner(['curl', '--noproxy', '*', '--location', '--max-redirs', '3', '--max-time', '20', '--connect-timeout', '8', '--max-filesize', '2097152', '--silent', '--show-error', '--write-out', '\n%{http_code}', url], capture_output=True, timeout=23, check=False)
        body, code = result.stdout.rsplit(b'\n', 1)
        return result.returncode == 0 and code == b'200' and len(body) <= 2097152 and all(m.encode('utf-8') in body for m in markers)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def check_targets(check=probe, sleep=time.sleep):
    bad = set(TARGETS)
    first_bad = {}
    for attempt in range(3):
        for name in sorted(bad.copy()):
            url, markers = TARGETS[name]
            if check(url, markers):
                bad.remove(name)
                first_bad.pop(name, None)
            elif name not in first_bad:
                first_bad[name] = datetime.now(UTC)
        if not bad:
            break
        if attempt < 2:
            sleep(45)
    return bad, min((first_bad[n] for n in bad), default=None)


class GitHub:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.token = os.environ['GITHUB_TOKEN']
        self.issue = None

    def call(self, method, path, data=None):
        url = 'https://api.github.com/repos/' + self.repo + path
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'}
        payload = None if data is None else json.dumps(data).encode('utf-8')
        req = request.Request(url, data=payload, headers=headers, method=method)
        try:
            with OPENER.open(req, timeout=20) as response:
                return json.load(response)
        except Exception:
            raise RuntimeError('Ошибка GitHub API') from None

    def find(self, title):
        matches = []
        for page in range(1, 101):
            items = self.call('GET', f'/issues?state=all&per_page=100&page={page}')
            for item in items:
                if item.get('title') == title and item.get('user', {}).get('login') == BOT and 'pull_request' not in item:
                    matches.append(item)
            if len(items) < 100:
                break
        else:
            raise RuntimeError('Слишком много issues')
        if len(matches) > 1:
            raise RuntimeError('Несколько служебных issues')
        return matches[0] if matches else None

    def load(self):
        self.issue = self.find(STATE_TITLE)
        if self.issue is None:
            return None
        if self.issue.get('state') != 'open':
            raise RuntimeError('Issue состояния закрыт')
        return parse_state(self.issue.get('body'))

    def save(self, state):
        body = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if self.issue is None:
            self.issue = self.call('POST', '/issues', {'title': STATE_TITLE, 'body': body})
        else:
            self.issue = self.call('PATCH', '/issues/' + str(self.issue['number']), {'body': body})

    def keepalive(self, now, branch):
        item = self.call('GET', '/contents/README.md?ref=' + request.quote(branch, safe=''))
        content = base64.b64decode(item['content'], validate=False).decode('utf-8')
        pattern = r'<!-- keepalive: \d{4}-\d{2} -->'
        marker = '<!-- keepalive: ' + now.astimezone(UTC).strftime('%Y-%m') + ' -->'
        if len(re.findall(pattern, content)) != 1:
            raise RuntimeError('Некорректный маркер README')
        updated = re.sub(pattern, marker, content)
        if updated != content:
            self.call('PUT', '/contents/README.md', {'message': 'Обновить отметку активности', 'content': base64.b64encode(updated.encode('utf-8')).decode('ascii'), 'sha': item['sha'], 'branch': branch})


class Telegram:
    def send(self, text):
        try:
            token = os.environ['TELEGRAM_BOT_TOKEN']
            chat = os.environ['TELEGRAM_CHAT_ID']
            data = json.dumps({'chat_id': chat, 'text': text}).encode('utf-8')
            req = request.Request('https://api.telegram.org/bot' + token + '/sendMessage', data=data, headers={'Content-Type': 'application/json'}, method='POST')
            with OPENER.open(req, timeout=20) as response:
                result = json.load(response)
            message_id = result['result']['message_id']
            if result.get('ok') is not True or type(message_id) is not int:
                raise ValueError()
            return message_id
        except Exception:
            raise RuntimeError('Ошибка отправки Telegram') from None


def send_test(store, sender, now):
    if store.find(TEST_TITLE) is not None:
        raise RuntimeError('Тест уже зарезервирован')
    store.call('POST', '/issues', {'title': TEST_TITLE, 'body': json.dumps({'schema': 1, 'reserved_at': stamp(now)})})
    local = now.astimezone(timezone(timedelta(hours=3)))
    try:
        message_id = sender.send('Тест внешнего сторожа сайта, ' + local.strftime('%d.%m %H:%M МСК') + '. Отвечать не нужно.')
    except Exception:
        raise RuntimeError('Тест зарезервирован; доставка неизвестна, повторная отправка запрещена') from None
    print('TEST_MESSAGE_ID=' + str(message_id))


class FakeStore:
    def __init__(self):
        self.state = None

    def load(self):
        return None if self.state is None else self.state.copy()

    def save(self, state):
        self.state = state.copy()


class FakeSender:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


def run_mock():
    store, sender = FakeStore(), FakeSender()
    now = datetime(2026, 11, 19, tzinfo=UTC)
    for minute, bad in [(0, set()), (1, {'steelracing.ru'}), (2, {'steelracing.ru'}), (11, set()), (12, set())]:
        transition(store, sender, bad, now + timedelta(minutes=minute))
    assert sender.messages == ['🔴 Внешний сторож: сайт недоступен: steelracing.ru', '🟢 Внешний сторож: сайт снова доступен, простой ~10 мин']
    store.state = {'schema': 1, 'status': 'down', 'started_at': stamp(now), 'notification': 'reserved', 'goals': ['steelracing.ru']}
    transition(store, sender, {'steelracing.ru'}, now)
    assert len(sender.messages) == 2
    print('mock OK')


def flag(name):
    return os.environ.get(name, '').lower() == 'true'


def main():
    scheduled = os.environ.get('GITHUB_EVENT_NAME') == 'schedule'
    dry = False if scheduled else os.environ.get('INPUT_DRY_RUN', 'true').lower() != 'false'
    mock = False if scheduled else flag('INPUT_MOCK')
    test = False if scheduled else flag('INPUT_SEND_TEST')
    if mock:
        if not dry or test:
            raise RuntimeError('Недопустимый режим mock')
        run_mock()
        return
    if test and dry:
        raise RuntimeError('Тест отправки требует отключить dry_run')
    if not dry:
        github = GitHub()
        with open(os.environ['GITHUB_EVENT_PATH'], encoding='utf-8') as f:
            event = json.load(f)
        branch = event.get('repository', {}).get('default_branch') or github.call('GET', '')['default_branch']
        if os.environ.get('GITHUB_REF') != 'refs/heads/' + branch:
            raise RuntimeError('Разрешена только основная ветка')
        if test:
            send_test(github, Telegram(), datetime.now(UTC))
            return
    bad, first_bad = check_targets()
    if dry:
        if bad:
            raise RuntimeError('Не ответили: ' + ', '.join(sorted(bad)))
        print('все цели OK')
        return
    now = datetime.now(UTC)
    transition(github, Telegram(), bad, first_bad or now)
    github.keepalive(now, branch)
    if bad:
        raise RuntimeError('Не ответили: ' + ', '.join(sorted(bad)))
    print('все цели OK')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Ошибка: ' + (str(exc) if isinstance(exc, RuntimeError) else 'операция не выполнена'), file=sys.stderr)
        sys.exit(1)

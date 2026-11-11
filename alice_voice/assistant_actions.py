"""Deterministic local requests, isolated from LLM output and prompts."""
import asyncio
import ipaddress
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode, urlsplit
from time import monotonic
from uuid import uuid4

from .memory import FactMemory, owner_name_from, unsuitable
from .windows_actions import WindowsActions


def search_url(query, domain=''):
    query = query.strip()
    if not query or len(query) > 1500:
        raise ValueError('Укажите поисковый запрос до 1500 символов.')
    if domain:
        domain = domain.strip()
        parsed = urlsplit(domain if '://' in domain else 'https://' + domain)
        host = (parsed.hostname or '').encode('idna').decode('ascii')
        if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or
                parsed.port or parsed.query or parsed.fragment or parsed.path not in ('', '/') or
                not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or
                '.' not in host or host.endswith(('.local', '.localhost'))):
            raise ValueError('Укажите публичный домен сайта, например mozilla.org.')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError('Для поиска по сайту нужен домен, не IP-адрес.')
        query = f'site:{host} {query}'
    return 'https://www.google.com/search?' + urlencode({'q': query})


def find_firefox(configured=''):
    candidates = [configured] if configured else []
    if not configured:
        if os.name == 'nt':
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe',
                                            0, winreg.KEY_READ | view) as key:
                            candidates.append(winreg.QueryValue(key, None).strip('"'))
                    except OSError:
                        pass
        candidates += [str(Path(os.environ.get(env, '')) / 'Mozilla Firefox' / 'firefox.exe')
                       for env in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA')]
    for candidate in candidates:
        path = Path(candidate)
        if path.is_absolute() and path.name.casefold() == 'firefox.exe' and path.is_file():
            return path
    raise RuntimeError('Firefox не найден. Установите Firefox или укажите путь к firefox.exe в настройках.')


def spoken_number(text):
    if text.isdigit():
        return int(text)
    units = dict(zip('ноль один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать'.split(), range(20)))
    tens = dict(zip('двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто'.split(), range(20, 100, 10)))
    hundreds = dict(zip('сто двести триста четыреста пятьсот шестьсот семьсот восемьсот девятьсот'.split(), range(100, 1000, 100)))
    words = text.split()
    if text == 'тысяча':
        return 1000
    if words and words[0] in hundreds:
        if len(words) == 1:
            return hundreds[words[0]]
        rest = spoken_number(' '.join(words[1:]))
        return hundreds[words[0]] + rest if rest is not None and 0 < rest < 100 else None
    if text in units or text in tens:
        return {**units, **tens}[text]
    if len(words) == 2 and words[0] in tens and 0 < units.get(words[1], 0) < 10:
        return tens[words[0]] + units[words[1]]
    return None


def parse_action(text):
    raw = text.strip().rstrip('.!?')
    normalized = ' '.join(raw.casefold().replace('ё', 'е').split())
    fixed = {'открой блокнот': ('app', 'блокнот'), 'запусти блокнот': ('app', 'блокнот'),
             'открой калькулятор': ('app', 'калькулятор'), 'открой проводник': ('app', 'проводник'),
             'открой настройки windows': ('settings', 'general'), 'открой настройки виндоус': ('settings', 'general'),
             'открой настройки звука': ('settings', 'sound'), 'покажи рабочий стол': ('desktop', None),
             'сверни все окна': ('desktop', None), 'сделай громче': ('audio', ('up', None)),
             'заблокируй экран': ('lock', None), 'что ты умеешь': ('help', None),
             'сделай тише': ('audio', ('down', None)), 'выключи звук': ('audio', ('mute', True)),
             'включи звук': ('audio', ('mute', False)), 'какая сейчас громкость': ('audio', ('status', None)),
             'что ты помнишь': ('recall', None), 'покажи память': ('recall', None),
             'очисти всю память': ('clear_memory', None), 'открой firefox': ('firefox', '')}
    fixed.update({'открой браузер': ('firefox', ''), 'открой файрфокс': ('firefox', ''),
                  'открой фаерфокс': ('firefox', '')})
    if normalized in fixed:
        return fixed[normalized]
    match = re.fullmatch(r'(?:установи|сделай) громкость(?: на)? (.+?)(?: процентов| процента| процент)?', normalized)
    if match and (number := spoken_number(match[1])) is not None:
        return 'audio', ('set', number)
    match = re.fullmatch(r'запомни\s*[: ,]\s*(.+)', raw, re.I)
    if match:
        return 'remember', match[1]
    match = re.fullmatch(r'забудь (?:факт |номер )?(.+)', normalized)
    if match and (number := spoken_number(match[1])) is not None:
        return 'forget', number
    match = re.fullmatch(r'(?:найди|поищи) на сайте\s+(\S+)\s+(.+)', raw, re.I)
    if match:
        aliases = {'википедия': 'ru.wikipedia.org', 'гитхаб': 'github.com',
                   'ютуб': 'youtube.com', 'мозилла': 'mozilla.org'}
        return 'search', (match[2], aliases.get(match[1].casefold(), match[1]))
    match = re.fullmatch(r'(?:найди в интернете|поищи в интернете|найди в (?:firefox|файрфокс|фаерфокс)|поиск в firefox)\s+(.+)', raw, re.I)
    if match:
        return 'search', (match[1], '')
    return None


class AssistantActions:
    def __init__(self, settings, *, backend=None, memory=None, context_cache=None):
        self.settings = settings
        self.backend = backend or WindowsActions()
        self.memory = memory or FactMemory(Path(settings.data_directory) / 'memory.sqlite3')
        self.context_cache = context_cache if context_cache is not None else {}
        self.pending_name = None
        self.memory_status = 'done'

    def context(self):
        if not self.settings.memory_enabled:
            return ''
        if isinstance(self.memory, FactMemory):
            return self.memory.context()  # Store cache also observes successful edits from Settings.
        key = self.settings.data_directory
        if key not in self.context_cache:
            self.context_cache[key] = self.memory.context()
        return self.context_cache[key]

    @staticmethod
    def needs_context(text):
        n = ' '.join(text.casefold().strip().rstrip('.!?').split())
        if n in ('привет', 'здравствуйте', 'добрый день', 'доброе утро', 'добрый вечер', 'как дела', 'спасибо'):
            return False
        return re.fullmatch(r'сколько будет [\d\s+*/=.,-]+|сколько будет (?:два плюс два|один плюс один)', n) is None

    def accepts_memory_input(self, text):
        n = ' '.join(text.casefold().strip().rstrip('.!?').replace('ё', 'е').split())
        return (self.pending_name is not None or n in ('как меня зовут', 'какое мое имя', 'какое имя сохранено')
                or bool(re.match(r'^(?:исправь|измени|поменяй) (?:мое |сохраненное |имя владельца |имя )', n))
                or (n.startswith('я ') and bool(owner_name_from(text, allow_bare=True)) and not unsuitable(text)))

    def _name_proposal(self, name, cancel, turn_id):
        row = self.memory.owner_record()
        if cancel.is_set():
            raise asyncio.CancelledError
        if row and row['value'].casefold() != name.casefold():
            self.memory_status = 'clarification'
            self.pending_name = {'name': name, 'id': row['id'], 'revision': row['revision'],
                                 'turn_id': turn_id, 'expires': monotonic() + 30}
            return f'Сейчас сохранено имя {row["value"]}. Заменить его на {name}?'
        identity = self.memory.remember('Меня зовут ' + name, cancel)
        return f'Запомнила. Номер факта {identity}.'

    def handle(self, text, cancel, *, turn_id=None):
        self.memory_status = 'done'
        if cancel.is_set():
            raise asyncio.CancelledError
        turn_id = str(turn_id) if turn_id is not None else uuid4().hex
        n = ' '.join(text.casefold().strip().rstrip('.!?').replace('ё', 'е').split())
        if self.pending_name is not None:
            saved = self.pending_name
            if not self.settings.memory_enabled:
                self.pending_name = None
                self.memory_status = 'permission'
                return 'Постоянная память отключена в настройках.'
            if monotonic() > saved['expires'] or n in ('нет', 'отмена', 'стоп'):
                self.pending_name = None
                self.memory_status = 'cancelled'
                return 'Имя владельца не изменено.'
            if n in ('да', 'да подтверждаю', 'подтверждаю'):
                if turn_id == saved['turn_id']:
                    self.memory_status = 'clarification'
                    return 'Подтвердите изменение отдельной репликой.'
                self.pending_name = None
                try:
                    self.memory.edit(saved['id'], saved['name'], expected_revision=saved['revision'], source='user_confirmation', cancel=cancel)
                except ValueError as error:
                    self.memory_status = 'error'
                    return str(error)
                return 'Имя владельца изменено на ' + saved['name'] + '.'
            self.pending_name = None  # An unrelated new utterance invalidates the proposal.
        if n in ('как меня зовут', 'какое мое имя', 'какое имя сохранено'):
            if not self.settings.memory_enabled:
                self.memory_status = 'permission'
                return 'Постоянная память отключена в настройках.'
            name = self.memory.owner_name()
            if not name:
                self.memory_status = 'clarification'
            return f'В профиле владельца сохранено имя {name}.' if name else 'Имя владельца ещё не сохранено. Как вас зовут?'
        correction = re.fullmatch(r'(?:исправь|измени|поменяй) (?:мое имя|имя владельца|сохраненное имя|имя)(?: на)?\s+(.+)', n)
        direct_name = owner_name_from(text, allow_bare=True) if n.startswith('я ') and not unsuitable(text) else ''
        if (correction or direct_name) and self.settings.memory_enabled:
            name = owner_name_from('Меня зовут ' + correction[1]) if correction else direct_name
            if name:
                # A bare introduction is not permission to create an owner profile.
                if direct_name and not self.memory.owner_name():
                    return None
                return self._name_proposal(name, cancel, turn_id)
        request = parse_action(text)
        if request is None:
            return None
        action, args = request
        if action == 'help':
            return ('Я умею отвечать на вопросы и озвучивать ответы. Команды: открой блокнот, калькулятор или проводник; '
                    'покажи рабочий стол; заблокируй экран; сделай громче или тише; какая сейчас громкость. '
                    'Память: запомни факт, что ты помнишь, забудь факт один, очисти всю память. '
                    'Поиск: найди в интернете запрос; найди на сайте википедия запрос. Поиск открывается в Firefox.')
        if action in ('remember', 'recall', 'forget', 'clear_memory'):
            self.context_cache.pop(self.settings.data_directory, None)
            if not self.settings.memory_enabled:
                self.memory_status = 'permission'
                return 'Постоянная память отключена в настройках.'
            if action == 'remember':
                if unsuitable(args):
                    self.memory_status = 'error'
                    return 'Примеры, цитаты, приветствия и проверки памяти не сохраняются как личные факты.'
                name = owner_name_from(args)
                if name:
                    return self._name_proposal(name, cancel, turn_id)
                identity = self.memory.remember(args, cancel)
                return f'Запомнила. Номер факта {identity}.'
            if action == 'recall':
                rows = self.memory.list(20)
                return '\n'.join(f'{identity}. {fact}' for identity, fact in rows) or 'Память пока пуста.'
            if action == 'forget':
                return 'Факт удалён.' if self.memory.forget(args, cancel) else 'Факт с таким номером не найден.'
            self.memory.clear(cancel)
            return 'Постоянная память очищена.'
        if not self.settings.desktop_commands:
            return 'Команды Windows и браузера отключены в настройках.'
        if action == 'app':
            return self.backend.open_app(args, cancel)
        if action == 'settings':
            return self.backend.settings(args, cancel)
        if action == 'desktop':
            return self.backend.desktop(cancel)
        if action == 'lock':
            return self.backend.lock(cancel)
        if action == 'audio':
            mode, value = args
            if mode == 'set' and not 0 <= value <= 100:
                raise ValueError('Громкость должна быть от 0 до 100 процентов.')
            return self.backend.audio(mode, value, cancel)
        if action in ('search', 'firefox'):
            url = search_url(*args) if action == 'search' else 'about:home'
            executable = find_firefox(self.settings.firefox_path)
            if cancel.is_set():
                raise asyncio.CancelledError
            subprocess.Popen([str(executable), '-new-tab', url], shell=False, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 'Открываю результаты поиска в Firefox.' if action == 'search' else 'Открываю Firefox.'

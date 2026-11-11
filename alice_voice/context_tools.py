"""Registered context tools and explicit user-only power confirmation states."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import os
import re
from threading import RLock, Event
from time import monotonic, time

from .assistant_actions import AssistantActions, parse_action, spoken_number, find_firefox, search_url
from .lmstudio import validate_arguments
from .memory import check_cancel


def normalize(text):
    return ' '.join(re.sub(r'[^\w\s]', ' ', text.casefold().replace('ё', 'е')).split())


@dataclass
class ToolResult:
    status: str
    message: str
    data: dict = field(default_factory=dict)
    verified: bool = False


class PowerConfirmation:
    def __init__(self, clock=monotonic):
        self.clock, self.lock = clock, RLock()
        self.action = None
        self.stage = 0
        self.deadline = 0.
        self.used = set()

    @property
    def pending(self):
        with self.lock:
            return self.action is not None and self.clock() <= self.deadline

    @property
    def summary(self):
        with self.lock:
            if not self.pending:
                return ''
            title = 'выключение' if self.action == 'shutdown' else 'перезагрузку'
            return (f'Питание: {title}. Подтверждение {self.stage} из 2. ' +
                    ('Ответьте «Да» или «Нет».' if self.stage == 1 else f'Скажите «Подтверждаю {title}».'))

    def cancel(self):
        with self.lock:
            had = self.action is not None
            self.action, self.stage, self.deadline = None, 0, 0.
            self.used.clear()
            return ToolResult('cancelled', 'Выключение или перезагрузка отменены.' if had else 'Действия остановлены.')

    def expire(self):
        with self.lock:
            if self.action is not None and self.clock() > self.deadline:
                self.cancel()
                return ToolResult('cancelled', 'Подтверждение питания сброшено: прошло 30 секунд без ответа.')
        return None

    def request(self, action, identity):
        if action not in ('shutdown', 'restart'):
            return ToolResult('error', 'Неизвестное действие питания.')
        with self.lock:
            self.action, self.stage, self.deadline = action, 1, self.clock() + 30
            self.used = {str(identity)}
            title = 'Выключить' if action == 'shutdown' else 'Перезагрузить'
            return ToolResult('clarification', f'{title} компьютер? Несохранённые изменения могут потеряться.',
                              {'power_pending': True, 'confirmation': 1, 'action': action})

    def advance(self, text, identity):
        with self.lock:
            expired = self.expire()
            if expired:
                return expired
            if self.action is None:
                return ToolResult('cancelled', 'Нет ожидающего действия питания.')
            text = normalize(text)
            if text in ('нет', 'отмена', 'стоп'):
                return self.cancel()
            if str(identity) in self.used:
                return ToolResult('clarification', 'Нужно отдельное подтверждение новой репликой.')
            self.used.add(str(identity))
            if self.stage == 1 and text in ('да', 'да подтверждаю', 'да я подтверждаю'):
                self.stage, self.deadline = 2, self.clock() + 30
                phrase = 'Подтверждаю выключение' if self.action == 'shutdown' else 'Подтверждаю перезагрузку'
                return ToolResult('clarification', f'Последнее подтверждение: скажите «{phrase}».',
                                  {'power_pending': True, 'confirmation': 2, 'action': self.action})
            expected = 'подтверждаю выключение' if self.action == 'shutdown' else 'подтверждаю перезагрузку'
            if self.stage == 2 and text == expected:
                action = self.action
                self.cancel()
                return ToolResult('done', 'Оба подтверждения получены.', {'execute': action})
            return ToolResult('clarification', self.summary, {'power_pending': True, 'confirmation': self.stage})


def schema(properties, required=None):
    return {'type': 'object', 'properties': properties, 'required': list(properties) if required is None else required,
            'additionalProperties': False}


def string(choices=None, maximum=200):
    return {'type': 'string', 'maxLength': maximum, **({'enum': choices} if choices else {})}


NUMBER = {'type': 'integer', 'minimum': 0, 'maximum': 100}
TOOLS = {
    'launch_app': ('permission_windows', 'Запустить установленное приложение или игру по имени', schema({'name': string()})),
    'window_control': ('permission_window_control', 'Выбрать окно и переключить, свернуть, развернуть или закрыть штатно',
        schema({'mode': string(['activate', 'minimize', 'maximize', 'restore', 'close']), 'name': string(),
                'window_id': {'type': 'integer', 'minimum': 1}}, ['mode'])),
    'windows_settings': ('permission_windows', 'Открыть конкретный раздел настроек Windows',
        schema({'page': string(['general', 'sound', 'display', 'network', 'bluetooth', 'apps', 'privacy', 'updates'])})),
    'volume': ('permission_windows', 'Системная громкость Windows, не громкость музыки внутри игры',
        schema({'mode': string(['set', 'up', 'down', 'mute', 'unmute', 'status']), 'value': NUMBER}, ['mode'])),
    'brightness': ('permission_windows', 'Яркость поддерживаемого дисплея в процентах', schema({'value': NUMBER})),
    'media': ('permission_windows', 'Системная медиакоманда', schema({'command': string(['play_pause', 'next', 'previous', 'stop'])})),
    'user_folder': ('permission_windows', 'Открыть стандартную пользовательскую папку', schema({'name': string(['downloads', 'documents', 'pictures'])})),
    'screenshot': ('permission_screen', 'Снимок экрана или активного окна в буфер обмена', schema({'target': string(['screen', 'active'])})),
    'desktop': ('permission_window_control', 'Показать рабочий стол', schema({})),
    'lock': ('permission_windows', 'Заблокировать экран Windows', schema({})),
    'browser_search': ('permission_browser', 'Открыть поиск в Firefox, при необходимости по домену сайта',
        schema({'query': string(maximum=1500), 'domain': string()}, ['query'])),
    'browser_open': ('permission_browser', 'Открыть публичный сайт или домашнюю страницу в Firefox', schema({'url': string(maximum=1500)}, [])),
    'game_menu': ('permission_games', 'Наблюдать меню игры, открыть настройки/раздел, проверить параметр, изменить поддерживаемый параметр или вернуться',
        schema({'goal': string(['open_settings', 'inspect_parameter', 'change_parameter', 'back']),
                'category': string(['general', 'graphics', 'controls', 'mouse_sensitivity', 'music_volume', 'audio', 'resolution', 'display_mode']),
                'value': NUMBER, 'direction': string(['set', 'up', 'down'])}, ['goal', 'category'])),
    'power_request': ('permission_power', 'Начать двухэтапное подтверждение выключения/перезагрузки; никогда не выполнять напрямую',
        schema({'action': string(['shutdown', 'restart'])})),
}
PERMISSION_NAMES = {'permission_windows': 'Команды Windows', 'permission_window_control': 'Управление окнами',
                    'permission_browser': 'Браузер и поиск', 'permission_screen': 'Анализ экрана',
                    'permission_input': 'Управление мышью и клавиатурой', 'permission_games': 'Работа с меню игр',
                    'permission_power': 'Выключение и перезагрузка'}


class ToolRouter:
    def __init__(self, settings, provider=None, *, desktop=None, notify=None, conversation=None):
        if desktop is None:
            from .desktop_backend import DesktopBackend
            desktop = DesktopBackend(settings)
        self.settings, self.desktop, self.provider, self.notify = settings, desktop, provider, notify
        from .conversation import ConversationContext
        self.conversation = conversation if conversation is not None else ConversationContext(settings.history_turns)
        self.task = self.conversation.task
        self.legacy = AssistantActions(settings, context_cache=self.conversation.preferences)
        self.power = PowerConfirmation()
        self.last_context = {}
        self._context_lock = RLock()
        self._background_hwnd = None
        self._background_context_at = None
        self.journal = deque(maxlen=40)
        self.resolver = None
        self.active_cancel = None

    @property
    def pending_choice(self):
        return self.conversation.selection

    @pending_choice.setter
    def pending_choice(self, value):
        self.conversation.selection = value

    def publish(self, result, tool='', *, active=False, ack=None):
        if self.active_cancel is not None and self.active_cancel.is_set() and result.status != 'cancelled':
            return result  # Late native callbacks must not change the new UI/task.
        event = {'status': result.status, 'tool': tool, 'message': result.message, 'time': time(),
                 'turn_id': self.task.turn_id,
                 'power_pending': self.power.pending, 'summary': self.power.summary,
                 'active': active, 'verified': result.verified}
        self.journal.append({k: v for k, v in event.items()})
        if ack is not None:
            event['ack'] = ack
        if self.notify:
            self.notify(event)
        return result

    def stop(self):
        if self.active_cancel is not None:
            self.active_cancel.set()
        from .game_control import release_all_inputs
        try:
            release_all_inputs()
        except Exception:
            pass  # Cancellation must still invalidate queued actions if Windows refuses release.
        self.pending_choice = None
        self.legacy.pending_name = None
        self.task.pending_clarification = ''
        return self.publish(self.power.cancel(), 'stop')

    def poll(self):
        result = self.power.expire()
        if result:
            self.publish(result, 'power_request')

    def observe_context(self):
        """Always read fresh focus; command execution must not use polling caches."""
        with self._context_lock:
            try:
                current = self.desktop.active_window()
                if current and current.get('hwnd') and current.get('pid') != os.getpid():
                    self.last_context = {k: current.get(k) for k in ('hwnd', 'title', 'exe', 'pid', 'rect')}
                    return dict(self.last_context)
            except Exception:
                pass
            return {}  # Cached focus is never evidence of the current active window.

    def poll_context(self):
        """Refresh background metadata on focus changes or every five seconds."""
        with self._context_lock:
            now = monotonic()
            try:
                hwnd = self.desktop.foreground_window_id()
            except Exception:
                hwnd = self._background_hwnd  # Retry full observation at the usual interval.
            if (self._background_context_at is None or hwnd != self._background_hwnd
                    or now - self._background_context_at >= 5.):
                self._background_hwnd = hwnd
                self._background_context_at = now
                self.observe_context()

    @staticmethod
    def game_context(context):
        text = (str(context.get('exe', '')) + ' ' + str(context.get('title', ''))).casefold()
        return any(word in text for word in ('minecraft', 'roblox', 'dota', 'cs2', 'counter-strike', 'game',
                                              'geoguessr', 'terraria', 'stardew', 'genshin', 'fortnite', 'valorant', 'unityplayer'))

    def fast_intent(self, text):
        raw, n = text.strip(), normalize(text)
        if n.startswith(('как ', 'почему ', 'что такое ', 'зачем ', 'какие ', 'что будет если ')):
            return None
        old = parse_action(raw)
        if old:
            name, args = old
            mapping = {'app': ('launch_app', {'name': args}), 'settings': ('windows_settings', {'page': args}),
                       'desktop': ('desktop', {}), 'lock': ('lock', {}),
                       'firefox': ('browser_open', {})}
            if name in mapping:
                return mapping[name]
            if name == 'audio':
                mode, value = args
                if mode == 'mute':
                    return 'volume', {'mode': 'mute' if value else 'unmute'}
                return 'volume', {'mode': mode, **({'value': value if value is not None else 10} if mode != 'status' else {})}
            if name == 'search':
                return 'browser_search', {'query': args[0], 'domain': args[1]}
        aliases = {'сделай потише': ('volume', {'mode': 'down', 'value': 10}),
                   'убавь звук': ('volume', {'mode': 'down', 'value': 10}),
                   'слишком громко': ('volume', {'mode': 'down', 'value': 10}),
                   'прибавь звук': ('volume', {'mode': 'up', 'value': 10}),
                   'следующий трек': ('media', {'command': 'next'}), 'предыдущий трек': ('media', {'command': 'previous'}),
                   'пауза музыки': ('media', {'command': 'play_pause'}), 'продолжи музыку': ('media', {'command': 'play_pause'}),
                   'сделай скриншот': ('screenshot', {'target': 'screen'}),
                   'сделай скриншот активного окна': ('screenshot', {'target': 'active'}),
                   'вернись назад': ('game_menu', {'goal': 'back', 'category': 'general'})}
        aliases.update({'сделай звук потише': ('volume', {'mode': 'down', 'value': 10}),
                        'сделай звук погромче': ('volume', {'mode': 'up', 'value': 10})})
        if n in aliases:
            return aliases[n]
        # Named sites must be resolved before the generic installed-app rule.
        # normalize() also removes sentence punctuation supplied by STT.
        opening = re.fullmatch(
            r'(?:пожалуйста )?(?:открой|открыть|запусти|перейди на|зайди на) '
            r'(?:пожалуйста )?(?:сайт )?(.+?)(?: пожалуйста)?', n)
        if opening:
            target = opening[1]
            if target in ('youtube', 'ютуб', 'ютьюб', 'ю туб'):
                return 'browser_open', {'url': 'https://www.youtube.com/'}
            if target in ('браузер', 'мой браузер', 'интернет браузер'):
                return 'browser_open', {}
        match = re.fullmatch(r'(?:открой(?: сайт)?|перейди на)\s+((?:https?://)?\S+\.\S+)', raw, re.I)
        if match:
            return 'browser_open', {'url': match[1]}
        match = re.fullmatch(r'(?:сделай|установи|измени) яркость(?: экрана)?(?: на)? (.+?)(?: процентов| процента| процент)?', n)
        if match and (value := spoken_number(match[1])) is not None:
            return 'brightness', {'value': value}
        for expression, tool, key in ((r'(?:мне нужен|мне нужна|запусти|открой) (.+)', 'launch_app', 'name'),
                                      (r'(?:переключись на|переключи на|сверни|разверни|восстанови|закрой) (.+)', 'window_control', 'name')):
            match = re.fullmatch(expression, n)
            if match and not any(w in n.split() for w in ('не', 'как', 'если', 'почему')):
                value = match[1]
                if tool == 'launch_app' and any(word in value for word in ('настрой', 'график', 'управлен', 'сайт', 'папк', 'загрузк', 'документ', 'изображен')):
                    break
                mode = next((v for k, v in (('сверни', 'minimize'), ('разверни', 'maximize'), ('восстанови', 'restore'), ('закрой', 'close')) if n.startswith(k)), 'activate')
                aliases = {'стим': 'Steam', 'дискорд': 'Discord', 'спотифай': 'Spotify', 'майнкрафт': 'Minecraft'}
                return tool, {key: '' if value in ('это окно', 'активное окно', 'окно') else aliases.get(value, value), **({'mode': mode} if tool == 'window_control' else {})}
        if re.fullmatch(r'(?:выключи|перезагрузи|выруби|перезапусти) (?:компьютер|ноутбук|пк)', n):
            return 'power_request', {'action': 'restart' if n.startswith(('перезагрузи', 'перезапусти')) else 'shutdown'}
        if 'настрой' in n or 'график' in n or 'чувствительность' in n or ('громкость' in n and ('игр' in n or 'музык' in n)):
            category = next((v for k, v in (('график', 'graphics'), ('управлен', 'controls'), ('чувствительность', 'mouse_sensitivity'),
                                           ('музык', 'music_volume'), ('разрешени', 'resolution'), ('режим экрана', 'display_mode'), ('звук', 'audio')) if k in n), 'general')
            game = ('игр' in n or category != 'general' or self.game_context(self.last_context)) and not ('windows' in n or 'виндоус' in n)
            if game:
                goal = 'inspect_parameter' if category in ('mouse_sensitivity', 'music_volume', 'resolution', 'display_mode') else 'open_settings'
                if any(w in n for w in ('уменьш', 'увелич', 'измени', 'установи')):
                    goal = 'change_parameter'
                direction = 'down' if 'уменьш' in n else 'up' if 'увелич' in n else 'set'
                amount = re.search(r'\b(?:на|до) (.+?)(?: процентов| процента| процент)?$', n)
                value = spoken_number(amount[1]) if amount else None
                return 'game_menu', {'goal': goal, 'category': category,
                                     **({'direction': direction} if goal == 'change_parameter' else {}),
                                     **({'value': value} if value is not None else {})}
            if 'windows' in n or 'виндоус' in n:
                return 'windows_settings', {'page': 'general'}
            pages = {'звук': 'sound', 'экран': 'display', 'сети': 'network', 'сетев': 'network', 'bluetooth': 'bluetooth',
                     'блютуз': 'bluetooth', 'приложени': 'apps', 'конфиденциаль': 'privacy', 'обновлен': 'updates'}
            for word, page in pages.items():
                if word in n:
                    return 'windows_settings', {'page': page}
            return ToolResult('clarification', 'Настройки игры или Windows?', {'choice_kind': 'settings'})
        for word, folder in (('загрузки', 'downloads'), ('документы', 'documents'), ('изображения', 'pictures')):
            if n in ('открой ' + word, 'открой папку ' + word):
                return 'user_folder', {'name': folder}
        return None

    @staticmethod
    def likely_action(text):
        n = normalize(text)
        return bool(re.search(r'\b(открой|запусти|переключи|переключись|сверни|разверни|закрой|убавь|прибавь|найди|поищи|измени|поменяй|уменьши|увеличь|снизь|повысь|включи|зайди|поставь|сделай|установи|покажи|вернись|выключи|перезагрузи|мне нужен|слишком громко)\b', n))

    async def handle(self, text, cancel, *, turn_id='', history=()):
        self.task.begin(text, turn_id)
        self._turn_tool, self._turn_arguments = 'intent', {}
        try:
            result = await self._handle(text, cancel, turn_id=turn_id, history=history)
            check_cancel(cancel)
        except asyncio.CancelledError:
            cancel.set()
            if self.legacy.pending_name and self.legacy.pending_name['turn_id'] == str(turn_id):
                self.legacy.pending_name = None
            raise
        if result is not None:
            self.task.record(self._turn_tool, self._turn_arguments, result, turn_id)
        return result

    async def _models(self, cancel):
        check_cancel(cancel)
        if self.settings.provider != 'ollama' or self.provider is None:
            raise ValueError('Список моделей сейчас доступен для Ollama. Выберите Ollama в настройках.')
        response = await self.provider.client.get(self.settings.ollama_url.rstrip('/') + '/api/tags', timeout=5)
        response.raise_for_status()
        check_cancel(cancel)
        rows = response.json()['models']
        if not isinstance(rows, list):
            raise ValueError('Неверный список моделей Ollama.')
        names = []
        for row in rows[:100]:
            name = row.get('name') if isinstance(row, dict) else None
            if isinstance(name, str) and name and len(name) <= 512 and not any(ord(c) < 32 for c in name):
                names.append(name)
        return list(dict.fromkeys(names))

    async def _handle(self, text, cancel, *, turn_id='', history=()):
        check_cancel(cancel)
        self.active_cancel = cancel
        self.poll()
        if self.power.pending:
            result = self.power.advance(text, str(turn_id))
            if result.data.get('execute'):
                return await self._execute_power(result.data['execute'], cancel)
            return self.publish(result, 'power_request')
        if normalize(text) in ('стоп', 'отмена') or (normalize(text)=='нет' and self.pending_choice):
            return self.stop()
        if re.search(r'\bне (?:открывай|закрывай|запускай|уменьшай|увеличивай|меняй|изменяй|выключай|перезагружай|нажимай|сворачивай|делай)\b', normalize(text)):
            return self.publish(ToolResult('cancelled', 'Действие не выполняю.'), 'intent')
        context = await asyncio.to_thread(self.observe_context)
        check_cancel(cancel)
        if self.legacy.accepts_memory_input(text):
            answer = await asyncio.to_thread(self.legacy.handle, text, cancel, turn_id=turn_id)
            check_cancel(cancel)
            if answer is not None:
                return self.publish(ToolResult(self.legacy.memory_status, answer,
                                               verified=self.legacy.memory_status == 'done'), 'owner_memory')
        n = normalize(text)
        from .conversation import choice_number
        if n in ('покажи доступные модели', 'покажи модели', 'список моделей', 'какие модели доступны'):
            self._turn_tool = 'list_models'
            try:
                models = await self._models(cancel)
            except Exception:
                return self.publish(ToolResult('error', 'Не удалось получить модели Ollama. Проверьте подключение.'), 'list_models')
            choices = [{'name': name, 'id': name} for name in models]
            self.pending_choice = {'choices': choices, 'tool': 'select_model', 'arguments': {}}
            return self.publish(ToolResult('clarification',
                ('Доступные модели: ' + '; '.join(f'{i}: {name}' for i, name in enumerate(models, 1))
                 + '. Назовите номер или имя модели.') if models else 'В Ollama нет установленных моделей.',
                {'choices': choices}), 'list_models')
        if re.fullmatch(r'нет (?:другой|другая|другое|этот не тот|не этот)', n):
            return self.publish(ToolResult('clarification', 'Какой документ или приложение выбрать вместо предыдущего? Назовите имя или путь.'), 'intent')
        browser_followup = re.search(r'\bнайди там\s+(.+)$', n)
        if browser_followup:
            if (self.task.selected_application != 'Firefox' or
                    'firefox' not in str(context.get('exe', '')).casefold()):
                return self.publish(ToolResult('clarification', 'Сейчас активно другое окно. Найти это в Firefox или в другом приложении?'), 'intent')
            return await self.execute('browser_search', {'query': browser_followup[1]}, cancel,
                                      turn_id=turn_id, user_text=text, context=context)
        old = parse_action(text)
        if old and old[0] in ('remember', 'recall', 'forget', 'clear_memory', 'help'):
            answer = await asyncio.to_thread(self.legacy.handle, text, cancel, turn_id=turn_id)
            check_cancel(cancel)
            if self.legacy.pending_name:
                return self.publish(ToolResult('clarification', answer), 'owner_memory')
            return self.publish(ToolResult(self.legacy.memory_status, answer,
                                           verified=self.legacy.memory_status == 'done'), old[0])
        if self.pending_choice:
            saved = self.pending_choice
            n = normalize(text)
            if saved.get('choice_kind') == 'settings' and n in ('игры', 'игра', 'настройки игры', 'windows', 'виндоус', 'настройки windows'):
                name, args = (('game_menu', {'goal': 'open_settings', 'category': 'general'}) if 'игр' in n else ('windows_settings', {'page': 'general'}))
                return await self.execute(name, args, cancel, turn_id=turn_id, user_text=text, context=context)
            if saved.get('choices'):
                choices = saved['choices']
                selected = [row for row in choices if n in {normalize(str(row.get(k, ''))) for k in ('name', 'title', 'id')}]
                index = choice_number(n)
                if index is not None and 1 <= index <= len(choices):
                    selected = [choices[index - 1]]
                if len(selected) == 1:
                    row = selected[0]
                    self.pending_choice = None
                    if saved['tool'] == 'select_model':
                        model = str(row.get('id') or row.get('name'))
                        self._turn_tool, self._turn_arguments = 'select_model', {'model': model}
                        try:
                            installed = await self._models(cancel)
                        except Exception:
                            return self.publish(ToolResult('error', 'Не удалось перепроверить наличие модели Ollama.'), 'select_model')
                        if model not in installed:
                            return self.publish(ToolResult('clarification', 'Эта модель уже недоступна. Попросите список моделей заново.'), 'select_model')
                        if model == self.settings.ollama_model:
                            return self.publish(ToolResult('done', 'Эта модель уже выбрана: ' + model + '.', verified=True), 'select_model')
                        return self.publish(ToolResult('pending', 'Переключаю модель на ' + model + '.',
                                                      {'selected_model': model}), 'select_model')
                    args = dict(saved['arguments'], name=str(row.get('id') or row.get('title') or row.get('name')))
                    if saved['tool'] == 'window_control' and row.get('hwnd'):
                        args = dict(saved['arguments'], window_id=int(row['hwnd']))
                        args.pop('name', None)
                    return await self.execute(saved['tool'], args, cancel, turn_id=turn_id, user_text=text, context=context)
                if index is not None or n.startswith(('выбери', 'выбрать', 'вариант')):
                    return self.publish(ToolResult('clarification', 'Уточните номер или имя из показанного списка.'), 'intent')
            # A new substantive request supersedes an unrelated old choice.
            self.pending_choice = None
        if choice_number(n) is not None and n.startswith(('выбери', 'выбрать', 'втор', 'перв', 'трет')):
            return self.publish(ToolResult('clarification', 'Какой список вы имеете в виду? Попросите показать варианты.'), 'intent')
        if n in ('сделай это', 'открой это', 'выбери это', 'найди это'):
            return self.publish(ToolResult('clarification', 'Что именно вы имеете в виду? Назовите объект или вариант.'), 'intent')
        if n == 'открой браузер':
            return await self.execute('browser_open', {}, cancel, turn_id=turn_id, user_text=text, context=context)
        intent = self.fast_intent(text)
        if intent is None and self.settings.natural_tools and self.likely_action(text) and self.provider is not None:
            from .intent_model import IntentResolver
            resolver = self.resolver or IntentResolver(self.provider)
            try:
                intent = await resolver.resolve(text, dict(context, task=self.task.reference()), list(history)[-6:], TOOLS, cancel)
            except (ValueError, RuntimeError):
                return self.publish(ToolResult('clarification', 'Не удалось определить действие. Уточните приложение или нужный параметр.'), 'intent')
        if isinstance(intent, ToolResult):
            if intent.data.get('choice_kind'):
                self.pending_choice = intent.data
            return self.publish(intent, 'intent')
        if intent is None:
            return None
        name, args = intent
        return await self.execute(name, args, cancel, turn_id=turn_id, user_text=text, context=context)

    async def execute(self, name, args, cancel, *, turn_id='', user_text='', context=None):
        check_cancel(cancel)
        self._turn_tool, self._turn_arguments = name, dict(args)
        context = dict(self.last_context if context is None else context)
        if name not in TOOLS:
            return self.publish(ToolResult('error', 'Такой инструмент не зарегистрирован.'), str(name)[:80])
        permission, description, rules = TOOLS[name]
        try:
            validate_arguments(args, rules)
            if name == 'volume' and args['mode'] == 'set' and 'value' not in args:
                raise ValueError('value required')
        except (ValueError, KeyError, TypeError):
            return self.publish(ToolResult('error', 'Параметры действия не прошли проверку. Уточните имя, режим или значение.'), name)
        if not getattr(self.settings, permission):
            label = PERMISSION_NAMES[permission]
            return self.publish(ToolResult('permission', f'Возможность «{label}» выключена. Включите её в настройках.'), name)
        if name == 'power_request':
            n = normalize(user_text)
            if not re.search(r'(выключ|перезагру|перезапу|выруби)', n) or not re.search(r'(компьютер|ноутбук|\bпк\b)', n):
                return self.publish(ToolResult('clarification', 'Для питания нужна явная команда: «Выключи компьютер» или «Перезагрузи компьютер».'), name)
            return self.publish(self.power.request(args['action'], str(turn_id)), name)
        self.publish(ToolResult('running', description), name, active=name == 'game_menu')
        try:
            check_cancel(cancel)
            fresh = await asyncio.to_thread(self.observe_context)
            check_cancel(cancel)
            bound = ((name == 'window_control' and not args.get('name') and not args.get('window_id'))
                     or (name == 'screenshot' and args.get('target') == 'active') or name == 'game_menu')
            if bound and (not fresh.get('hwnd') or fresh.get('hwnd') != context.get('hwnd')):
                return self.publish(ToolResult('clarification', 'Активное окно изменилось или недоступно. Назовите нужное приложение.'), name)
            if name == 'browser_search' and 'там' in normalize(user_text).split() and fresh.get('hwnd') != context.get('hwnd'):
                return self.publish(ToolResult('clarification', 'Активное окно изменилось. В каком браузере искать?'), name)
            self.task.focus = fresh
            if name in ('browser_search', 'browser_open'):
                result = await asyncio.to_thread(self._browser, name, args, cancel)
            elif name == 'game_menu':
                from .game_control import GameController
                raw = await asyncio.to_thread(GameController(self.settings, self.desktop).run,
                    args['goal'], args['category'], args.get('value'), cancel,
                    int(context.get('hwnd') or 0),
                    lambda message: None if cancel.is_set() else self.publish(ToolResult('running', str(message)), name, active=True),
                    direction=args.get('direction', 'set'))
                result = ToolResult(**{k: raw[k] for k in ('status', 'message', 'data', 'verified') if k in raw})
            else:
                bound = (name == 'window_control' and not args.get('name') and not args.get('window_id')) or (name == 'screenshot' and args.get('target') == 'active')
                target = int(context.get('hwnd') or 0) if bound else 0
                if bound and not target:
                    return self.publish(ToolResult('clarification', 'Не определено окно. Назовите нужное приложение.'), name)
                raw = await asyncio.to_thread(self.desktop.execute, name, args, cancel, target)
                result = ToolResult(**{k: raw[k] for k in ('status', 'message', 'data', 'verified') if k in raw})
        except asyncio.CancelledError:
            cancel.set()
            self.publish(ToolResult('cancelled', 'Действие отменено.'), name)
            raise
        except (ValueError, RuntimeError) as error:
            result = ToolResult('error', str(error)[:500])
        except Exception:
            result = ToolResult('error', 'Действие недоступно. Проверьте окно, устройство и разрешения Windows.')
        check_cancel(cancel)  # Thread/native work can finish after cancellation.
        if result.status == 'clarification' and result.data.get('choices'):
            self.pending_choice = {'choices': result.data['choices'], 'tool': name, 'arguments': args}
            choices = result.data['choices'][:8]
            result.message += ' ' + '; '.join(f'{index + 1}: {row.get("name") or row.get("title") or row.get("id")}' for index, row in enumerate(choices)) + '. Назовите вариант или его номер.'
        return self.publish(result, name)

    def _browser(self, name, args, cancel):
        import subprocess
        from urllib.parse import urlsplit
        if name == 'browser_search':
            url = search_url(args['query'], args.get('domain', ''))
        else:
            value = args.get('url', '').strip()
            if value:
                url = value if '://' in value else 'https://' + value
                parsed = urlsplit(url)
                if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 33 for c in url):
                    raise ValueError('Укажите обычный HTTP/HTTPS-адрес сайта без пароля.')
            else:
                url = 'about:home'
        firefox = find_firefox(self.settings.firefox_path)
        check_cancel(cancel)
        subprocess.Popen([str(firefox), '-new-tab', url], shell=False, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ToolResult('done', 'Отправила команду открытия вкладки Firefox.', verified=False)

    async def _execute_power(self, action, cancel):
        check_cancel(cancel)
        if not self.settings.permission_power:
            return self.publish(ToolResult('permission', 'Разрешение питания отозвано. Действие отменено.'), 'power_request')
        ack = Event()
        self.publish(ToolResult('running', 'Выполняю ' + ('выключение' if action == 'shutdown' else 'перезагрузку') + ' компьютера.'),
                     'power_request', active=True, ack=ack)
        # GUI acknowledges only after its visible confirmation state was painted.
        deadline = monotonic() + 3
        while not ack.is_set():
            check_cancel(cancel)
            if monotonic() > deadline:
                return self.publish(ToolResult('cancelled', 'Не удалось показать финальное состояние в интерфейсе. Питание отменено.'), 'power_request')
            await asyncio.sleep(.02)
        check_cancel(cancel)
        if not self.settings.permission_power:
            return self.publish(ToolResult('permission', 'Разрешение питания отозвано. Действие отменено.'), 'power_request')
        raw = await asyncio.to_thread(self.desktop.execute, 'power', {'action': action}, cancel)
        return self.publish(ToolResult(**{k: raw[k] for k in ('status', 'message', 'data', 'verified') if k in raw}), 'power_request')

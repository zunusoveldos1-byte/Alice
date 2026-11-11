import asyncio
from threading import Event
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import Mock
from alice_voice.config import Settings
from alice_voice.context_tools import ToolRouter, PowerConfirmation, ToolResult


class PowerTests(TestCase):
    def test_two_distinct_confirmations_are_required(self):
        now=[0.];p=PowerConfirmation(clock=lambda:now[0])
        self.assertEqual(p.request('shutdown','one').status,'clarification')
        self.assertFalse(p.advance('подтверждаю выключение','two').data.get('execute'))
        self.assertFalse(p.advance('да','one').data.get('execute'))
        self.assertFalse(p.advance('да подтверждаю выключение','three').data.get('execute'))
        self.assertFalse(p.advance('да','four').data.get('execute'))
        self.assertFalse(p.advance('подтверждаю перезагрузку','five').data.get('execute'))
        self.assertTrue(p.advance('подтверждаю выключение','six').data.get('execute'))
        self.assertFalse(p.pending)

    def test_cancel_timeout_and_replay_do_not_execute(self):
        for cancellation in ('нет','отмена','стоп'):
            p=PowerConfirmation();p.request('restart','a');p.advance('да','b')
            self.assertEqual(p.advance(cancellation,'c').status,'cancelled');self.assertFalse(p.pending)
        now=[0.];p=PowerConfirmation(clock=lambda:now[0]);p.request('restart','a');now[0]=31
        self.assertEqual(p.advance('да','b').status,'cancelled');self.assertFalse(p.pending)
        p.request('restart','c');p.advance('да','d')
        self.assertFalse(p.advance('подтверждаю перезагрузку','d').data.get('execute'))


class RouterTests(IsolatedAsyncioTestCase):
    async def test_youtube_is_a_site_not_an_installed_application(self):
        from unittest.mock import patch
        for text in ('Открой YouTube.', 'Открой ютуб', 'Открыть Ютуб!',
                     'Открой сайт YouTube', 'Перейди на ютьюб', 'Открой, пожалуйста, YouTube.'):
            with self.subTest(text=text):
                router, backend = self.make()
                with patch.object(router, '_browser', return_value=ToolResult('done', 'Запрос открытия передан.')) as browser:
                    result = await router.handle(text, Event(), turn_id=text)
                self.assertEqual(result.status, 'done')
                browser.assert_called_once()
                self.assertEqual(browser.call_args.args[:2], ('browser_open', {'url': 'https://www.youtube.com/'}))
                backend.execute.assert_not_called()

    async def test_youtube_route_respects_permission_and_negation(self):
        from unittest.mock import patch
        for text, settings, status in (
                ('Открой YouTube.', Settings(permission_browser=False), 'permission'),
                ('Не открывай YouTube', Settings(), 'cancelled')):
            router, backend = self.make(settings)
            with patch.object(router, '_browser') as browser:
                result = await router.handle(text, Event(), turn_id=text)
            self.assertEqual(result.status, status)
            browser.assert_not_called(); backend.execute.assert_not_called()

    async def test_browser_with_infinitive_or_politeness_is_not_an_app(self):
        from unittest.mock import patch
        for text in ('Открыть браузер.', 'Открой, пожалуйста, браузер', 'Открой мой браузер'):
            router, backend = self.make()
            with patch.object(router, '_browser', return_value=ToolResult('done', 'Запрос открытия передан.')) as browser:
                await router.handle(text, Event(), turn_id=text)
            browser.assert_called_once()
            self.assertEqual(browser.call_args.args[:2], ('browser_open', {}))
            backend.execute.assert_not_called()

    def make(self,settings=None):
        backend=Mock();backend.active_window.return_value={'hwnd':22,'title':'Game','exe':'game.exe','pid':999}
        backend.list_windows.return_value=[]
        backend.execute.return_value={'status':'done','message':'Отправила команду запуска.','verified':False,'data':{}}
        return ToolRouter(settings or Settings(),desktop=backend),backend

    async def test_natural_phrases_have_same_intent(self):
        for text in ('Открой блокнот','Запусти блокнот','Мне нужен блокнот'):
            r,b=self.make();result=await r.handle(text,Event(),turn_id=text)
            self.assertEqual(result.status,'done');self.assertEqual(b.execute.call_args.args[:2],('launch_app',{'name':'блокнот'}))
        for text in ('Сделай потише','Убавь звук','Слишком громко'):
            r,b=self.make();await r.handle(text,Event(),turn_id=text)
            self.assertEqual(b.execute.call_args.args[:2],('volume',{'mode':'down','value':10}))

    async def test_unnamed_window_is_bound_to_observed_handle(self):
        r,b=self.make();await r.handle('Закрой это окно',Event(),turn_id='a')
        self.assertEqual(b.execute.call_args.args[3],22)

    async def test_negated_game_command_never_reaches_executor(self):
        r,b=self.make(Settings(permission_games=True,permission_input=True))
        result=await r.handle('Не уменьшай громкость музыки в игре',Event(),turn_id='a')
        self.assertEqual(result.status,'cancelled');b.execute.assert_not_called()

    async def test_game_target_and_relative_amount_are_preserved(self):
        r,b=self.make()
        self.assertEqual(r.fast_intent('Установи громкость музыки в игре на двадцать процентов')[1]['value'],20)
        args=r.fast_intent('Уменьши громкость музыки в игре на пять процентов')[1]
        self.assertEqual((args['value'],args['direction']),(5,'down'))

    async def test_numeric_window_choice_preserves_identity_not_duplicate_title(self):
        r,b=self.make()
        r.pending_choice={'tool':'window_control','arguments':{'mode':'close','name':'X'},
                          'choices':[{'hwnd':31,'title':'X'},{'hwnd':32,'title':'X'}]}
        await r.handle('два',Event(),turn_id='b')
        self.assertEqual(b.execute.call_args.args[1]['window_id'],32)

    async def test_permissions_and_strict_arguments_precede_execution(self):
        r,b=self.make(Settings(permission_window_control=False))
        result=await r.execute('window_control',{'mode':'close','name':'Game'},Event())
        self.assertEqual(result.status,'permission');b.execute.assert_not_called()
        r,b=self.make()
        for args in ({'mode':'set','value':True},{'mode':'set','value':101},{'mode':'set','value':20,'shell':'calc'}):
            result=await r.execute('volume',args,Event())
            self.assertEqual(result.status,'error')
        b.execute.assert_not_called()

    async def test_game_settings_does_not_open_windows_settings(self):
        r,b=self.make(Settings(permission_games=False))
        result=await r.handle('Открой настройки игры',Event(),turn_id='a')
        self.assertEqual(result.status,'permission');b.execute.assert_not_called()
        r.last_context={'hwnd':0,'title':'','exe':''}
        b.active_window.return_value={}
        result=await r.handle('Открой настройки',Event(),turn_id='b')
        self.assertEqual(result.status,'clarification')

    async def test_model_can_only_select_registered_tools_and_cannot_confirm_power(self):
        r,b=self.make(Settings(permission_power=True));r.resolver=Mock()
        result=await r.execute('shell',{'command':'del *'},Event())
        self.assertEqual(result.status,'error');b.execute.assert_not_called()
        result=await r.execute('power',{'action':'shutdown'},Event())
        self.assertEqual(result.status,'error');b.execute.assert_not_called()

    async def test_power_requires_gui_ack_and_cancel_blocks_final_effect(self):
        events=[]
        r,b=self.make(Settings(permission_power=True));r.notify=events.append
        await r.handle('Выключи компьютер',Event(),turn_id='1')
        await r.handle('Да',Event(),turn_id='2')
        cancel=Event();cancel.set()
        with self.assertRaises(asyncio.CancelledError):
            await r.handle('Подтверждаю выключение',cancel,turn_id='3')
        b.execute.assert_not_called()

    async def test_power_executes_once_only_after_two_turns_and_visible_gui_ack(self):
        r,b=self.make(Settings(permission_power=True))
        def notify(event):
            if 'ack' in event:
                event['ack'].set()
        r.notify=notify
        await r.handle('Перезагрузи компьютер',Event(),turn_id='one')
        b.execute.assert_not_called()
        await r.handle('Да я подтверждаю',Event(),turn_id='two')
        b.execute.assert_not_called()
        await r.handle('Подтверждаю перезагрузку',Event(),turn_id='three')
        b.execute.assert_called_once()
        self.assertEqual(b.execute.call_args.args[:2],('power',{'action':'restart'}))
        self.assertFalse(r.power.pending)

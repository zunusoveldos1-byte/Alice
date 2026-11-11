"""Native Qt regression checks for shared-session shell surfaces."""
import unittest
import test_main_window


class ShellRepairTests(unittest.TestCase):
    run_ui = test_main_window.MainWindowTests.run_ui

    def test_embedded_settings_and_reusable_independent_mini(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PyQt6.QtCore import Qt, QThread, QPoint
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app=QApplication([])
            with TemporaryDirectory() as directory, patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'):
                c=VoiceSessionController(Settings())
                w=MainWindow(c,Path(directory)/'settings.json'); w.show()
                runtime=c.runtime; history=c.completed_turns
                w.nav_buttons['mini'].click(); QTest.qWait(80)
                mini=w.mini
                assert mini.isVisible() and mini.controller is c
                assert mini.thread()==app.thread()
                assert mini.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
                assert mini.parentWidget() is None
                start=mini.pos()
                QTest.mousePress(mini.drag_handle,Qt.MouseButton.LeftButton,pos=QPoint(12,12))
                QTest.mouseMove(mini.drag_handle,QPoint(52,32))
                QTest.mouseRelease(mini.drag_handle,Qt.MouseButton.LeftButton,pos=QPoint(52,32))
                assert mini.pos()!=start
                w._open_mini(); assert w.mini is mini
                mini.return_button.click(); QTest.qWait(40)
                assert w.isVisible() and not mini.isVisible()
                w._open_mini(); mini.close(); QTest.qWait(40)
                assert not c.closing and w.isVisible()
                with patch.object(c,'reconfigure') as reconfigure:
                    w.settings_btn.click(); QTest.qWait(40)
                    assert w.current_page=='settings'
                    p=w.settings_page
                    assert not p.isWindow() and w.pages.currentWidget() is p
                    assert p.categories.count()==8
                    assert any(p.categories.item(i).text()=='Ярмарка' for i in range(p.categories.count()))
                    for i in range(p.categories.count()):
                        p.categories.setCurrentRow(i); QTest.qWait(10)
                        assert p.sections.currentIndex()==i
                    assert p.values()==w.settings_store.desired
                    w.select_page('voice'); w._show_settings()
                    assert w.settings_page is p
                    reconfigure.assert_not_called()
                assert c.runtime is runtime and c.completed_turns is history
                p.inputs['history_turns'].setValue(9)
                p.save_button.click()
                for _ in range(100):
                    QTest.qWait(20)
                    if not w.settings_store.busy: break
                assert Settings.load(w.settings_path).history_turns==9
                assert len(w.clock.text())==5 and w.clock.text()[2]==':'
                w.close(); QTest.qWait(150)
        ''')

import unittest
from tests import test_main_window


class MemoryFactsUITests(unittest.TestCase):
    run_ui = test_main_window.MainWindowTests.run_ui

    def test_view_edit_delete_and_cache_refresh_use_temporary_store(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication, QMessageBox
            from alice_voice.memory import FactMemory
            from alice_voice.ui.memory_dialog import MemoryFactsDialog
            app = QApplication([])
            with TemporaryDirectory() as directory:
                memory = FactMemory(Path(directory) / 'memory.sqlite3')
                identity = memory.remember('Меня зовут Элдос')
                other_reader = FactMemory(memory.path)
                assert other_reader.owner_name() == 'Элдос'
                dialog = MemoryFactsDialog(memory)
                def wait():
                    for _ in range(100):
                        QTest.qWait(10)
                        if not dialog.busy:
                            return
                    raise AssertionError('Memory worker did not finish')
                wait()
                assert dialog.table.rowCount() == 1
                dialog.table.selectRow(0)
                with patch('alice_voice.ui.memory_dialog.QInputDialog.getText', return_value=('Игорь', True)), \\
                     patch('alice_voice.ui.memory_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
                    dialog.edit_selected()
                assert other_reader.owner_name() == 'Элдос'
                with patch('alice_voice.ui.memory_dialog.QInputDialog.getText', return_value=('Игорь', True)), \\
                     patch('alice_voice.ui.memory_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                    dialog.edit_selected()
                    wait()
                assert other_reader.owner_name() == 'Игорь'
                dialog.table.selectRow(0)
                with patch('alice_voice.ui.memory_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                    dialog.delete_selected()
                    wait()
                assert dialog.table.rowCount() == 0
                assert other_reader.owner_name() == ''
                dialog.close()
        ''')

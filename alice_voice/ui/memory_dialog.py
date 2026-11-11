"""Review and revision-checked editing of explicit persistent facts."""
from threading import Thread
from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QPushButton, QInputDialog, QMessageBox)


class MemorySignals(QObject):
    result = pyqtSignal(object, str)


class MemoryFactsDialog(QDialog):
    def __init__(self, memory, parent=None):
        super().__init__(parent)
        self.memory, self.rows, self.busy = memory, [], False
        self.setWindowTitle('Сохранённые факты')
        self.resize(920, 480)
        layout = QVBoxLayout(self)
        self.notice = QLabel('Профиль владельца, предпочтения и факты хранятся отдельно. '
            'Записи без подтверждённого источника не используются в ответах.')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.table = QTableWidget(0, 6)
        self.table.setStyleSheet('''
            QTableWidget { background: #142A43; color: #EEF6FF; gridline-color: #345679;
                selection-background-color: #254A6A; selection-color: #FFFFFF; }
            QHeaderView::section, QTableCornerButton::section {
                background: #1B3654; color: #EEF6FF; border: 1px solid #345679; padding: 5px; }
            QHeaderView { background: #142A43; }
        ''')
        self.table.setAccessibleName('Сохранённые факты и их происхождение')
        self.table.setHorizontalHeaderLabels(['Категория', 'Поле', 'Значение', 'Источник', 'Статус', 'Изменено'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(2, 250)
        for column, width in ((0, 150), (1, 120), (3, 160), (4, 120)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)
        controls = QHBoxLayout()
        self.refresh, self.edit, self.delete = QPushButton('Обновить'), QPushButton('Исправить'), QPushButton('Удалить')
        close = QPushButton('Закрыть')
        for button in (self.refresh, self.edit, self.delete, close):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.refresh.clicked.connect(self.reload)
        self.edit.clicked.connect(self.edit_selected)
        self.delete.clicked.connect(self.delete_selected)
        close.clicked.connect(self.close)
        self.signals = MemorySignals(self)
        self.signals.result.connect(self._result, Qt.ConnectionType.QueuedConnection)
        self.reload()

    def _work(self, operation):
        if self.busy:
            return
        self.busy = True
        for button in (self.refresh, self.edit, self.delete):
            button.setEnabled(False)
        signals = self.signals
        def run():
            try:
                operation()
                rows, message = self.memory.records(include_inactive=True), ''
            except ValueError as error:
                rows, message = None, str(error)
            except Exception:
                rows, message = None, 'Не удалось обновить память. Проверьте доступ к файлу и повторите.'
            try:
                signals.result.emit(rows, message)
            except RuntimeError:
                pass
        Thread(target=run, name='alice-memory-settings', daemon=True).start()

    def reload(self):
        self._work(lambda: None)

    def _result(self, rows, message):
        self.busy = False
        for button in (self.refresh, self.edit, self.delete):
            button.setEnabled(True)
        if message:
            self.notice.setText(message)
            return
        self.rows = rows
        self.table.setRowCount(len(rows))
        categories = {'profile': 'Профиль владельца', 'preference': 'Предпочтения', 'fact': 'Факт', 'conversation_summary': 'Резюме беседы'}
        statuses = {'confirmed': 'Подтверждено', 'quarantined': 'Исключено', 'unverified': 'Нужна проверка', 'derived': 'Резюме'}
        sources = {'user_command': 'Команда пользователя', 'user_confirmation': 'Отдельное подтверждение',
                   'user_settings': 'Правка в настройках', 'user_requirement': 'Указано владельцем',
                   'legacy_unknown': 'Источник неизвестен', 'derived_summary': 'Резюме беседы'}
        for i, row in enumerate(rows):
            field = {'owner_name': 'Имя владельца', 'language': 'Язык', 'application': 'Приложения',
                     'response_length': 'Длина ответа'}.get(row['field'], 'Запись №' + str(row['id']))
            values = (categories.get(row['category'], row['category']), field, row['value'],
                      sources.get(row['source'], row['source']), statuses.get(row['status'], row['status']), row['updated_at'])
            for j, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.table.setItem(i, j, item)
        self.notice.setText('Записей: ' + str(len(rows)) + '. В ответах используется только подтверждённая память. '
            'Исключённые записи и записи без источника сохранены для проверки, резюме хранятся отдельно.')

    def _selected(self):
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def edit_selected(self):
        row = self._selected()
        if row is None or self.busy:
            return
        value, accepted = QInputDialog.getText(self, 'Исправить факт', 'Новое значение:', text=row['value'])
        if not accepted or not value.strip():
            return
        if row['category'] == 'profile' and row['field'] == 'owner_name' and value.strip() != row['value']:
            answer = QMessageBox.question(self, 'Подтвердить изменение имени',
                f'Сейчас сохранено имя {row["value"]}. Заменить его на {value.strip()}?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._work(lambda: self.memory.edit(row['id'], value, expected_revision=row['revision'], source='user_settings'))

    def delete_selected(self):
        row = self._selected()
        if row is None or self.busy:
            return
        answer = QMessageBox.question(self, 'Удалить факт', f'Удалить запись №{row["id"]}?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._work(lambda: self.memory.forget(row['id'], expected_revision=row['revision']))

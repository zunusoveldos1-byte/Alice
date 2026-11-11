"""Application-wide keyboard focus policy, including native controls in dialogs."""
from PyQt6.QtCore import QObject, QEvent, Qt, QRectF, QTimer
from PyQt6 import sip
import weakref
import atexit
from PyQt6.QtGui import QColor, QPen
from PyQt6.QtWidgets import QApplication, QProxyStyle, QStyle

class FocusStyle(QProxyStyle):
    def drawPrimitive(self, element, option, painter, widget=None):
        if element == QStyle.PrimitiveElement.PE_FrameFocusRect:
            if widget is not None and widget.property('keyboardFocus'):
                painter.save()
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor('#51B5FF'), 1))
                painter.drawRoundedRect(QRectF(option.rect).adjusted(1,1,-1,-1), 4,4)
                painter.restore()
            return
        super().drawPrimitive(element, option, painter, widget)

class FocusPolicy(QObject):
    def mark(self, widget, value):
        if widget is not None and hasattr(widget, 'style'):
            if widget.property('keyboardFocus') == value:
                return
            widget.setProperty('keyboardFocus', value)
            ref=weakref.ref(widget)
            def refresh():
                target=ref()
                if target is not None and not sip.isdeleted(target):
                    target.style().unpolish(target)
                    target.style().polish(target)
                    target.update()
            QTimer.singleShot(0,refresh)
            widget.update()
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            self.mark(QApplication.focusWidget(), False)
            self.mark(obj, False)
        elif event.type() == QEvent.Type.FocusIn:
            self.mark(obj, event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason))
        elif event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.mark(QApplication.focusWidget(), True)
        return False

def install_focus_policy():
    app=QApplication.instance()
    if not hasattr(app, '_alice_focus_policy'):
        app._alice_focus_style = FocusStyle('Fusion')
        app.setStyle(app._alice_focus_style)
        app._alice_focus_policy=FocusPolicy(app)
        app.installEventFilter(app._alice_focus_policy)
        app_ref = weakref.ref(app)
        def detach():
            current = app_ref()
            if current is not None and not sip.isdeleted(current):
                current.removeEventFilter(current._alice_focus_policy)
        # Qt can destroy focused widgets after Python's event machinery has
        # started shutting down, including callers which never ran app.exec().
        atexit.register(detach)

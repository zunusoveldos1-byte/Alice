import argparse
import logging
import sys


def main():
    logging.basicConfig(level=logging.WARNING,
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('alice_voice').setLevel(logging.INFO)
    parser = argparse.ArgumentParser(description='Alice Voice: PCM-only dialog')
    parser.add_argument('--settings', default='settings.json')
    parser.add_argument('--devices', action='store_true', help='list PortAudio devices, no capture')
    parser.add_argument('--smoke', action='store_true', help='open and close GUI without microphone')
    args = parser.parse_args()
    if args.devices:
        import sounddevice as sd
        print(sd.query_devices())
        return 0
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from PyQt6.QtCore import QTimer
    from .config import Settings, SettingsMigrationError
    from .controller import VoiceSessionController
    from .ui.main_window import MainWindow
    app = QApplication(sys.argv[:1])
    try:
        settings = Settings.load(args.settings)
    except SettingsMigrationError as error:
        print(str(error), file=sys.stderr)
        if not args.smoke:
            QMessageBox.critical(None, 'Перенос ключа ElevenLabs', str(error))
        return 2
    except (ValueError, TypeError, OSError):
        print('Неверные настройки. Исправьте файл или запустите с другим --settings.', file=sys.stderr)
        return 2
    controller = VoiceSessionController(settings)
    window = MainWindow(controller, args.settings)
    # Keep the restored window inside the current monitor's usable DIP area.
    available = window.screen().availableGeometry()
    window.resize(min(window.width(), available.width()),
                  min(window.height(), available.height()))
    window.showFullScreen()
    if args.smoke:
        QTimer.singleShot(500, window.close)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())

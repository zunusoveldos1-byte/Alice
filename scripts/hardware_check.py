"""Explicit Windows hardware check, separate from offline automated tests."""
import argparse
import json
import sys
from pathlib import Path
from time import perf_counter, sleep
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alice_voice.audio import AudioCapture, FrameInbox


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-input', action='store_true')
    parser.add_argument('--device', type=int, default=None)
    args = parser.parse_args()
    import sounddevice as sd
    result = {'device': args.device if args.device is not None else int(sd.default.device[0])}
    try:
        sd.check_input_settings(device=args.device, samplerate=16000, channels=1, dtype='int16')
        result['input_format_supported'] = True
        if args.check_input:
            inbox = FrameInbox()
            capture = AudioCapture(inbox, args.device)
            started = perf_counter()
            try:
                capture.open()
                sleep(.5)
            finally:
                capture.close()
            result.update(captured_frames=inbox.size, dropped=inbox.damaged.is_set(),
                          elapsed_ms=round((perf_counter() - started) * 1000, 1),
                          device_closed=capture.stream is None)
            inbox.clear()
    except Exception:
        result['input_format_supported'] = False
        result['input_error'] = 'Default/selected input unavailable or unsupported.'
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        voice = win32com.client.Dispatch('SAPI.SpVoice')
        result['sapi_voices'] = [v.GetDescription() for v in voice.GetVoices()]
        voice = None
        pythoncom.CoUninitialize()
    except Exception:
        result['sapi_error'] = 'SAPI unavailable.'
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""Bounded playback reference for conservative speaker-mode interruption.

This is echo evidence, not AEC or speaker identification. Uncertain audio
must not authorize interruption while the output/tail overlaps a phrase.
"""
from collections import deque
import re
from threading import Lock

import numpy as np


def words(text):
    return re.findall(r'\w+', text.casefold().replace('ё', 'е'))


def is_response_stop(text):
    return words(text) in (['стоп'], ['замолчи'], ['хватит'])


def matches_assistant(text, answer):
    heard, spoken = words(text), words(answer)
    if not heard or not spoken:
        return False
    if ' '.join(heard) in ' '.join(spoken):
        return True
    # Be conservative with recognizer omissions/reordering in speaker mode.
    return len(heard) > 1 and sum(word in spoken for word in heard) / len(heard) >= .6


class PlaybackReference:
    """Three bounded spans: full clips up to 60 s, streamed history up to 3 s."""
    rate = 2000
    span_samples = 3 * rate

    def __init__(self):
        self.lock = Lock()
        self.chunks = deque(maxlen=3)

    def add(self, pcm, started):
        samples = np.frombuffer(pcm.data, dtype='<i2').astype(np.float32)
        if pcm.channels == 2:
            samples = samples.reshape(-1, 2).mean(axis=1)
        # Match microphone preprocessing: resample to 16 kHz, then average
        # groups of eight samples. No work occurs in the PortAudio callback.
        count = min(int(len(samples) * 16000 / pcm.sample_rate), 60 * 16000)
        if count < 8:
            return
        at = np.arange(count) * pcm.sample_rate / 16000
        signal = np.interp(at, np.arange(len(samples)), samples).astype(np.float32)
        signal = signal[:len(signal) // 8 * 8].reshape(-1, 8).mean(axis=1) / 32768
        with self.lock:
            streaming_packet = len(signal) <= .15 * self.rate
            if streaming_packet and self.chunks:
                previous_start, previous = self.chunks[-1]
                gap = started - (previous_start + len(previous) / self.rate)
                # Allow timer jitter, but retain genuine output underruns as
                # separate spans. Never compress a long silence into speech.
                if abs(gap) <= .04:
                    started = previous_start
                    signal = np.concatenate((previous, signal))
                    self.chunks.pop()
            if streaming_packet and len(signal) > self.span_samples:
                discarded = len(signal) - self.span_samples
                signal = signal[discarded:]
                started += discarded / self.rate
            self.chunks.append((started, signal))

    def classify(self, pcm, ended):
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        if len(samples) < 2560:  # At least 160 ms of observed microphone PCM.
            return 'unknown'
        samples = samples[:len(samples) // 8 * 8].reshape(-1, 8).mean(axis=1)
        samples -= samples.mean()
        energy = float(np.dot(samples, samples))
        if energy / len(samples) < .000004:
            return 'unknown'
        with self.lock:
            chunks = tuple(self.chunks)
        available = False
        for start, reference in chunks:
            # Search up to 500 ms of acoustic/device delay plus 100 ms of
            # timestamp uncertainty, within the actual playback time range.
            offset = ended - len(samples) / self.rate - start
            left = max(0, int((offset - .5) * self.rate))
            right = max(0, min(len(reference), int((offset + .1) * self.rate) + len(samples)))
            window = reference[left:right]
            if len(window) < len(samples):
                continue
            available = True
            dots = np.correlate(window, samples, mode='valid')
            sums = np.concatenate(([0.], np.cumsum(window, dtype=np.float64)))
            squares = np.concatenate(([0.], np.cumsum(window * window, dtype=np.float64)))
            n = len(samples)
            variance = np.maximum(squares[n:] - squares[:-n] - (sums[n:] - sums[:-n]) ** 2 / n, 1e-12)
            correlation = np.abs(dots) / np.sqrt(energy * variance)
            if float(correlation.max(initial=0)) >= .55:
                return 'echo'
        return 'independent' if available else 'unknown'

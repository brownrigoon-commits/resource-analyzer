"""Cooperative adapter for an existing, sequential local voice worker.

This module does not start a job or change any model. The host worker must
retain its existing input seals, model lock, output validation and checkpoint
rules, choose the device before loading a model, and call checkpoint only
between utterances. Copy resource_client.py and resource_ipc.py beside this
file when embedding.
"""
from pathlib import Path
import importlib.util
import sys


def session(stage):
    mapped = {'prepare': 'audio-preprocess', 'tokens': 'cosyvoice-synthesis',
              'waveform': 'cosyvoice-synthesis', 'qa': 'whisper-asr',
              'qa-medium': 'whisper-asr'}
    if stage not in mapped:
        raise ValueError('Unsupported voice stage')
    sdk_dir = str(Path(__file__).resolve().parent)
    if sdk_dir not in sys.path:
        sys.path.insert(0, sdk_dir)
    spec = importlib.util.spec_from_file_location(
        'embedded_resource_client', Path(__file__).with_name('resource_client.py'))
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    # The inference worker may block every socket and DNS lookup. File IPC
    # keeps that existing offline protection intact, including loopback.
    return client.FileResourceSession(mapped[stage])

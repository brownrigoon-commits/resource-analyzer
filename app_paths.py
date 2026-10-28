"""Portable application identity and writable state locations."""
from __future__ import annotations

import os
from pathlib import Path
import sys

APP_ID = 'resource-analyzer'
APP_NAME = '자원 분석기'
VERSION = '0.7.3'


def bundle_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def state_dir(override=None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    return base / 'ResourceAnalyzer'

"""Expose game_data modules to offline tests without importing CLI/cloud setup."""
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
if 'game_data' not in sys.modules:
    package = types.ModuleType('game_data')
    package.__path__ = [str(ROOT / 'src/game_data')]
    sys.modules['game_data'] = package

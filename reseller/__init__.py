"""Relay reseller panel for 3X-UI."""
from pathlib import Path


def _read_version():
    # The VERSION file at the project root is the single source of truth.
    try:
        return (Path(__file__).resolve().parent.parent / 'VERSION').read_text(encoding='ascii').strip()
    except OSError:
        return '0.0.0'


__version__ = _read_version()

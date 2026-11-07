"""Runtime reload including persisted threshold values.

The foundation loader currently drops its parsed thresholds. Keep the web and
scan reload path consistent without changing the foundation's public contract.
"""
from dataclasses import fields, replace
import json

from almali_library_curator.config import SETTINGS_FILE_NAME, Thresholds, load_settings


def load_runtime_settings(config_dir=None):
    settings = load_settings(config_dir)
    try:
        saved = json.loads((settings.config_dir / SETTINGS_FILE_NAME).read_text(encoding='utf-8'))
        thresholds = saved.get('thresholds', {})
        if not isinstance(thresholds, dict):
            return settings
        values = {}
        for field in fields(Thresholds):
            value = thresholds.get(field.name)
            if type(value) is int and value > 0:
                values[field.name] = value
        return replace(settings, thresholds=replace(settings.thresholds, **values))
    except (OSError, ValueError, AttributeError):
        return settings

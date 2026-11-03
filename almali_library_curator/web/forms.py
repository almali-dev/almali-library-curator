"""Strict settings parsing before any persistence; only non-secret fields."""
from dataclasses import asdict, fields, replace
import math
import re

from almali_library_curator.config import SECRET_FIELDS, Thresholds


def boolean(raw):
    if raw in ('true', '1', 'on'):
        return True
    if raw in ('false', '0', 'off'):
        return False
    raise ValueError('invalid boolean')


def validate_settings(settings, form):
    changes, errors = {}, {}

    def parse(name, parser, key):
        if name in form:
            try:
                changes[name] = parser(form[name])
            except (ValueError, TypeError, OverflowError):
                errors[name] = 'web.form.' + key

    def grace(raw):
        value = int(raw)
        if not 3 <= value <= 60:
            raise ValueError('grace range')
        return value

    def rating(raw):
        value = float(raw)
        if not math.isfinite(value) or not 0 <= value <= 10:
            raise ValueError('rating range')
        return value

    def schedule(raw):
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', raw):
            raise ValueError('invalid time')
        return raw

    def collection(raw):
        if not raw.strip():
            raise ValueError('empty name')
        return raw.strip()

    parse('grace_days', grace, 'grace_range')
    parse('minimum_rating', rating, 'rating_range')
    parse('scan_time', schedule, 'scan_time')
    parse('collection_name', collection, 'collection_required')
    for name in ('dry_run', 'rating_protection_enabled'):
        parse(name, boolean, 'boolean')
    for name in ('protected_libraries', 'excluded_users'):
        if name in form:
            changes[name] = tuple(part.strip() for raw in form.getlist(name)
                                  for part in raw.split(',') if part.strip())
    thresholds = asdict(settings.thresholds)
    for field in fields(Thresholds):
        name = field.name
        if name in form:
            try:
                value = int(form[name])
                if value < 1:
                    raise ValueError('positive months required')
                thresholds[name] = value
            except (ValueError, TypeError):
                errors[name] = 'web.form.threshold_range'
    for name in SECRET_FIELDS & form.keys():
        errors[name] = 'web.form.secrets_unsupported'
    if errors:
        return None, errors
    return replace(settings, thresholds=Thresholds(**thresholds), **changes), {}

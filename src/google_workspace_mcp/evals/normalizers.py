"""Normalize evaluation answers."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum

# === Constants ===

MAX_ANSWER_CHARACTERS = 8192
_INTEGER_PATTERN = re.compile(r'^[+-]?[0-9]+$')
_DATE_PATTERN = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')


class AnswerNormalizationError(ValueError):
    """Report invalid answer values."""


class NormalizerName(StrEnum):
    """Name one answer normalizer."""

    EXACT_STRING = 'exact_string'
    INTEGER = 'integer'
    DECIMAL_1 = 'decimal_1'
    BOOLEAN = 'boolean'
    DATE = 'date'
    UTC_DATETIME = 'utc_datetime'
    ENUM = 'enum'


def _bounded_text(value: str) -> str:
    """Return one bounded answer."""
    if not isinstance(value, str):
        raise AnswerNormalizationError('answer must be text')
    if len(value) > MAX_ANSWER_CHARACTERS:
        raise AnswerNormalizationError('answer exceeds the size limit')
    normalized = value.strip()
    if not normalized:
        raise AnswerNormalizationError('answer must be non-empty')
    return normalized


def _integer(value: str) -> str:
    """Normalize one integer."""
    if not _INTEGER_PATTERN.fullmatch(value) or len(value.lstrip('+-')) > 128:
        raise AnswerNormalizationError('answer must be an integer')
    return str(int(value))


def _decimal_1(value: str) -> str:
    """Normalize one decimal."""
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise AnswerNormalizationError(
            'answer must be a finite decimal'
        ) from error
    if not parsed.is_finite() or len(value) > 128:
        raise AnswerNormalizationError('answer must be a finite decimal')
    try:
        return format(
            parsed.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP),
            '.1f',
        )
    except InvalidOperation as error:
        raise AnswerNormalizationError(
            'answer must be a finite decimal'
        ) from error


def _boolean(value: str) -> str:
    """Normalize one boolean."""
    normalized = value.lower()
    if normalized not in {'true', 'false'}:
        raise AnswerNormalizationError('answer must be true or false')
    return normalized


def _date(value: str) -> str:
    """Normalize one date."""
    try:
        if not _DATE_PATTERN.fullmatch(value):
            raise ValueError
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise AnswerNormalizationError('answer must be an ISO date') from error
    return parsed.isoformat()


def _utc_datetime(value: str) -> str:
    """Normalize one UTC datetime."""
    try:
        if 'T' not in value:
            raise ValueError
        parsed = datetime.fromisoformat(
            value[:-1] + '+00:00' if value.endswith('Z') else value
        )
        if parsed.tzinfo is None or parsed.microsecond != 0:
            raise ValueError
    except ValueError as error:
        raise AnswerNormalizationError(
            'answer must be an ISO datetime'
        ) from error
    return parsed.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _enum(value: str, enum_values: tuple[str, ...] | None) -> str:
    """Normalize one closed enum."""
    if not enum_values:
        raise AnswerNormalizationError('enum values are required')
    normalized_values = {item.lower(): item for item in enum_values}
    if len(normalized_values) != len(enum_values):
        raise AnswerNormalizationError('enum values must be unique')
    normalized = normalized_values.get(value.lower())
    if normalized is None:
        raise AnswerNormalizationError('answer is outside the enum')
    return normalized


def normalize_answer(
    value: str,
    normalizer: str | NormalizerName,
    *,
    enum_values: tuple[str, ...] | None = None,
) -> str:
    """Normalize one evaluation answer."""
    text = _bounded_text(value)
    try:
        name = NormalizerName(normalizer)
    except ValueError as error:
        raise AnswerNormalizationError('unknown answer normalizer') from error
    match name:
        case NormalizerName.EXACT_STRING:
            return text
        case NormalizerName.INTEGER:
            return _integer(text)
        case NormalizerName.DECIMAL_1:
            return _decimal_1(text)
        case NormalizerName.BOOLEAN:
            return _boolean(text)
        case NormalizerName.DATE:
            return _date(text)
        case NormalizerName.UTC_DATETIME:
            return _utc_datetime(text)
        case NormalizerName.ENUM:
            return _enum(text, enum_values)

"""Execute Calendar batch mutations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .constants import MAX_BATCH_OPERATIONS
from .errors import CalendarInputError, CalendarProviderError
from .schemas import (
    BatchItemResult,
    BatchMutationResponse,
    BatchOperation,
    BatchOperationType,
)
from .time import all_day_range, normalize_time_range

_ALLOWED_EVENT_FIELDS = frozenset(
    {
        'summary',
        'description',
        'location',
        'start',
        'end',
        'attendees',
        'reminders',
        'recurrence',
    }
)


def _validated_body(
    operation: BatchOperation, *, create: bool
) -> dict[str, object]:
    """Validate Calendar batch body."""
    if operation.body is None:
        action = 'create' if create else 'update'
        raise CalendarInputError(f'{action} batch operation requires body')
    body = dict(operation.body)
    if not body or set(body).difference(_ALLOWED_EVENT_FIELDS):
        raise CalendarInputError('batch event body is invalid')
    if create and not {'summary', 'start', 'end'}.issubset(body):
        raise CalendarInputError('create batch event body is incomplete')
    for field in ('summary', 'description', 'location'):
        value = body.get(field)
        if value is not None and (
            not isinstance(value, str) or len(value) > 4_000
        ):
            raise CalendarInputError('batch event text is invalid')
    start = body.get('start')
    end = body.get('end')
    if start is not None or end is not None:
        if not isinstance(start, Mapping) or not isinstance(end, Mapping):
            raise CalendarInputError('batch event boundaries are invalid')
        if 'dateTime' in start and 'dateTime' in end:
            time_zone = start.get('timeZone') or end.get('timeZone')
            if not isinstance(time_zone, str):
                raise CalendarInputError('batch event timezone is required')
            normalize_time_range(
                str(start['dateTime']), str(end['dateTime']), time_zone
            )
        elif 'date' in start and 'date' in end:
            all_day_range(str(start['date']), str(end['date']))
        else:
            raise CalendarInputError('batch event boundaries are invalid')
    attendees = body.get('attendees')
    if attendees is not None:
        if not isinstance(attendees, list) or len(attendees) > 100:
            raise CalendarInputError('batch attendees are invalid')
        for attendee in attendees:
            if not isinstance(attendee, Mapping):
                raise CalendarInputError('batch attendee is invalid')
            email = attendee.get('email')
            if (
                not isinstance(email, str)
                or '@' not in email
                or len(email) > 320
            ):
                raise CalendarInputError('batch attendee is invalid')
    recurrence = body.get('recurrence')
    if recurrence is not None:
        if not isinstance(recurrence, list) or len(recurrence) > 10:
            raise CalendarInputError('batch recurrence is invalid')
        if any(
            not isinstance(value, str)
            or not value.startswith(('RRULE:', 'RDATE:', 'EXDATE:'))
            for value in recurrence
        ):
            raise CalendarInputError('batch recurrence is invalid')
    return body


class BatchGateway(Protocol):
    """Describe Calendar batch gateway."""

    def service(self) -> Any:
        """Return Calendar provider service."""
        ...


class CalendarBatchExecutor:
    """Execute bounded Calendar batch."""

    def __init__(self, gateway: BatchGateway) -> None:
        """Initialize Calendar batch executor."""
        self._gateway = gateway

    @staticmethod
    def _request(service: Any, operation: BatchOperation) -> Any:
        """Build Calendar batch request."""
        events = service.events()
        if operation.operation is BatchOperationType.CREATE:
            return events.insert(
                calendarId=operation.calendar_id,
                body=_validated_body(operation, create=True),
                sendUpdates=operation.send_updates.value,
            )
        if not operation.event_id or not operation.etag:
            raise CalendarInputError(
                'update and delete batch operations require event ID and ETag'
            )
        if operation.operation is BatchOperationType.UPDATE:
            request = events.patch(
                calendarId=operation.calendar_id,
                eventId=operation.event_id,
                body=_validated_body(operation, create=False),
                sendUpdates=operation.send_updates.value,
            )
        else:
            request = events.delete(
                calendarId=operation.calendar_id,
                eventId=operation.event_id,
                sendUpdates=operation.send_updates.value,
            )
        request.headers['If-Match'] = operation.etag
        return request

    def execute(
        self, operations: Sequence[BatchOperation]
    ) -> BatchMutationResponse:
        """Execute mixed Calendar mutations."""
        if not 1 <= len(operations) <= MAX_BATCH_OPERATIONS:
            raise CalendarInputError('batch operation count is invalid')
        identifiers = [value.operation_id for value in operations]
        if len(identifiers) != len(set(identifiers)):
            raise CalendarInputError('batch operation IDs must be unique')
        service = self._gateway.service()
        results: dict[str, BatchItemResult] = {}
        operation_map = {value.operation_id: value for value in operations}

        def callback(
            request_id: str,
            response: Any,
            exception: Exception | None,
        ) -> None:
            """Collect Calendar batch result."""
            operation = operation_map[request_id]
            if exception is not None:
                results[request_id] = BatchItemResult(
                    operation_id=request_id,
                    success=False,
                    error='Calendar batch item failed',
                )
                return
            if operation.operation is BatchOperationType.DELETE:
                results[request_id] = BatchItemResult(
                    operation_id=request_id,
                    success=True,
                    event_id=operation.event_id,
                    deleted=True,
                )
                return
            event_id = ''
            if isinstance(response, dict):
                event_id = str(response.get('id', ''))[:256]
            results[request_id] = BatchItemResult(
                operation_id=request_id,
                success=True,
                event_id=event_id or operation.event_id,
            )

        batch = service.new_batch_http_request(callback=callback)
        for operation in operations:
            batch.add(
                self._request(service, operation),
                request_id=operation.operation_id,
            )
        try:
            batch.execute()
        except Exception:
            raise CalendarProviderError(
                'Calendar batch request is temporarily unavailable'
            ) from None
        return BatchMutationResponse(
            items=tuple(
                results.get(
                    value.operation_id,
                    BatchItemResult(
                        operation_id=value.operation_id,
                        success=False,
                        error='Calendar batch item failed',
                    ),
                )
                for value in operations
            )
        )

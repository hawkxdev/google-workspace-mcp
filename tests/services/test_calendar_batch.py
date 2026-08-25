"""Test Calendar batch mutations."""

from __future__ import annotations

from typing import Any

import pytest

from google_workspace_mcp.services.calendar.batch import CalendarBatchExecutor
from google_workspace_mcp.services.calendar.errors import CalendarInputError
from google_workspace_mcp.services.calendar.schemas import (
    BatchOperation,
    BatchOperationType,
)


class FakeRequest:
    """Store Calendar batch request."""

    def __init__(self, response: Any = None) -> None:
        """Initialize batch request fake."""
        self.response = response
        self.headers: dict[str, str] = {}


class FakeEvents:
    """Build Calendar batch requests."""

    def insert(self, **kwargs: Any) -> FakeRequest:
        """Build batch insert request."""
        return FakeRequest({'id': kwargs['body'].get('id', 'created')})

    def patch(self, **kwargs: Any) -> FakeRequest:
        """Build batch patch request."""
        return FakeRequest({'id': kwargs['eventId']})

    def delete(self, **kwargs: Any) -> FakeRequest:
        """Build batch delete request."""
        return FakeRequest(None)


class FakeBatch:
    """Execute Calendar batch callbacks."""

    def __init__(self, callback: Any, failing: set[str]) -> None:
        """Initialize batch callback fake."""
        self.callback = callback
        self.failing = failing
        self.requests: list[tuple[str, FakeRequest]] = []

    def add(self, request: FakeRequest, request_id: str) -> None:
        """Add one batch request."""
        self.requests.append((request_id, request))

    def execute(self) -> None:
        """Execute all batch callbacks."""
        for request_id, request in self.requests:
            if request_id in self.failing:
                self.callback(
                    request_id, None, RuntimeError('provider marker')
                )
            else:
                self.callback(request_id, request.response, None)


class FakeService:
    """Expose Calendar batch resources."""

    def __init__(self, failing: set[str] | None = None) -> None:
        """Initialize batch service fake."""
        self.event_values = FakeEvents()
        self.failing = failing or set()

    def events(self) -> FakeEvents:
        """Return batch events resource."""
        return self.event_values

    def new_batch_http_request(self, callback: Any) -> FakeBatch:
        """Return Calendar batch request."""
        return FakeBatch(callback, self.failing)


class FakeGateway:
    """Return Calendar batch service."""

    def __init__(self, service: FakeService) -> None:
        """Initialize batch gateway fake."""
        self._service = service

    def service(self) -> FakeService:
        """Return fake Calendar service."""
        return self._service


def test_batch_preserves_partial_success_and_order() -> None:
    operations = (
        BatchOperation(
            operation_id='create-1',
            operation=BatchOperationType.CREATE,
            calendar_id='primary',
            body={
                'summary': 'Created',
                'start': {'date': '2026-08-25'},
                'end': {'date': '2026-08-26'},
            },
        ),
        BatchOperation(
            operation_id='update-1',
            operation=BatchOperationType.UPDATE,
            calendar_id='primary',
            event_id='event-1',
            etag='etag-1',
            body={'summary': 'Updated'},
        ),
        BatchOperation(
            operation_id='delete-1',
            operation=BatchOperationType.DELETE,
            calendar_id='primary',
            event_id='event-2',
            etag='etag-2',
        ),
    )
    result = CalendarBatchExecutor(
        FakeGateway(FakeService(failing={'update-1'}))
    ).execute(operations)
    assert [item.operation_id for item in result.items] == [
        'create-1',
        'update-1',
        'delete-1',
    ]
    assert result.items[0].success is True
    assert result.items[1].success is False
    assert result.items[1].error == 'Calendar batch item failed'
    assert result.items[2].deleted is True


def test_batch_rejects_duplicate_and_future_operations() -> None:
    duplicate = BatchOperation(
        operation_id='same',
        operation=BatchOperationType.DELETE,
        calendar_id='primary',
        event_id='event-1',
        etag='etag',
    )
    executor = CalendarBatchExecutor(FakeGateway(FakeService()))
    with pytest.raises(CalendarInputError, match='unique'):
        executor.execute((duplicate, duplicate))

    with pytest.raises(CalendarInputError, match='body'):
        executor.execute(
            (
                BatchOperation(
                    operation_id='create',
                    operation=BatchOperationType.CREATE,
                    calendar_id='primary',
                ),
            )
        )

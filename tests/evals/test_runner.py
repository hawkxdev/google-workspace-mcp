"""Test the canonical evaluation runner."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from google_workspace_mcp.evals import runner as runner_module
from google_workspace_mcp.evals.models import FixtureBindings, ServiceName
from google_workspace_mcp.evals.normalizers import NormalizerName
from google_workspace_mcp.evals.runner import (
    EvaluationRunError,
    EvaluationRunner,
    ModelTurn,
    RunBudget,
    RunLimits,
    ToolCall,
    ToolSpec,
)
from google_workspace_mcp.evals.validation import (
    EvaluationCatalog,
    EvaluationPair,
)

# === Fakes ===


class RecordingSession:
    """Record evaluation tool calls."""

    def __init__(
        self,
        tools: tuple[ToolSpec, ...],
        responses: dict[str, object],
    ) -> None:
        """Configure one fake session."""
        self.tools = tools
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        """Return configured tools."""
        return self.tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> object:
        """Record and answer one call."""
        self.calls.append((name, arguments))
        response = self.responses[name]
        if isinstance(response, Exception):
            raise response
        return response


class RecordingModel:
    """Record model turns."""

    model_name = 'deepseek-v4-pro'
    version = 'test-model-client'

    def __init__(
        self,
        turns: list[ModelTurn],
        *,
        input_tokens: int = 20,
    ) -> None:
        """Configure model responses."""
        self.turns = turns
        self.input_tokens = input_tokens
        self.requests: list[
            tuple[list[dict[str, Any]], tuple[ToolSpec, ...], int]
        ] = []
        self.count_requests = 0

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: tuple[ToolSpec, ...],
    ) -> int:
        """Return a fixed token count."""
        self.count_requests += 1
        return self.input_tokens

    async def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: tuple[ToolSpec, ...],
        *,
        max_tokens: int,
    ) -> ModelTurn:
        """Return the next model turn."""
        self.requests.append((messages.copy(), tools, max_tokens))
        return self.turns.pop(0)


class RecordingSessionFactory:
    """Create recording sessions."""

    def __init__(self, session: RecordingSession) -> None:
        """Store one fake session."""
        self.session = session
        self.opened: list[ServiceName] = []

    @asynccontextmanager
    async def __call__(
        self,
        service: ServiceName,
    ) -> AsyncIterator[RecordingSession]:
        """Yield one recording session."""
        self.opened.append(service)
        yield self.session


# === Helpers ===


def _pair(
    *,
    expected: str = '1',
    tools: tuple[str, ...] = ('drive_get_file',),
) -> EvaluationPair:
    """Build one runner pair."""
    return EvaluationPair(
        task_id='drive_01',
        question='Read drive_ledger_file and return one.',
        expected_answer=expected,
        normalizer=NormalizerName.INTEGER,
        fixture_refs=('drive_ledger_file',),
        allowed_tools=tools,
        minimum_mcp_calls=1,
    )


def _tool(name: str) -> ToolSpec:
    """Build one tool definition."""
    return ToolSpec(
        name=name,
        description='Read synthetic data.',
        input_schema={'type': 'object'},
    )


def _drive_tools() -> tuple[ToolSpec, ...]:
    """Build the exact Drive registry."""
    return tuple(
        _tool(name)
        for name in (
            'drive_search_files',
            'drive_get_file',
            'drive_list_folder',
        )
    )


def _tool_turn(*calls: ToolCall) -> ModelTurn:
    """Build one tool use turn."""
    return ModelTurn(
        stop_reason='tool_use',
        assistant_content=tuple(
            {
                'type': 'tool_use',
                'id': call.call_id,
                'name': call.name,
                'input': call.arguments,
            }
            for call in calls
        ),
        tool_calls=calls,
        final_text=None,
        output_tokens=12,
    )


def _final_turn(answer: str, *, stop_reason: str = 'end_turn') -> ModelTurn:
    """Build one final model turn."""
    return ModelTurn(
        stop_reason=stop_reason,
        assistant_content=({'type': 'text', 'text': answer},),
        tool_calls=(),
        final_text=answer,
        output_tokens=4,
    )


# === Pair execution ===


@pytest.mark.asyncio
async def test_runner_executes_all_tool_calls_in_order(
    applied_bindings: FixtureBindings,
) -> None:
    tools = _drive_tools()
    session = RecordingSession(
        tools,
        {
            'drive_get_file': {
                'file_id': applied_bindings.objects[
                    'drive_ledger_file'
                ].identifiers.file_id,
                'name': 'Synthetic ledger teal-harbor-n5s8.csv',
                'mime_type': 'text/csv',
            },
            'drive_search_files': {
                'files': [
                    {
                        'file_id': applied_bindings.objects[
                            'drive_ledger_file'
                        ].identifiers.file_id,
                        'name': 'Synthetic ledger teal-harbor-n5s8.csv',
                        'mime_type': 'text/csv',
                    }
                ]
            },
        },
    )
    model = RecordingModel(
        [
            _tool_turn(
                ToolCall(
                    'call-1',
                    'drive_get_file',
                    {'file_id': 'drive_ledger_file'},
                ),
                ToolCall(
                    'call-2',
                    'drive_search_files',
                    {'exact_name': 'Synthetic ledger teal-harbor-n5s8.csv'},
                ),
            ),
            _final_turn('1'),
        ]
    )
    runner = EvaluationRunner(
        model,
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(tools=('drive_get_file', 'drive_search_files')),
        RunBudget(),
    )

    assert result.status == 'passed'
    assert result.called_tools == ('drive_get_file', 'drive_search_files')
    assert result.mcp_calls == 2
    assert [name for name, _ in session.calls] == [
        'drive_get_file',
        'drive_search_files',
    ]
    second_messages = model.requests[1][0]
    tool_results = second_messages[-1]['content']
    assert [item['tool_use_id'] for item in tool_results] == [
        'call-1',
        'call-2',
    ]


@pytest.mark.asyncio
async def test_runner_rejects_an_answer_without_a_tool_call(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    runner = EvaluationRunner(
        RecordingModel([_final_turn('1')]),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'failed_route'
    assert result.error_category == 'minimum_mcp_calls'


@pytest.mark.asyncio
async def test_runner_requires_every_allowed_tool(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(
        _drive_tools(),
        {'drive_get_file': {'file_id': 'foreign'}},
    )
    runner = EvaluationRunner(
        RecordingModel(
            [
                _tool_turn(
                    ToolCall(
                        'call-1',
                        'drive_get_file',
                        {'file_id': 'drive_ledger_file'},
                    )
                ),
                _final_turn('1'),
            ]
        ),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(tools=('drive_get_file', 'drive_search_files')),
        RunBudget(),
    )

    assert result.status == 'failed_route'
    assert result.error_category == 'required_tools'


@pytest.mark.asyncio
async def test_runner_rejects_a_disallowed_tool_before_execution(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    runner = EvaluationRunner(
        RecordingModel(
            [
                _tool_turn(
                    ToolCall(
                        'call-1',
                        'drive_copy_file',
                        {'file_id': 'drive_ledger_file'},
                    )
                )
            ]
        ),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'failed_route'
    assert session.calls == []


@pytest.mark.asyncio
async def test_runner_rejects_registry_drift_in_each_pair_session(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(
        (*_drive_tools(), _tool('drive_copy_file')),
        {},
    )
    model = RecordingModel([_final_turn('1')])
    runner = EvaluationRunner(
        model,
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'failed_route'
    assert result.error_category == 'route_validation'
    assert model.count_requests == 0
    assert model.requests == []


@pytest.mark.asyncio
async def test_runner_requires_structured_content(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {'drive_get_file': None})
    runner = EvaluationRunner(
        RecordingModel(
            [
                _tool_turn(
                    ToolCall(
                        'call-1',
                        'drive_get_file',
                        {'file_id': 'drive_ledger_file'},
                    )
                )
            ]
        ),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'failed_route'
    assert result.error_category == 'route_validation'


@pytest.mark.asyncio
async def test_runner_reports_output_truncation(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    runner = EvaluationRunner(
        RecordingModel([_final_turn('partial', stop_reason='max_tokens')]),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'limit_max_tokens'


@pytest.mark.asyncio
async def test_runner_rejects_refusal_text(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    runner = EvaluationRunner(
        RecordingModel([_final_turn('Cannot answer', stop_reason='refusal')]),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'model_error'
    assert result.error_category == 'model_stop'


@pytest.mark.asyncio
async def test_runner_sanitizes_model_failure(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    runner = EvaluationRunner(
        RecordingModel([]),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    budget = RunBudget()
    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        budget,
    )

    assert result.status == 'model_error'
    assert result.error_category == 'model_api'
    assert result.model_turns == 1
    assert budget.model_turns == 1


@pytest.mark.asyncio
async def test_runner_counts_and_sanitizes_mcp_failure(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(
        _drive_tools(),
        {'drive_get_file': RuntimeError('private provider response')},
    )
    runner = EvaluationRunner(
        RecordingModel(
            [
                _tool_turn(
                    ToolCall(
                        'call-1',
                        'drive_get_file',
                        {'file_id': 'drive_ledger_file'},
                    )
                )
            ]
        ),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'mcp_error'
    assert result.error_category == 'mcp_call'
    assert result.called_tools == ('drive_get_file',)
    assert result.mcp_calls == 1


@pytest.mark.asyncio
async def test_runner_sanitizes_unexpected_execution_error(
    monkeypatch: pytest.MonkeyPatch,
    applied_bindings: FixtureBindings,
) -> None:
    secret = 'unexpected-private-error-a82f'
    session = RecordingSession(
        _drive_tools(),
        {'drive_get_file': {'file_id': 'foreign'}},
    )

    def fail_normalization(*args: object, **kwargs: object) -> str:
        """Raise one unexpected failure."""
        raise RuntimeError(secret)

    monkeypatch.setattr(
        runner_module,
        'normalize_answer',
        fail_normalization,
    )
    runner = EvaluationRunner(
        RecordingModel(
            [
                _tool_turn(
                    ToolCall(
                        'call-1',
                        'drive_get_file',
                        {'file_id': 'drive_ledger_file'},
                    )
                ),
                _final_turn('1'),
            ]
        ),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'execution_error'
    assert result.error_category == 'execution_error'
    assert result.model_turns == 2
    assert result.mcp_calls == 1
    assert secret not in repr(result)


@pytest.mark.asyncio
async def test_runner_classifies_unparseable_answer_after_valid_route(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(
        _drive_tools(),
        {
            'drive_get_file': {
                'file_id': applied_bindings.objects[
                    'drive_ledger_file'
                ].identifiers.file_id,
                'name': 'Synthetic ledger teal-harbor-n5s8.csv',
                'mime_type': 'text/csv',
            }
        },
    )
    runner = EvaluationRunner(
        RecordingModel(
            [
                _tool_turn(
                    ToolCall(
                        'call-1',
                        'drive_get_file',
                        {'file_id': 'drive_ledger_file'},
                    )
                ),
                _final_turn('one'),
            ]
        ),
        RecordingSessionFactory(session),
        applied_bindings,
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'wrong_answer'
    assert result.error_category == 'answer_normalization'
    assert result.mcp_calls == 1


@pytest.mark.asyncio
async def test_runner_enforces_pair_turn_limit(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(
        _drive_tools(),
        {
            'drive_get_file': {
                'file_id': applied_bindings.objects[
                    'drive_ledger_file'
                ].identifiers.file_id,
                'name': 'Synthetic ledger teal-harbor-n5s8.csv',
                'mime_type': 'text/csv',
            }
        },
    )
    runner = EvaluationRunner(
        RecordingModel(
            [
                _tool_turn(
                    ToolCall(
                        'call-1',
                        'drive_get_file',
                        {'file_id': 'drive_ledger_file'},
                    )
                )
            ]
        ),
        RecordingSessionFactory(session),
        applied_bindings,
        limits=RunLimits(max_model_turns_per_pair=1),
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'limit_model_turns'
    assert len(runner.model.requests) == 1


@pytest.mark.asyncio
async def test_runner_checks_input_budget_before_paid_call(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    model = RecordingModel([_final_turn('1')], input_tokens=101)
    runner = EvaluationRunner(
        model,
        RecordingSessionFactory(session),
        applied_bindings,
        limits=RunLimits(max_input_tokens_per_run=100),
    )

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
    )

    assert result.status == 'limit_run_budget'
    assert model.requests == []


@pytest.mark.asyncio
async def test_runner_reserves_full_output_capacity_before_paid_call(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    model = RecordingModel([_final_turn('1')])
    runner = EvaluationRunner(
        model,
        RecordingSessionFactory(session),
        applied_bindings,
    )
    budget = RunBudget(output_tokens=819_199)

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        budget,
    )

    assert result.status == 'limit_run_budget'
    assert model.count_requests == 0
    assert model.requests == []


@pytest.mark.asyncio
async def test_runner_rejects_an_expired_run_deadline(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    factory = RecordingSessionFactory(session)
    model = RecordingModel([_final_turn('1')])
    runner = EvaluationRunner(model, factory, applied_bindings)

    result = await runner.run_pair(
        ServiceName.DRIVE,
        _pair(),
        RunBudget(),
        run_deadline=time.monotonic() - 1,
    )

    assert result.status == 'limit_run_budget'
    assert factory.opened == []
    assert model.requests == []


# === Run orchestration ===


@pytest.mark.asyncio
async def test_run_timeout_covers_preflight(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    runner = EvaluationRunner(
        RecordingModel([_final_turn('OK')]),
        RecordingSessionFactory(session),
        applied_bindings,
        limits=RunLimits(max_run_seconds=0),
    )
    catalog = EvaluationCatalog(
        fixture_version='stage12-v1',
        service=ServiceName.DRIVE,
        pairs=(_pair(),),
    )

    with pytest.raises(EvaluationRunError, match='run preflight timed out'):
        await runner.execute((catalog,))


@pytest.mark.asyncio
async def test_registry_failure_precedes_model_preflight(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(
        (*_drive_tools(), _tool('drive_copy_file')),
        {},
    )
    model = RecordingModel([_final_turn('OK')])
    runner = EvaluationRunner(
        model,
        RecordingSessionFactory(session),
        applied_bindings,
    )
    catalog = EvaluationCatalog(
        fixture_version='stage12-v1',
        service=ServiceName.DRIVE,
        pairs=(_pair(),),
    )

    with pytest.raises(
        EvaluationRunError,
        match='MCP registry preflight failed',
    ):
        await runner.execute((catalog,))

    assert model.count_requests == 0
    assert model.requests == []


@pytest.mark.asyncio
async def test_run_uses_a_fresh_session_for_each_pair(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    factory = RecordingSessionFactory(session)
    runner = EvaluationRunner(
        RecordingModel([_final_turn('1'), _final_turn('1')]),
        factory,
        applied_bindings,
    )
    first = _pair()
    second = EvaluationPair(
        task_id='drive_02',
        question=first.question,
        expected_answer=first.expected_answer,
        normalizer=first.normalizer,
        fixture_refs=first.fixture_refs,
        allowed_tools=first.allowed_tools,
        minimum_mcp_calls=first.minimum_mcp_calls,
    )
    catalog = EvaluationCatalog(
        fixture_version='stage12-v1',
        service=ServiceName.DRIVE,
        pairs=(first, second),
    )

    results = await runner.run_catalogs((catalog,), preflight=False)

    assert len(results) == 2
    assert factory.opened == [ServiceName.DRIVE, ServiceName.DRIVE]


@pytest.mark.asyncio
async def test_run_stops_external_calls_after_run_budget(
    applied_bindings: FixtureBindings,
) -> None:
    session = RecordingSession(_drive_tools(), {})
    factory = RecordingSessionFactory(session)
    model = RecordingModel(
        [_final_turn('1')],
        input_tokens=101,
    )
    runner = EvaluationRunner(
        model,
        factory,
        applied_bindings,
        limits=RunLimits(max_input_tokens_per_run=100),
    )
    first = _pair()
    second = EvaluationPair(
        task_id='drive_02',
        question=first.question,
        expected_answer=first.expected_answer,
        normalizer=first.normalizer,
        fixture_refs=first.fixture_refs,
        allowed_tools=first.allowed_tools,
        minimum_mcp_calls=first.minimum_mcp_calls,
    )
    catalog = EvaluationCatalog(
        fixture_version='stage12-v1',
        service=ServiceName.DRIVE,
        pairs=(first, second),
    )

    results = await runner.run_catalogs((catalog,), preflight=False)

    assert [result.status for result in results] == [
        'limit_run_budget',
        'limit_run_budget',
    ]
    assert factory.opened == [ServiceName.DRIVE]
    assert model.count_requests == 1

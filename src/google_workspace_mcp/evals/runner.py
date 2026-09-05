"""Run isolated MCP evaluations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from .models import FixtureBindings, ServiceName
from .normalizers import (
    AnswerNormalizationError,
    NormalizerName,
    normalize_answer,
)
from .routing import (
    RouteValidationError,
    project_tool_result,
    resolve_tool_arguments,
)
from .validation import (
    READONLY_TOOLS,
    EvaluationCatalog,
    EvaluationPair,
)

# === Constants ===

MODEL_NAME = 'deepseek-v4-pro'
SYSTEM_PROMPT = (
    'Solve the task only with the provided tools. '
    'Identifier arguments must use the exact public logical references '
    'named in the task. Use calendar_primary for the primary calendar '
    'identifier. Never invent or request provider IDs. Return only the '
    'single value requested by the task with no explanation.'
)


class EvaluationRunError(RuntimeError):
    """Report a failed run preflight."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Describe one model tool."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Describe one model tool call."""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelTurn:
    """Describe one model response."""

    stop_reason: str | None
    assistant_content: tuple[dict[str, Any], ...]
    tool_calls: tuple[ToolCall, ...]
    final_text: str | None
    output_tokens: int


class ModelGateway(Protocol):
    """Define the model client contract."""

    model_name: str
    version: str

    async def count_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: tuple[ToolSpec, ...],
    ) -> int:
        """Count one model input."""
        ...

    async def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: tuple[ToolSpec, ...],
        *,
        max_tokens: int,
    ) -> ModelTurn:
        """Create one model response."""
        ...


class ToolSession(Protocol):
    """Define one MCP session contract."""

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        """List session tools."""
        ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> object:
        """Call one session tool."""
        ...


SessionFactory = Callable[
    [ServiceName],
    AbstractAsyncContextManager[ToolSession],
]


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Bound one evaluation run."""

    max_tokens_per_turn: int = 2048
    max_model_turns_per_pair: int = 8
    max_mcp_calls_per_pair: int = 12
    max_pair_seconds: float = 180.0
    max_model_turns_per_run: int = 400
    max_preflight_model_calls: int = 1
    max_mcp_calls_per_run: int = 600
    max_run_seconds: float = 9000.0
    max_input_tokens_per_run: int = 5_000_000
    max_output_tokens_per_run: int = 819_200


@dataclass(slots=True)
class RunBudget:
    """Track aggregate run usage."""

    model_turns: int = 0
    preflight_model_calls: int = 0
    mcp_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class PairResult:
    """Store one sanitized terminal result."""

    task_id: str
    status: str
    called_tools: tuple[str, ...]
    model_turns: int
    mcp_calls: int
    input_tokens: int
    output_tokens: int
    expected_sha256: str
    actual_sha256: str | None
    error_category: str | None


@dataclass(frozen=True, slots=True)
class RunExecution:
    """Store one complete run outcome."""

    results: tuple[PairResult, ...]
    budget: RunBudget
    registries: dict[ServiceName, tuple[str, ...]]


class _LimitReached(Exception):
    """Carry one terminal limit status."""

    def __init__(self, status: str) -> None:
        """Store one limit status."""
        self.status = status


class _ModelFailure(Exception):
    """Mark one sanitized model failure."""


class _McpFailure(Exception):
    """Mark one sanitized MCP failure."""


def _digest(value: str) -> str:
    """Hash one normalized answer."""
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _enum_values(pair: EvaluationPair) -> tuple[str, ...] | None:
    """Return closed values for enum pairs."""
    if pair.normalizer is NormalizerName.ENUM:
        return (pair.expected_answer,)
    return None


class EvaluationRunner:
    """Run isolated evaluation pairs."""

    def __init__(
        self,
        model: ModelGateway,
        session_factory: SessionFactory,
        bindings: FixtureBindings,
        *,
        limits: RunLimits | None = None,
    ) -> None:
        """Configure one evaluation runner."""
        self.model = model
        self._session_factory = session_factory
        self._bindings = bindings
        self._limits = limits or RunLimits()

    def _result(
        self,
        pair: EvaluationPair,
        *,
        status: str,
        called_tools: list[str],
        model_turns: int,
        mcp_calls: int,
        input_tokens: int,
        output_tokens: int,
        actual: str | None = None,
        error_category: str | None = None,
    ) -> PairResult:
        """Build one sanitized pair result."""
        return PairResult(
            task_id=pair.task_id,
            status=status,
            called_tools=tuple(called_tools),
            model_turns=model_turns,
            mcp_calls=mcp_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            expected_sha256=_digest(pair.expected_answer),
            actual_sha256=_digest(actual) if actual is not None else None,
            error_category=error_category,
        )

    def _reserve_model_turn(
        self,
        budget: RunBudget,
        pair_turns: int,
    ) -> None:
        """Reserve one paid model turn."""
        if pair_turns >= self._limits.max_model_turns_per_pair:
            raise _LimitReached('limit_model_turns')
        if budget.model_turns >= self._limits.max_model_turns_per_run:
            raise _LimitReached('limit_run_budget')
        budget.model_turns += 1

    def _require_model_turn_capacity(
        self,
        budget: RunBudget,
        pair_turns: int,
    ) -> None:
        """Reject exhausted model turn budgets."""
        if pair_turns >= self._limits.max_model_turns_per_pair:
            raise _LimitReached('limit_model_turns')
        if budget.model_turns >= self._limits.max_model_turns_per_run:
            raise _LimitReached('limit_run_budget')

    def _reserve_input(self, budget: RunBudget, tokens: int) -> None:
        """Reserve one counted model input."""
        if tokens < 0 or (
            budget.input_tokens + tokens
            > self._limits.max_input_tokens_per_run
        ):
            raise _LimitReached('limit_run_budget')
        budget.input_tokens += tokens

    def _record_output(self, budget: RunBudget, tokens: int) -> None:
        """Record one model output."""
        if tokens < 0:
            raise _LimitReached('limit_run_budget')
        budget.output_tokens += tokens
        if budget.output_tokens > self._limits.max_output_tokens_per_run:
            raise _LimitReached('limit_run_budget')

    def _require_output_capacity(
        self,
        budget: RunBudget,
        max_tokens: int,
    ) -> None:
        """Require room for one fixed model response."""
        if (
            max_tokens <= 0
            or budget.output_tokens + max_tokens
            > self._limits.max_output_tokens_per_run
        ):
            raise _LimitReached('limit_run_budget')

    def _reserve_mcp_call(
        self,
        budget: RunBudget,
        pair_calls: int,
    ) -> None:
        """Reserve one MCP tool call."""
        if pair_calls >= self._limits.max_mcp_calls_per_pair:
            raise _LimitReached('limit_mcp_calls')
        if budget.mcp_calls >= self._limits.max_mcp_calls_per_run:
            raise _LimitReached('limit_run_budget')
        budget.mcp_calls += 1

    @staticmethod
    def _allowed_specs(
        service: ServiceName,
        pair: EvaluationPair,
        available: tuple[ToolSpec, ...],
    ) -> tuple[ToolSpec, ...]:
        """Select exact pair tools."""
        names = tuple(tool.name for tool in available)
        if (
            len(names) != len(READONLY_TOOLS[service])
            or set(names) != READONLY_TOOLS[service]
        ):
            raise RouteValidationError('session tool registry is not exact')
        by_name = {tool.name: tool for tool in available}
        if not set(pair.allowed_tools) <= set(by_name):
            raise RouteValidationError(
                'allowed tool is absent from the session'
            )
        return tuple(by_name[name] for name in pair.allowed_tools)

    async def run_pair(
        self,
        service: ServiceName,
        pair: EvaluationPair,
        budget: RunBudget,
        *,
        run_deadline: float | None = None,
    ) -> PairResult:
        """Run one pair in one fresh MCP session."""
        started_input = budget.input_tokens
        started_output = budget.output_tokens
        called_tools: list[str] = []
        pair_turns = 0
        pair_calls = 0
        page_tokens: dict[str, str] = {}
        page_token_aliases: dict[str, str] = {}
        pair_timeout = self._limits.max_pair_seconds
        run_timeout_applies = False
        if run_deadline is not None:
            remaining = run_deadline - time.monotonic()
            if remaining <= 0:
                return self._result(
                    pair,
                    status='limit_run_budget',
                    called_tools=[],
                    model_turns=0,
                    mcp_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                    error_category='limit_run_budget',
                )
            if remaining < pair_timeout:
                pair_timeout = remaining
                run_timeout_applies = True
        try:
            async with asyncio.timeout(pair_timeout):
                async with self._session_factory(service) as session:
                    try:
                        available = await session.list_tools()
                    except Exception as error:
                        raise _McpFailure from error
                    tools = self._allowed_specs(service, pair, available)
                    messages: list[dict[str, Any]] = [
                        {'role': 'user', 'content': pair.question}
                    ]
                    while True:
                        self._require_model_turn_capacity(
                            budget,
                            pair_turns,
                        )
                        self._require_output_capacity(
                            budget,
                            self._limits.max_tokens_per_turn,
                        )
                        try:
                            input_tokens = await self.model.count_tokens(
                                messages,
                                tools,
                            )
                        except Exception as error:
                            raise _ModelFailure from error
                        self._reserve_input(budget, input_tokens)
                        self._reserve_model_turn(budget, pair_turns)
                        pair_turns += 1
                        try:
                            turn = await self.model.create_message(
                                messages,
                                tools,
                                max_tokens=self._limits.max_tokens_per_turn,
                            )
                        except Exception as error:
                            raise _ModelFailure from error
                        self._record_output(budget, turn.output_tokens)
                        if turn.stop_reason == 'max_tokens':
                            raise _LimitReached('limit_max_tokens')
                        if turn.tool_calls:
                            tool_results: list[dict[str, Any]] = []
                            for call in turn.tool_calls:
                                arguments = resolve_tool_arguments(
                                    pair,
                                    call.name,
                                    call.arguments,
                                    self._bindings,
                                    allowed_page_tokens=page_tokens,
                                )
                                self._reserve_mcp_call(budget, pair_calls)
                                pair_calls += 1
                                called_tools.append(call.name)
                                try:
                                    structured = await session.call_tool(
                                        call.name,
                                        arguments,
                                    )
                                except Exception as error:
                                    raise _McpFailure from error
                                if isinstance(structured, Mapping):
                                    next_page_token = structured.get(
                                        'next_page_token'
                                    )
                                    if isinstance(next_page_token, str):
                                        alias = page_token_aliases.get(
                                            next_page_token
                                        )
                                        if alias is None:
                                            alias = (
                                                f'page_{len(page_tokens) + 1}'
                                            )
                                            page_tokens[alias] = (
                                                next_page_token
                                            )
                                            page_token_aliases[
                                                next_page_token
                                            ] = alias
                                projected = project_tool_result(
                                    pair,
                                    call.name,
                                    structured,
                                    self._bindings,
                                    page_token_aliases=page_token_aliases,
                                )
                                tool_results.append(
                                    {
                                        'type': 'tool_result',
                                        'tool_use_id': call.call_id,
                                        'content': json.dumps(
                                            projected,
                                            ensure_ascii=True,
                                            sort_keys=True,
                                        ),
                                    }
                                )
                            messages.extend(
                                [
                                    {
                                        'role': 'assistant',
                                        'content': list(
                                            turn.assistant_content
                                        ),
                                    },
                                    {'role': 'user', 'content': tool_results},
                                ]
                            )
                            continue
                        if turn.stop_reason == 'tool_use':
                            return self._result(
                                pair,
                                status='model_error',
                                called_tools=called_tools,
                                model_turns=pair_turns,
                                mcp_calls=pair_calls,
                                input_tokens=budget.input_tokens
                                - started_input,
                                output_tokens=budget.output_tokens
                                - started_output,
                                error_category='missing_tool_use',
                            )
                        if turn.stop_reason != 'end_turn':
                            return self._result(
                                pair,
                                status='model_error',
                                called_tools=called_tools,
                                model_turns=pair_turns,
                                mcp_calls=pair_calls,
                                input_tokens=budget.input_tokens
                                - started_input,
                                output_tokens=budget.output_tokens
                                - started_output,
                                error_category='model_stop',
                            )
                        if turn.final_text is None:
                            return self._result(
                                pair,
                                status='model_error',
                                called_tools=called_tools,
                                model_turns=pair_turns,
                                mcp_calls=pair_calls,
                                input_tokens=budget.input_tokens
                                - started_input,
                                output_tokens=budget.output_tokens
                                - started_output,
                                error_category='missing_answer',
                            )
                        normalized = normalize_answer(
                            turn.final_text,
                            pair.normalizer,
                            enum_values=_enum_values(pair),
                        )
                        if pair_calls < pair.minimum_mcp_calls:
                            return self._result(
                                pair,
                                status='failed_route',
                                called_tools=called_tools,
                                model_turns=pair_turns,
                                mcp_calls=pair_calls,
                                input_tokens=budget.input_tokens
                                - started_input,
                                output_tokens=budget.output_tokens
                                - started_output,
                                actual=normalized,
                                error_category='minimum_mcp_calls',
                            )
                        if set(called_tools) != set(pair.allowed_tools):
                            return self._result(
                                pair,
                                status='failed_route',
                                called_tools=called_tools,
                                model_turns=pair_turns,
                                mcp_calls=pair_calls,
                                input_tokens=budget.input_tokens
                                - started_input,
                                output_tokens=budget.output_tokens
                                - started_output,
                                actual=normalized,
                                error_category='required_tools',
                            )
                        status = (
                            'passed'
                            if normalized == pair.expected_answer
                            else 'wrong_answer'
                        )
                        return self._result(
                            pair,
                            status=status,
                            called_tools=called_tools,
                            model_turns=pair_turns,
                            mcp_calls=pair_calls,
                            input_tokens=budget.input_tokens - started_input,
                            output_tokens=budget.output_tokens
                            - started_output,
                            actual=normalized,
                            error_category=(
                                None
                                if status == 'passed'
                                else 'answer_mismatch'
                            ),
                        )
        except _LimitReached as error:
            return self._result(
                pair,
                status=error.status,
                called_tools=called_tools,
                model_turns=pair_turns,
                mcp_calls=pair_calls,
                input_tokens=budget.input_tokens - started_input,
                output_tokens=budget.output_tokens - started_output,
                error_category=error.status,
            )
        except TimeoutError:
            timeout_status = (
                'limit_run_budget'
                if run_timeout_applies
                else 'limit_pair_timeout'
            )
            return self._result(
                pair,
                status=timeout_status,
                called_tools=called_tools,
                model_turns=pair_turns,
                mcp_calls=pair_calls,
                input_tokens=budget.input_tokens - started_input,
                output_tokens=budget.output_tokens - started_output,
                error_category=timeout_status,
            )
        except RouteValidationError:
            return self._result(
                pair,
                status='failed_route',
                called_tools=called_tools,
                model_turns=pair_turns,
                mcp_calls=pair_calls,
                input_tokens=budget.input_tokens - started_input,
                output_tokens=budget.output_tokens - started_output,
                error_category='route_validation',
            )
        except AnswerNormalizationError:
            return self._result(
                pair,
                status='wrong_answer',
                called_tools=called_tools,
                model_turns=pair_turns,
                mcp_calls=pair_calls,
                input_tokens=budget.input_tokens - started_input,
                output_tokens=budget.output_tokens - started_output,
                error_category='answer_normalization',
            )
        except _ModelFailure:
            return self._result(
                pair,
                status='model_error',
                called_tools=called_tools,
                model_turns=pair_turns,
                mcp_calls=pair_calls,
                input_tokens=budget.input_tokens - started_input,
                output_tokens=budget.output_tokens - started_output,
                error_category='model_api',
            )
        except _McpFailure:
            return self._result(
                pair,
                status='mcp_error',
                called_tools=called_tools,
                model_turns=pair_turns,
                mcp_calls=pair_calls,
                input_tokens=budget.input_tokens - started_input,
                output_tokens=budget.output_tokens - started_output,
                error_category='mcp_call',
            )
        except Exception:
            return self._result(
                pair,
                status='execution_error',
                called_tools=called_tools,
                model_turns=pair_turns,
                mcp_calls=pair_calls,
                input_tokens=budget.input_tokens - started_input,
                output_tokens=budget.output_tokens - started_output,
                error_category='execution_error',
            )

    async def preflight_model(self, budget: RunBudget) -> None:
        """Verify model access before MCP calls."""
        messages: list[dict[str, Any]] = [
            {'role': 'user', 'content': 'Reply with OK.'}
        ]
        try:
            self._require_output_capacity(budget, 1)
            input_tokens = await self.model.count_tokens(messages, ())
            self._reserve_input(budget, input_tokens)
            if (
                budget.preflight_model_calls
                >= self._limits.max_preflight_model_calls
            ):
                raise _LimitReached('limit_run_budget')
            turn = await self.model.create_message(
                messages,
                (),
                max_tokens=1,
            )
            budget.preflight_model_calls += 1
            self._record_output(budget, turn.output_tokens)
        except Exception as error:
            raise EvaluationRunError('model preflight failed') from error

    async def preflight_registries(
        self,
        services: tuple[ServiceName, ...],
    ) -> dict[ServiceName, tuple[str, ...]]:
        """Verify exact readonly registries."""
        return await preflight_tool_registries(
            self._session_factory,
            services,
        )

    async def run_catalogs(
        self,
        catalogs: tuple[EvaluationCatalog, ...],
        *,
        preflight: bool = True,
    ) -> tuple[PairResult, ...]:
        """Run catalogs in fixed order."""
        execution = await self._execute(catalogs, preflight=preflight)
        return execution.results

    async def execute(
        self,
        catalogs: tuple[EvaluationCatalog, ...],
    ) -> RunExecution:
        """Execute one complete preflighted run."""
        return await self._execute(catalogs, preflight=True)

    async def _execute(
        self,
        catalogs: tuple[EvaluationCatalog, ...],
        *,
        preflight: bool,
    ) -> RunExecution:
        """Run catalogs and retain aggregate state."""
        budget = RunBudget()
        services = tuple(catalog.service for catalog in catalogs)
        registries: dict[ServiceName, tuple[str, ...]] = {}
        started = time.monotonic()
        run_deadline = started + self._limits.max_run_seconds
        if preflight:
            try:
                if time.monotonic() >= run_deadline:
                    raise TimeoutError
                async with asyncio.timeout(
                    max(0.0, run_deadline - time.monotonic())
                ):
                    registries = await self.preflight_registries(services)
                    if time.monotonic() >= run_deadline:
                        raise TimeoutError
                    await self.preflight_model(budget)
                    if time.monotonic() >= run_deadline:
                        raise TimeoutError
            except TimeoutError as error:
                raise EvaluationRunError('run preflight timed out') from error
        results: list[PairResult] = []
        run_budget_exhausted = False
        for catalog in catalogs:
            for pair in catalog.pairs:
                if run_budget_exhausted or (
                    time.monotonic() - started > self._limits.max_run_seconds
                ):
                    results.append(
                        self._result(
                            pair,
                            status='limit_run_budget',
                            called_tools=[],
                            model_turns=0,
                            mcp_calls=0,
                            input_tokens=0,
                            output_tokens=0,
                            error_category='limit_run_budget',
                        )
                    )
                    continue
                result = await self.run_pair(
                    catalog.service,
                    pair,
                    budget,
                    run_deadline=run_deadline,
                )
                results.append(result)
                if result.status == 'limit_run_budget':
                    run_budget_exhausted = True
        return RunExecution(
            results=tuple(results),
            budget=budget,
            registries=registries,
        )


async def preflight_tool_registries(
    session_factory: SessionFactory,
    services: tuple[ServiceName, ...],
) -> dict[ServiceName, tuple[str, ...]]:
    """Verify exact readonly registries."""
    registries: dict[ServiceName, tuple[str, ...]] = {}
    for service in services:
        try:
            async with session_factory(service) as session:
                tools = await session.list_tools()
        except Exception as error:
            raise EvaluationRunError(
                'MCP registry preflight failed'
            ) from error
        names = tuple(sorted(tool.name for tool in tools))
        if (
            len(names) != len(READONLY_TOOLS[service])
            or set(names) != READONLY_TOOLS[service]
        ):
            raise EvaluationRunError('MCP registry preflight failed')
        registries[service] = names
    return registries

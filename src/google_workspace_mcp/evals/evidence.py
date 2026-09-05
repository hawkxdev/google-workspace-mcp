"""Render sanitized evaluation evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from google_workspace_mcp.services.calendar.constants import (
    REQUEST_RETRIES as CALENDAR_REQUEST_RETRIES,
)
from google_workspace_mcp.services.docs.constants import (
    REQUEST_RETRIES as DOCS_REQUEST_RETRIES,
)
from google_workspace_mcp.services.drive.constants import (
    REQUEST_RETRIES as DRIVE_REQUEST_RETRIES,
)
from google_workspace_mcp.services.gmail.constants import (
    REQUEST_RETRIES as GMAIL_REQUEST_RETRIES,
)
from google_workspace_mcp.services.sheets.constants import (
    REQUEST_RETRIES as SHEETS_REQUEST_RETRIES,
)

from .models import FIXTURE_VERSION, ServiceName
from .runner import MODEL_NAME, PairResult, RunLimits
from .validation import READONLY_TOOLS, SERVICE_ORDER

# === Constants ===

RUNNER_VERSION = 'stage12-v1'
SERVICE_GATEWAY_RETRIES = {
    ServiceName.GMAIL: GMAIL_REQUEST_RETRIES,
    ServiceName.CALENDAR: CALENDAR_REQUEST_RETRIES,
    ServiceName.DRIVE: DRIVE_REQUEST_RETRIES,
    ServiceName.SHEETS: SHEETS_REQUEST_RETRIES,
    ServiceName.DOCS: DOCS_REQUEST_RETRIES,
}
TERMINAL_STATUSES = frozenset(
    {
        'passed',
        'wrong_answer',
        'failed_route',
        'model_error',
        'mcp_error',
        'execution_error',
        'limit_max_tokens',
        'limit_model_turns',
        'limit_mcp_calls',
        'limit_pair_timeout',
        'limit_run_budget',
    }
)
ERROR_CATEGORIES = frozenset(
    {
        'answer_mismatch',
        'answer_normalization',
        'execution_error',
        'limit_max_tokens',
        'limit_model_turns',
        'limit_mcp_calls',
        'limit_pair_timeout',
        'limit_run_budget',
        'minimum_mcp_calls',
        'model_api',
        'model_stop',
        'mcp_call',
        'missing_answer',
        'missing_tool_use',
        'route_validation',
        'required_tools',
    }
)
_SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$')


class EvidenceValidationError(ValueError):
    """Report invalid evidence data."""


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    """Store one complete evaluation report."""

    fixture_version: str
    model: str
    model_client_version: str
    mcp_client_version: str
    runner_version: str
    registries: dict[ServiceName, tuple[str, ...]]
    results: tuple[PairResult, ...]
    model_turns: int
    preflight_model_calls: int
    mcp_calls: int
    input_tokens: int
    output_tokens: int
    evaluator_retries: int
    orchestration_retries: int
    service_gateway_retries: dict[ServiceName, int]


def _validate_results(report: EvidenceReport) -> None:
    """Validate terminal result coverage."""
    limits = RunLimits()
    if len(report.results) != 50:
        raise EvidenceValidationError(
            'evidence requires exactly fifty terminal results'
        )
    expected_ids = {
        f'{service.value}_{index:02d}'
        for service in SERVICE_ORDER
        for index in range(1, 11)
    }
    task_ids = [result.task_id for result in report.results]
    if set(task_ids) != expected_ids or len(task_ids) != len(set(task_ids)):
        raise EvidenceValidationError('evidence task ids are invalid')
    for result in report.results:
        if result.status not in TERMINAL_STATUSES:
            raise EvidenceValidationError(
                'evidence contains an unknown terminal status'
            )
        if (
            result.error_category is not None
            and result.error_category not in ERROR_CATEGORIES
        ):
            raise EvidenceValidationError(
                'evidence contains an unknown error category'
            )
        if not _SHA256_PATTERN.fullmatch(result.expected_sha256):
            raise EvidenceValidationError(
                'evidence contains an invalid answer hash'
            )
        if result.actual_sha256 is not None and not _SHA256_PATTERN.fullmatch(
            result.actual_sha256
        ):
            raise EvidenceValidationError(
                'evidence contains an invalid answer hash'
            )
        if result.status == 'passed' and (
            result.actual_sha256 != result.expected_sha256
            or result.error_category is not None
        ):
            raise EvidenceValidationError(
                'evidence contains an inconsistent passed result'
            )
        service = ServiceName(result.task_id.split('_', maxsplit=1)[0])
        if not set(result.called_tools) <= READONLY_TOOLS[service]:
            raise EvidenceValidationError('evidence contains a forbidden tool')
        if result.mcp_calls != len(result.called_tools):
            raise EvidenceValidationError(
                'evidence tool counts are inconsistent'
            )
        if (
            result.model_turns > limits.max_model_turns_per_pair
            or result.mcp_calls > limits.max_mcp_calls_per_pair
            or result.input_tokens < 0
            or result.output_tokens < 0
        ):
            raise EvidenceValidationError(
                'evidence contains an exceeded pair budget'
            )


def _validate_registries(report: EvidenceReport) -> None:
    """Validate exact service registries."""
    if set(report.registries) != set(SERVICE_ORDER):
        raise EvidenceValidationError(
            'evidence requires five exact registries'
        )
    for service in SERVICE_ORDER:
        if report.registries[service] != tuple(
            sorted(READONLY_TOOLS[service])
        ):
            raise EvidenceValidationError(
                'evidence requires five exact registries'
            )


def _validate_counters(report: EvidenceReport) -> None:
    """Validate aggregate counters."""
    limits = RunLimits()
    if report.preflight_model_calls != 1:
        raise EvidenceValidationError('evidence preflight count is invalid')
    if (
        report.model_turns > limits.max_model_turns_per_run
        or report.mcp_calls > limits.max_mcp_calls_per_run
        or report.input_tokens > limits.max_input_tokens_per_run
        or report.output_tokens > limits.max_output_tokens_per_run
        or min(
            report.model_turns,
            report.mcp_calls,
            report.input_tokens,
            report.output_tokens,
        )
        < 0
    ):
        raise EvidenceValidationError(
            'evidence contains an exceeded run budget'
        )
    if report.model_turns != sum(item.model_turns for item in report.results):
        raise EvidenceValidationError(
            'evidence model turn count is inconsistent'
        )
    if report.mcp_calls != sum(item.mcp_calls for item in report.results):
        raise EvidenceValidationError(
            'evidence MCP call count is inconsistent'
        )
    if report.input_tokens < sum(item.input_tokens for item in report.results):
        raise EvidenceValidationError(
            'evidence input token count is inconsistent'
        )
    if report.output_tokens < sum(
        item.output_tokens for item in report.results
    ):
        raise EvidenceValidationError(
            'evidence output token count is inconsistent'
        )
    if report.evaluator_retries != 0 or report.orchestration_retries != 0:
        raise EvidenceValidationError('evidence retry count is invalid')
    if report.service_gateway_retries != SERVICE_GATEWAY_RETRIES:
        raise EvidenceValidationError(
            'evidence gateway retry counts are invalid'
        )


def _validate_report(report: EvidenceReport) -> None:
    """Validate one complete report."""
    if report.fixture_version != FIXTURE_VERSION:
        raise EvidenceValidationError('evidence fixture version is invalid')
    if report.model != MODEL_NAME:
        raise EvidenceValidationError('evidence model is invalid')
    if report.runner_version != RUNNER_VERSION:
        raise EvidenceValidationError('evidence runner version is invalid')
    if not report.model_client_version or not report.mcp_client_version:
        raise EvidenceValidationError('evidence client versions are missing')
    _validate_registries(report)
    _validate_results(report)
    _validate_counters(report)


def render_evidence(
    report: EvidenceReport,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> str:
    """Render one minimal evidence document."""
    _validate_report(report)
    result_header = (
        '| '
        + ' | '.join(
            (
                'Task',
                'Status',
                'Tools',
                'Model turns',
                'MCP calls',
                'Input tokens',
                'Output tokens',
                'Expected SHA256',
                'Actual SHA256',
                'Error',
            )
        )
        + ' |'
    )
    lines = [
        '# Stage 12 evaluation evidence',
        '',
        '## Versions',
        '',
        f'- Fixture: `{report.fixture_version}`',
        f'- Runner: `{report.runner_version}`',
        f'- Model: `{report.model}`',
        f'- Model client: `{report.model_client_version}`',
        f'- MCP client: `{report.mcp_client_version}`',
        '',
        '## Registries',
        '',
        '| Service | Tools |',
        '|---|---|',
    ]
    for service in SERVICE_ORDER:
        tools = ', '.join(f'`{name}`' for name in report.registries[service])
        lines.append(f'| {service.value} | {tools} |')
    lines.extend(
        [
            '',
            '## Results',
            '',
            result_header,
            '|---|---|---|---:|---:|---:|---:|---|---|---|',
        ]
    )
    for result in report.results:
        lines.append(
            '| '
            + ' | '.join(
                (
                    result.task_id,
                    result.status,
                    ', '.join(result.called_tools),
                    str(result.model_turns),
                    str(result.mcp_calls),
                    str(result.input_tokens),
                    str(result.output_tokens),
                    result.expected_sha256,
                    result.actual_sha256 or '',
                    result.error_category or '',
                )
            )
            + ' |'
        )
    lines.extend(
        [
            '',
            '## Budgets and retries',
            '',
            f'- Model turns: `{report.model_turns}`',
            f'- Preflight model calls: `{report.preflight_model_calls}`',
            f'- MCP calls: `{report.mcp_calls}`',
            f'- Input tokens: `{report.input_tokens}`',
            f'- Output tokens: `{report.output_tokens}`',
            f'- Evaluator retries: `{report.evaluator_retries}`',
            f'- Orchestration retries: `{report.orchestration_retries}`',
            '- Service gateway retries: '
            '`gmail=2 calendar=2 drive=2 sheets=3 docs=3`',
            '',
        ]
    )
    rendered = '\n'.join(lines)
    if any(value and value in rendered for value in forbidden_values):
        raise EvidenceValidationError('evidence contains a forbidden value')
    return rendered


def write_evidence(
    path: Path,
    report: EvidenceReport,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> None:
    """Write one validated evidence document."""
    path.write_text(
        render_evidence(report, forbidden_values=forbidden_values),
        encoding='utf-8',
    )

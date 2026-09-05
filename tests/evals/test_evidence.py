"""Test sanitized evaluation evidence."""

from __future__ import annotations

import hashlib
from dataclasses import fields as dataclass_fields
from dataclasses import replace

import pytest

from google_workspace_mcp.evals.evidence import (
    TERMINAL_STATUSES,
    EvidenceReport,
    EvidenceValidationError,
    render_evidence,
)
from google_workspace_mcp.evals.models import ServiceName
from google_workspace_mcp.evals.runner import PairResult
from google_workspace_mcp.evals.validation import READONLY_TOOLS

# === Helpers ===


def _results() -> tuple[PairResult, ...]:
    """Build fifty sanitized results."""
    results = []
    for service in ServiceName:
        for index in range(1, 11):
            results.append(
                PairResult(
                    task_id=f'{service.value}_{index:02d}',
                    status='passed',
                    called_tools=(sorted(READONLY_TOOLS[service])[0],),
                    model_turns=2,
                    mcp_calls=1,
                    input_tokens=100,
                    output_tokens=10,
                    expected_sha256='a' * 64,
                    actual_sha256='a' * 64,
                    error_category=None,
                )
            )
    return tuple(results)


def _report() -> EvidenceReport:
    """Build one complete evidence report."""
    return EvidenceReport(
        fixture_version='stage12-v1',
        model='deepseek-v4-pro',
        model_client_version='1.3.0',
        mcp_client_version='2.0.0',
        runner_version='stage12-v1',
        registries={
            service: tuple(sorted(READONLY_TOOLS[service]))
            for service in ServiceName
        },
        results=_results(),
        model_turns=100,
        preflight_model_calls=1,
        mcp_calls=50,
        input_tokens=5000,
        output_tokens=500,
        evaluator_retries=0,
        orchestration_retries=0,
        service_gateway_retries={
            ServiceName.GMAIL: 2,
            ServiceName.CALENDAR: 2,
            ServiceName.DRIVE: 2,
            ServiceName.SHEETS: 3,
            ServiceName.DOCS: 3,
        },
    )


# === Evidence contract ===


def test_evidence_contains_only_sanitized_fields() -> None:
    rendered = render_evidence(_report())

    assert 'gmail_01' in rendered
    assert 'deepseek-v4-pro' in rendered
    assert 'a' * 64 in rendered
    assert 'Expected answer' not in rendered
    assert 'Question' not in rendered
    assert 'Tool input' not in rendered
    assert 'https://' not in rendered


def test_evidence_requires_fifty_unique_results() -> None:
    with pytest.raises(
        EvidenceValidationError,
        match='exactly fifty terminal results',
    ):
        render_evidence(replace(_report(), results=_results()[:-1]))


def test_evidence_requires_five_exact_registries() -> None:
    report = _report()
    registries = dict(report.registries)
    registries.pop(ServiceName.DOCS)

    with pytest.raises(
        EvidenceValidationError,
        match='five exact registries',
    ):
        render_evidence(replace(report, registries=registries))


def test_evidence_rejects_duplicate_registry_tools() -> None:
    report = _report()
    registries = dict(report.registries)
    registries[ServiceName.DRIVE] = (
        *registries[ServiceName.DRIVE],
        registries[ServiceName.DRIVE][0],
    )

    with pytest.raises(
        EvidenceValidationError,
        match='five exact registries',
    ):
        render_evidence(replace(report, registries=registries))


def test_evidence_rejects_exceeded_pair_budget() -> None:
    results = list(_results())
    results[0] = replace(results[0], model_turns=9)
    report = replace(
        _report(),
        results=tuple(results),
        model_turns=107,
    )

    with pytest.raises(
        EvidenceValidationError,
        match='exceeded pair budget',
    ):
        render_evidence(report)


def test_evidence_rejects_exceeded_run_budget() -> None:
    with pytest.raises(
        EvidenceValidationError,
        match='exceeded run budget',
    ):
        render_evidence(replace(_report(), output_tokens=819_201))


def test_evidence_rejects_a_control_secret() -> None:
    secret = 'control-secret-6e951b'

    with pytest.raises(
        EvidenceValidationError,
        match='evidence contains a forbidden value',
    ):
        render_evidence(
            replace(_report(), model_client_version=secret),
            forbidden_values=(secret,),
        )


def test_evidence_rejects_unknown_status_and_error() -> None:
    results = list(_results())
    results[0] = replace(
        results[0],
        status='unknown',
        error_category='private raw exception',
    )

    with pytest.raises(
        EvidenceValidationError,
        match='unknown terminal status',
    ):
        render_evidence(replace(_report(), results=tuple(results)))


def test_evidence_rejects_inconsistent_passed_result() -> None:
    results = list(_results())
    results[0] = replace(results[0], actual_sha256='b' * 64)

    with pytest.raises(
        EvidenceValidationError,
        match='inconsistent passed result',
    ):
        render_evidence(replace(_report(), results=tuple(results)))


def test_evidence_models_expose_only_approved_fields() -> None:
    assert {field.name for field in dataclass_fields(PairResult)} == {
        'task_id',
        'status',
        'called_tools',
        'model_turns',
        'mcp_calls',
        'input_tokens',
        'output_tokens',
        'expected_sha256',
        'actual_sha256',
        'error_category',
    }
    assert {field.name for field in dataclass_fields(EvidenceReport)} == {
        'fixture_version',
        'model',
        'model_client_version',
        'mcp_client_version',
        'runner_version',
        'registries',
        'results',
        'model_turns',
        'preflight_model_calls',
        'mcp_calls',
        'input_tokens',
        'output_tokens',
        'evaluator_retries',
        'orchestration_retries',
        'service_gateway_retries',
    }


@pytest.mark.parametrize('status', sorted(TERMINAL_STATUSES))
def test_every_terminal_status_excludes_raw_control_data(status: str) -> None:
    secret = 'private-control-value-a91f42'
    error_categories = {
        'wrong_answer': 'answer_mismatch',
        'failed_route': 'route_validation',
        'model_error': 'model_api',
        'mcp_error': 'mcp_call',
        'execution_error': 'execution_error',
        'limit_max_tokens': 'limit_max_tokens',
        'limit_model_turns': 'limit_model_turns',
        'limit_mcp_calls': 'limit_mcp_calls',
        'limit_pair_timeout': 'limit_pair_timeout',
        'limit_run_budget': 'limit_run_budget',
    }
    results = list(_results())
    replacement = replace(
        results[0],
        status=status,
        actual_sha256=(
            results[0].expected_sha256
            if status == 'passed'
            else hashlib.sha256(secret.encode('utf-8')).hexdigest()
        ),
        error_category=error_categories.get(status),
    )
    results[0] = replacement

    rendered = render_evidence(
        replace(_report(), results=tuple(results)),
        forbidden_values=(secret,),
    )

    assert secret not in rendered

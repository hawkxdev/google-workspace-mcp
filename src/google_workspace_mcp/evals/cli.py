"""Run evaluation fixture commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .apply import (
    apply_fixture,
    build_application_services,
    save_bindings,
)
from .evidence import (
    RUNNER_VERSION,
    SERVICE_GATEWAY_RETRIES,
    EvidenceReport,
    write_evidence,
)
from .models import (
    FIXTURE_VERSION,
    ApplicationConfirmation,
    ServiceName,
    load_bindings,
)
from .oauth_storage import FileTokenStorage
from .preview import build_preview
from .readiness import (
    FixtureReadinessError,
    GoogleReadinessProbe,
    check_readiness,
    mark_bindings_ready,
    require_readiness_state,
    require_ready_for_xml,
)
from .run_state import claim_canonical_run
from .runner import EvaluationRunner, preflight_tool_registries
from .validation import READONLY_TOOLS, load_evaluation_catalogs


def _parser() -> argparse.ArgumentParser:
    """Build fixture command parser."""
    parser = argparse.ArgumentParser(
        prog='python -m google_workspace_mcp.evals'
    )
    subcommands = parser.add_subparsers(dest='command', required=True)
    preview = subcommands.add_parser('preview')
    preview.add_argument('--bindings', type=Path)
    apply = subcommands.add_parser('apply')
    apply.add_argument('--bindings', type=Path, required=True)
    apply.add_argument(
        '--credentials-dir',
        type=Path,
        required=True,
    )
    apply.add_argument(
        '--fixture-version',
        choices=(FIXTURE_VERSION,),
        required=True,
    )
    apply.add_argument('--preview-digest', required=True)
    apply.add_argument(
        '--acknowledge-writes',
        action='store_true',
        required=True,
    )
    readiness = subcommands.add_parser('readiness')
    readiness.add_argument('--bindings', type=Path, required=True)
    readiness.add_argument(
        '--credentials-dir',
        type=Path,
        required=True,
    )
    validate = subcommands.add_parser('validate')
    validate.add_argument('--evals-dir', type=Path, required=True)
    validate.add_argument('--bindings', type=Path)
    authorize = subcommands.add_parser('authorize')
    authorize.add_argument('--bindings', type=Path, required=True)
    authorize.add_argument('--oauth-dir', type=Path, required=True)
    _add_service_urls(authorize)
    run = subcommands.add_parser('run')
    run.add_argument('--evals-dir', type=Path, required=True)
    run.add_argument('--bindings', type=Path, required=True)
    run.add_argument('--oauth-dir', type=Path, required=True)
    run.add_argument('--evidence', type=Path, required=True)
    _add_service_urls(run)
    return parser


def _add_service_urls(parser: argparse.ArgumentParser) -> None:
    """Add five required MCP URLs."""
    for service in ServiceName:
        parser.add_argument(
            f'--{service.value}-url',
            required=True,
        )


def _service_urls(arguments: argparse.Namespace) -> dict[ServiceName, str]:
    """Read five service URLs."""
    return {
        service: getattr(arguments, f'{service.value}_url')
        for service in ServiceName
    }


def _binding_private_values(arguments: argparse.Namespace) -> tuple[str, ...]:
    """Collect private binding values."""
    bindings = load_bindings(arguments.bindings)
    values = {
        str(value)
        for binding in bindings.objects.values()
        for value in binding.identifiers.model_dump().values()
        if isinstance(value, str | int)
        and not isinstance(value, bool)
        and value != ''
    }
    if bindings.owner_email is not None:
        values.add(bindings.owner_email.get_secret_value())
    if bindings.calendar_primary_id is not None:
        values.add(bindings.calendar_primary_id.get_secret_value())
    return tuple(sorted(values))


async def _oauth_private_values(oauth_directory: Path) -> tuple[str, ...]:
    """Collect private OAuth values."""
    values: set[str] = set()
    for service in ServiceName:
        storage = FileTokenStorage(oauth_directory / f'{service.value}.json')
        tokens = await storage.get_tokens()
        client_info = await storage.get_client_info()
        if tokens is not None:
            values.add(tokens.access_token)
            if tokens.refresh_token:
                values.add(tokens.refresh_token)
        if client_info is not None:
            values.add(client_info.client_id)
            if client_info.client_secret:
                values.add(client_info.client_secret)
    return tuple(sorted(value for value in values if value))


async def _require_complete_oauth(oauth_directory: Path) -> None:
    """Require five reusable OAuth states."""
    for service in ServiceName:
        storage = FileTokenStorage(oauth_directory / f'{service.value}.json')
        tokens = await storage.get_tokens()
        client_info = await storage.get_client_info()
        granted_scopes = (
            tuple(tokens.scope.split())
            if tokens is not None and tokens.scope is not None
            else ()
        )
        if (
            tokens is None
            or not tokens.refresh_token
            or len(granted_scopes) != len(READONLY_TOOLS[service])
            or set(granted_scopes) != READONLY_TOOLS[service]
            or client_info is None
            or not client_info.client_id
        ):
            raise ValueError('evaluation OAuth state is incomplete')


def _run_validate(arguments: argparse.Namespace) -> int:
    """Validate public evaluation catalogs."""
    forbidden_values = (
        _binding_private_values(arguments)
        if arguments.bindings is not None
        else ()
    )
    catalogs = load_evaluation_catalogs(
        arguments.evals_dir,
        forbidden_values=forbidden_values,
    )
    print(
        json.dumps(
            {
                'catalog_count': len(catalogs),
                'pair_count': sum(len(catalog.pairs) for catalog in catalogs),
                'status': 'valid',
            },
            sort_keys=True,
        )
    )
    return 0


async def _run_authorize(arguments: argparse.Namespace) -> int:
    """Authorize five evaluation clients."""
    from .adapters import OAuthSessionFactory

    bindings = load_bindings(arguments.bindings)
    require_ready_for_xml(bindings)
    factory = OAuthSessionFactory(
        _service_urls(arguments),
        arguments.oauth_dir,
        allow_browser=True,
    )
    registries = await preflight_tool_registries(
        factory,
        tuple(ServiceName),
    )
    print(
        json.dumps(
            {
                'authorized_service_count': len(registries),
                'status': 'ready',
            },
            sort_keys=True,
        )
    )
    return 0


async def _run_evaluations(arguments: argparse.Namespace) -> int:
    """Run fifty isolated evaluations."""
    from .adapters import DeepSeekModelGateway, OAuthSessionFactory

    bindings = load_bindings(arguments.bindings)
    require_ready_for_xml(bindings)
    binding_values = _binding_private_values(arguments)
    catalogs = load_evaluation_catalogs(
        arguments.evals_dir,
        forbidden_values=binding_values,
    )
    service_urls = _service_urls(arguments)
    await _require_complete_oauth(arguments.oauth_dir)
    model = DeepSeekModelGateway()
    factory = OAuthSessionFactory(
        service_urls,
        arguments.oauth_dir,
        allow_browser=False,
    )
    runner = EvaluationRunner(model, factory, bindings)
    try:
        claim_canonical_run(arguments.oauth_dir, bindings.fixture_version)
        execution = await runner.execute(catalogs)
    finally:
        await model.close()
    forbidden_values = (
        *binding_values,
        *await _oauth_private_values(arguments.oauth_dir),
        *service_urls.values(),
        os.environ.get('DEEPSEEK_API_KEY', ''),
    )
    report = EvidenceReport(
        fixture_version=bindings.fixture_version,
        model=model.model_name,
        model_client_version=model.version,
        mcp_client_version=factory.version,
        runner_version=RUNNER_VERSION,
        registries=execution.registries,
        results=execution.results,
        model_turns=execution.budget.model_turns,
        preflight_model_calls=execution.budget.preflight_model_calls,
        mcp_calls=execution.budget.mcp_calls,
        input_tokens=execution.budget.input_tokens,
        output_tokens=execution.budget.output_tokens,
        evaluator_retries=0,
        orchestration_retries=0,
        service_gateway_retries=SERVICE_GATEWAY_RETRIES,
    )
    write_evidence(
        arguments.evidence,
        report,
        forbidden_values=forbidden_values,
    )
    passed = sum(result.status == 'passed' for result in execution.results)
    print(
        json.dumps(
            {
                'pair_count': len(execution.results),
                'passed_count': passed,
                'status': 'passed'
                if passed == len(execution.results)
                else 'failed',
            },
            sort_keys=True,
        )
    )
    return 0 if passed == len(execution.results) else 1


def _run_apply(arguments: argparse.Namespace) -> int:
    """Run confirmed fixture writes."""
    confirmation = ApplicationConfirmation(
        fixture_version=arguments.fixture_version,
        preview_digest=arguments.preview_digest,
        acknowledge_writes=arguments.acknowledge_writes,
    )
    result = apply_fixture(
        arguments.bindings,
        confirmation,
        credentials_dir=arguments.credentials_dir,
    )
    print(
        json.dumps(
            {
                'fixture_version': result.fixture_version,
                'state': result.state.value,
                'applied_operation_count': len(result.applied_operations),
            },
            sort_keys=True,
        )
    )
    return 0


def _run_readiness(arguments: argparse.Namespace) -> int:
    """Run live readiness checks."""
    bindings = load_bindings(arguments.bindings)
    require_readiness_state(bindings)
    calendar_primary = bindings.calendar_primary_id
    if calendar_primary is None:
        raise ValueError('calendar_primary_id is required for readiness')
    calendar_primary_id = calendar_primary.get_secret_value()
    if not calendar_primary_id:
        raise ValueError('calendar_primary_id is required for readiness')
    try:
        services = build_application_services(arguments.credentials_dir)
        probe = GoogleReadinessProbe(
            services,
            calendar_primary_id=calendar_primary_id,
        )
        report = check_readiness(bindings, probe)
    except Exception:
        raise FixtureReadinessError('fixture readiness check failed') from None
    result = bindings
    if report.status == 'ready':
        result = mark_bindings_ready(bindings, report)
        save_bindings(arguments.bindings, result)
    ready_count = sum(item.status == 'ready' for item in report.items)
    print(
        json.dumps(
            {
                'binding_state': result.state.value,
                'fixture_version': report.fixture_version,
                'not_ready_count': len(report.items) - ready_count,
                'probe_count': report.probe_count,
                'readiness_status': report.status,
                'ready_count': ready_count,
            },
            sort_keys=True,
        )
    )
    return 0 if report.status == 'ready' else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run one fixture command."""
    arguments = _parser().parse_args(argv)
    if arguments.command == 'preview':
        bindings = (
            load_bindings(arguments.bindings)
            if arguments.bindings is not None
            else None
        )
        preview = build_preview(bindings)
        print(preview.document.model_dump_json(indent=2))
        return 0
    if arguments.command == 'apply':
        return _run_apply(arguments)
    if arguments.command == 'readiness':
        return _run_readiness(arguments)
    try:
        if arguments.command == 'validate':
            return _run_validate(arguments)
        if arguments.command == 'authorize':
            return asyncio.run(_run_authorize(arguments))
        return asyncio.run(_run_evaluations(arguments))
    except Exception:
        print('evaluation command failed', file=sys.stderr)
        return 1

"""Apply confirmed fixture writes."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

from google_workspace_mcp.google_auth import (
    GoogleCredentials,
    GoogleCredentialStore,
)
from google_workspace_mcp.services.calendar.client import (
    build_calendar_service,
)
from google_workspace_mcp.services.calendar.constants import CALENDAR_SCOPES
from google_workspace_mcp.services.docs.client import build_docs_service
from google_workspace_mcp.services.docs.constants import DOCS_SCOPES
from google_workspace_mcp.services.drive.client import build_drive_service
from google_workspace_mcp.services.drive.constants import DRIVE_SCOPES
from google_workspace_mcp.services.gmail.client import build_gmail_service
from google_workspace_mcp.services.gmail.constants import GMAIL_SCOPE
from google_workspace_mcp.services.sheets.client import build_sheets_service
from google_workspace_mcp.services.sheets.constants import SHEETS_SCOPES

from .catalog import (
    MARKER_MESSAGE_ALPHA_ROOT,
    MARKER_MESSAGE_BETA_ROOT,
    OBJECTS_BY_REF,
)
from .models import (
    ApplicationConfirmation,
    BindingState,
    FixtureBindings,
    ObjectBinding,
    ProviderIdentifiers,
    ServiceName,
    load_bindings,
)
from .preview import build_preview, confirm_application
from .requests import (
    WRITE_OPERATION_IDS,
    GoogleServiceSet,
    PreparedOperation,
    ServiceFactory,
    build_write_operations,
)

# === Errors ===


class FixtureApplicationError(RuntimeError):
    """Report one failed write."""


# === Credential services ===

ServiceBuilder = Callable[[GoogleCredentials], Any]

_SERVICE_BUILDERS: dict[
    ServiceName,
    tuple[tuple[str, ...], ServiceBuilder],
] = {
    ServiceName.GMAIL: ((GMAIL_SCOPE,), build_gmail_service),
    ServiceName.CALENDAR: (CALENDAR_SCOPES, build_calendar_service),
    ServiceName.DRIVE: (DRIVE_SCOPES, build_drive_service),
    ServiceName.SHEETS: (SHEETS_SCOPES, build_sheets_service),
    ServiceName.DOCS: (DOCS_SCOPES, build_docs_service),
}


def _safe_metadata(path: Path) -> os.stat_result:
    """Read path metadata without links."""
    try:
        return path.lstat()
    except OSError as error:
        raise ValueError('seed credential path is unavailable') from error


def validate_seed_credentials(
    credentials_dir: Path,
) -> dict[ServiceName, Path]:
    """Validate five seed credential paths."""
    directory_metadata = _safe_metadata(credentials_dir)
    if not stat.S_ISDIR(directory_metadata.st_mode):
        raise ValueError('seed credential directory must be a directory')
    if directory_metadata.st_uid != os.getuid():
        raise ValueError('seed credential directory has a foreign owner')
    if stat.S_IMODE(directory_metadata.st_mode) != 0o700:
        raise ValueError('seed credential directory mode must be 0700')
    paths: dict[ServiceName, Path] = {}
    for service in ServiceName:
        credential_path = credentials_dir / f'{service.value}.json'
        metadata = _safe_metadata(credential_path)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('seed credential must be a regular file')
        if metadata.st_uid != os.getuid():
            raise ValueError('seed credential file has a foreign owner')
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError('seed credential file mode must be 0600')
        paths[service] = credential_path
    return paths


def build_application_services(credentials_dir: Path) -> GoogleServiceSet:
    """Build five authenticated services."""
    credential_paths = validate_seed_credentials(credentials_dir)
    services: dict[ServiceName, Any] = {}
    for service, credential_path in credential_paths.items():
        required_scopes, builder = _SERVICE_BUILDERS[service]
        credentials = GoogleCredentialStore(
            credential_path,
            required_scopes=required_scopes,
        ).get_credentials()
        services[service] = builder(credentials)
    return GoogleServiceSet(
        gmail=services[ServiceName.GMAIL],
        calendar=services[ServiceName.CALENDAR],
        drive=services[ServiceName.DRIVE],
        sheets=services[ServiceName.SHEETS],
        docs=services[ServiceName.DOCS],
    )


# === Request resolution ===

_PLACEHOLDERS = (
    '__binding_calendar_primary_id__',
    '__binding_gmail_thread_alpha__',
    '__binding_drive_fixture_folder__',
    '__binding_sheets_primary__',
    '__binding_docs_primary__',
    '__binding_docs_primary_tab__',
)


def _secret_value(value: Any, name: str) -> str:
    """Require one private string."""
    if value is None:
        raise ValueError(f'{name} is required for fixture application')
    resolved = value.get_secret_value()
    if not resolved:
        raise ValueError(f'{name} is required for fixture application')
    return resolved


def _bound_identifier(
    bindings: FixtureBindings,
    logical_ref: str,
    field: str,
) -> str:
    """Read one prior provider identifier."""
    binding = bindings.objects.get(logical_ref)
    if binding is None:
        raise ValueError(f'{logical_ref} is not bound')
    value = getattr(binding.identifiers, field)
    if not isinstance(value, str) or not value:
        raise ValueError(f'{logical_ref}.{field} is not bound')
    return value


def _placeholder_value(
    placeholder: str,
    bindings: FixtureBindings,
) -> str:
    """Resolve one request placeholder."""
    match placeholder:
        case '__binding_calendar_primary_id__':
            return _secret_value(
                bindings.calendar_primary_id,
                'calendar_primary_id',
            )
        case '__binding_gmail_thread_alpha__':
            return _bound_identifier(
                bindings,
                'gmail_thread_alpha',
                'thread_id',
            )
        case '__binding_drive_fixture_folder__':
            return _bound_identifier(
                bindings,
                'drive_fixture_folder',
                'file_id',
            )
        case '__binding_sheets_primary__':
            return _bound_identifier(
                bindings,
                'sheets_primary',
                'spreadsheet_id',
            )
        case '__binding_docs_primary__':
            return _bound_identifier(
                bindings,
                'docs_primary',
                'document_id',
            )
        case '__binding_docs_primary_tab__':
            return _bound_identifier(
                bindings,
                'docs_primary_tab',
                'tab_id',
            )
        case _:
            raise ValueError('unknown fixture request placeholder')


def _replace_recipient(raw_message: str, owner_email: str) -> str:
    """Replace synthetic MIME recipient."""
    try:
        decoded = base64.urlsafe_b64decode(raw_message.encode('ascii'))
        message = BytesParser(policy=policy.default).parsebytes(decoded)
    except Exception as error:
        raise ValueError('Gmail fixture message is invalid') from error
    if message.get('To') != 'fixture-owner@example.com':
        raise ValueError('Gmail fixture recipient is invalid')
    message.replace_header('To', owner_email)
    encoded = base64.urlsafe_b64encode(message.as_bytes())
    return encoded.decode('ascii')


def _resolve_body_value(
    value: Any,
    bindings: FixtureBindings,
    owner_email: str,
    *,
    field_name: str | None = None,
) -> Any:
    """Resolve one nested request value."""
    if field_name == 'raw' and isinstance(value, str):
        return _replace_recipient(value, owner_email)
    if isinstance(value, str) and value in _PLACEHOLDERS:
        return _placeholder_value(value, bindings)
    if isinstance(value, Mapping):
        return {
            key: _resolve_body_value(
                item,
                bindings,
                owner_email,
                field_name=str(key),
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [
            _resolve_body_value(item, bindings, owner_email) for item in value
        ]
    return value


def _resolve_request(
    operation: PreparedOperation,
    bindings: FixtureBindings,
    owner_email: str,
) -> None:
    """Resolve one request in place."""
    request = operation.request
    uri = request.uri
    for placeholder in _PLACEHOLDERS:
        if placeholder in uri:
            resolved = quote(
                _placeholder_value(placeholder, bindings),
                safe='',
            )
            uri = uri.replace(placeholder, resolved)
    request.uri = uri
    raw_body = request.body
    if raw_body is not None:
        body_text = (
            raw_body.decode('utf-8')
            if isinstance(raw_body, bytes)
            else raw_body
        )
        body_value = json.loads(body_text)
        resolved_body = _resolve_body_value(
            body_value,
            bindings,
            owner_email,
        )
        serialized = json.dumps(resolved_body)
        request.body = (
            serialized.encode('utf-8')
            if isinstance(raw_body, bytes)
            else serialized
        )
    unresolved = request.uri
    if request.body is not None:
        unresolved += str(request.body)
    if '__binding_' in unresolved:
        raise ValueError('fixture request contains unresolved bindings')


# === Response bindings ===


def _response_mapping(value: Any) -> Mapping[str, Any]:
    """Require one provider response object."""
    if not isinstance(value, Mapping):
        raise ValueError('Google write response must be an object')
    return value


def _required_string(value: Any, field: str) -> str:
    """Require one response identifier."""
    if not isinstance(value, str) or not value:
        raise ValueError(f'Google write response requires {field}')
    return value


def _required_integer(value: Any, field: str) -> int:
    """Require one integer identifier."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f'Google write response requires {field}')
    return value


def _object_binding(
    logical_ref: str,
    **identifiers: str | int,
) -> ObjectBinding:
    """Build one validated object binding."""
    fixture_object = OBJECTS_BY_REF[logical_ref]
    return ObjectBinding(
        logical_ref=logical_ref,
        service=fixture_object.service,
        resource_kind=fixture_object.resource_kind,
        identifiers=ProviderIdentifiers.model_validate(identifiers),
    )


def _gmail_message_outputs(
    response: Mapping[str, Any],
    *,
    message_ref: str,
    thread_ref: str,
    delivery_ref: str,
    message_header_id: str | None,
) -> dict[str, ObjectBinding]:
    """Bind one sent Gmail message."""
    message_id = _required_string(response.get('id'), 'id')
    thread_id = _required_string(response.get('threadId'), 'threadId')
    message_identifiers: dict[str, str] = {'message_id': message_id}
    if message_header_id is not None:
        message_identifiers['message_header_id'] = message_header_id
    return {
        message_ref: _object_binding(
            message_ref,
            **message_identifiers,
        ),
        thread_ref: _object_binding(thread_ref, thread_id=thread_id),
        delivery_ref: _object_binding(
            delivery_ref,
            message_id=message_id,
            thread_id=thread_id,
        ),
    }


def _sheet_identifiers(
    response: Mapping[str, Any],
) -> tuple[int, int]:
    """Read two spreadsheet tab identifiers."""
    sheets = response.get('sheets')
    if not isinstance(sheets, Sequence) or isinstance(sheets, str | bytes):
        raise ValueError('Google write response requires sheets')
    by_title: dict[str, int] = {}
    for sheet in sheets:
        if not isinstance(sheet, Mapping):
            raise ValueError('Google write response has invalid sheets')
        properties = sheet.get('properties')
        if not isinstance(properties, Mapping):
            raise ValueError('Google write response has invalid sheets')
        title = _required_string(properties.get('title'), 'sheet title')
        sheet_id = _required_integer(
            properties.get('sheetId'),
            'sheetId',
        )
        by_title[title] = sheet_id
    try:
        return by_title['Inputs'], by_title['Summary']
    except KeyError as error:
        raise ValueError(
            'Google write response requires fixture sheets'
        ) from error


def _document_tab_id(response: Mapping[str, Any]) -> str:
    """Read the primary document tab."""
    tabs = response.get('tabs')
    if not isinstance(tabs, Sequence) or isinstance(tabs, str | bytes):
        raise ValueError('Google write response requires tabs')
    if len(tabs) != 1 or not isinstance(tabs[0], Mapping):
        raise ValueError('Google write response requires one primary tab')
    properties = tabs[0].get('tabProperties')
    if not isinstance(properties, Mapping):
        raise ValueError('Google write response requires tab properties')
    return _required_string(properties.get('tabId'), 'tabId')


def _operation_outputs(
    operation_id: str,
    response: Mapping[str, Any],
) -> dict[str, ObjectBinding]:
    """Extract logical outputs from one response."""
    match operation_id:
        case 'gmail.create_draft.cobalt':
            message = response.get('message')
            if not isinstance(message, Mapping):
                raise ValueError('Google write response requires message')
            return {
                'gmail_draft_cobalt': _object_binding(
                    'gmail_draft_cobalt',
                    draft_id=_required_string(response.get('id'), 'id'),
                ),
                'gmail_draft_message_cobalt': _object_binding(
                    'gmail_draft_message_cobalt',
                    message_id=_required_string(
                        message.get('id'),
                        'message.id',
                    ),
                ),
            }
        case 'gmail.send_message.alpha_root':
            return _gmail_message_outputs(
                response,
                message_ref='gmail_message_alpha_root',
                thread_ref='gmail_thread_alpha',
                delivery_ref='gmail_delivery_alpha_root',
                message_header_id=(
                    f'{MARKER_MESSAGE_ALPHA_ROOT}@example.invalid'
                ),
            )
        case 'gmail.send_message.alpha_reply':
            message_id = _required_string(response.get('id'), 'id')
            thread_id = _required_string(
                response.get('threadId'),
                'threadId',
            )
            return {
                'gmail_message_alpha_reply': _object_binding(
                    'gmail_message_alpha_reply',
                    message_id=message_id,
                ),
                'gmail_delivery_alpha_reply': _object_binding(
                    'gmail_delivery_alpha_reply',
                    message_id=message_id,
                    thread_id=thread_id,
                ),
            }
        case 'gmail.send_message.beta_root':
            return _gmail_message_outputs(
                response,
                message_ref='gmail_message_beta_root',
                thread_ref='gmail_thread_beta',
                delivery_ref='gmail_delivery_beta_root',
                message_header_id=(
                    f'{MARKER_MESSAGE_BETA_ROOT}@example.invalid'
                ),
            )
        case operation if operation.startswith('calendar.create_event.'):
            suffix = operation.rsplit('.', maxsplit=1)[-1]
            return {
                f'calendar_event_{suffix}': _object_binding(
                    f'calendar_event_{suffix}',
                    event_id=_required_string(response.get('id'), 'id'),
                )
            }
        case 'drive.create.folder':
            return {
                'drive_fixture_folder': _object_binding(
                    'drive_fixture_folder',
                    file_id=_required_string(response.get('id'), 'id'),
                )
            }
        case 'drive.create.note':
            return {
                'drive_note_file': _object_binding(
                    'drive_note_file',
                    file_id=_required_string(response.get('id'), 'id'),
                )
            }
        case 'drive.create.ledger':
            return {
                'drive_ledger_file': _object_binding(
                    'drive_ledger_file',
                    file_id=_required_string(response.get('id'), 'id'),
                )
            }
        case 'sheets.create.primary':
            spreadsheet_id = _required_string(
                response.get('spreadsheetId'),
                'spreadsheetId',
            )
            inputs_id, summary_id = _sheet_identifiers(response)
            return {
                'sheets_primary': _object_binding(
                    'sheets_primary',
                    spreadsheet_id=spreadsheet_id,
                ),
                'sheets_inputs_tab': _object_binding(
                    'sheets_inputs_tab',
                    spreadsheet_id=spreadsheet_id,
                    sheet_id=inputs_id,
                ),
                'sheets_summary_tab': _object_binding(
                    'sheets_summary_tab',
                    spreadsheet_id=spreadsheet_id,
                    sheet_id=summary_id,
                ),
            }
        case 'docs.create.primary':
            document_id = _required_string(
                response.get('documentId'),
                'documentId',
            )
            tab_id = _document_tab_id(response)
            return {
                'docs_primary': _object_binding(
                    'docs_primary',
                    document_id=document_id,
                ),
                'docs_primary_tab': _object_binding(
                    'docs_primary_tab',
                    document_id=document_id,
                    tab_id=tab_id,
                ),
            }
        case 'sheets.write.primary_values' | 'docs.write.primary_text':
            return {}
        case _:
            raise ValueError('unknown fixture write operation')


def _updated_bindings(
    bindings: FixtureBindings,
    operation: PreparedOperation,
    response: Mapping[str, Any],
) -> FixtureBindings:
    """Register one complete operation."""
    outputs = _operation_outputs(operation.preview.operation_id, response)
    expected_outputs = frozenset(operation.preview.logical_outputs)
    if frozenset(outputs) != expected_outputs:
        raise ValueError('Google write outputs do not match the operation')
    objects = {**bindings.objects, **outputs}
    applied = bindings.applied_operations | {operation.preview.operation_id}
    state = (
        BindingState.APPLIED
        if applied == frozenset(WRITE_OPERATION_IDS)
        else BindingState.PLANNED
    )
    return FixtureBindings(
        fixture_version=bindings.fixture_version,
        state=state,
        owner_email=bindings.owner_email,
        calendar_primary_id=bindings.calendar_primary_id,
        credentials=bindings.credentials,
        objects=objects,
        applied_operations=applied,
    )


# === Atomic registry ===


def _private_payload(bindings: FixtureBindings) -> dict[str, Any]:
    """Serialize bindings with private values."""
    payload = bindings.model_dump(mode='json')
    payload['owner_email'] = (
        bindings.owner_email.get_secret_value()
        if bindings.owner_email is not None
        else None
    )
    payload['calendar_primary_id'] = (
        bindings.calendar_primary_id.get_secret_value()
        if bindings.calendar_primary_id is not None
        else None
    )
    payload['applied_operations'] = [
        operation_id
        for operation_id in WRITE_OPERATION_IDS
        if operation_id in bindings.applied_operations
    ]
    return payload


def _secure_registry_directory(path: Path) -> int:
    """Open the protected registry directory."""
    directory = path.parent
    try:
        metadata = directory.lstat()
    except OSError as error:
        raise ValueError('bindings directory is unavailable') from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError('bindings directory must be a directory')
    if metadata.st_uid != os.getuid():
        raise ValueError('bindings directory has a foreign owner')
    directory.chmod(0o700)
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        return os.open(directory, flags)
    except OSError as error:
        raise ValueError('bindings directory is unavailable') from error


def _save_bindings(path: Path, bindings: FixtureBindings) -> None:
    """Atomically save private bindings."""
    encoded = (
        json.dumps(
            _private_payload(bindings),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + '\n'
    ).encode('utf-8')
    directory_fd = _secure_registry_directory(path)
    temporary_name = f'.{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp'
    temporary_fd: int | None = None
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, 'O_NOFOLLOW', 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(temporary_fd, 0o600)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(temporary_fd, remaining)
            if written <= 0:
                raise OSError('incomplete bindings write')
            remaining = remaining[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            with contextlib.suppress(OSError):
                os.close(temporary_fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)


# === Application ===


def _execution_operations(
    service_factory: ServiceFactory,
    confirmed: tuple[PreparedOperation, ...],
) -> tuple[PreparedOperation, ...]:
    """Build requests matching the preview."""
    operations = build_write_operations(service_factory())
    if tuple(item.preview for item in operations) != tuple(
        item.preview for item in confirmed
    ):
        raise ValueError('execution requests do not match the preview')
    return operations


def apply_fixture(
    bindings_path: Path,
    confirmation: ApplicationConfirmation,
    *,
    credentials_dir: Path = Path('private/google-tokens'),
    service_factory: ServiceFactory | None = None,
) -> FixtureBindings:
    """Apply one confirmed fixture plan."""
    bindings = load_bindings(bindings_path)
    preview = build_preview(bindings)
    confirmed = confirm_application(preview, confirmation)
    owner_email = _secret_value(bindings.owner_email, 'owner_email')
    _secret_value(bindings.calendar_primary_id, 'calendar_primary_id')
    factory = service_factory
    if factory is None:

        def factory() -> GoogleServiceSet:
            """Build credential-backed services."""
            return build_application_services(credentials_dir)

    operations = _execution_operations(factory, confirmed)
    current = bindings
    for operation in operations:
        try:
            _resolve_request(operation, current, owner_email)
            response = _response_mapping(
                operation.request.execute(num_retries=0)
            )
            current = _updated_bindings(current, operation, response)
            _save_bindings(bindings_path, current)
        except Exception:
            operation_id = operation.preview.operation_id
            raise FixtureApplicationError(
                f'fixture operation failed: {operation_id}'
            ) from None
    return current

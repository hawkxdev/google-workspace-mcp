"""Guard canonical evaluation runs."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def claim_canonical_run(directory: Path, fixture_version: str) -> Path:
    """Claim one fixture run exactly once."""
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError('run state directory must be a directory')
    if metadata.st_uid != os.getuid():
        raise ValueError('run state directory has a foreign owner')
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError('run state directory mode must be 0700')
    path = directory / f'.{fixture_version}-canonical-run.json'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise ValueError(
            'canonical evaluation run is already claimed'
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        payload = json.dumps(
            {'fixture_version': fixture_version, 'status': 'started'},
            ensure_ascii=True,
            sort_keys=True,
        ).encode('utf-8')
        remaining = memoryview(payload + b'\n')
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError('incomplete run state write')
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path

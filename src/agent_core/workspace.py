from __future__ import annotations

import logging
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_core.settings import get_settings

LOG = logging.getLogger(__name__)


@contextmanager
def job_workspace(job_id: str | None = None) -> Iterator[Path]:
    """Create an isolated workspace directory for a single job."""
    settings = get_settings()
    if settings.keep_workdir:
        base_dir = settings.agent_workdir
        base_dir.mkdir(parents=True, exist_ok=True)
        suffix = job_id or uuid.uuid4().hex[:8]
        workspace = base_dir / suffix
        workspace.mkdir(parents=True, exist_ok=True)
        LOG.debug("Using persistent workspace: %s", workspace)
        yield workspace
        return

    with tempfile.TemporaryDirectory(prefix="agent-") as temp_dir:
        workspace = Path(temp_dir)
        LOG.debug("Using temporary workspace: %s", workspace)
        yield workspace

"""Actual revision generation exercises the configured formatter/linter module hooks."""

from __future__ import annotations

import shutil
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import Script

from lychd.config.constants import PATH_MIGRATION_CONFIG


def test_revision_generation_runs_repository_hooks_in_the_active_python(tmp_path: Path) -> None:
    configuration = Config(str(PATH_MIGRATION_CONFIG))
    configuration.set_main_option("script_location", str(tmp_path))
    (tmp_path / "versions").mkdir()
    shutil.copyfile(PATH_MIGRATION_CONFIG.parent / "script.py.mako", tmp_path / "script.py.mako")
    generated = command.revision(configuration, message="Hook fixture", rev_id="hook_fixture")
    assert isinstance(generated, Script)
    body = Path(generated.path).read_text()
    assert 'revision = "hook_fixture"' in body
    assert "revision = 'hook_fixture'" not in body

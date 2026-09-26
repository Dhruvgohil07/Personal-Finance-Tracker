"""The migrations must produce exactly the schema the models describe."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

pytestmark = pytest.mark.integration


def test_models_match_migrations(migrated_engine: Engine, alembic_config: Config) -> None:
    # `alembic check` autogenerates a migration in memory and raises if it is
    # not empty, i.e. if someone changed a model but forgot the migration.
    command.check(alembic_config)


def test_downgrade_and_upgrade_again(migrated_engine: Engine, alembic_config: Config) -> None:
    # Proves downgrade() works and upgrade() can run again on a clean DB.
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

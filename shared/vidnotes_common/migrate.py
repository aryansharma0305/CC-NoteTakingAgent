"""`python -m vidnotes_common.migrate [revision]` -- apply Alembic migrations.

Builds the Alembic config in code so the migrations ship inside the installed
package and no alembic.ini is needed in each container.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import get_settings


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "head"
    command.upgrade(alembic_config(), target)
    print(f"migrations applied ({target})")


if __name__ == "__main__":
    main()

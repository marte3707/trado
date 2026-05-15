from pathlib import Path

import structlog
from sqlalchemy import create_engine, inspect

from alembic import command
from alembic.config import Config
from app.core.config import get_settings

logger = structlog.get_logger()

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"
_ALEMBIC_DIR = _BACKEND_ROOT / "alembic"


def _alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def apply_migrations() -> None:
    """Bring the database to alembic head, handling pre-alembic legacy databases.

    Strategy:
      * fresh DB (no tables yet) → upgrade head from scratch (only viable if
        baseline holds the full schema; on this project create_schema() owns the
        initial CREATE TABLE step, so we additionally stamp to head when the schema
        is already in place).
      * legacy DB with schema but no alembic_version → heuristically stamp at the
        right revision before upgrading, by checking whether post-baseline columns
        exist.
      * DB already managed by alembic → straight upgrade head.
    """
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        cfg = _alembic_config()
        if "alembic_version" not in table_names:
            if "signals" in table_names:
                signal_cols = {col["name"] for col in inspector.get_columns("signals")}
                if "source_candle_open_time" in signal_cols:
                    command.stamp(cfg, "head")
                    logger.info("alembic_stamped_head_for_pre_existing_schema")
                else:
                    command.stamp(cfg, "0001_baseline")
                    logger.info("alembic_stamped_baseline_for_pre_existing_schema")
            else:
                logger.info("alembic_no_tables_yet")
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()

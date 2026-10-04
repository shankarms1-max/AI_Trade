import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.logging import SecretRedactionFilter


class ContextDefaults(logging.Filter):
    fields = ("component", "snapshot_id", "pipeline_run_id", "worker_id",
              "strategy_candidate_id", "shadow_trade_id")

    def filter(self, record: logging.LogRecord) -> bool:
        for field in self.fields:
            if not hasattr(record, field):
                setattr(record, field, "-")
        return True


class PrefixFilter(logging.Filter):
    def __init__(self, prefix: str) -> None:
        super().__init__()
        self.prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self.prefix)


def configure_rotating_logging(
    level: str = "INFO", log_dir: str = "logs", max_bytes: int = 10_485_760,
    backup_count: int = 7,
) -> None:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s component=%(component)s "
        "snapshot_id=%(snapshot_id)s pipeline_run_id=%(pipeline_run_id)s "
        "worker_id=%(worker_id)s %(message)s"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    for filename, min_level, prefix in (
        ("app.log", logging.DEBUG, None), ("collector.log", logging.DEBUG, "app.collector"),
        ("pipeline.log", logging.DEBUG, "app.pipeline"), ("error.log", logging.ERROR, None),
    ):
        handler = RotatingFileHandler(directory / filename, maxBytes=max_bytes, backupCount=backup_count)
        handler.setLevel(min_level)
        if prefix:
            handler.addFilter(PrefixFilter(prefix))
        handlers.append(handler)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(ContextDefaults())
        handler.addFilter(SecretRedactionFilter())
    logging.basicConfig(level=level.upper(), handlers=handlers, force=True)

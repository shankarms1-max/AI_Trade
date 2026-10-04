from typing import Any

from app.broker.base import BrokerSessionError
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def create_market_data_client(settings: Settings) -> Any:
    """Create a consumer-key-only client for Kotak's read-only market APIs."""

    logger.info("KOTAK_AUTH_STARTED")
    try:
        from neo_api_client import NeoAPI

        client = NeoAPI(
            consumer_key=settings.kotak_consumer_key.get_secret_value(),
            environment="prod",
        )
    except Exception as exc:
        logger.error("KOTAK_AUTH_FAILED")
        raise BrokerSessionError("Kotak market-data client initialization failed") from exc

    logger.info("KOTAK_AUTH_SUCCESS")
    return client


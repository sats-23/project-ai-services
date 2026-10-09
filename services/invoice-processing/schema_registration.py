"""
Lazy registration of the invoice extraction schema with the extract service.

The schema is registered on the first job submission rather than at application
startup, so the service does not depend on the extract service being up at boot.
"""

import asyncio

import requests

from common.error_utils import APIError, ErrorCode
from common.misc_utils import get_logger
from schema import INVOICE_SCHEMA_PAYLOAD
from settings import settings

logger = get_logger("schema_registration")

_registered = False
_lock = asyncio.Lock()


def _register_invoice_schema() -> None:
    """Register invoice schema with the extract service (blocking).

    - 201: Schema created
    - 409: Schema already exists (idempotent)
    - Any other status or network error: raises RuntimeError
    """
    if not settings.invoice.extract_url:
        raise RuntimeError("EXTRACT_URL is not configured")

    url = f"{settings.invoice.extract_url.rstrip('/')}/v1/schemas"
    logger.info(f"Registering invoice schema with extract service at {url}...")

    try:
        resp = requests.post(url, json=INVOICE_SCHEMA_PAYLOAD, timeout=10.0)
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error connecting to extract service schema endpoint: {exc}") from exc

    if resp.status_code == 201:
        logger.info("Successfully registered invoice extraction schema")
    elif resp.status_code == 409:
        logger.info("Invoice extraction schema already registered (idempotent)")
    else:
        raise RuntimeError(
            f"Failed to register invoice schema with extract service: HTTP {resp.status_code} - {resp.text}"
        )


async def ensure_invoice_schema_registered() -> None:
    """Register the invoice schema once, on first use.

    Concurrent callers wait on a lock so only one registration is attempted.
    A failed attempt is not cached, so the next request retries.
    Raises HTTP 503 if the extract service cannot be reached or rejects the schema.
    """
    global _registered
    if _registered:
        return

    async with _lock:
        if _registered:
            return
        try:
            await asyncio.to_thread(_register_invoice_schema)
        except RuntimeError as exc:
            logger.error(str(exc))
            APIError.raise_error(
                ErrorCode.INTERNAL_SERVER_ERROR,
                "Extract service is not available to register the invoice schema",
                status_code=503,
            )
        _registered = True

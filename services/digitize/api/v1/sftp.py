"""
SFTP Connector API — /v1/sftp

Endpoints
---------
GET  /v1/sftp/config   — show current (no-secret) SFTP config
GET  /v1/sftp/status   — poller running state + last poll info
POST /v1/sftp/start    — start the background poller
POST /v1/sftp/stop     — stop the background poller
POST /v1/sftp/trigger  — force an immediate poll (blocking, returns result)
"""

from fastapi import APIRouter, status

from common.misc_utils import get_logger
from digitize.connectors.poller import poller
from digitize.connectors.sftp import config_summary

router = APIRouter()
logger = get_logger("sftp_router")


@router.get(
    "/config",
    summary="Show SFTP connector configuration",
    description=(
        "Returns the current hardcoded SFTP connection settings. "
        "Secrets (password, private key) are never included in the response."
    ),
)
def get_sftp_config():
    return config_summary()


@router.get(
    "/status",
    summary="Poller status",
    description="Returns whether the background poller is running plus stats from the last cycle.",
)
def get_poller_status():
    return poller.status()


@router.post(
    "/start",
    status_code=status.HTTP_200_OK,
    summary="Start the SFTP poller",
    description=(
        "Starts the background polling thread. "
        "Runs an immediate first poll, then repeats every 5 minutes. "
        "Idempotent — safe to call if already running."
    ),
)
def start_poller():
    poller.start()
    return {"message": "SFTP poller started", "status": poller.status()}


@router.post(
    "/stop",
    status_code=status.HTTP_200_OK,
    summary="Stop the SFTP poller",
    description="Signals the background polling thread to stop gracefully.",
)
def stop_poller():
    poller.stop()
    return {"message": "SFTP poller stopped", "status": poller.status()}


@router.post(
    "/trigger",
    status_code=status.HTTP_200_OK,
    summary="Trigger an immediate poll",
    description=(
        "Forces a poll cycle right now on the calling thread (blocking). "
        "Useful for manual testing without waiting for the 5-minute interval."
    ),
)
def trigger_poll():
    poller.trigger_now()
    return {"message": "Poll cycle completed", "status": poller.status()}

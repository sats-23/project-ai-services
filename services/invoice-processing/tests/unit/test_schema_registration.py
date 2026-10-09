"""
Unit tests for lazy invoice schema registration.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi import HTTPException

import schema_registration
from settings import settings


@pytest.fixture(autouse=True)
def reset_state():
    schema_registration._registered = False
    with patch.object(settings.invoice, "extract_url", "http://extract-test:6000"):
        yield
    schema_registration._registered = False


@pytest.mark.parametrize("status_code", [201, 409])
def test_register_schema_success(status_code):
    with patch("requests.post", return_value=MagicMock(status_code=status_code)) as mock_post:
        schema_registration._register_invoice_schema()
        mock_post.assert_called_once()


def test_register_schema_failure_raises():
    resp = MagicMock(status_code=500, text="Internal Server Error")
    with patch("requests.post", return_value=resp):
        with pytest.raises(RuntimeError, match="Failed to register invoice schema"):
            schema_registration._register_invoice_schema()


def test_register_schema_network_error_raises():
    with patch("requests.post", side_effect=requests.RequestException("Connection refused")):
        with pytest.raises(RuntimeError, match="Network error connecting to extract service"):
            schema_registration._register_invoice_schema()


def test_register_schema_missing_url_raises():
    with patch.object(settings.invoice, "extract_url", ""):
        with pytest.raises(RuntimeError, match="EXTRACT_URL is not configured"):
            schema_registration._register_invoice_schema()


@pytest.mark.asyncio
async def test_ensure_registers_only_once():
    with patch("requests.post", return_value=MagicMock(status_code=201)) as mock_post:
        await schema_registration.ensure_invoice_schema_registered()
        await schema_registration.ensure_invoice_schema_registered()
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_failure_returns_503_and_retries_next_time():
    with patch("requests.post", side_effect=requests.RequestException("Connection refused")):
        with pytest.raises(HTTPException) as exc_info:
            await schema_registration.ensure_invoice_schema_registered()
        assert exc_info.value.status_code == 503

    with patch("requests.post", return_value=MagicMock(status_code=201)) as mock_post:
        await schema_registration.ensure_invoice_schema_registered()
        mock_post.assert_called_once()

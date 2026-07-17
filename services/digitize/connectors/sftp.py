"""
SFTP Connector — PoC (hardcoded credentials).

Responsibilities
----------------
* Open / close an SFTP session via paramiko.
* List files in the remote folder (PDF + DOCX only).
* Download changed files to a local temp directory.
* Compute SHA-256 checksums for change-detection.

Hardcoded connection values live in ``SFTPConfig`` at the top of this file.
Replace them directly when testing against a real server.
"""

import hashlib
import shutil
from pathlib import Path
from typing import Optional

import paramiko

from common.misc_utils import get_logger

logger = get_logger("sftp_connector")

# ── PoC hardcoded values ─────────────────────────────────────────────────────
# Fill these in before running.

SFTP_HOST: str = "10.20.185.60"          # e.g. "192.168.1.50"
SFTP_PORT: int = 22
SFTP_USERNAME: str = "root"
SFTP_REMOTE_PATH: str = "/var/sats/documents"  # trailing slash optional

# Authentication — exactly one of the two should be set.
# Option A: password auth
SFTP_PASSWORD: Optional[str] = None          # e.g. "s3cr3t"

# Option B: private-key auth (path on the machine running this service)
SFTP_PRIVATE_KEY_PATH: Optional[str] = None  # e.g. "/home/app/.ssh/id_rsa"
SFTP_PRIVATE_KEY_PASSPHRASE: Optional[str] = None

# Public key to display (informational only — shown in /v1/sftp/config)
SFTP_PUBLIC_KEY: str = (
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC... poc@rag-system"
)

# ─────────────────────────────────────────────────────────────────────────────

# Supported file extensions for ingestion
SUPPORTED_EXTS = {".pdf", ".docx"}


def _make_transport() -> paramiko.Transport:
    """Open a raw paramiko Transport to the configured host/port."""
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))

    if SFTP_PRIVATE_KEY_PATH:
        pkey = paramiko.RSAKey.from_private_key_file(
            SFTP_PRIVATE_KEY_PATH,
            password=SFTP_PRIVATE_KEY_PASSPHRASE,
        )
        transport.connect(username=SFTP_USERNAME, pkey=pkey)
        logger.debug(f"SFTP transport connected to {SFTP_HOST} via private key")
    elif SFTP_PASSWORD is not None:
        transport.connect(username=SFTP_USERNAME, password=SFTP_PASSWORD)
        logger.debug(f"SFTP transport connected to {SFTP_HOST} via password")
    else:
        raise ValueError(
            "SFTP auth not configured: set either SFTP_PASSWORD or SFTP_PRIVATE_KEY_PATH"
        )

    return transport


def list_remote_files(sftp: paramiko.SFTPClient) -> list[str]:
    """
    Return filenames (not full paths) of supported documents in the remote folder.

    Args:
        sftp: An open paramiko SFTPClient.

    Returns:
        List of filenames with .pdf / .docx extension.
    """
    try:
        entries = sftp.listdir(SFTP_REMOTE_PATH)
    except IOError as exc:
        raise IOError(
            f"Cannot list remote directory '{SFTP_REMOTE_PATH}': {exc}"
        ) from exc

    supported = [f for f in entries if Path(f).suffix.lower() in SUPPORTED_EXTS]
    logger.debug(f"Remote listing: {len(supported)} supported file(s) in {SFTP_REMOTE_PATH}")
    return supported


def checksum_remote_file(sftp: paramiko.SFTPClient, filename: str) -> str:
    """
    Stream a remote file through SHA-256 without writing to disk.

    Args:
        sftp: An open paramiko SFTPClient.
        filename: Filename (relative to SFTP_REMOTE_PATH).

    Returns:
        Lowercase hex digest string.
    """
    remote_path = f"{SFTP_REMOTE_PATH.rstrip('/')}/{filename}"
    sha = hashlib.sha256()
    with sftp.open(remote_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def download_files(
    sftp: paramiko.SFTPClient,
    filenames: list[str],
    local_dir: Path,
) -> list[Path]:
    """
    Download *filenames* from the remote folder into *local_dir*.

    Existing files in *local_dir* are overwritten.

    Args:
        sftp: An open paramiko SFTPClient.
        filenames: Filenames to download (relative to SFTP_REMOTE_PATH).
        local_dir: Local directory to write into (created if absent).

    Returns:
        List of local Path objects for the downloaded files.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    for filename in filenames:
        remote_path = f"{SFTP_REMOTE_PATH.rstrip('/')}/{filename}"
        local_path = local_dir / filename
        try:
            sftp.get(remote_path, str(local_path))
            logger.info(f"Downloaded: {remote_path} → {local_path}")
            downloaded.append(local_path)
        except Exception as exc:
            logger.error(f"Failed to download {remote_path}: {exc}")

    return downloaded


# ── High-level helper used by the poller ─────────────────────────────────────

class SFTPSession:
    """
    Context-manager wrapper that owns a paramiko Transport + SFTPClient.

    Usage::

        with SFTPSession() as sess:
            files = sess.list_files()
            checksums = {f: sess.checksum(f) for f in files}
            sess.download(files, local_dir)
    """

    def __init__(self) -> None:
        self._transport: Optional[paramiko.Transport] = None
        self._client: Optional[paramiko.SFTPClient] = None

    def __enter__(self) -> "SFTPSession":
        self._transport = _make_transport()
        self._client = paramiko.SFTPClient.from_transport(self._transport)
        return self

    def __exit__(self, *_) -> None:
        if self._client:
            self._client.close()
        if self._transport:
            self._transport.close()

    # Convenience delegates

    def list_files(self) -> list[str]:
        return list_remote_files(self._client)

    def checksum(self, filename: str) -> str:
        return checksum_remote_file(self._client, filename)

    def download(self, filenames: list[str], local_dir: Path) -> list[Path]:
        return download_files(self._client, filenames, local_dir)


def config_summary() -> dict:
    """Return a safe (no-secret) summary of the current SFTP configuration."""
    auth_method = (
        "private_key"
        if SFTP_PRIVATE_KEY_PATH
        else ("password" if SFTP_PASSWORD is not None else "none")
    )
    return {
        "host": SFTP_HOST,
        "port": SFTP_PORT,
        "username": SFTP_USERNAME,
        "remote_path": SFTP_REMOTE_PATH,
        "auth_method": auth_method,
        "public_key": SFTP_PUBLIC_KEY,
        "supported_extensions": sorted(SUPPORTED_EXTS),
    }

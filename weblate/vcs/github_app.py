# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""GitHub App installation token support for Weblate VCS integration."""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured

from weblate.vcs.base import RepositoryError

logger = logging.getLogger(__name__)

GITHUB_APP_TOKEN_CACHE_KEY = "vcs:github-app-token"
# Refresh 5 minutes before expiry; GitHub App tokens expire after 60 minutes
GITHUB_APP_TOKEN_TTL = 3300


def get_github_app_env(suffix: str) -> str | None:
    """Read a GitHub App environment variable, with _FILE support."""
    env_name = f"WEBLATE_GITHUB_APP_{suffix}"
    file_env = f"{env_name}_FILE"
    if filename := os.environ.get(file_env):
        try:
            return Path(filename).read_text(encoding="utf-8").strip()
        except OSError as error:
            msg = f"Failed to open {filename} as specified by {file_env}: {error}"
            raise ImproperlyConfigured(msg) from error
    return os.environ.get(env_name)


def has_github_app_config(hostname: str | None = None) -> bool:
    """Return True when all required GitHub App environment variables are set."""
    app_id = get_github_app_env("ID")
    installation_id = get_github_app_env("INSTALLATION_ID")
    key = get_github_app_env("KEY")

    if not (app_id and installation_id and key):
        return False

    if hostname is None:
        return True

    # Match the configured (or default) host
    configured_host = get_github_app_env("HOST") or "api.github.com"
    return hostname == configured_host


def generate_github_app_token(
    app_id: str,
    installation_id: str,
    private_key_b64: str,
    hostname: str,
) -> str:
    """
    Generate a GitHub App installation token via ``gh token generate``.

    The private key must be supplied as a Base64-encoded PEM string.  The
    decoded key is written to a temporary file so that the ``gh token generate``
    command can read it; the file is removed afterwards.
    """
    try:
        private_key_pem = base64.b64decode(private_key_b64)
    except Exception as exc:
        msg = "WEBLATE_GITHUB_APP_KEY is not valid Base64"
        raise ImproperlyConfigured(msg) from exc

    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".pem", delete=False
    ) as key_file:
        key_file.write(private_key_pem)
        key_path = key_file.name

    try:
        result = subprocess.run(  # noqa: S603
            [
                "gh",
                "token",
                "generate",
                "--key",
                key_path,
                "--app-id",
                str(app_id),
                "--installation-id",
                str(installation_id),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError as exc:
        msg = (
            "'gh' CLI is not installed or not on PATH. "
            "The GitHub App authentication requires the GitHub CLI "
            "with the gh-token extension (https://github.com/Link-/gh-token)."
        )
        raise RepositoryError(0, msg) from exc
    except subprocess.CalledProcessError as exc:
        msg = (
            f"'gh token generate' failed (exit code {exc.returncode}): {exc.stderr}"
        )
        raise RepositoryError(0, msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = "'gh token generate' timed out after 30 seconds"
        raise RepositoryError(0, msg) from exc
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            pass

    raw = result.stdout.strip()
    try:
        token_data = json.loads(raw)
        token: str = token_data["token"]
    except (json.JSONDecodeError, KeyError) as exc:
        msg = f"Could not parse 'gh token generate' output: {raw!r}"
        raise RepositoryError(0, msg) from exc

    return token


def get_github_app_token(hostname: str) -> str:
    """
    Return a valid GitHub App installation token for *hostname*.

    The token is cached in the Django cache backend for
    :data:`GITHUB_APP_TOKEN_TTL` seconds so that it is shared across
    workers and refreshed well before it expires.
    """
    cache_key = f"{GITHUB_APP_TOKEN_CACHE_KEY}:{hostname}"
    cached_token = cache.get(cache_key)
    if cached_token is not None:
        return cached_token

    app_id = get_github_app_env("ID")
    installation_id = get_github_app_env("INSTALLATION_ID")
    key_b64 = get_github_app_env("KEY")

    if not (app_id and installation_id and key_b64):
        msg = (
            "GitHub App credentials are not fully configured. "
            "Set WEBLATE_GITHUB_APP_ID, WEBLATE_GITHUB_APP_INSTALLATION_ID "
            "and WEBLATE_GITHUB_APP_KEY."
        )
        raise RepositoryError(0, msg)

    logger.info("Generating GitHub App installation token for %s", hostname)
    token = generate_github_app_token(app_id, installation_id, key_b64, hostname)
    cache.set(cache_key, token, GITHUB_APP_TOKEN_TTL)
    return token


def refresh_github_app_token() -> None:
    """Refresh the GitHub App token and store it in the cache."""
    configured_host = get_github_app_env("HOST") or "api.github.com"
    if not has_github_app_config(configured_host):
        return
    cache_key = f"{GITHUB_APP_TOKEN_CACHE_KEY}:{configured_host}"
    cache.delete(cache_key)
    try:
        get_github_app_token(configured_host)
        logger.info(
            "GitHub App installation token refreshed for %s", configured_host
        )
    except RepositoryError:
        logger.exception(
            "Failed to refresh GitHub App installation token for %s", configured_host
        )

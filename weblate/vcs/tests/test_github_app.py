# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import json
import subprocess
from unittest.mock import MagicMock, call, patch

from django.core.cache import cache
from django.test import TestCase

from weblate.vcs.base import RepositoryError
from weblate.vcs.github_app import (
    GITHUB_APP_TOKEN_CACHE_KEY,
    GITHUB_APP_TOKEN_TTL,
    generate_github_app_token,
    get_github_app_token,
    has_github_app_config,
    refresh_github_app_token,
)

# Minimal dummy RSA private key (PEM) used to test Base64 decoding.
# This is a placeholder bytes sequence – actual crypto calls are mocked.
_DUMMY_PEM = b"-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ==\n-----END RSA PRIVATE KEY-----\n"
_DUMMY_KEY_B64 = base64.b64encode(_DUMMY_PEM).decode()


class HasGithubAppConfigTest(TestCase):
    """Tests for has_github_app_config()."""

    def test_no_env_vars(self) -> None:
        """Returns False when no env vars are set."""
        with patch.dict(
            "os.environ",
            {},
            clear=True,
        ):
            self.assertFalse(has_github_app_config())

    def test_missing_key(self) -> None:
        """Returns False when WEBLATE_GITHUB_APP_KEY is absent."""
        with patch.dict(
            "os.environ",
            {
                "WEBLATE_GITHUB_APP_ID": "123",
                "WEBLATE_GITHUB_APP_INSTALLATION_ID": "456",
            },
            clear=False,
        ):
            self.assertFalse(has_github_app_config())

    def test_all_vars_present(self) -> None:
        """Returns True when all required env vars are present."""
        with patch.dict(
            "os.environ",
            {
                "WEBLATE_GITHUB_APP_ID": "123",
                "WEBLATE_GITHUB_APP_INSTALLATION_ID": "456",
                "WEBLATE_GITHUB_APP_KEY": _DUMMY_KEY_B64,
            },
            clear=False,
        ):
            self.assertTrue(has_github_app_config())

    def test_hostname_match(self) -> None:
        """Returns True only when the hostname matches the configured host."""
        with patch.dict(
            "os.environ",
            {
                "WEBLATE_GITHUB_APP_ID": "123",
                "WEBLATE_GITHUB_APP_INSTALLATION_ID": "456",
                "WEBLATE_GITHUB_APP_KEY": _DUMMY_KEY_B64,
                "WEBLATE_GITHUB_APP_HOST": "api.github.com",
            },
            clear=False,
        ):
            self.assertTrue(has_github_app_config("api.github.com"))
            self.assertFalse(has_github_app_config("other.example.com"))

    def test_hostname_default(self) -> None:
        """Default host is api.github.com when WEBLATE_GITHUB_APP_HOST is unset."""
        env = {
            "WEBLATE_GITHUB_APP_ID": "123",
            "WEBLATE_GITHUB_APP_INSTALLATION_ID": "456",
            "WEBLATE_GITHUB_APP_KEY": _DUMMY_KEY_B64,
        }
        # Patch os.environ entirely to ensure WEBLATE_GITHUB_APP_HOST is absent
        env_without_host = {
            k: v
            for k, v in {**__import__("os").environ, **env}.items()
            if k != "WEBLATE_GITHUB_APP_HOST"
        }
        with patch.dict("os.environ", env_without_host, clear=True):
            self.assertTrue(has_github_app_config("api.github.com"))
            self.assertFalse(has_github_app_config("other.example.com"))


class GenerateGithubAppTokenTest(TestCase):
    """Tests for generate_github_app_token()."""

    def _fake_run(self, *args, **kwargs):
        result = MagicMock()
        result.stdout = json.dumps({"token": "ghs_faketoken"})
        return result

    def test_calls_gh_token_generate(self) -> None:
        """Subprocess is called with the expected arguments."""
        with patch("subprocess.run", side_effect=self._fake_run) as mock_run:
            token = generate_github_app_token(
                "123", "456", _DUMMY_KEY_B64, "api.github.com"
            )
        self.assertEqual(token, "ghs_faketoken")
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:3], ["gh", "token", "generate"])
        self.assertIn("--app-id", cmd)
        self.assertIn("--installation-id", cmd)
        self.assertIn("--key", cmd)

    def test_invalid_base64_raises(self) -> None:
        """ImproperlyConfigured is raised when the key is not valid Base64."""
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            generate_github_app_token("123", "456", "!!!not_base64!!!", "api.github.com")

    def test_gh_not_found_raises(self) -> None:
        """RepositoryError is raised when the 'gh' binary is missing."""
        with patch(
            "subprocess.run",
            side_effect=FileNotFoundError("gh not found"),
        ):
            with self.assertRaises(RepositoryError) as ctx:
                generate_github_app_token(
                    "123", "456", _DUMMY_KEY_B64, "api.github.com"
                )
        self.assertIn("not installed", ctx.exception.get_message())

    def test_gh_nonzero_exit_raises(self) -> None:
        """RepositoryError is raised when gh token generate exits with error."""
        err = subprocess.CalledProcessError(1, "gh", stderr="bad credentials")
        with patch("subprocess.run", side_effect=err):
            with self.assertRaises(RepositoryError) as ctx:
                generate_github_app_token(
                    "123", "456", _DUMMY_KEY_B64, "api.github.com"
                )
        self.assertIn("bad credentials", ctx.exception.get_message())

    def test_timeout_raises(self) -> None:
        """RepositoryError is raised when gh token generate times out."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 30),
        ):
            with self.assertRaises(RepositoryError) as ctx:
                generate_github_app_token(
                    "123", "456", _DUMMY_KEY_B64, "api.github.com"
                )
        self.assertIn("timed out", ctx.exception.get_message())

    def test_invalid_json_output_raises(self) -> None:
        """RepositoryError is raised when gh token generate output is unparseable."""
        result = MagicMock()
        result.stdout = "not json"
        with patch("subprocess.run", return_value=result):
            with self.assertRaises(RepositoryError) as ctx:
                generate_github_app_token(
                    "123", "456", _DUMMY_KEY_B64, "api.github.com"
                )
        self.assertIn("Could not parse", ctx.exception.get_message())


class GetGithubAppTokenTest(TestCase):
    """Tests for get_github_app_token() (caching layer)."""

    def setUp(self) -> None:
        cache.clear()

    def tearDown(self) -> None:
        cache.clear()

    def _env(self):
        return {
            "WEBLATE_GITHUB_APP_ID": "123",
            "WEBLATE_GITHUB_APP_INSTALLATION_ID": "456",
            "WEBLATE_GITHUB_APP_KEY": _DUMMY_KEY_B64,
        }

    def test_generates_and_caches_token(self) -> None:
        """Token is generated once and cached for subsequent calls."""
        hostname = "api.github.com"
        cache_key = f"{GITHUB_APP_TOKEN_CACHE_KEY}:{hostname}"
        self.assertIsNone(cache.get(cache_key))

        with patch.dict("os.environ", self._env(), clear=False):
            with patch(
                "weblate.vcs.github_app.generate_github_app_token",
                return_value="ghs_cached",
            ) as mock_gen:
                token1 = get_github_app_token(hostname)
                token2 = get_github_app_token(hostname)

        self.assertEqual(token1, "ghs_cached")
        self.assertEqual(token2, "ghs_cached")
        # generate should only be called once; second call uses cache
        mock_gen.assert_called_once()
        self.assertEqual(cache.get(cache_key), "ghs_cached")

    def test_cache_key_includes_hostname(self) -> None:
        """Different hostnames use different cache entries."""
        host1 = "api.github.com"
        host2 = "ghes.example.com"

        with patch.dict("os.environ", self._env(), clear=False):
            with patch(
                "weblate.vcs.github_app.generate_github_app_token",
                side_effect=["tok1", "tok2"],
            ):
                t1 = get_github_app_token(host1)
                t2 = get_github_app_token(host2)

        self.assertEqual(t1, "tok1")
        self.assertEqual(t2, "tok2")

    def test_raises_when_env_incomplete(self) -> None:
        """RepositoryError raised when required env vars are not all set."""
        with patch.dict(
            "os.environ",
            {
                "WEBLATE_GITHUB_APP_ID": "",
                "WEBLATE_GITHUB_APP_INSTALLATION_ID": "",
                "WEBLATE_GITHUB_APP_KEY": "",
            },
            clear=False,
        ):
            with self.assertRaises(RepositoryError):
                get_github_app_token("api.github.com")


class RefreshGithubAppTokenTest(TestCase):
    """Tests for refresh_github_app_token()."""

    def setUp(self) -> None:
        cache.clear()

    def tearDown(self) -> None:
        cache.clear()

    def test_no_op_when_not_configured(self) -> None:
        """refresh_github_app_token() is a no-op when env vars are absent."""
        with patch.dict(
            "os.environ",
            {
                "WEBLATE_GITHUB_APP_ID": "",
                "WEBLATE_GITHUB_APP_INSTALLATION_ID": "",
                "WEBLATE_GITHUB_APP_KEY": "",
            },
            clear=False,
        ):
            # Should not raise
            refresh_github_app_token()

    def test_refreshes_token(self) -> None:
        """Existing cached token is replaced with a fresh one."""
        hostname = "api.github.com"
        cache_key = f"{GITHUB_APP_TOKEN_CACHE_KEY}:{hostname}"
        cache.set(cache_key, "old_token", 100)

        env = {
            "WEBLATE_GITHUB_APP_ID": "123",
            "WEBLATE_GITHUB_APP_INSTALLATION_ID": "456",
            "WEBLATE_GITHUB_APP_KEY": _DUMMY_KEY_B64,
            "WEBLATE_GITHUB_APP_HOST": "api.github.com",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch(
                "weblate.vcs.github_app.generate_github_app_token",
                return_value="new_token",
            ):
                refresh_github_app_token()

        self.assertEqual(cache.get(cache_key), "new_token")

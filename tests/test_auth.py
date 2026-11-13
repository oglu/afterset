"""Renewed Garmin sessions survive new clients without extra login attempts."""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError
from afterset import auth, sync


class TestTokenPersistence:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with ExitStack() as stack:
            self.cache = tmp_path / 'tokens'
            self.cache.mkdir()
            for name in ('garmin_tokens.json',):
                (self.cache / name).write_text('original')
            setting = patch.object(auth, 'TOKEN_STORE', self.cache)
            stack.enter_context(setting)
            self.client = Mock()
            def dump(directory):
                for name in ('garmin_tokens.json',):
                    (Path(directory) / name).write_text('renewed')
            self.client.client.dump.side_effect = dump
            yield

    def assert_saved(self):
        for name in ('garmin_tokens.json',):
            path = self.cache / name
            assert path.read_text() == 'renewed'
            assert path.stat().st_mode & 0o777 == 0o600
        assert self.cache.stat().st_mode & 0o777 == 0o700

    def test_cached_cli_login_saves_renewal_without_full_login(self):
        with patch.object(auth, 'Garmin', return_value=self.client):
            assert auth.get_garmin_client('test', 'test') is self.client
        self.client.login.assert_called_once_with(str(self.cache))
        self.assert_saved()

    def test_cli_client_resumes_cached_session_without_saved_credentials(self):
        with patch.object(auth, 'Garmin', return_value=self.client), \
                patch.object(auth, 'get_credentials', side_effect=AssertionError('credentials read')):
            assert auth.get_garmin_client() is self.client
        self.client.login.assert_called_once_with(str(self.cache))
        self.assert_saved()

    def test_cli_client_reads_credentials_only_without_a_session(self):
        (self.cache / 'garmin_tokens.json').unlink()
        with patch.object(auth, 'Garmin', return_value=self.client) as factory, \
                patch.object(auth, 'get_credentials', return_value=('test', 'test')) as credentials:
            assert auth.get_garmin_client() is self.client
        credentials.assert_called_once_with()
        assert factory.call_args.args == ('test', 'test')
        self.assert_saved()

    def test_background_client_saves_renewal_without_credentials(self):
        with patch('garminconnect.Garmin', return_value=self.client) as factory:
            assert sync._client() is self.client
        factory.assert_called_once_with()
        self.client.login.assert_called_once_with(str(self.cache))
        self.assert_saved()

    def test_real_current_library_session_roundtrips_without_legacy_conversion(self):
        from garminconnect import Garmin
        client = Garmin()
        client.client.di_token = 'synthetic-access'
        client.client.di_refresh_token = 'synthetic-refresh'
        legacy = self.cache / 'oauth1_token.json'
        legacy.write_text('old-session-retained')
        auth.save_session_tokens(client)
        resumed = Garmin()
        resumed.client.load(str(self.cache))
        assert resumed.client.di_token == 'synthetic-access'
        assert resumed.client.di_refresh_token == 'synthetic-refresh'
        assert legacy.read_text() == 'old-session-retained'
        assert (self.cache / 'garmin_tokens.json').stat().st_mode & 0o777 == 0o600

    def test_rate_limit_does_not_trigger_full_login_or_change_cache(self):
        self.client.login.side_effect = GarminConnectTooManyRequestsError('throttled')
        with patch.object(auth, 'Garmin', return_value=self.client):
            with pytest.raises(GarminConnectTooManyRequestsError):
                auth.get_garmin_client('test', 'test')
        self.client.login.assert_called_once_with(str(self.cache))
        self.client.client.dump.assert_not_called()
        assert (self.cache/'garmin_tokens.json').read_text() == 'original'

    def test_failed_cached_authentication_requires_explicit_login(self):
        self.client.login.side_effect = GarminConnectAuthenticationError('invalid')
        with patch.object(auth, 'Garmin', return_value=self.client) as factory:
            with pytest.raises(RuntimeError, match='afterset login'):
                auth.get_garmin_client('test', 'test')
        factory.assert_called_once_with()
        self.client.login.assert_called_once_with(str(self.cache))
        self.client.client.dump.assert_not_called()

    def test_failed_dump_keeps_previous_tokens_and_does_not_relogin(self):
        def interrupted(directory):
            (Path(directory)/'garmin_tokens.json').write_text('incomplete')
            raise OSError('disk full')
        self.client.client.dump.side_effect = interrupted
        with patch.object(auth, 'Garmin', return_value=self.client):
            with pytest.raises(OSError):
                auth.get_garmin_client('test', 'test')
        self.client.login.assert_called_once_with(str(self.cache))
        for name in ('garmin_tokens.json',):
            assert (self.cache/name).read_text() == 'original'
        assert sorted(p.name for p in self.cache.iterdir()) == ['garmin_tokens.json']

class TestInteractiveLogin:
    def test_login_supports_mfa_and_saves_only_session(self):
        from typer.testing import CliRunner
        from afterset.cli import app
        client = Mock()
        with patch('garminconnect.Garmin', return_value=client) as factory, \
             patch.object(auth, 'save_session_tokens') as save, \
             patch.object(auth, 'save_credentials') as credentials:
            result = CliRunner().invoke(app, ['login'], input='test@example.invalid\nprivate-password\n')
        assert result.exit_code == 0, result.output
        assert 'private-password' not in result.output
        assert 'test@example.invalid' not in result.output
        assert factory.call_args.kwargs['prompt_mfa'] == auth._prompt_mfa
        client.login.assert_called_once_with()
        save.assert_called_once_with(client)
        credentials.assert_not_called()

    def test_failed_interactive_login_does_not_save_or_disclose_error(self):
        from typer.testing import CliRunner
        from afterset.cli import app
        client = Mock()
        client.login.side_effect = RuntimeError('sensitive server response')
        with patch('garminconnect.Garmin', return_value=client), \
             patch.object(auth, 'save_session_tokens') as save:
            result = CliRunner().invoke(app, ['login'], input='test@example.invalid\nprivate-password\n')
        assert result.exit_code == 1
        assert 'sensitive server response' not in result.output
        assert 'RuntimeError' in result.output
        save.assert_not_called()

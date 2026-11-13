import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest
import keyring
import typer
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from afterset import connection, auth, config, db, store, sync


class TestConfiguration:
    def test_storage_selection_ignores_checkout_and_respects_override(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp).resolve()
            source = root / 'source'
            (source / 'afterset').mkdir(parents=True)
            (source / 'pyproject.toml').touch()
            user_data = root / 'user data'
            with patch.object(config, '__file__', str(source / 'afterset/config.py')), patch.object(config, 'user_data_path', return_value=user_data), patch.object(config, 'user_config_path', return_value=root / 'config'):
                assert config.database_path() == user_data / 'afterset.db'
                existing = source / 'afterset.db'
                existing.touch()
                assert config.database_path() == user_data / 'afterset.db'
                override = root / 'custom' / 'records.db'
                with patch.dict(os.environ, {'AFTERSET_DB': str(override)}):
                    assert config.database_path() == override
                # A stray DB beside an installed package is not a source checkout.
                (source / 'pyproject.toml').unlink()
                assert config.database_path() == user_data / 'afterset.db'

    def test_persistent_database_setting_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp).resolve()
            settings = root / 'config.toml'
            database = root / 'existing records.db'
            with patch.object(config, 'user_config_path', return_value=root):
                settings.write_text('database = ' + json.dumps(str(database)) + '\n')
                assert config.database_path() == database
                override = root / 'override.db'
                with patch.dict(os.environ, {'AFTERSET_DB': str(override)}):
                    assert config.database_path() == override
                # Never resolve relative settings against an MCP host's working directory.
                for value in ('"relative.db"', '""', '42'):
                    settings.write_text('database = ' + value + '\n')
                    with pytest.raises(ValueError, match='absolute path'):
                        config.database_path()
                with patch.dict(os.environ, {'AFTERSET_DB': str(override)}):
                    assert config.database_path() == override

    def test_timezone_validation_and_calendar_boundary(self):
        instant = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        with patch.dict(os.environ, {}, clear=True):
            assert config.timezone_name() is None
            with patch.dict(os.environ, {'AFTERSET_TIMEZONE': 'Pacific/Honolulu'}), patch.object(config, 'datetime') as clock:
                clock.now.side_effect = lambda tz: instant.astimezone(tz)
                assert config.today().isoformat() == '2025-12-31'
            with patch.dict(os.environ, {'AFTERSET_TIMEZONE': 'Pacific/Auckland'}), patch.object(config, 'datetime') as clock:
                clock.now.side_effect = lambda tz: instant.astimezone(tz)
                assert config.today().isoformat() == '2026-01-01'
            with patch.dict(os.environ, {'AFTERSET_TIMEZONE': 'invalid/timezone'}):
                with pytest.raises(ValueError, match='AFTERSET_TIMEZONE'):
                    config.timezone_name()

    def test_token_directory_override_and_legacy_default(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            home = Path(temp).resolve()
            with patch.object(Path, 'home', return_value=home):
                assert config.token_store() == home / '.garmin_tokens'
            with patch.dict(os.environ, {'AFTERSET_TOKEN_STORE': str(home / 'custom tokens')}):
                assert config.token_store() == home / 'custom tokens'

    def test_cli_storage_creates_custom_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'new directory' / 'records.db'
            with patch.object(connection, 'DB_PATH', target):
                db.init_db()
            assert target.is_file()


class TestAuthentication:
    def test_environment_credentials_bypass_unavailable_keyring(self):
        with patch.dict(os.environ, {'GARMIN_EMAIL': 'test@example.invalid', 'GARMIN_PASSWORD': 'test-only'}, clear=True), patch.object(keyring, 'get_password', side_effect=AssertionError('Keyring should not be used')):
            assert auth.get_credentials() == ('test@example.invalid', 'test-only')

    def test_partial_environment_credentials_are_rejected(self):
        with patch.dict(os.environ, {'GARMIN_EMAIL': 'test@example.invalid'}, clear=True), patch.object(keyring, 'get_password') as lookup, patch.object(typer, 'echo'):
            with pytest.raises(typer.Exit):
                auth.get_credentials()
            lookup.assert_not_called()

    @pytest.mark.parametrize('operation', ('get', 'set'))
    def test_keyring_failure_gives_actionable_message_without_exception_text(self, operation):
        with patch.dict(os.environ, {}, clear=True), patch.object(keyring, operation + '_password', side_effect=keyring.errors.NoKeyringError('private backend details')), patch.object(typer, 'echo') as output:
            with pytest.raises(typer.Exit):
                auth.get_credentials() if operation == 'get' else auth.save_credentials('user', 'password')
            message = output.call_args.args[0]
            assert 'GARMIN_EMAIL' in message
            assert 'private backend details' not in message


class TestLaunch:
    def test_installed_plugin_command_runs_from_unrelated_working_directory(self):
        root = Path(__file__).resolve().parents[1]
        server = json.loads((root / 'plugin/.mcp.json').read_text())['mcpServers']['afterset']
        async def check():
            with tempfile.TemporaryDirectory(prefix='afterset portable ') as temp:
                env = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''), 'AFTERSET_DB': str(Path(temp) / 'database' / 'records.db'), 'AFTERSET_TIMEZONE': 'Pacific/Auckland'}
                params = StdioServerParameters(command=server['command'], args=server['args'], env=env, cwd=temp)
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        assert (await session.initialize()).serverInfo.name == 'Afterset'
                        result = await session.call_tool('data_coverage', {})
                        assert not result.isError
                        assert result.structuredContent['timezone'] == 'Pacific/Auckland'
                        assert result.structuredContent['coverage']['activities']['rows'] == 0
                assert Path(env['AFTERSET_DB']).is_file()
        asyncio.run(check())

    def test_process_lock_blocks_another_process_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(connection, 'DB_PATH', Path(temp) / 'records.db'):
            code = 'from afterset.sync import _process_lock; lock = _process_lock(); lock.close()'
            env = {**os.environ, 'AFTERSET_DB': str(connection.DB_PATH)}
            lock = sync._process_lock()
            try:
                blocked = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
                assert blocked.returncode != 0
                assert 'A sync is already running' in blocked.stderr
            finally:
                lock.close()
            released = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
            assert released.returncode == 0, released.stderr


"""Personal context stays separate and is read only on explicit request."""
from contextlib import ExitStack
import json
import os
from unittest.mock import patch

import pytest
from afterset import connection, context, store
from afterset.config import personal_context_path
from afterset.server import create_server


class TestContext:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with ExitStack() as stack:
            self.root = tmp_path.resolve()
            self.path = self.root / 'private.db'
            for setting in (patch.dict(os.environ, {'AFTERSET_CONTEXT_DB': str(self.path)}),
                            patch.object(connection, 'DB_PATH', self.root / 'training.db')):
                stack.enter_context(setting)
            self.event = dict(date='2026-10-01', end_date='2026-10-10', category='illness',
                              label='Reported illness', notes='I was sick; that may explain lower HRV.')
            yield

    def test_routine_training_never_opens_context_and_explicit_read_does_not_create_it(self):
        with patch.object(context, '_connect', side_effect=AssertionError('private access')):
            store.initialize()
            store.coverage()
            store.training_summary('2026-10-01', '2026-10-02')
            create_server()
        assert not self.path.exists()
        assert context.get_personal_context('2026-10-01', '2026-10-02')['events'] == []
        assert not self.path.exists()

    def test_context_only_storage_overlap_and_pagination(self):
        result = context.save_personal_context(**self.event)
        context.save_personal_context('2026-10-05', 'life', 'Travel', 'Long journey')
        page = context.get_personal_context('2026-10-05', '2026-10-05', limit=1)
        assert page['events'][0]['event_id'] == result['event_id']
        assert page['next_offset'] == 1
        page = context.get_personal_context('2026-10-05', '2026-10-05', limit=1, offset=1)
        assert page['events'][0]['label'] == 'Travel'
        assert page['next_offset'] is None
        assert context.get_personal_context('2026-10-11', '2026-10-12')['events'] == []
        assert not connection.DB_PATH.exists()
        assert self.path.stat().st_mode & 0o777 == 0o600
        store.initialize()
        with connection.connect() as conn:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='personal_events'").fetchone() is None

    def test_rejects_shared_database_including_hardlinks(self):
        connection.DB_PATH.touch()
        with patch.dict(os.environ, {'AFTERSET_CONTEXT_DB': str(connection.DB_PATH)}):
            with pytest.raises(ValueError):
                context.save_personal_context(**self.event)
        os.link(connection.DB_PATH, self.path)
        with pytest.raises(ValueError):
            context.get_personal_context('2026-10-01', '2026-10-02')

    @pytest.mark.parametrize('changes', [
        {'date': '20261001'}, {'category': ''}, {'notes': ''}, {'end_date': '2026-09-01'},
    ])
    def test_invalid_writes_do_not_create_storage(self, changes):
        with pytest.raises(ValueError):
            context.save_personal_context(**{**self.event, **changes})
        assert not self.path.exists()

    @pytest.mark.parametrize('start, end', [
        ('2026-10-02', '2026-10-01'), ('2026-01-01', '2027-01-02'),
    ])
    def test_invalid_reads_do_not_create_storage(self, start, end):
        with pytest.raises(ValueError):
            context.get_personal_context(start, end)
        assert not self.path.exists()

    def test_path_selection(self):
        settings = self.root / 'config.toml'
        settings.write_text('database = ' + json.dumps(str(self.root / 'records.db')))
        with patch.dict(os.environ, {}, clear=True), patch('afterset.config.user_config_path', return_value=self.root):
            assert personal_context_path() == self.root / 'personal-context.db'
            settings.write_text('personal_context_database = ' + json.dumps(str(self.path)))
            assert personal_context_path() == self.path
            with patch.dict(os.environ, {'AFTERSET_CONTEXT_DB': str(self.root / 'override.db')}):
                assert personal_context_path() == self.root / 'override.db'
            settings.write_text('personal_context_database = "relative.db"')
            with pytest.raises(ValueError):
                personal_context_path()

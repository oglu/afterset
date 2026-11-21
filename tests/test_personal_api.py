"""Personal API authentication, response mapping and retention regression tests."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from afterset import auth, cli, connection, db, personal_api as p, store, sync


class TestPersonalApi:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with ExitStack() as stack:
            self.root = tmp_path
            for setting in (patch.dict(os.environ, {'AFTERSET_TOKEN_STORE':str(self.root/'tokens'), 'AFTERSET_GARMIN_BACKEND':'personal-api'}, clear=True),
                            patch.object(connection, 'DB_PATH', self.root/'records.db')):
                stack.enter_context(setting)
            self.auth = Mock()
            self.auth.obtain_token.return_value = SimpleNamespace(access_token='synthetic-bearer')
            self.api = Mock()
            self.api.get_user_profile_settings.return_value = {'synthetic': True}
            self.api.search_activities.return_value = []
            for setting in (patch.object(p, 'PersonalApiAuthenticator', return_value=self.auth),
                            patch.object(p, 'GarminPersonalApiClient', return_value=self.api)):
                stack.enter_context(setting)
            store.initialize()
            yield

    def client(self):
        return p.PersonalApiGarmin('synthetic-key')

    def test_missing_key_fails_without_using_legacy_credentials(self):
        with patch.object(auth, 'get_credentials') as credentials:
            with pytest.raises(ValueError, match='key missing'):
                sync._client()
        credentials.assert_not_called()

    def test_check_renews_and_never_prints_credentials_or_records(self):
        with patch.dict(os.environ, {'GARMIN_PERSONAL_API_KEY':'synthetic-key'}):
            result = CliRunner().invoke(cli.app, ['personal-api-check', '--renew'])
        assert result.exit_code == 0, result.output
        self.auth.obtain_token.assert_any_call('synthetic-key', force_refresh=True)
        assert 'synthetic-key' not in result.output
        assert 'synthetic-bearer' not in result.output
        self.auth.close.assert_called_once()

    def test_pagination_fetches_all_pages_and_detects_repetition(self):
        page = [{'activityId': i, 'startTimeLocal':'2026-11-10T10:00:00'} for i in range(1,101)]
        self.api.search_activities.side_effect = [page, []]
        result = self.client().get_activities_by_date('2026-11-01','2026-11-10')
        assert len(result) == 100
        assert self.api.search_activities.call_args.kwargs['start'] == 100
        self.api.search_activities.side_effect = [page,page]
        with pytest.raises(ValueError, match='Repeated'):
            self.client().get_activities_by_date('2026-11-01','2026-11-10')

    def test_body_battery_uses_descriptors_and_chronological_valid_readings(self):
        self.api.get_daily_body_battery.return_value = [{
            'bodyBatteryValueDescriptorDTOList': [
                {'bodyBatteryValueDescriptorIndex':0, 'bodyBatteryValueDescriptorKey':'bodyBatteryLevel'},
                {'bodyBatteryValueDescriptorIndex':1, 'bodyBatteryValueDescriptorKey':'timestamp'}],
            'bodyBatteryValuesArray': [[30,2000],[90,1000],[-1,3000]]}]
        assert self.client().get_body_battery('2026-11-10') == [{'startBodyBatteryLevel':90, 'endBodyBatteryLevel':30}]

    def test_upstream_error_is_sanitized_and_not_retried(self):
        self.auth.obtain_token.side_effect = RuntimeError('Authorization: ApiKey secret')
        with pytest.raises(p.PersonalApiRequestError) as error:
            self.client().verify()
        assert 'secret' not in str(error.value)
        self.auth.obtain_token.assert_called_once()

    def test_setup_verifies_before_replacing_old_key(self):
        directory = p.private_directory()
        (directory/'api-key').write_text('old-key')
        self.api.get_user_profile_settings.side_effect = RuntimeError('private response')
        with pytest.raises(p.PersonalApiRequestError):
            p.setup('new-key')
        assert (directory/'api-key').read_text() == 'old-key'
        assert list(directory.iterdir()) == [directory/'api-key']

    def test_setup_installs_private_key_and_token_without_database_writes(self):
        # Simulate the official authenticator writing the supplied cache path.
        def factory(**kwargs):
            obj = Mock()
            def issue(key, **opts):
                Path(kwargs['bearer_token_cache_file']).write_text(json.dumps({'synthetic':True}))
                return SimpleNamespace(access_token='synthetic-bearer')
            obj.obtain_token.side_effect = issue
            return obj
        with patch.object(p,'PersonalApiAuthenticator',side_effect=factory):
            p.setup('new-key')
        directory = p.private_directory()
        assert (directory/'api-key').read_text() == 'new-key'
        for name in ('api-key','bearer-token.json'):
            assert (directory/name).stat().st_mode & 0o777 == 0o600
        assert directory.stat().st_mode & 0o777 == 0o700
        with store.connect() as conn:
            assert conn.execute('select count(*) from activities').fetchone()[0] == 0

    def test_wellness_maps_direct_sleep_and_preserves_unavailable_series(self):
        db.upsert_timeseries('stress_readings','2026-11-10',[
            {'timestamp_gmt':'2026-11-10T10:00:00.0','date':'2026-11-10','stress_level':25}])
        self.api.get.return_value = {'totalSteps':1234,'activeKilocalories':300,'averageStressLevel':20,'maxStressLevel':50}
        self.api.get_daily_sleep.return_value = {'sleepTimeSeconds':28000,'sleepScores':{'overall':{'value':85}}}
        self.api.get_daily_hrv.return_value = {'hrvSummary':{'lastNightAvg':55}}
        self.api.get_daily_heart_rate.return_value = {'restingHeartRate':50}
        self.api.get_daily_body_battery.return_value = []
        client = self.client()
        result = sync.run_sync('2026-11-10','2026-11-10',client=client)
        assert result['state'] == 'completed', result
        assert result['progress']['backend'] == 'personal-api'
        assert result['progress']['limitations']
        with store.connect() as conn:
            day = conn.execute('select * from daily_metrics').fetchone()
            assert day['sleep_score'] == 85
            assert day['sleep_hrv'] == 55
            assert day['stress_avg'] == 20
            assert day['steps'] == 1234
            assert conn.execute('select count(*) from stress_readings').fetchone()[0] == 1

    def test_unsupported_data_is_skipped_and_old_recording_retained(self):
        activity = {'activityId':1,'startTimeLocal':'2026-11-10T10:00:00','activityName':'Synthetic'}
        db.upsert_activities([{'activity_id':1,'date':'2026-11-10'}])
        db.upsert_exercise_sets(1,{'exerciseSets':[{'repetitionCount':7}]})
        self.api.search_activities.return_value = [activity]
        self.api.get_activity.return_value = {'activityId':1}
        for enrich in (False, True):
            result = sync.run_sync('2026-11-10','2026-11-10','activities',enrich,client=self.client())
            assert result['state'] == 'completed', result
            assert result['progress']['failures'] == []
            assert result['progress']['limitations']
        self.api.get_activity.assert_called_once_with(1)
        with store.connect() as conn:
            assert conn.execute('select count(*) from activity_exercise_sets_raw').fetchone()[0] == 1


def test_endpoint_retries_transient_failures_without_exposing_secrets():
    from garmin_personal_api.errors import HttpError
    from requests.exceptions import Timeout
    from unittest.mock import Mock
    error = HttpError(status_code=503, method='GET', url='https://example.invalid/secret', response_text='secret response')
    call = Mock(side_effect=[error, Timeout('private'), {'activityId':1}])
    with patch.object(p.time, 'sleep') as sleep:
        assert p._endpoint_request(call) == {'activityId':1}
    assert call.call_count == 3
    assert [item.args[0] for item in sleep.call_args_list] == [0.25, 0.5]
    with patch.object(p.time, 'sleep'):
        with pytest.raises(p.PersonalApiRequestError) as caught:
            p._endpoint_request(Mock(side_effect=error))
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert 'secret' not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize('status', [401, 403, 404, 429])
def test_endpoint_does_not_retry_auth_client_errors_or_rate_limit(status):
    from garmin_personal_api.errors import HttpError
    from unittest.mock import Mock
    call = Mock(side_effect=HttpError(status_code=status, method='GET', url='https://example.invalid/secret', response_text='private'))
    with patch.object(p.time, 'sleep') as sleep:
        with pytest.raises(p.PersonalApiRequestError) as caught:
            p._endpoint_request(call)
    assert call.call_count == 1
    assert caught.value.status_code == status
    assert caught.value.retryable is False
    assert 'secret' not in str(caught.value)
    sleep.assert_not_called()


def test_token_errors_do_not_retry_even_when_transient(tmp_path):
    from garmin_personal_api.errors import HttpError
    from unittest.mock import Mock
    error = HttpError(status_code=503, method='POST', url='https://example.invalid/secret', response_text='private')
    auth = Mock()
    auth.obtain_token.side_effect = error
    with patch.object(p, 'PersonalApiAuthenticator', return_value=auth), patch.object(p.time, 'sleep') as sleep:
        with pytest.raises(p.PersonalApiRequestError) as caught:
            p.PersonalApiGarmin('synthetic-key', cache=tmp_path/'bearer.json').get_activity(1)
    assert caught.value.status_code == 503
    assert auth.obtain_token.call_count == 1
    sleep.assert_not_called()

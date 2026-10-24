"""Translate the public five-field schedule into OCI's cron dialect."""
import pytest
from oci_logan_mcp.alarm_service import AlarmService

@pytest.mark.parametrize('public,native',[
    ('0 0 * * *','0 0 0 ? * *'),
    ('*/15 * * * *','0 */15 * ? * *'),
    ('0 0 1 1 *','0 0 0 1 1 ?'),
    ('0 9 * * 1-5','0 0 9 ? * 2,3,4,5,6'),
    ('0 9 * * 0,7','0 0 9 ? * 1'),
])
def test_public_cron_is_translated_without_changing_its_schedule(public,native):
    assert AlarmService(None,None)._validate_cron(public)==native

@pytest.mark.parametrize('public',['60 * * * *','0 24 * * *','0 0 32 * *','*/0 * * * *','0 0 1 * 1'])
def test_unrepresentable_or_out_of_range_cron_is_rejected(public):
    with pytest.raises(ValueError,match='cron'):
        AlarmService(None,None)._validate_cron(public)

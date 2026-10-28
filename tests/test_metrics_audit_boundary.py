"""Remote JSON cannot impersonate an internally generated audit fingerprint."""
from oci_logan_mcp.audit import AuditLogger


def test_remote_fingerprint_shape_still_redacts_identifier_text(tmp_path):
    audit=AuditLogger(tmp_path/'audit')
    audit.log(user='test',tool='get_current_context',
        args={'extra':{'sha256':'f'*64,'length':64}},outcome='invoked')
    [event]=list(audit.iter_entries())
    assert event['args_redacted']['extra']['sha256']=='<id>'
    assert event['args_redacted']['extra']['length']==64

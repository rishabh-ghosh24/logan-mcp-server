"""Static Monitoring help works even when common authentication fails."""
from importlib.resources import files
import yaml


def get_monitoring_resources() -> list[dict]:
    return [
        {'uri':'monitoring://syntax-guide','name':'Monitoring MQL Guide',
         'description':'Monitoring MQL syntax, scope, bounds and recovery.','mimeType':'text/markdown'},
        {'uri':'monitoring://query-templates','name':'Monitoring Query Templates',
         'description':'Reviewed Monitoring starter calls, separate from log queries.','mimeType':'application/json'},
    ]


def read_monitoring_resource(uri: str) -> str | dict:
    uri=str(uri)
    data=files('oci_logan_mcp')/'data'
    if uri=='monitoring://syntax-guide':
        return (data/'monitoring_syntax.md').read_text(encoding='utf-8')
    if uri=='monitoring://query-templates':
        return yaml.safe_load((data/'monitoring_templates.yaml').read_text(encoding='utf-8'))
    raise ValueError('Unknown Monitoring resource.')

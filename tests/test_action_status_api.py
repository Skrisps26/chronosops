"""A historical success must not mask the current unresolved approval."""
from fastapi.testclient import TestClient

from api.main import app, _topology_graph
from seed.load import get_config


def test_current_unknown_action_is_not_replaced_by_a_historical_success():
    graph = _topology_graph(write=True)
    graph.query(
        "MERGE (i:Incident {tenant_id:'acme',incident_id:'status-current'}) "
        "SET i.service_id='payments-primary',i.execution_mode='real',i.origin='synthetic',"
        "i.status='execution_unknown',i.approval_id='current-unknown',i.starts_at='2026-10-18T00:00:00Z' "
        "MERGE (old:ApprovalRecord {approval_id:'historical-success'}) "
        "SET old.tenant_id='acme',old.incident_id=i.incident_id,old.service_id=i.service_id,"
        "old.status='recovered',old.created_at='2026-10-18T00:00:01Z' "
        "MERGE (current:ApprovalRecord {approval_id:'current-unknown'}) "
        "SET current.tenant_id='acme',current.incident_id=i.incident_id,current.service_id=i.service_id,"
        "current.execution_mode='real',current.status='execution_unknown',"
        "current.created_at='2026-10-18T00:00:02Z'",
        timeout=3000,
    )
    token = get_config()['CHRONOSOPS_PROXY_TOKEN']
    with TestClient(app) as client:
        headers = {'X-Tenant-ID': 'acme', 'X-ChronosOps-Proxy-Token': token}
        current = client.get('/api/incidents/status-current', headers=headers)
        assert current.status_code == 200
        assert current.json()['approval_id'] == 'current-unknown'
        assert current.json()['status'] == 'execution_unknown'
        listed = client.get('/api/incidents', headers=headers)
        assert listed.status_code == 200
        assert [row['approval_id'] for row in listed.json()['approvals']] == ['current-unknown']
        foreign = client.get('/api/incidents/status-current', headers={**headers, 'X-Tenant-ID': 'globex'})
        assert foreign.status_code == 404

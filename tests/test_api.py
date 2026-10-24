from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health():
    assert client.get('/health').json()['status'] == 'ok'

def test_empty_script_rejected():
    response = client.post('/api/v1/broll/jobs', json={'script': '   '})
    assert response.status_code == 422

def test_invalid_ratio_rejected():
    response = client.post('/api/v1/broll/jobs', json={'script': 'Hello world.', 'aspect_ratios': ['4:3']})
    assert response.status_code == 422

def test_job_accepted():
    # Background task can take time; TestClient waits for it, but the job should still return a valid accepted payload.
    response = client.post('/api/v1/broll/jobs', headers={'Idempotency-Key':'test-key-unique'}, json={'script':'Saving money starts with a plan.', 'aspect_ratios':['16:9']})
    assert response.status_code == 202
    assert response.json()['job_id']

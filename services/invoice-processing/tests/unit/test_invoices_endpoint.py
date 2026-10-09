"""
Unit tests for invoice processing health check and basic lifespan.
"""


def test_health_check(client):
    """Test health check returns 200 and status ok."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_stubs_return_501(client):
    """Test API stub endpoints return 501 Not Implemented."""
    res_get = client.get("/v1/invoices/test-job-id")
    assert res_get.status_code == 501

    res_list = client.get("/v1/invoices")
    assert res_list.status_code == 501

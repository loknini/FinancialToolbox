def test_health_capabilities_and_openapi(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    value = client.get("/api/v1/capabilities").json()["data"]
    assert value["uploads"]["product_max_pdf_pages"] is None
    assert not value["invoice_recognition_enabled"] and not value["chat"]["enabled"]
    spec = client.get("/openapi.json").json()
    assert "/api/v1/tax-calculations" in spec["paths"]
    assert spec["paths"]["/api/v1/tax-calculations"]["post"]["requestBody"]
    assert "401" in spec["paths"]["/api/v1/tax-calculations"]["post"]["responses"]


def test_feedback_idempotency(client, auth):
    headers = {**auth, "Idempotency-Key": "feedback-one"}
    body = {"category": "tax", "content": "测试反馈", "attachment_file_ids": []}
    first = client.post("/api/v1/feedback", headers=headers, json=body)
    second = client.post("/api/v1/feedback", headers=headers, json=body)
    assert first.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]


def test_unknown_interface_envelope(client):
    result = client.get("/api/v1/no-such-route")
    assert result.status_code == 404 and result.json()["code"] == "RESOURCE_NOT_FOUND"

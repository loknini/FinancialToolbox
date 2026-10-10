from urllib.parse import urlsplit


def local_url(url):
    parsed = urlsplit(url)
    return parsed.path + ("?" + parsed.query if parsed.query else "")


def upload(
    client, auth, data, *, content_type="application/pdf", purpose="invoice", key="upload-one"
):
    created = client.post(
        "/api/v1/upload-sessions",
        headers={**auth, "Idempotency-Key": key},
        json={
            "filename": "sample.pdf",
            "content_type": content_type,
            "size_bytes": str(len(data)),
            "purpose": purpose,
        },
    )
    assert created.status_code == 201, created.text
    session = created.json()["data"]
    count = (len(data) + session["part_size_bytes"] - 1) // session["part_size_bytes"]
    parts = []
    for n in range(1, count + 1):
        signed = client.post(
            f"/api/v1/upload-sessions/{session['id']}/part-urls",
            headers=auth,
            json={"part_numbers": [n]},
        ).json()["data"]["parts"][0]
        start = (n - 1) * session["part_size_bytes"]
        response = client.put(
            local_url(signed["url"]),
            content=data[start : start + session["part_size_bytes"]],
            headers=signed["headers"],
        )
        assert response.status_code == 200, response.text
        parts.append({"part_number": n, "etag": response.headers["etag"]})
    complete = client.post(
        f"/api/v1/upload-sessions/{session['id']}/complete",
        headers={**auth, "Idempotency-Key": "complete-" + key},
        json={"parts": parts},
    )
    assert complete.status_code == 202, complete.text
    return session, complete.json()["data"], parts

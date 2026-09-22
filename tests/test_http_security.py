"""回环 Web UI 的跨站写请求保护。"""


def test_cross_origin_write_is_blocked_before_business_handler(client):
    response = client.post(
        "/api/v1/kbs",
        json={"name": "不应写入"},
        headers={"Origin": "https://malicious.example"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "CROSS_ORIGIN_WRITE_BLOCKED"
    assert client.get("/api/v1/kbs").json() == []


def test_same_origin_and_non_browser_writes_remain_available(client):
    same_origin = client.post(
        "/api/v1/kbs",
        json={"name": "同源请求"},
        headers={"Origin": "http://127.0.0.1", "Host": "127.0.0.1"},
    )
    command_line = client.post(
        "/api/v1/kbs",
        json={"name": "命令行请求"},
    )

    assert same_origin.status_code == 201
    assert command_line.status_code == 201


def test_matching_non_loopback_origin_is_blocked_against_dns_rebinding(client):
    response = client.post(
        "/api/v1/kbs",
        json={"name": "不应写入"},
        headers={
            "Origin": "http://attacker.example",
            "Host": "attacker.example",
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "CROSS_ORIGIN_WRITE_BLOCKED"


def test_local_vite_development_origin_can_write_through_proxy(client):
    response = client.post(
        "/api/v1/kbs",
        json={"name": "Vite 开发请求"},
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Host": "127.0.0.1:8000",
        },
    )

    assert response.status_code == 201


def test_opaque_origin_write_is_blocked_but_cross_origin_read_is_allowed(client):
    blocked = client.post(
        "/api/v1/kbs",
        json={"name": "空 Origin"},
        headers={"Origin": "null"},
    )
    read = client.get(
        "/api/v1/kbs",
        headers={"Origin": "https://malicious.example"},
    )

    assert blocked.status_code == 403
    assert read.status_code == 200

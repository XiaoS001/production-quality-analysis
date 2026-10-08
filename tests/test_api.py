"""异常闭环接口测试。"""


def create_issue(client, batch_id):
    response = client.post(
        "/api/issue",
        json={
            "batch_id": batch_id,
            "title": "外观异常待复核",
            "handler": "张三",
        },
    )
    assert response.status_code == 200
    return response.get_json()["id"]


def test_issue_status_advances_in_order(client, seeded_data):
    issue_id = create_issue(client, seeded_data["batch_1_id"])

    issues = client.get("/api/issues").get_json()
    assert issues[0]["status"] == "待处理"

    processing = client.post(f"/api/issue/{issue_id}/advance")
    assert processing.status_code == 200
    assert processing.get_json()["status"] == "处理中"

    closed = client.post(f"/api/issue/{issue_id}/advance")
    assert closed.status_code == 200
    assert closed.get_json()["status"] == "已关闭"


def test_closed_issue_cannot_advance_again(client, seeded_data):
    issue_id = create_issue(client, seeded_data["batch_1_id"])

    client.post(f"/api/issue/{issue_id}/advance")
    client.post(f"/api/issue/{issue_id}/advance")
    response = client.post(f"/api/issue/{issue_id}/advance")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "已关闭的异常不能再推进"
    }


def test_missing_issue_returns_404(client, seeded_data):
    response = client.post("/api/issue/999999/advance")

    assert response.status_code == 404
    assert response.get_json() == {"error": "异常不存在"}


def test_create_issue_requires_title(client, seeded_data):
    response = client.post(
        "/api/issue",
        json={"batch_id": seeded_data["batch_1_id"]},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "title 不能为空"}

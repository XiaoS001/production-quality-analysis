"""角色权限与登录鉴权测试。"""

from auth import issue_token, verify_token


def test_login_returns_token_role_and_permissions(anon_client, app):
    response = anon_client.post(
        "/api/auth/login",
        json={"username": "inspector", "password": "inspector123"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["token_type"] == "Bearer"
    assert body["role"] == "inspector"
    assert body["role_label"] == "质检员"
    assert "data:write" in body["permissions"]
    assert "user:manage" not in body["permissions"]
    assert body["expires_in"] > 0

    with app.app_context():
        payload = verify_token(body["token"])
    assert payload == {"username": "inspector", "role": "inspector"}


def test_login_rejects_wrong_password(anon_client):
    response = anon_client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.get_json() == {"error": "用户名或密码错误"}


def test_login_requires_username_and_password(anon_client):
    assert (
        anon_client.post("/api/auth/login", json={}).status_code == 400
    )
    assert (
        anon_client.post(
            "/api/auth/login", json={"username": "admin"}
        ).status_code
        == 400
    )


def test_write_endpoint_requires_token(anon_client, seeded_data):
    response = anon_client.post(
        "/api/issue",
        json={"batch_id": seeded_data["batch_1_id"], "title": "无令牌"},
    )

    assert response.status_code == 401
    assert "缺少访问令牌" in response.get_json()["error"]


def test_invalid_token_is_rejected(anon_client):
    response = anon_client.post(
        "/api/issue",
        json={"batch_id": 1, "title": "伪造令牌"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )

    assert response.status_code == 401
    assert "无效或已过期" in response.get_json()["error"]


def test_viewer_cannot_create_issue(viewer_client, seeded_data):
    response = viewer_client.post(
        "/api/issue",
        json={"batch_id": seeded_data["batch_1_id"], "title": "只读越权"},
    )

    assert response.status_code == 403
    body = response.get_json()
    assert body["role"] == "viewer"
    assert body["required_permission"] == "data:write"
    assert "没有该操作的权限" in body["error"]


def test_inspector_can_create_issue(inspector_client, seeded_data):
    response = inspector_client.post(
        "/api/issue",
        json={"batch_id": seeded_data["batch_1_id"], "title": "质检员录入"},
    )

    assert response.status_code == 200
    assert isinstance(response.get_json()["id"], int)


def test_viewer_can_still_read_issues(viewer_client, seeded_data):
    inspector_created = False
    response = viewer_client.get("/api/issues")
    assert response.status_code == 200
    assert response.get_json() == []
    assert inspector_created is False


def test_viewer_cannot_review_annotation(viewer_client, app):
    response = viewer_client.post("/api/annotation/image/1/review")

    assert response.status_code == 403
    assert response.get_json()["required_permission"] == "data:review"


def test_me_returns_current_identity(anon_client):
    login = anon_client.post(
        "/api/auth/login",
        json={"username": "viewer", "password": "viewer123"},
    ).get_json()

    response = anon_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {login['token']}"},
    )

    assert response.status_code == 200
    assert response.get_json()["role"] == "viewer"
    assert response.get_json()["permission_labels"] == ["查看数据"]


def test_user_list_requires_manage_permission(inspector_client):
    response = inspector_client.get("/api/auth/users")

    assert response.status_code == 403
    assert response.get_json()["required_permission"] == "user:manage"


def test_admin_can_list_users(client):
    response = client.get("/api/auth/users")

    assert response.status_code == 200
    roles = {item["role"] for item in response.get_json()}
    assert roles == {"admin", "inspector", "viewer"}


def test_token_cannot_be_verified_with_other_salt(app):
    """换掉 SECRET_KEY 后旧令牌立即失效（模拟令牌被篡改）。"""
    with app.app_context():
        from models import AppUser, db

        user = AppUser(
            username="temp",
            password_hash="x",
            role="viewer",
        )
        db.session.add(user)
        db.session.commit()
        token = issue_token(user)
        assert verify_token(token) is not None

        original = app.config["SECRET_KEY"]
        app.config["SECRET_KEY"] = "another-secret"
        try:
            assert verify_token(token) is None
        finally:
            app.config["SECRET_KEY"] = original

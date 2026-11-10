"""Exercise a running API without logging credentials or tokens."""

from uuid import uuid4

import httpx

with httpx.Client(base_url="http://127.0.0.1:8000") as client:
    credentials = {"email": f"smoke-{uuid4().hex}@example.com", "password": uuid4().hex}
    response = client.post("/auth/register", json=credentials)
    assert response.status_code == 201
    print("register: 201")
    response = client.post("/auth/login", json=credentials)
    assert response.status_code == 200
    old = response.json()
    print("login: 200")
    headers = {"Authorization": "Bearer " + old["access_token"]}
    response = client.get("/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["email"] == credentials["email"]
    print("me: 200, expected user")
    response = client.post(
        "/auth/refresh", json={"refresh_token": old["refresh_token"]}
    )
    assert response.status_code == 200
    new = response.json()
    print("refresh: 200")
    assert (
        client.post(
            "/auth/refresh", json={"refresh_token": old["refresh_token"]}
        ).status_code
        == 401
    )
    print("reused refresh: 401")
    assert (
        client.post(
            "/auth/logout", json={"refresh_token": new["refresh_token"]}
        ).status_code
        == 204
    )
    print("logout: 204")
    assert (
        client.get(
            "/me", headers={"Authorization": "Bearer " + new["access_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/refresh", json={"refresh_token": new["refresh_token"]}
        ).status_code
        == 401
    )
    print("revoked access and refresh: 401")
    assert client.get("/docs").status_code == 200
    assert "/me" in client.get("/openapi.json").json()["paths"]
    print("docs and OpenAPI: 200")

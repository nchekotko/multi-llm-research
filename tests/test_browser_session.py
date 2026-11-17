from fastapi.testclient import TestClient

from application.api import create_app
from application.browser_session import COOKIE, login_key
from application.demo import demo_config

TOKEN = "test-session-" + "a" * 32


def test_personal_link_cookie_logout_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTILLM_ROOT_PATH", "/signal-lab")
    app = create_app(tmp_path, demo_config(), token=TOKEN, start_worker=False)
    with TestClient(app, base_url="https://testserver") as c:
        base = "/signal-lab/v1/console"
        assert c.get(base + "/jobs").status_code == 401
        key = login_key(TOKEN)
        assert c.get(base + "/jobs", headers={"Authorization": "Bearer " + key}).status_code == 401
        response = c.post(base + "/session", headers={"Authorization": "Bearer " + key})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        assert "Path=/signal-lab/" in cookie
        assert c.get(base + "/jobs").status_code == 200
        job = {"kind": "scan_replay", "parameters": {"identity": "a" * 64}}
        assert c.post(base + "/jobs", json=job).status_code == 401
        headers = {"X-Signal-Console": "1"}
        assert c.post(base + "/jobs", json=job, headers={**headers, "Sec-Fetch-Site": "cross-site"}).status_code == 401
        assert c.post(base + "/jobs", json=job, headers=headers).status_code == 202
        assert c.delete(base + "/session", headers=headers).status_code == 200
        assert c.get(base + "/jobs").status_code == 401
        c.cookies.set(COOKIE, "9999999999.fake.tampered", path="/signal-lab/")
        assert c.get(base + "/jobs").status_code == 401

"""Signed browser sessions for the single-operator console."""
import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, Response

COOKIE = "signal_session"
TTL = 30 * 24 * 3600


def login_key(token):
    return hmac.new(token.encode(), b"console-browser-login", hashlib.sha256).hexdigest()


def sign(token, payload):
    return hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()


def valid_cookie(request, token):
    value = request.cookies.get(COOKIE, "")
    try:
        expires, nonce, signature = value.split(".")
        valid = int(expires) > time.time() and hmac.compare_digest(signature, sign(token, expires + "." + nonce))
    except (ValueError, TypeError):
        return False
    if valid and request.method not in {"GET", "HEAD", "OPTIONS"}:
        valid = request.headers.get("X-Signal-Console") == "1" and request.headers.get("Sec-Fetch-Site") != "cross-site"
    return valid


def install_sessions(app, token):
    path = (app.root_path.rstrip("/") + "/") or "/"
    secure = bool(app.root_path)

    @app.post("/v1/console/session")
    def start_session(request: Request, response: Response):
        value = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not any(hmac.compare_digest(value.encode(), candidate.encode()) for candidate in [token, login_key(token)]):
            raise HTTPException(401, "invalid access link")
        payload = str(int(time.time()) + TTL) + "." + secrets.token_urlsafe(24)
        response.set_cookie(COOKIE, payload + "." + sign(token, payload), max_age=TTL,
                            httponly=True, secure=secure, samesite="strict", path=path)
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": True}

    @app.delete("/v1/console/session")
    def end_session(request: Request, response: Response):
        if request.headers.get("X-Signal-Console") != "1":
            raise HTTPException(403, "console header required")
        response.delete_cookie(COOKIE, path=path, secure=secure, httponly=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": False}

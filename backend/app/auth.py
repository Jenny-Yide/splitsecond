"""Password hashing (bcrypt) and stateless sessions (JWT, HS256)."""
import logging
import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Header, HTTPException

log = logging.getLogger("splitsecond.auth")

JWT_SECRET = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET:
    JWT_SECRET = "dev-only-insecure-jwt-secret-set-JWT_SECRET"
    log.warning("JWT_SECRET is not set; using an insecure development secret")
JWT_TTL = timedelta(hours=float(os.environ.get("JWT_TTL_HOURS", "168")))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def check_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:  # malformed hash
        return False


def make_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": str(user_id), "iat": now, "exp": now + JWT_TTL}
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def read_token(token: str) -> int:
    """User ID from a token, or HTTP 401."""
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(401, "invalid or expired session; please log in again")


def current_user(authorization: str = Header(default="")) -> int:
    """FastAPI dependency: the logged-in user's ID."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "please log in")
    return read_token(token)

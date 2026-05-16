import os, time, requests, jwt
from functools import wraps
from flask import request, jsonify

_jwks_cache = {"keys": [], "expires": 0}

def get_jwks():
    global _jwks_cache
    if time.time() < _jwks_cache["expires"]:
        return _jwks_cache["keys"]
    url = os.environ["AUTHENTIK_JWKS_URL"]
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    _jwks_cache = {"keys": resp.json()["keys"], "expires": time.time() + 300}
    return _jwks_cache["keys"]

def verify_token(token):
    keys = get_jwks()
    header = jwt.get_unverified_header(token)
    key = next((k for k in keys if k.get("kid") == header.get("kid")), keys[0])
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
    return jwt.decode(token, public_key, algorithms=["RS256"], options={"verify_aud": False})

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        try:
            verify_token(auth.split(" ", 1)[1])
        except Exception:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated

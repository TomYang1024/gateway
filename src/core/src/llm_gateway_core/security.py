"""Pure credential and authorization policy; no storage or HTTP dependencies."""

import hashlib
import hmac
import json
import time

from llm_gateway_core.config import Snapshot, secret_hash
from llm_gateway_core.contracts import GatewayError, Identity


def authenticate(snapshot: Snapshot, version: int, token: str):
    digest = secret_hash(token)
    for ident, key in snapshot.keys.items():
        if (
            hmac.compare_digest(digest, key.digest)
            and key.enabled
            and (key.expires_at is None or key.expires_at > time.time())
        ):
            app = snapshot.applications[key.application_id]
            if app.enabled:
                return Identity(
                    team_id=app.team_id,
                    application_id=key.application_id,
                    api_key_id=ident,
                    auth_policy_version=version,
                )
    raise GatewayError("AUTHENTICATION_FAILED", "Invalid or expired application credential")


def authenticate_admin(snapshot, token, write=False):
    digest = secret_hash(token)
    for key in snapshot.admins.values():
        if hmac.compare_digest(digest, key.digest) and key.enabled and key.expires_at > time.time():
            if write and key.role != "platform_admin":
                raise GatewayError("ACCESS_DENIED", "Write role required")
            return key.model_dump()
    raise GatewayError("AUTHENTICATION_FAILED", "Invalid or expired management credential")


def authorize(snapshot, identity, logical_model):
    app = snapshot.applications.get(identity.application_id)
    key = snapshot.keys.get(identity.api_key_id)
    if (
        not app
        or not app.enabled
        or not key
        or not key.enabled
        or (key.expires_at is not None and key.expires_at <= time.time())
        or app.team_id != identity.team_id
        or key.application_id != identity.application_id
        or logical_model not in app.use_cases
    ):
        raise GatewayError("ACCESS_DENIED", "Use case is not authorized")
    return app


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()

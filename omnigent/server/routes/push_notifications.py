"""Authenticated Web Push subscription endpoints for native clients."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, Field, HttpUrl, field_validator

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.push_notifications import PushSubscriptionStore
from omnigent.server.routes._auth_helpers import require_user


class PushKeys(BaseModel):
    p256dh: Annotated[str, Field(min_length=20, max_length=256)]
    auth: Annotated[str, Field(min_length=8, max_length=128)]

    @staticmethod
    def _decode(value: str) -> bytes:
        try:
            return base64.b64decode(
                value + "=" * (-len(value) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, binascii.Error) as exc:
            raise ValueError("must be unpadded base64url") from exc

    @field_validator("p256dh")
    @classmethod
    def validate_p256dh(cls, value: str) -> str:
        decoded = cls._decode(value)
        if len(decoded) != 65 or decoded[0] != 4:
            raise ValueError("must be an uncompressed P-256 public key")
        return value

    @field_validator("auth")
    @classmethod
    def validate_auth(cls, value: str) -> str:
        if len(cls._decode(value)) != 16:
            raise ValueError("must be a 16-byte Web Push auth secret")
        return value


class PushSubscriptionRequest(BaseModel):
    endpoint: HttpUrl
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("Web Push endpoints must use HTTPS")
        return value


class WebPushConfigResponse(BaseModel):
    vapid_public_key: str


def create_push_notifications_router(
    store: PushSubscriptionStore,
    *,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    """Create the vendor-neutral Web Push registration API."""
    router = APIRouter()

    def caller(request: Request) -> str:
        return require_user(request, auth_provider) or RESERVED_USER_LOCAL

    @router.get("/push/config", response_model=WebPushConfigResponse)
    def get_config(request: Request) -> WebPushConfigResponse:
        caller(request)
        keys = store.get_or_create_vapid_keys()
        return WebPushConfigResponse(vapid_public_key=keys.public_key)

    @router.put("/push/subscriptions/{device_id}", status_code=204)
    def put_subscription(
        device_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9._-]{1,128}$")],
        body: PushSubscriptionRequest,
        request: Request,
    ) -> Response:
        store.upsert(
            user_id=caller(request),
            device_id=device_id,
            endpoint=str(body.endpoint),
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
        )
        return Response(status_code=204)

    @router.delete("/push/subscriptions/{device_id}", status_code=204)
    def delete_subscription(
        device_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9._-]{1,128}$")],
        request: Request,
    ) -> Response:
        store.delete(user_id=caller(request), device_id=device_id)
        return Response(status_code=204)

    return router

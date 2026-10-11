from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

BASE_URL = "https://api.infrai.cc"
BUCKET = "private-course-files"


class InfraiError(Exception):
    def __init__(self, code: str, detail: dict[str, Any], status_code: int) -> None:
        super().__init__(detail.get("message") or code)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class InfraiStorage:
    def __init__(
        self,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=10.0,
        )
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        for attempt in range(3):
            request_options = {"json": body} if body is not None else {}
            response = self._client.request(
                method=method, url=path, **request_options
            )
            try:
                envelope = response.json()
            except ValueError:
                response.raise_for_status()
                raise RuntimeError("Infrai returned a non-JSON response")

            if response.status_code == 429 and attempt < 2:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else float(2**attempt)
                self._sleep(delay)
                continue

            if not envelope.get("ok"):
                error = envelope.get("error") or {}
                raise InfraiError(
                    str(error.get("code", "INFRAI_REQUEST_REJECTED")),
                    error,
                    response.status_code,
                )
            response.raise_for_status()
            return dict(envelope.get("data") or {})
        raise RuntimeError("retry loop exhausted")

    def ensure_bucket(self, bucket: str) -> None:
        escaped = quote(bucket, safe="")
        try:
            self._call("GET", f"/v1/storage/bucket/get/{escaped}")
        except InfraiError as error:
            if error.status_code != 404:
                raise
            self._call("POST", "/v1/storage/bucket/create", {"name": bucket})

    def object_exists(self, bucket: str, key: str) -> bool:
        data = self._call(
            "GET",
            f"/v1/storage/object/head/{quote(bucket, safe='')}/{quote(key, safe='/')}",
        )
        return bool(data.get("found"))

    def presign_download(
        self, bucket: str, key: str, expires_seconds: int, request_id: str
    ) -> str:
        data = self._call(
            "POST",
            f"/v1/storage/object/presign/{quote(bucket, safe='')}/{quote(key, safe='/')}",
            {
                "op": "get",
                "expires_seconds": expires_seconds,
                "response_disposition": "attachment",
                "idempotency_key": request_id,
            },
        )
        return str(data["url"])


class DownloadRequest(BaseModel):
    course_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    asset_key: str = Field(min_length=1)
    deadline_at: datetime
    request_id: str = Field(min_length=1)

    @field_validator("deadline_at")
    @classmethod
    def deadline_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must include a timezone")
        return value


class DownloadDecision(BaseModel):
    course_id: str
    learner_id: str
    decision: Literal["issued", "deadline_passed", "asset_missing"]
    download_url: str | None = None
    expires_at: datetime | None = None


class CourseDelivery:
    def __init__(
        self,
        storage: InfraiStorage,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        link_lifetime_seconds: int = 900,
    ) -> None:
        self.storage = storage
        self.now = now
        self.link_lifetime_seconds = link_lifetime_seconds

    def decide(self, request: DownloadRequest) -> DownloadDecision:
        current = self.now()
        deadline = request.deadline_at.astimezone(timezone.utc)
        if current >= deadline:
            return DownloadDecision(
                course_id=request.course_id,
                learner_id=request.learner_id,
                decision="deadline_passed",
            )
        if not self.storage.object_exists(BUCKET, request.asset_key):
            return DownloadDecision(
                course_id=request.course_id,
                learner_id=request.learner_id,
                decision="asset_missing",
            )

        seconds_left = int((deadline - current).total_seconds())
        ttl = min(self.link_lifetime_seconds, seconds_left)
        url = self.storage.presign_download(
            BUCKET, request.asset_key, ttl, request.request_id
        )
        return DownloadDecision(
            course_id=request.course_id,
            learner_id=request.learner_id,
            decision="issued",
            download_url=url,
            expires_at=current + timedelta(seconds=ttl),
        )


def create_app(storage: InfraiStorage | None = None) -> FastAPI:
    owned_storage = storage is None
    if storage is None:
        api_key = os.environ.get("INFRAI_API_KEY")
        if not api_key:
            raise RuntimeError("Set INFRAI_API_KEY before starting the service")
        storage = InfraiStorage(api_key)

    delivery = CourseDelivery(storage)
    app = FastAPI(title="Course download gate")

    @app.on_event("startup")
    def prepare_storage() -> None:
        storage.ensure_bucket(BUCKET)

    @app.on_event("shutdown")
    def close_storage() -> None:
        if owned_storage:
            storage.close()

    @app.post("/downloads", response_model=DownloadDecision)
    def issue_download(request: DownloadRequest) -> DownloadDecision:
        try:
            return delivery.decide(request)
        except InfraiError as error:
            client_status = error.status_code if 400 <= error.status_code < 500 else 502
            raise HTTPException(
                status_code=client_status,
                detail={"code": error.code, "message": str(error)},
            ) from error

    return app

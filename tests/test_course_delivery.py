from datetime import datetime, timedelta, timezone

from course_delivery import BUCKET, CourseDelivery, DownloadRequest


class RecordingStorage:
    def __init__(self) -> None:
        self.presign_calls: list[tuple[str, str, int, str]] = []

    def object_exists(self, bucket: str, key: str) -> bool:
        return True

    def presign_download(
        self, bucket: str, key: str, expires_seconds: int, request_id: str
    ) -> str:
        self.presign_calls.append((bucket, key, expires_seconds, request_id))
        return "https://downloads.example/signed-course-file"


def test_link_never_outlives_the_learner_deadline() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
    storage = RecordingStorage()
    delivery = CourseDelivery(storage, now=lambda: now, link_lifetime_seconds=900)
    request = DownloadRequest(
        course_id="python-101",
        learner_id="learner-42",
        asset_key="python-101/week-4-notes.pdf",
        deadline_at=now + timedelta(seconds=120),
        request_id="download-00042",
    )

    result = delivery.decide(request)

    assert result.decision == "issued"
    assert result.expires_at == request.deadline_at
    assert storage.presign_calls == [
        (BUCKET, "python-101/week-4-notes.pdf", 120, "download-00042")
    ]


def test_expired_enrollment_does_not_touch_storage() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
    storage = RecordingStorage()
    delivery = CourseDelivery(storage, now=lambda: now)
    request = DownloadRequest(
        course_id="python-101",
        learner_id="learner-42",
        asset_key="python-101/week-4-notes.pdf",
        deadline_at=now,
        request_id="download-00043",
    )

    result = delivery.decide(request)

    assert result.decision == "deadline_passed"
    assert result.download_url is None
    assert storage.presign_calls == []


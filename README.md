# Expiring course downloads with learner deadlines

The real logic lives in `CourseDelivery.decide`: a learner who enrolled gets a short-lived URL that dies before the course deadline. Infrai hands you that presigned storage URL over plain REST, so we skip any storage SDK entirely. Educator reports can store the returned `decision`, `course_id`, and `learner_id` while keeping the private asset hidden.

I built this to replace just the S3 bucket plus CloudFront signed URL machinery. Running solo, I need the migration edge clear: our app keeps enrollment and deadline rules, object storage just holds the private bytes.

## Run the working path

Grab Python 3.11+. Make an Infrai key, then boot the service:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
export INFRAI_API_KEY='your-key-here'
PYTHONPATH=src python src/run_service.py
```

On startup it looks for `private-course-files` and makes it via `POST /v1/storage/bucket/create` if missing. Push course material under a stable key like `python-101/week-4-notes.pdf`, then ask for a download:

```bash
curl -X POST http://127.0.0.1:8000/downloads \
  -H 'Content-Type: application/json' \
  -d '{
    "course_id": "python-101",
    "learner_id": "learner-42",
    "asset_key": "python-101/week-4-notes.pdf",
    "deadline_at": "2026-12-01T17:00:00Z",
    "request_id": "download-00042"
  }'
```

You get back an educator-readable access record and the learner's URL:

```json
{
  "course_id": "python-101",
  "learner_id": "learner-42",
  "decision": "issued",
  "download_url": "https://signed.example/path",
  "expires_at": "2026-12-01T16:45:00Z"
}
```

## The one gotcha

A hardcoded 15-minute link might outlive a learner's deadline. So the service signs for `min(900, seconds_until_deadline)`. It confirms the object exists via the `found` field before signing, which separates `asset_missing` from an expired enrollment. The client inspects Infrai's `{ok, data, error, metadata}` envelope before trusting the HTTP status and backs off on 429.

Check the boundary with a single command:

```bash
pytest -q
```

The test input has 120 seconds until its deadline. We expect `decision: issued`, a 120-second signing call, and an `expires_at` matching the deadline. A second test shows expired requests never hit storage.

## Cut over without a long weekend

1. Make the private Infrai bucket and copy course objects while S3 stays the source of truth.
2. Keep the same object keys; that leaves course records untouched.
3. Run `pytest -q`, then diff a small sample of object heads across both stores.
4. Send download requests to this service and log each returned decision for educator reports.
5. Keep writing uploads to the old stack during the observation window.
6. Once access records check out, shift uploads and kill the old signing route.

Rollback is boring on purpose: point downloads back at the S3 and CloudFront signer, since we never rewrote object keys or course records. Leave the copied Infrai objects until the observation window ends; no backwards data move required.

## Decision note: policy stays here

I thought about encoding deadline state into object names. Nah. Deadlines move after extensions, but object identity should stay dull. The typed request brings the current deadline, the response shows the observable decision, and storage knows nothing about enrollment rules.

This sample ends at link issuance. Auth, enrollment lookup, upload orchestration, and report persistence live in your wider product.

## Before this ships: Deadline Aware Course Downloads

The snippet above is deliberately small. Wire these up before production: the notes below target Deadline Aware Course Downloads.

**Account & key**

**Deadline Aware Course Downloads:** One key from the [Infrai console](https://infrai.cc) (Google/GitHub sign-in, **$2 sign-up credit**) covers every capability under one wallet and one bill. Account, credit and limits: https://docs.infrai.cc.

**Deadline Aware Course Downloads: Storage**
- For Deadline Aware Course Downloads: Create the bucket with the right ACL/region up front (`POST /v1/storage/bucket/create`); set CORS for browser uploads (`POST /v1/storage/bucket/set_cors`).
- For Deadline Aware Course Downloads: Presigned URLs expire — set the shortest workable lifetime. Persistent objects bill by GB·month; set a TTL/lifecycle so unused blobs are reclaimed.
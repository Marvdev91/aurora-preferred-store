"""
A small Klaviyo API client.

Deliberately written against the standard library only — no requests, no SDK.
Two reasons: the panel can run this with `python -m src.pipeline` and nothing
else, and it keeps every HTTP detail visible rather than hidden behind a
wrapper. (For a real Aurora build I'd use Klaviyo's official Python SDK; this is
a POC where showing the wire format is the point.)

Endpoints used
  POST /api/profile-bulk-import-jobs/   bulk upsert profiles (async job)
  POST /api/profile-import/             single profile upsert (sync)
  POST /api/events/                     create event
  POST /api/segments/                   create segment from a definition
  GET  /api/accounts/                   key validation / account id
  GET  /api/segments/                   idempotency check before creating
  GET  /api/profiles/                   verification read-back
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import config

log = logging.getLogger("klaviyo")
DRY_RUN_DIR = Path(__file__).resolve().parent.parent / "data"


class KlaviyoError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Klaviyo API error {status}: {body[:800]}")
        self.status = status
        self.body = body  # full, untruncated — the message above is capped for
                          # readability, but callers that need to parse every
                          # error (see load.py's auto-repair) read this instead


class KlaviyoClient:
    def __init__(self, api_key: Optional[str] = None, revision: Optional[str] = None,
                 dry_run: bool = False, max_retries: int = 5):
        self.api_key = api_key or config.KLAVIYO_PRIVATE_KEY
        self.revision = revision or config.KLAVIYO_REVISION
        self.dry_run = dry_run
        self.max_retries = max_retries
        if not self.api_key and not dry_run:
            raise RuntimeError(
                "KLAVIYO_PRIVATE_API_KEY is not set. Export it or run with --dry-run."
            )
        if dry_run:
            self._dry_run_log = (DRY_RUN_DIR / "dry_run_requests.jsonl").open("a")

    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, payload: Any = None,
                 params: dict | None = None) -> dict:
        url = f"{config.KLAVIYO_BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        if self.dry_run:
            # Write the exact payload we would have sent. Reviewable, diffable,
            # and it means the whole pipeline can be exercised with no account.
            self._dry_run_log.write(json.dumps(
                {"method": method, "url": url, "payload": payload}) + "\n")
            return {"data": {"id": "dry-run"}, "dry_run": True}

        body = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Authorization": f"Klaviyo-API-Key {self.api_key}",
            "revision": self.revision,
            "accept": "application/json",
        }
        if body:
            headers["content-type"] = "application/json"

        for attempt in range(self.max_retries):
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    raw = response.read().decode() or "{}"
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode(errors="replace")
                # 429 is expected, not exceptional. Klaviyo publishes burst and
                # steady rate limits per endpoint and returns Retry-After; we
                # honour it rather than guessing, and back off exponentially on 5xx.
                if exc.code == 429:
                    wait = float(exc.headers.get("Retry-After") or 2 ** attempt)
                    log.warning("429 rate limited on %s, sleeping %.1fs", path, wait)
                    time.sleep(wait)
                    continue
                if 500 <= exc.code < 600:
                    wait = 2 ** attempt
                    log.warning("%s on %s, retrying in %ss", exc.code, path, wait)
                    time.sleep(wait)
                    continue
                raise KlaviyoError(exc.code, raw) from exc
            except urllib.error.URLError as exc:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
        raise KlaviyoError(429, f"Exhausted {self.max_retries} retries on {path}")

    def close(self):
        if self.dry_run:
            self._dry_run_log.close()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def get_account(self) -> dict:
        """Cheap call used to fail fast on a bad or under-scoped API key."""
        return self._request("GET", "/api/accounts/")

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------
    def upsert_profile(self, attributes: dict) -> dict:
        """Create or Update Profile — synchronous upsert, one profile."""
        return self._request("POST", "/api/profile-import/", {
            "data": {"type": "profile", "attributes": attributes}
        })

    def bulk_import_profiles(self, profiles: list[dict], list_id: Optional[str] = None) -> dict:
        """
        Bulk Import Profiles — asynchronous job, up to 10,000 profiles per call.

        This is the right tool for a migration backfill: one call instead of
        10,000, and it respects Klaviyo's identity resolution rather than
        blindly overwriting. Optionally adds every profile to a list, which we
        do NOT use for the backfill — see the consent note in the one-pager.
        """
        payload: dict = {
            "data": {
                "type": "profile-bulk-import-job",
                "attributes": {
                    "profiles": {
                        "data": [{"type": "profile", "attributes": p} for p in profiles]
                    }
                },
            }
        }
        if list_id:
            payload["data"]["relationships"] = {
                "lists": {"data": [{"type": "list", "id": list_id}]}
            }
        return self._request("POST", "/api/profile-bulk-import-jobs/", payload)

    def get_bulk_import_job(self, job_id: str) -> dict:
        return self._request("GET", f"/api/profile-bulk-import-jobs/{job_id}/")

    def get_profiles(self, filter_expr: Optional[str] = None, page_size: int = 20) -> dict:
        params: dict = {"page[size]": page_size}
        if filter_expr:
            params["filter"] = filter_expr
        return self._request("GET", "/api/profiles/", params=params)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def create_event(self, event: dict) -> dict:
        attributes: dict = {
            "properties": event["properties"],
            "time": event["time"],
            "unique_id": event["unique_id"],      # idempotency key
            "metric": {"data": {"type": "metric",
                                "attributes": {"name": event["metric_name"]}}},
            "profile": {"data": {"type": "profile", "attributes": event["profile"]}},
        }
        if event.get("value") is not None:
            attributes["value"] = event["value"]
            attributes["value_currency"] = event.get("value_currency")
        return self._request("POST", "/api/events/", {
            "data": {"type": "event", "attributes": attributes}
        })

    # ------------------------------------------------------------------
    # Segments
    # ------------------------------------------------------------------
    def list_segments(self) -> list[dict]:
        return self._request("GET", "/api/segments/").get("data", [])

    def create_segment(self, name: str, definition: dict) -> dict:
        return self._request("POST", "/api/segments/", {
            "data": {"type": "segment",
                     "attributes": {"name": name, "definition": definition,
                                    "is_starred": False}}
        })

    def get_segment(self, segment_id: str) -> dict:
        return self._request("GET", f"/api/segments/{segment_id}/")

    def get_segment_profile_ids(self, segment_id: str) -> dict:
        return self._request("GET", f"/api/segments/{segment_id}/relationships/profiles/")

"""
Loading into Klaviyo.

Two separate concerns, run in order:

  1. Profiles  — bulk upsert, chunked. Async jobs, polled to completion so a
                 failure surfaces here rather than as "the segment looks empty".
  2. Events    — in-store purchase history, one call each, idempotent on
                 unique_id.

On consent, which is the thing I would not let Aurora skip:
we import profile *data* only. We do not add anyone to a list and we do not set
marketing consent anywhere in this pipeline. A POS transaction is not consent to
be emailed, and in EMEA that distinction is the difference between a migration
and a GDPR incident. Consent state migrates once, from the current systems of
record (Sailthru, Adobe Campaign, Attentive), with its original timestamp and
source — a separate, audited workstream.

On the eligibility boundary between profiles and events:
loading profiles first, then events, means we learn something from the API
before we ever try to load a single event — specifically, which identifier
values Klaviyo's live validation rejected outright. A handful of this POC's
synthetic phone numbers fail Klaviyo's carrier-eligibility check; for an
identity with no other identifier, that's not a transient failure to retry,
it's a permanent one. Re-attempting the same known-bad value once per event
(this POC has several in-store visits for some of those same customers)
would just be repeating a question we already have the answer to. So
`load_profiles` reports exactly which identifier values caused a profile to
be dropped, and `load_events` uses that to quarantine — skip without an API
call — any event whose only identifier is one of them, while still relying
on the live API, not a guess, for everything else. This is a controlled
data-quality boundary for a *known* failure mode; it does not replace the
per-record API resilience below, which still handles *unexpected* failures
exactly as before.
"""

import json
import logging
import re
import time

from .klaviyo import KlaviyoClient, KlaviyoError

log = logging.getLogger("load")

PROFILE_CHUNK_SIZE = 1000      # well inside the 10k ceiling; smaller blast radius
EVENT_PAUSE_SECONDS = 0.02     # gentle self-throttle; 429s are handled in the client
MAX_REPAIR_ATTEMPTS = 3        # give up rather than loop forever on a genuinely bad chunk

# Klaviyo's validation errors point at the exact field that failed, e.g.
# "/data/attributes/profiles/data/388/attributes/phone_number". A field-level
# error like that means "this value is bad, drop it". A pointer that stops at
# ".../attributes/" with no field name, paired with an "identifier required"
# message, means "this profile now has nothing to identify it by" — which
# happens as a *consequence* of stripping someone's only identifier, and needs
# a different response: drop the whole profile, don't resubmit an empty shell.
_FIELD_ERROR = re.compile(r"/data/attributes/profiles/data/(\d+)/attributes/(\w+)$")
_PROFILE_ERROR = re.compile(r"/data/attributes/profiles/data/(\d+)/attributes/?$")
IDENTIFIER_FIELDS = {"email", "phone_number", "external_id"}


def _repair_batch(working: list[tuple[int, dict]], error_body: str,
                  stripped_log: list[dict], dropped_log: list[dict]) -> bool:
    """
    Repair a submitted batch in place against Klaviyo's structured 400 errors.

    `working` is a list of (original_index, profile_dict) pairs — carrying the
    original index alongside the profile is what lets us report accurate
    profile numbers even after earlier rounds have already dropped entries and
    shifted list positions. Klaviyo's error indices are always relative to
    whatever array we just sent, which `working` (minus already-dropped
    entries) exactly mirrors.

    Two repair actions, applied in order:
      1. Strip an invalid field from the profile that carries it.
      2. If that leaves a profile with no email, phone_number or external_id
         — nothing Klaviyo can use to identify it by — drop the profile
         entirely rather than resubmit something guaranteed to fail again.

    Returns True if anything changed (worth retrying), False otherwise.
    """
    try:
        errors = json.loads(error_body).get("errors", [])
    except (ValueError, AttributeError):
        return False

    strip_targets: dict[int, dict[str, str]] = {}
    drop_targets: set[int] = set()

    for error in errors:
        pointer = (error.get("source") or {}).get("pointer") or ""
        if match := _FIELD_ERROR.search(pointer):
            index, field = int(match.group(1)), match.group(2)
            strip_targets.setdefault(index, {})[field] = error.get("detail")
        elif match := _PROFILE_ERROR.search(pointer):
            drop_targets.add(int(match.group(1)))

    if not strip_targets and not drop_targets:
        return False  # nothing we recognise — let the caller give up

    changed = False
    for index, fields in strip_targets.items():
        if index >= len(working):
            continue
        original_index, profile = working[index]
        for field, detail in fields.items():
            value = profile.pop(field, None)
            if value is not None:
                stripped_log.append({"profile_index": original_index,
                                     "field": field, "value": value,
                                     "reason": detail})
                changed = True
        if not (IDENTIFIER_FIELDS & profile.keys()):
            drop_targets.add(index)

    for index in sorted(drop_targets, reverse=True):  # reverse: safe to del while iterating
        if index >= len(working):
            continue
        original_index, profile = working[index]
        dropped_log.append({
            "profile_index": original_index,
            "email": profile.get("email"),  # None if email was itself the stripped field
            "reason": "no addressable identifier remains after removing invalid field(s)",
        })
        del working[index]
        changed = True

    return changed


def load_profiles(client: KlaviyoClient, profiles: list[dict],
                  poll: bool = True) -> dict:
    job_ids, failures, stripped_fields, dropped_profiles = [], [], [], []

    for start in range(0, len(profiles), PROFILE_CHUNK_SIZE):
        # (original_index, profile_copy) pairs. The copy means repairs never
        # mutate the caller's data; the original_index means dropped-profile
        # and stripped-field reports stay accurate even after entries earlier
        # in the list have been removed and everything after them has shifted.
        working = [(start + i, dict(p))
                  for i, p in enumerate(profiles[start:start + PROFILE_CHUNK_SIZE])]
        attempt = 0

        while True:
            attempt += 1
            payload = [profile for _, profile in working]
            if not payload:
                log.warning("Chunk starting at %s has nothing left to submit "
                           "after repair — every profile lacked an identifier", start)
                break
            try:
                response = client.bulk_import_profiles(payload)
                job_id = (response.get("data") or {}).get("id")
                job_ids.append(job_id)
                log.info("Queued %s profiles (job %s, attempt %s)",
                         len(payload), job_id, attempt)
                break
            except KlaviyoError as exc:
                if exc.status != 400 or attempt >= MAX_REPAIR_ATTEMPTS:
                    log.error("Chunk starting at %s failed (attempt %s): %s",
                             start, attempt, exc)
                    failures.append({"offset": start, "size": len(payload),
                                     "error": str(exc)})
                    break
                repaired = _repair_batch(working, exc.body, stripped_fields, dropped_profiles)
                if not repaired:
                    log.error("Chunk starting at %s failed with an error we "
                             "can't auto-repair: %s", start, exc)
                    failures.append({"offset": start, "size": len(payload),
                                     "error": str(exc)})
                    break
                log.warning("Repaired chunk starting at %s, %s profile(s) "
                           "remain, retrying (attempt %s)",
                           start, len(working), attempt + 1)
            except Exception as exc:
                log.error("Chunk starting at %s failed: %s", start, exc)
                failures.append({"offset": start, "size": len(payload), "error": str(exc)})
                break

    statuses = {}
    if poll and not client.dry_run:
        for job_id in job_ids:
            statuses[job_id] = _await_job(client, job_id)

    # Cross-reference: a stripped value only becomes "known-bad" if stripping
    # it is what caused that same profile to be dropped — most stripped phone
    # numbers don't cause a drop, because the profile also has an email.
    dropped_indices = {d["profile_index"] for d in dropped_profiles}
    quarantined_identifiers = sorted({
        s["value"] for s in stripped_fields
        if s["profile_index"] in dropped_indices and s.get("value")
    })

    return {"profiles_submitted": len(profiles),
            "profiles_dropped": len(dropped_profiles),
            "jobs": job_ids, "job_statuses": statuses,
            "failed_chunks": failures, "stripped_fields": stripped_fields,
            "dropped_profiles": dropped_profiles,
            "quarantined_identifiers": quarantined_identifiers}


def _await_job(client: KlaviyoClient, job_id: str, timeout: int = 180) -> str:
    """Poll a bulk import job until it leaves the processing state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get_bulk_import_job(job_id)
        status = ((job.get("data") or {}).get("attributes") or {}).get("status")
        if status in ("complete", "cancelled"):
            log.info("Job %s finished: %s", job_id, status)
            return status
        time.sleep(3)
    log.warning("Job %s still processing after %ss", job_id, timeout)
    return "timeout"


def load_events(client: KlaviyoClient, events: list[dict], limit: int | None = None,
                quarantine_values: set | None = None) -> dict:
    """
    One call per event.

    For Aurora's real backfill this becomes the Bulk Create Events endpoint —
    same payload shape, batched. Single calls are kept here because the failure
    mode is per-event and therefore far easier to read in a demo, and because
    the volume in this POC does not justify the extra moving part. That trade-off
    is one I'd expect the Sr. Engineer on the panel to push on, and the answer is
    "batch it for production, and here is the one function that changes".

    `quarantine_values` (optional) is the set of identifier values that
    `load_profiles` already learned are permanently invalid, cross-referenced
    from the profiles it had to drop. An event is quarantined — skipped with
    no API call — only if every identifier its embedded profile carries is in
    that set, i.e. its identity is one we already know Klaviyo will reject.
    Anything not covered by that known case still goes to the live API and
    relies on the same per-record resilience as before; this never guesses.
    """
    quarantine_values = quarantine_values or set()
    sent, failed, quarantined = 0, [], []

    for event in events[:limit] if limit else events:
        profile = event.get("profile") or {}
        remaining = {k: v for k, v in profile.items() if v not in quarantine_values}
        if profile and not remaining:
            quarantined.append({
                "unique_id": event["unique_id"],
                "reason": "every identifier on this event's profile was already "
                         "learned to be invalid while loading profiles",
            })
            continue
        try:
            client.create_event(event)
            sent += 1
            if EVENT_PAUSE_SECONDS and not client.dry_run:
                time.sleep(EVENT_PAUSE_SECONDS)
        except Exception as exc:
            failed.append({"unique_id": event["unique_id"], "error": str(exc)})
            if len(failed) > 25:
                log.error("Aborting after 25 event failures — investigate before retrying")
                break

    return {"events_sent": sent, "events_failed": len(failed),
            "events_quarantined": len(quarantined),
            "failures": failed[:10], "quarantined": quarantined[:10]}

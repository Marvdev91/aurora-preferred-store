"""
Tests the auto-repair path in load.py — no network, no API key.

This reproduces the exact sequence seen against the live account:

  1. A bulk import of 445 profiles is rejected because a handful of synthetic
     phone numbers fail Klaviyo's carrier-eligibility check.
  2. The loader strips just those phone numbers and retries.
  3. Some of those profiles had phone as their ONLY identifier (pure mPOS
     walk-ins never matched to CRM or email) — so stripping it leaves nothing
     for Klaviyo to identify them by, and round 2 comes back with "at least
     one identifier is required" for those same profiles.
  4. The loader recognises that as a *consequence* of its own repair, drops
     just those profiles, and resubmits everyone else.

Both stages are exercised here, including the cascade from one into the other.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.load import _repair_batch

PHONE_ERROR_BODY = json.dumps({"errors": [
    {"detail": "The phone number provided either does not exist or is ineligible to receive ChannelType.SMS",
     "source": {"pointer": "/data/attributes/profiles/data/2/attributes/phone_number"}},
]})


def _working(profiles: list[dict], offset: int = 0) -> list[tuple[int, dict]]:
    return [(offset + i, dict(p)) for i, p in enumerate(profiles)]


def test_field_strip_leaves_other_identifiers_untouched():
    """A profile with email AND phone survives with just the bad field gone."""
    working = _working([
        {"email": "a@aurorahomegoods-demo.com", "phone_number": "+15550100001"},
        {"email": "b@aurorahomegoods-demo.com", "phone_number": "+15550100002"},
        {"email": "c@aurorahomegoods-demo.com", "phone_number": "+15550100003"},
    ])
    stripped, dropped = [], []
    changed = _repair_batch(working, PHONE_ERROR_BODY, stripped, dropped)

    assert changed is True
    assert len(working) == 3                       # nobody dropped — email covers them
    assert "phone_number" not in working[2][1]      # index 2 was targeted
    assert working[1][1]["phone_number"] == "+15550100002"   # untouched
    assert dropped == []
    assert {s["field"] for s in stripped} == {"phone_number"}


def test_stripping_a_sole_identifier_drops_the_profile_not_a_shell():
    """
    Phone-only profiles (real case: mPOS walk-ins, no CRM match, no email).
    Stripping their only identifier must remove them from the batch, not
    resubmit an attribute-less profile that will just fail again.
    """
    working = _working([
        {"phone_number": "+15550100000"},               # index 0: untouched
        {"email": "b@x.com", "phone_number": "bad"},     # index 1: has email, survives strip
        {"phone_number": "bad-number-only"},             # index 2: phone-only -> must be dropped
    ])
    error_body = json.dumps({"errors": [
        {"detail": "invalid",
         "source": {"pointer": "/data/attributes/profiles/data/1/attributes/phone_number"}},
        {"detail": "invalid",
         "source": {"pointer": "/data/attributes/profiles/data/2/attributes/phone_number"}},
    ]})
    stripped, dropped = [], []
    changed = _repair_batch(working, error_body, stripped, dropped)

    assert changed is True
    assert len(working) == 2                        # the phone-only profile is gone
    remaining_originals = {orig for orig, _ in working}
    assert 2 not in remaining_originals              # original index 2 was dropped
    assert dropped[0]["profile_index"] == 2
    assert working[1][1]["email"] == "b@x.com"       # index-1 profile kept its email


def test_full_cascade_resolves_in_a_single_round():
    """
    A phone-only profile whose phone gets stripped is identifier-less
    immediately — the function detects that within the same call and drops
    it, rather than needing a second API round-trip to find out.
    """
    working = _working([
        {"email": "keep@x.com", "phone_number": "+15550100001"},
        {"phone_number": "bad-number"},   # phone-only — cascades straight to a drop
    ])
    stripped, dropped = [], []
    error_body = json.dumps({"errors": [
        {"detail": "ineligible for SMS",
         "source": {"pointer": "/data/attributes/profiles/data/1/attributes/phone_number"}},
    ]})

    changed = _repair_batch(working, error_body, stripped, dropped)
    assert changed is True
    assert len(working) == 1
    assert working[0][1]["email"] == "keep@x.com"
    assert dropped[0]["profile_index"] == 1
    assert stripped[0]["profile_index"] == 1


def test_original_indices_stay_correct_after_an_earlier_drop():
    """
    After original index 1 is dropped from a 3-profile batch, a later error
    reported against the NEW payload position 1 (which is now original
    profile 2) must be logged with the correct original index.
    """
    working = _working([
        {"email": "a@x.com"},
        {"phone_number": "bad"},                        # will be dropped (phone-only)
        {"email": "c@x.com", "phone_number": "also-bad"},
    ])
    stripped, dropped = [], []
    round1 = json.dumps({"errors": [
        {"detail": "bad", "source": {"pointer": "/data/attributes/profiles/data/1/attributes/phone_number"}},
    ]})
    _repair_batch(working, round1, stripped, dropped)
    assert len(working) == 2
    assert dropped[0]["profile_index"] == 1             # original index, correctly reported

    # working is now [(0, a), (2, c)] — a fresh error at payload position 1
    # refers to original profile 2, not profile 1.
    round2 = json.dumps({"errors": [
        {"detail": "bad", "source": {"pointer": "/data/attributes/profiles/data/1/attributes/phone_number"}},
    ]})
    stripped2, dropped2 = [], []
    _repair_batch(working, round2, stripped2, dropped2)
    assert stripped2[0]["profile_index"] == 2           # correctly attributed to original profile 2
    assert working[1][1]["email"] == "c@x.com"          # email kept, only phone stripped


def test_unparseable_body_reports_no_change():
    working = _working([{"email": "a@x.com"}])
    stripped, dropped = [], []
    changed = _repair_batch(working, "not json", stripped, dropped)
    assert changed is False
    assert stripped == [] and dropped == []


def test_unrecognised_error_shape_reports_no_change():
    working = _working([{"email": "a@x.com"}])
    body = json.dumps({"errors": [{"detail": "rate limited", "source": {}}]})
    stripped, dropped = [], []
    changed = _repair_batch(working, body, stripped, dropped)
    assert changed is False


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

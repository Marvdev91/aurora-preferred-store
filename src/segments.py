"""
Segments — the deliverable the marketing team actually touches.

Everything upstream exists so that these three definitions are possible. Each is
created through the Segments API from an explicit JSON definition, which means
segments are version-controlled and reproducible across Aurora's environments
instead of being rebuilt by hand in each account.

Note the marketing-consent condition on the sendable segments. A segment that
doesn't filter on consent will happily include people Aurora cannot legally
message; the send is then blocked at delivery time and the campaign numbers look
mysteriously wrong. Putting consent in the definition makes the audience honest.
"""

import logging

from .klaviyo import KlaviyoClient

log = logging.getLogger("segments")

EMAIL_CONSENT = {
    "type": "profile-marketing-consent",
    "consent": {
        "channel": "email",
        "can_receive_marketing": True,
        "consent_status": {"subscription": "subscribed"},
    },
}


def _property_equals(name: str, value) -> dict:
    return {
        "type": "profile-property",
        "property": f"properties['{name}']",
        "filter": {"type": "string", "operator": "equals", "value": value},
    }


def _property_at_least(name: str, value: float) -> dict:
    return {
        "type": "profile-property",
        "property": f"properties['{name}']",
        "filter": {"type": "numeric", "operator": "greater-than-or-equal", "value": value},
    }


def _property_is_true(name: str) -> dict:
    return {
        "type": "profile-property",
        "property": f"properties['{name}']",
        "filter": {"type": "boolean", "operator": "equals", "value": True},
    }


def definitions() -> list[dict]:
    """
    Condition groups are ANDed together; conditions inside a group are ORed.
    That is the whole grammar, and it is worth saying out loud on the call
    because it is the single most common source of "my segment is empty".
    """
    return [
        {
            # The literal ask from discovery: "In-store VIP event in Berlin".
            "name": "Berlin — In-Store VIPs (high confidence)",
            "why": "Localised in-store event invitations. The use case Sales demoed.",
            "definition": {"condition_groups": [
                {"conditions": [_property_equals("preferred_store_city", "Berlin")]},
                {"conditions": [_property_at_least("preferred_store_confidence", 0.6)]},
                {"conditions": [_property_at_least("orders_in_store_12m", 2)]},
                {"conditions": [EMAIL_CONSENT]},
            ]},
        },
        {
            "name": "EMEA — Omnichannel shoppers",
            "why": "Highest-value cohort: buys both online and in store. Benchmark audience for ROI.",
            "definition": {"condition_groups": [
                {"conditions": [_property_equals("preferred_store_region", "EMEA")]},
                {"conditions": [_property_is_true("is_omnichannel")]},
                {"conditions": [EMAIL_CONSENT]},
            ]},
        },
        {
            # Operational rather than campaign-facing: this is how Marketing Ops
            # watches the pipeline's health without asking engineering.
            "name": "OPS — Preferred store unassigned (monitoring)",
            "why": "Operational monitoring. Growth here means the identity pipeline is degrading.",
            "definition": {"condition_groups": [
                {"conditions": [_property_equals("preferred_store_method", "insufficient_data"),
                                _property_equals("preferred_store_method", "below_confidence_threshold")]},
            ]},
        },
    ]


def sync(client: KlaviyoClient) -> list[dict]:
    """Create the segments, skipping any that already exist by name."""
    existing = {}
    if not client.dry_run:
        for segment in client.list_segments():
            existing[segment["attributes"]["name"]] = segment["id"]

    results = []
    for spec in definitions():
        if spec["name"] in existing:
            log.info("Segment already exists, skipping: %s", spec["name"])
            results.append({"name": spec["name"], "id": existing[spec["name"]],
                            "created": False, "why": spec["why"]})
            continue
        try:
            response = client.create_segment(spec["name"], spec["definition"])
            segment_id = (response.get("data") or {}).get("id")
            log.info("Created segment %s (%s)", spec["name"], segment_id)
            results.append({"name": spec["name"], "id": segment_id,
                            "created": True, "why": spec["why"]})
        except Exception as exc:
            log.error("Could not create segment %s: %s", spec["name"], exc)
            results.append({"name": spec["name"], "error": str(exc), "why": spec["why"]})
    return results

"""
The Preferred Store engine.

Turns resolved identities plus normalised transactions into the profile
properties Aurora's marketing team will actually segment on.

Scoring model (v1, deliberately explainable):

    score(store) = Σ over that customer's transactions at the store of
                       recency_weight × (1 + capped_spend_weight)

    recency_weight     = 0.5 ^ (days_ago / HALF_LIFE_DAYS)
    capped_spend_weight = min(amount_gbp / 100, 3)

Why this shape, and not something cleverer:

  * Recency decay beats a raw count. Someone who visited Munich six times last
    January and Berlin three times last month has moved. A count says Munich.
  * Spend is weighted but capped, so one sofa purchase doesn't permanently
    outrank a year of weekly visits.
  * It is a closed-form formula the Director of Marketing Ops can re-derive by
    hand from a customer record. Nobody has to take an ML model on faith, and
    when a customer complains about a wrong store invite there is an auditable
    answer. That matters more than a couple of points of accuracy.
  * Confidence = top store's share of total score. Below MIN_CONFIDENCE we
    assign nothing rather than guess — a blank is recoverable, a wrong
    localisation is a bad customer experience.

The same pass also derives the storefront/currency/locale properties, which is
what Use Case 1 (Abandoned Checkout) needs to localise a send. That is the point
of doing this use case first: it is the foundation the other two sit on.
"""

from collections import defaultdict
from datetime import datetime, timezone

from . import config
from .identity import Identity
from .normalize import Transaction

MODEL_VERSION = "recency_weighted_spend_v1"


def _recency_weight(days_ago: float) -> float:
    return 0.5 ** (days_ago / config.HALF_LIFE_DAYS)


def _spend_weight(amount_gbp: float) -> float:
    return min(amount_gbp / config.SPEND_NORMALISER_GBP, config.SPEND_WEIGHT_CAP)


def build_profiles(identities: dict[str, Identity],
                   transactions: list[Transaction],
                   txn_to_key: dict[str, str],
                   now: datetime | None = None) -> tuple[list[dict], dict]:
    """
    Returns (profiles, stats).

    A "profile" here is a plain dict in Klaviyo's attribute shape, ready to hand
    to the loader. Keeping the scoring layer ignorant of the API means it can be
    unit tested with no network and no key — see tests/test_preferred_store.py.
    """
    now = now or datetime.now(timezone.utc)
    by_identity: dict[str, list[Transaction]] = defaultdict(list)
    for txn in transactions:
        key = txn_to_key.get(txn.txn_id)
        if key:
            by_identity[key].append(txn)

    profiles: list[dict] = []
    assigned = low_confidence = insufficient = 0

    for key, identity in identities.items():
        if not identity.is_addressable:
            continue  # nothing to send to; a profile here is just noise in the account

        txns = [t for t in by_identity.get(key, [])
                if (now - t.occurred_at).days <= config.LOOKBACK_DAYS]
        in_store = [t for t in txns if t.channel == "in_store" and t.store_id]
        online = [t for t in txns if t.channel == "online"]

        # ---- Preferred store ------------------------------------------------
        scores: dict[str, float] = defaultdict(float)
        for txn in in_store:
            days_ago = max((now - txn.occurred_at).total_seconds() / 86400.0, 0.0)
            scores[txn.store_id] += _recency_weight(days_ago) * (1 + _spend_weight(txn.amount_gbp))

        preferred, confidence, method = None, 0.0, "insufficient_data"
        if len(in_store) >= config.MIN_TXNS_FOR_PREFERENCE and scores:
            total = sum(scores.values())
            store_id, top_score = max(scores.items(), key=lambda kv: kv[1])
            confidence = round(top_score / total, 3) if total else 0.0
            if confidence >= config.MIN_CONFIDENCE:
                preferred, method = config.STORES_BY_ID[store_id], MODEL_VERSION
                assigned += 1
            else:
                method = "below_confidence_threshold"
                low_confidence += 1
        else:
            insufficient += 1

        # ---- Storefront / localisation (feeds Use Case 1) -------------------
        site_counts: dict[str, int] = defaultdict(int)
        for txn in online:
            site_counts[txn.site_id] += 1
        if site_counts:
            site_id = max(site_counts.items(), key=lambda kv: kv[1])[0]
        elif preferred:
            site_id = config.COUNTRY_TO_STOREFRONT.get(preferred["country"])
        else:
            site_id = config.COUNTRY_TO_STOREFRONT.get(identity.country or "")
        storefront = config.STOREFRONTS.get(site_id) if site_id else None

        in_store_rev = round(sum(t.amount_gbp for t in in_store), 2)
        online_rev = round(sum(t.amount_gbp for t in online), 2)
        total_rev = in_store_rev + online_rev

        properties = {
            # --- Preferred store (Use Case 2) ---
            "preferred_store_id": preferred["store_id"] if preferred else None,
            "preferred_store_name": preferred["name"] if preferred else None,
            "preferred_store_city": preferred["city"] if preferred else None,
            "preferred_store_country": preferred["country"] if preferred else None,
            "preferred_store_region": preferred["region"] if preferred else None,
            "preferred_store_confidence": confidence,
            "preferred_store_method": method,
            "preferred_store_calculated_at": now.isoformat(timespec="seconds"),
            # --- Localisation (Use Case 1) ---
            "primary_storefront": site_id,
            "preferred_currency": storefront["currency"] if storefront else None,
            "preferred_locale": storefront["locale"] if storefront else None,
            "storefront_base_url": storefront["base_url"] if storefront else None,
            # --- Channel behaviour (Use Case 3 / exec reporting) ---
            "orders_in_store_12m": len(in_store),
            "orders_online_12m": len(online),
            "revenue_in_store_gbp_12m": in_store_rev,
            "revenue_online_gbp_12m": online_rev,
            "in_store_revenue_share": round(in_store_rev / total_rev, 3) if total_rev else 0.0,
            "is_omnichannel": bool(in_store and online),
            # --- Lineage: every derived property says where it came from.
            # This is what makes the pipeline debuggable a year from now.
            "identity_signal_count": identity.identifier_count,
            "data_sources": sorted({t.source for t in txns}),
        }

        profile = {
            "email": identity.primary_email,
            "phone_number": identity.primary_phone,
            "external_id": identity.external_id,
            "first_name": identity.first_name,
            "last_name": identity.last_name,
            "location": {
                "city": preferred["city"] if preferred else None,
                "country": (preferred["country"] if preferred else identity.country),
            },
            "properties": {k: v for k, v in properties.items() if v is not None},
        }
        profiles.append({k: v for k, v in profile.items() if v not in (None, {}, [])})

    stats = {
        "profiles_built": len(profiles),
        "preferred_store_assigned": assigned,
        "below_confidence_threshold": low_confidence,
        "insufficient_in_store_history": insufficient,
        # Two rates, because they answer two different questions. The first is
        # "how much of the base can we localise today" (what the VP cares about).
        # The second is "does the model work when it has data to work with"
        # (what the CTO cares about). Quoting only one of them is how these
        # projects get misread in either direction.
        "assignment_rate_all_profiles_pct": round(100 * assigned / max(len(profiles), 1), 1),
        "assignment_rate_eligible_pct": round(
            100 * assigned / max(assigned + low_confidence, 1), 1),
        "model_version": MODEL_VERSION,
    }
    return profiles, stats


def build_in_store_events(transactions: list[Transaction],
                          txn_to_key: dict[str, str],
                          identities: dict[str, Identity]) -> list[dict]:
    """
    In-store purchases as Klaviyo events.

    Profile properties answer "where does this person shop". Events answer
    "what did they buy, when" — which is what makes in-store history usable in
    flow triggers, segment conditions and CLV, rather than a static label.

    unique_id is the source transaction id, so re-running the backfill is safe:
    Klaviyo records the first event for a given profile+metric+unique_id and
    ignores repeats. Idempotency is not optional on a migration you will run
    more than once.
    """
    events = []
    for txn in transactions:
        if txn.channel != "in_store":
            continue
        key = txn_to_key.get(txn.txn_id)
        identity = identities.get(key) if key else None
        if not identity or not identity.is_addressable:
            continue
        store = config.STORES_BY_ID[txn.store_id]
        events.append({
            "metric_name": "Placed Order In-Store",
            "unique_id": txn.txn_id,
            "time": txn.occurred_at.isoformat(timespec="seconds"),
            "value": txn.amount,
            "value_currency": txn.currency,
            "profile": {
                k: v for k, v in {
                    "email": identity.primary_email,
                    "phone_number": identity.primary_phone,
                    "external_id": identity.external_id,
                }.items() if v
            },
            "properties": {
                "store_id": store["store_id"],
                "store_name": store["name"],
                "store_city": store["city"],
                "store_country": store["country"],
                "region": store["region"],
                "pos_system": store["pos"],
                "source_system": txn.source,
                "order_value_gbp": txn.amount_gbp,
                "items": txn.items,
            },
        })
    return events

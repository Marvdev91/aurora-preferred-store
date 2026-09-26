"""
Tests for the parts that encode business rules.

These run with no API key and no network. That is the point: the scoring model
and the identity logic are the bits Aurora has to trust, so they need to be
testable independently of Klaviyo being reachable.

Run: python -m pytest tests -q     (or: python tests/test_preferred_store.py)
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.identity import resolve
from src.normalize import Transaction, clean_email, to_e164
from src.preferred_store import build_profiles

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def txn(store_id, days_ago, amount_gbp=50.0, txn_id=None, email="a@aurorahomegoods-demo.com",
        phone=None, loyalty=None, channel="in_store", site_id=None):
    return Transaction(
        txn_id=txn_id or f"mpos:{store_id}-{days_ago}-{amount_gbp}",
        source="mpos" if channel == "in_store" else "sfcc",
        channel=channel,
        occurred_at=NOW - timedelta(days=days_ago),
        amount=amount_gbp, currency="GBP", amount_gbp=amount_gbp,
        store_id=store_id if channel == "in_store" else None,
        site_id=site_id, email=email, phone=phone, loyalty_id=loyalty,
    )


def _build(transactions, crm=None):
    identities, txn_to_key, _ = resolve(transactions, crm or [])
    return build_profiles(identities, transactions, txn_to_key, now=NOW)


# ---------------------------------------------------------------- normalisation

def test_email_cleaning_handles_till_typos():
    assert clean_email("  ANNA@Aurorahomegoods-demo.com ") == "anna@aurorahomegoods-demo.com"
    assert clean_email("not an email") is None
    assert clean_email(None) is None


def test_phone_normalisation_across_markets():
    assert to_e164("07700900123", "GB") == "+447700900123"
    assert to_e164("004915112345678", "DE") == "+4915112345678"
    assert to_e164("+33612345678", "FR") == "+33612345678"
    assert to_e164("(212) 555-0147", "US") == "+12125550147"
    assert to_e164("12", "GB") is None          # too short to be real


# ------------------------------------------------------------------- scoring

def test_recency_beats_raw_visit_count():
    """Six old Munich visits should lose to three recent Berlin visits."""
    transactions = (
        [txn("DE-MUC-01", 300 + i, txn_id=f"mpos:m{i}") for i in range(6)]
        + [txn("DE-BER-01", 10 + i, txn_id=f"mpos:b{i}") for i in range(3)]
    )
    profiles, _ = _build(transactions)
    assert profiles[0]["properties"]["preferred_store_id"] == "DE-BER-01"


def test_single_visit_never_assigns_a_store():
    profiles, stats = _build([txn("UK-LON-01", 5)])
    assert "preferred_store_id" not in profiles[0]["properties"]
    assert profiles[0]["properties"]["preferred_store_method"] == "insufficient_data"
    assert stats["insufficient_in_store_history"] == 1


def test_evenly_split_shopper_is_left_unassigned():
    """A genuine 50/50 split is below the confidence floor — assign nothing."""
    transactions = [txn("UK-LON-01", 10, txn_id="mpos:l1"), txn("UK-MAN-01", 10, txn_id="mpos:m1"),
                    txn("UK-LON-01", 11, txn_id="mpos:l2"), txn("UK-MAN-01", 11, txn_id="mpos:m2")]
    profiles, _ = _build(transactions)
    props = profiles[0]["properties"]
    assert "preferred_store_id" not in props
    assert props["preferred_store_method"] == "below_confidence_threshold"


def test_large_basket_cannot_outrank_sustained_loyalty():
    """One £5,000 sofa in Paris must not beat regular Berlin visits."""
    transactions = [txn("FR-PAR-01", 20, amount_gbp=5000.0, txn_id="mpos:sofa")] + \
                   [txn("DE-BER-01", 5 + i, amount_gbp=40.0, txn_id=f"mpos:b{i}") for i in range(6)]
    profiles, _ = _build(transactions)
    assert profiles[0]["properties"]["preferred_store_id"] == "DE-BER-01"


def test_confidence_is_reported_and_bounded():
    transactions = [txn("US-NYC-01", 3, txn_id="sq:1"), txn("US-NYC-01", 4, txn_id="sq:2"),
                    txn("US-CHI-01", 200, txn_id="sq:3")]
    profiles, _ = _build(transactions)
    confidence = profiles[0]["properties"]["preferred_store_confidence"]
    assert 0.5 < confidence <= 1.0


# --------------------------------------------------------------- localisation

def test_storefront_properties_drive_abandoned_checkout_localisation():
    transactions = [txn(None, 5, channel="online", site_id="aurora-de", txn_id="sfcc:1"),
                    txn(None, 8, channel="online", site_id="aurora-de", txn_id="sfcc:2"),
                    txn(None, 9, channel="online", site_id="aurora-uk", txn_id="sfcc:3")]
    profiles, _ = _build(transactions)
    props = profiles[0]["properties"]
    assert props["primary_storefront"] == "aurora-de"
    assert props["preferred_currency"] == "EUR"
    assert props["preferred_locale"] == "de-DE"
    assert props["storefront_base_url"].endswith(".de")


# ----------------------------------------------------------------- identity

def test_loyalty_card_stitches_an_emailless_pos_record_to_a_real_person():
    """The whole reason a naive email join fails on Aurora's mPOS data."""
    transactions = [
        txn("DE-BER-01", 5, email="anna@aurorahomegoods-demo.com", loyalty="LOY-1", txn_id="mpos:1"),
        txn("DE-BER-01", 6, email=None, loyalty="LOY-1", txn_id="mpos:2"),
        txn("DE-BER-01", 7, email=None, loyalty="LOY-1", txn_id="mpos:3"),
    ]
    profiles, stats = _build(transactions)
    assert stats["profiles_built"] == 1          # one person, not three
    assert profiles[0]["email"] == "anna@aurorahomegoods-demo.com"
    assert profiles[0]["properties"]["orders_in_store_12m"] == 3


def test_crm_record_bridges_phone_only_and_email_only_transactions():
    crm = [{"crm_id": "AH-CRM-1", "email": "lukas@aurorahomegoods-demo.com",
            "phone": "+4915112345678", "loyalty_id": "LOY-9",
            "first_name": "Lukas", "last_name": "Schmidt", "country": "DE"}]
    transactions = [
        txn("DE-MUC-01", 4, email="lukas@aurorahomegoods-demo.com", txn_id="sq:1"),
        txn("DE-MUC-01", 6, email=None, phone="+4915112345678", txn_id="mpos:1"),
    ]
    profiles, stats = _build(transactions, crm)
    assert stats["profiles_built"] == 1
    assert profiles[0]["external_id"] == "AH-CRM-1"
    assert profiles[0]["properties"]["orders_in_store_12m"] == 2


def test_profiles_without_any_contact_channel_are_not_created():
    """A loyalty number alone is not something you can send an email to."""
    transactions = [txn("UK-LON-01", 3, email=None, loyalty="LOY-77", txn_id="mpos:1"),
                    txn("UK-LON-01", 4, email=None, loyalty="LOY-77", txn_id="mpos:2")]
    profiles, stats = _build(transactions)
    assert profiles == []
    assert stats["profiles_built"] == 0


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

"""
Normalisation layer: three source schemas in, one canonical transaction out.

This is the part of the integration that people underestimate. The Klaviyo side
of an omnichannel build is genuinely straightforward; the expensive part is
agreeing what "a purchase" means when Square, mPOS and SFCC each describe it
differently, and then holding that contract stable as Aurora's stack changes.

Canonical shape (see Transaction below) is the contract. Anything that wants to
feed this pipeline — a future POS, a new storefront, an acquisition — implements
a mapper to this shape and nothing downstream changes.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class Transaction:
    """One purchase, from any channel, in a single shape."""
    txn_id: str                     # namespaced: "square:SQ-123" — used for event idempotency
    source: str                     # sfcc | square | mpos
    channel: str                    # online | in_store
    occurred_at: datetime           # always timezone-aware UTC
    amount: float                   # in original currency
    currency: str
    amount_gbp: float               # converted for cross-market comparison only
    store_id: Optional[str] = None  # in_store only
    site_id: Optional[str] = None   # online only
    email: Optional[str] = None
    phone: Optional[str] = None
    loyalty_id: Optional[str] = None
    crm_id: Optional[str] = None
    items: list = field(default_factory=list)

    @property
    def has_identifier(self) -> bool:
        return any([self.email, self.phone, self.loyalty_id, self.crm_id])


# ---------------------------------------------------------------------------
# Identifier hygiene
# ---------------------------------------------------------------------------

def clean_email(raw: Optional[str]) -> Optional[str]:
    """Lowercase, strip, and reject anything that isn't plausibly an address."""
    if not raw:
        return None
    value = raw.strip().lower()
    if "@" not in value or " " in value:
        return None
    return value


def to_e164(raw: Optional[str], country: Optional[str]) -> Optional[str]:
    """
    Normalise a POS phone number to E.164.

    Klaviyo requires E.164 for SMS, and Aurora's SMS business (currently with
    Attentive) is the follow-on opportunity — so getting phone hygiene right now
    is what makes that migration cheap later rather than a second data project.
    """
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", raw)
    dial = config.COUNTRY_DIAL_CODES.get(country or "", "")

    if digits.startswith("+"):
        candidate = digits
    elif digits.startswith("00"):
        candidate = "+" + digits[2:]
    elif digits.startswith("0"):
        # National format with trunk prefix (UK 07…, FR 06…, DE 0…)
        candidate = f"+{dial}{digits[1:]}" if dial else None
    elif dial and digits.startswith(dial):
        candidate = f"+{digits}"
    elif dial:
        candidate = f"+{dial}{digits}"
    else:
        candidate = None

    if not candidate:
        return None
    # E.164 allows up to 15 digits after the +.
    body = candidate[1:]
    return candidate if body.isdigit() and 8 <= len(body) <= 15 else None


def to_gbp(amount: float, currency: str) -> float:
    rate = config.FX_TO_GBP.get(currency)
    if rate is None:
        # Unknown currency is a data-quality event, not something to silently zero.
        raise ValueError(f"No FX rate configured for currency {currency!r}")
    return round(amount * rate, 2)


def _parse_utc(value: str, assume_country: Optional[str] = None) -> datetime:
    """
    Parse a timestamp to timezone-aware UTC.

    mPOS writes naive local timestamps with no offset. We treat them as UTC here
    and flag it as an assumption — in a real build this is a question for
    Aurora's engineering team on day one, because a several-hour skew quietly
    corrupts recency scoring and 'abandoned in the last hour' style flow timing.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Per-source mappers
# ---------------------------------------------------------------------------

def map_sfcc(record: dict) -> Transaction:
    site = config.STOREFRONTS[record["site_id"]]
    return Transaction(
        txn_id=f"sfcc:{record['order_no']}",
        source="sfcc",
        channel="online",
        occurred_at=_parse_utc(record["creation_date"]),
        amount=float(record["order_total"]),
        currency=record["currency"],
        amount_gbp=to_gbp(float(record["order_total"]), record["currency"]),
        site_id=record["site_id"],
        email=clean_email(record.get("customer_email")),
        crm_id=record.get("customer_no"),
        items=[{"sku": i["product_id"], "name": i["product_name"], "qty": i["quantity"]}
               for i in record.get("product_items", [])],
    )


def map_square(record: dict) -> Transaction:
    store = config.STORES_BY_ID[record["location_id"]]
    customer = record.get("customer") or {}
    amount = record["total_money"]["amount"] / 100.0   # Square stores minor units
    currency = record["total_money"]["currency"]
    return Transaction(
        txn_id=f"square:{record['id']}",
        source="square",
        channel="in_store",
        occurred_at=_parse_utc(record["created_at"]),
        amount=amount,
        currency=currency,
        amount_gbp=to_gbp(amount, currency),
        store_id=store["store_id"],
        email=clean_email(customer.get("email_address")),
        phone=to_e164(customer.get("phone_number"), store["country"]),
        loyalty_id=customer.get("reference_id"),
        items=[{"sku": i["catalog_object_id"], "name": i["name"], "qty": int(i["quantity"])}
               for i in record.get("line_items", [])],
    )


def map_mpos(record: dict) -> Transaction:
    store = config.STORES_BY_ID[record["storeCode"]]
    return Transaction(
        txn_id=f"mpos:{record['transactionId']}",
        source="mpos",
        channel="in_store",
        occurred_at=_parse_utc(record["timestamp"], store["country"]),
        amount=float(record["amount"]),
        currency=record["currencyCode"],
        amount_gbp=to_gbp(float(record["amount"]), record["currencyCode"]),
        store_id=store["store_id"],
        email=clean_email(record.get("customerEmail")),
        phone=to_e164(record.get("customerPhone"), store["country"]),
        loyalty_id=record.get("loyaltyCardNumber"),
        items=[{"sku": i["sku"], "name": i["description"], "qty": i["qty"]}
               for i in record.get("items", [])],
    )


def load_all() -> tuple[list[Transaction], list[dict], dict]:
    """Read the source extracts, normalise them, and report coverage."""
    crm = json.loads((DATA_DIR / "sfdc_customers.json").read_text())
    sources = [
        ("sfcc_orders.json", map_sfcc),
        ("square_transactions.json", map_square),
        ("mpos_transactions.json", map_mpos),
    ]

    transactions: list[Transaction] = []
    rejected: list[dict] = []

    for filename, mapper in sources:
        for record in json.loads((DATA_DIR / filename).read_text()):
            try:
                transactions.append(mapper(record))
            except Exception as exc:  # a bad record must never kill a batch
                rejected.append({"file": filename, "error": str(exc), "record": record})

    in_store = [t for t in transactions if t.channel == "in_store"]
    identified = [t for t in in_store if t.has_identifier]
    stats = {
        "transactions_total": len(transactions),
        "transactions_online": len(transactions) - len(in_store),
        "transactions_in_store": len(in_store),
        "in_store_identified": len(identified),
        # The headline number for the CTO and the VP of Marketing alike:
        # personalisation can only ever reach this share of in-store revenue.
        "in_store_identity_coverage_pct": round(100 * len(identified) / max(len(in_store), 1), 1),
        "rejected_records": len(rejected),
    }
    return transactions, crm, stats

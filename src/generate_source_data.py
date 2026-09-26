"""
Generates synthetic extracts from Aurora's four relevant source systems.

The point of this module is NOT the fake data — it's that the data is
deliberately *messy in the ways real retail data is messy*. Every piece of mess
here maps to something I have seen break an enterprise migration:

  * Three different schemas for "a customer bought something".
  * Square (AMER) captures email at the till, sometimes. Casing and whitespace
    are whatever the cashier typed.
  * mPOS (EMEA) captures a phone number in local format, never an email.
  * The same human appears as different identifiers in different systems, with
    only partial overlap — so a naive email join loses them.
  * A meaningful share of in-store transactions are anonymous walk-ins. They are
    unusable for personalisation and we need to say so out loud, with a number.

Run: python -m src.pipeline generate
"""

import json
import random
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config


def _ascii_fold(value: str) -> str:
    """
    Strip accents for use in an email local-part.

    Klaviyo's profile-import validation rejects non-ASCII characters in email
    addresses (confirmed against a live account: 'Müller' -> 400 Invalid email
    address). Display names (first_name/last_name properties) keep the correct
    spelling; only the derived email is folded. NFKD decomposition separates a
    base letter from its diacritic (u + combining ¨), so encoding to ASCII and
    dropping anything that doesn't fit removes just the accent: "müller" -> "muller".
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return decomposed.encode("ascii", "ignore").decode("ascii")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

FIRST_NAMES = ["Anna", "Lukas", "Marie", "Thomas", "Sofia", "James", "Olivia", "Noah",
               "Emma", "Liam", "Chloe", "Felix", "Hannah", "Daniel", "Camille", "Ethan",
               "Isabelle", "Jonas", "Grace", "Mateo"]
LAST_NAMES = ["Schmidt", "Müller", "Dubois", "Bernard", "Smith", "Jones", "Taylor",
              "Brown", "Wilson", "Martin", "Rossi", "Nguyen", "Okafor", "Kaur", "Novak"]

PRODUCTS = [
    ("AH-1001", "Linen Duvet Set",        129.00),
    ("AH-1042", "Stoneware Dinner Set",    89.00),
    ("AH-2210", "Oak Console Table",      349.00),
    ("AH-2255", "Wool Throw",              59.00),
    ("AH-3301", "Ceramic Table Lamp",      75.00),
    ("AH-3390", "Rattan Storage Basket",   34.00),
    ("AH-4410", "Copper Cookware Set",    219.00),
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local_phone(country: str, rng: random.Random) -> str:
    """Return a phone number in the messy local format each POS actually stores."""
    subscriber = "".join(str(rng.randint(0, 9)) for _ in range(10))
    if country == "GB":
        # UK mPOS stores the national format with the trunk zero.
        return "07" + subscriber[:9]
    if country == "DE":
        # German mPOS stores 00-prefixed international, some records use +.
        return rng.choice(["0049", "+49"]) + subscriber[:10]
    if country == "FR":
        return "0" + subscriber[:9]
    # North America: Square stores a mix of formats.
    return rng.choice([
        f"+1{subscriber[:10]}",
        f"({subscriber[:3]}) {subscriber[3:6]}-{subscriber[6:10]}",
    ])


def _dirty_email(email: str, rng: random.Random) -> str:
    """Emails captured at a till are rarely clean."""
    roll = rng.random()
    if roll < 0.12:
        return email.upper()
    if roll < 0.20:
        return f"  {email} "
    return email


def generate(seed: int = config.RANDOM_SEED, customers: int = config.CUSTOMER_COUNT) -> dict:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    DATA_DIR.mkdir(exist_ok=True)

    crm_records, sfcc_orders, square_txns, mpos_txns = [], [], [], []

    for i in range(customers):
        crm_id = f"AH-CRM-{100000 + i}"
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        email = f"{_ascii_fold(first).lower()}.{_ascii_fold(last).lower()}{i}@{config.DEMO_EMAIL_DOMAIN}"

        # Pick the customer's "true" home market, then their home store cluster.
        region_store = rng.choice(config.STORES)
        country = region_store["country"]
        storefront = config.COUNTRY_TO_STOREFRONT[country]
        phone = _local_phone(country, rng)
        loyalty_id = f"LOY-{rng.randint(10**7, 10**8 - 1)}"

        # --- SFDC / CRM: the only place email + phone + crm_id reliably co-exist.
        # 15% of customers are NOT in CRM — they shop but never created an account.
        in_crm = rng.random() > 0.15
        if in_crm:
            crm_records.append({
                "crm_id": crm_id, "email": email, "phone": phone,
                "loyalty_id": loyalty_id, "first_name": first, "last_name": last,
                "country": country,
            })

        # --- SFCC online orders (0-6 over the year) ---
        n_online = rng.choices([0, 1, 2, 3, 4, 6], weights=[25, 25, 20, 15, 10, 5])[0]
        for _ in range(n_online):
            sku, name, base = rng.choice(PRODUCTS)
            sf = config.STOREFRONTS[storefront]
            qty = rng.randint(1, 3)
            sfcc_orders.append({
                "order_no": f"SO-{rng.randint(10**8, 10**9 - 1)}",
                "site_id": storefront,
                "customer_email": email,
                "customer_no": crm_id if in_crm else None,
                "currency": sf["currency"],
                "order_total": round(base * qty * rng.uniform(0.9, 1.2), 2),
                "creation_date": _iso(now - timedelta(days=rng.randint(0, config.LOOKBACK_DAYS))),
                "product_items": [{"product_id": sku, "product_name": name, "quantity": qty}],
            })

        # --- In-store visits ---
        # Most customers are loyal to one store; ~25% split across two, which is
        # exactly the population where "preferred store" gets interesting.
        home_stores = [s for s in config.STORES if s["country"] == country]
        primary = rng.choice(home_stores)
        secondary = rng.choice([s for s in home_stores if s["store_id"] != primary["store_id"]]) \
            if len(home_stores) > 1 and rng.random() < 0.25 else None

        n_store = rng.choices([0, 1, 2, 3, 5, 8], weights=[30, 18, 20, 15, 12, 5])[0]
        for _ in range(n_store):
            store = secondary if (secondary and rng.random() < 0.35) else primary
            sku, name, base = rng.choice(PRODUCTS)
            when = now - timedelta(days=rng.randint(0, config.LOOKBACK_DAYS))
            amount = round(base * rng.randint(1, 2) * rng.uniform(0.9, 1.3), 2)

            if store["pos"] == "square":
                # Square: email present ~55% of the time, loyalty id ~30%.
                square_txns.append({
                    "id": f"SQ-{rng.randint(10**9, 10**10 - 1)}",
                    "location_id": store["store_id"],
                    "created_at": _iso(when),
                    "total_money": {"amount": int(amount * 100), "currency": store["currency"]},
                    "customer": {
                        "email_address": _dirty_email(email, rng) if rng.random() < 0.55 else None,
                        "phone_number": phone if rng.random() < 0.30 else None,
                        "reference_id": loyalty_id if rng.random() < 0.30 else None,
                    },
                    "line_items": [{"catalog_object_id": sku, "name": name, "quantity": "1"}],
                })
            else:
                # mPOS: phone-led, email almost never captured (GDPR-cautious EU rollout).
                mpos_txns.append({
                    "transactionId": f"MP-{rng.randint(10**9, 10**10 - 1)}",
                    "storeCode": store["store_id"],
                    "timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),  # no timezone! see normalize.py
                    "amount": amount,
                    "currencyCode": store["currency"],
                    "loyaltyCardNumber": loyalty_id if rng.random() < 0.45 else None,
                    "customerPhone": phone if rng.random() < 0.50 else None,
                    "customerEmail": email if rng.random() < 0.10 else None,
                    "items": [{"sku": sku, "description": name, "qty": 1}],
                })

    # --- Anonymous walk-ins: ~22% of in-store volume carries no identifier at all.
    for _ in range(int((len(square_txns) + len(mpos_txns)) * 0.28)):
        store = rng.choice(config.STORES)
        sku, name, base = rng.choice(PRODUCTS)
        when = now - timedelta(days=rng.randint(0, config.LOOKBACK_DAYS))
        amount = round(base * rng.uniform(0.8, 1.4), 2)
        if store["pos"] == "square":
            square_txns.append({
                "id": f"SQ-{rng.randint(10**9, 10**10 - 1)}",
                "location_id": store["store_id"], "created_at": _iso(when),
                "total_money": {"amount": int(amount * 100), "currency": store["currency"]},
                "customer": None,
                "line_items": [{"catalog_object_id": sku, "name": name, "quantity": "1"}],
            })
        else:
            mpos_txns.append({
                "transactionId": f"MP-{rng.randint(10**9, 10**10 - 1)}",
                "storeCode": store["store_id"],
                "timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),
                "amount": amount, "currencyCode": store["currency"],
                "loyaltyCardNumber": None, "customerPhone": None, "customerEmail": None,
                "items": [{"sku": sku, "description": name, "qty": 1}],
            })

    rng.shuffle(square_txns)
    rng.shuffle(mpos_txns)

    outputs = {
        "sfdc_customers.json": crm_records,
        "sfcc_orders.json": sfcc_orders,
        "square_transactions.json": square_txns,
        "mpos_transactions.json": mpos_txns,
    }
    for filename, payload in outputs.items():
        (DATA_DIR / filename).write_text(json.dumps(payload, indent=2))

    return {name: len(payload) for name, payload in outputs.items()}

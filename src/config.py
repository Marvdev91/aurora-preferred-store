"""
Central configuration for the Aurora Home Goods "Preferred Store" proof-of-concept.

Everything that a real implementation would pull from Aurora's systems of record
(store master data, storefront config, FX rates) is isolated here so the rest of
the pipeline stays free of hard-coded business data.
"""

import os

# --------------------------------------------------------------------------
# Klaviyo API configuration
# --------------------------------------------------------------------------
# We PIN the API revision rather than floating on "latest". Klaviyo revisions are
# date-versioned and supported for roughly two years; floating means a breaking
# change lands in Aurora's production pipeline without a deploy. Upgrading the
# revision is a deliberate, tested change — that is a dependency we call out in
# the kickoff as a shared, recurring task, not a one-off.
KLAVIYO_REVISION = os.getenv("KLAVIYO_REVISION", "2025-01-15")
KLAVIYO_BASE_URL = "https://a.klaviyo.com"
KLAVIYO_PRIVATE_KEY = os.getenv("KLAVIYO_PRIVATE_API_KEY", "")
KLAVIYO_PUBLIC_ID = os.getenv("KLAVIYO_PUBLIC_API_KEY", "")

# --------------------------------------------------------------------------
# Synthetic data configuration
# --------------------------------------------------------------------------
# IMPORTANT GOTCHA (found the hard way, documented in the one-pager):
# Klaviyo silently drops events created against @example.com / @test.com style
# addresses. The API returns 202 and nothing ever appears in the account. Any
# demo data therefore has to use a non-reserved domain.
DEMO_EMAIL_DOMAIN = "aurorahomegoods-demo.com"

RANDOM_SEED = 20260919
CUSTOMER_COUNT = 400

# --------------------------------------------------------------------------
# Aurora store master data (would come from Aurora's store system / SFDC)
# --------------------------------------------------------------------------
# pos: which POS system writes the transaction record. Square in AMER, mPOS in EMEA.
STORES = [
    # --- EMEA (mPOS) ---
    {"store_id": "DE-BER-01", "name": "Aurora Berlin Mitte",      "city": "Berlin",     "country": "DE", "region": "EMEA", "pos": "mpos",   "currency": "EUR"},
    {"store_id": "DE-BER-02", "name": "Aurora Berlin Charlottenburg", "city": "Berlin", "country": "DE", "region": "EMEA", "pos": "mpos",   "currency": "EUR"},
    {"store_id": "DE-MUC-01", "name": "Aurora Munich Maxvorstadt", "city": "Munich",    "country": "DE", "region": "EMEA", "pos": "mpos",   "currency": "EUR"},
    {"store_id": "FR-PAR-01", "name": "Aurora Paris Marais",      "city": "Paris",      "country": "FR", "region": "EMEA", "pos": "mpos",   "currency": "EUR"},
    {"store_id": "UK-LON-01", "name": "Aurora London Oxford St",  "city": "London",     "country": "GB", "region": "EMEA", "pos": "mpos",   "currency": "GBP"},
    {"store_id": "UK-MAN-01", "name": "Aurora Manchester Trafford", "city": "Manchester", "country": "GB", "region": "EMEA", "pos": "mpos", "currency": "GBP"},
    # --- AMER (Square) ---
    {"store_id": "US-NYC-01", "name": "Aurora NYC SoHo",          "city": "New York",   "country": "US", "region": "AMER", "pos": "square", "currency": "USD"},
    {"store_id": "US-NYC-02", "name": "Aurora NYC Brooklyn",      "city": "New York",   "country": "US", "region": "AMER", "pos": "square", "currency": "USD"},
    {"store_id": "US-CHI-01", "name": "Aurora Chicago Loop",      "city": "Chicago",    "country": "US", "region": "AMER", "pos": "square", "currency": "USD"},
    {"store_id": "CA-TOR-01", "name": "Aurora Toronto Queen St",  "city": "Toronto",    "country": "CA", "region": "AMER", "pos": "square", "currency": "CAD"},
]

STORES_BY_ID = {s["store_id"]: s for s in STORES}

# --------------------------------------------------------------------------
# SFCC storefronts — one per market. Drives currency / locale / catalog links,
# which is what Use Case 1 (Abandoned Checkout) needs at send time.
# --------------------------------------------------------------------------
STOREFRONTS = {
    "aurora-us": {"country": "US", "currency": "USD", "locale": "en-US", "base_url": "https://www.aurorahome.com/us",  "region": "AMER"},
    "aurora-ca": {"country": "CA", "currency": "CAD", "locale": "en-CA", "base_url": "https://www.aurorahome.com/ca",  "region": "AMER"},
    "aurora-de": {"country": "DE", "currency": "EUR", "locale": "de-DE", "base_url": "https://www.aurorahome.de",      "region": "EMEA"},
    "aurora-fr": {"country": "FR", "currency": "EUR", "locale": "fr-FR", "base_url": "https://www.aurorahome.fr",      "region": "EMEA"},
    "aurora-uk": {"country": "GB", "currency": "GBP", "locale": "en-GB", "base_url": "https://www.aurorahome.co.uk",   "region": "EMEA"},
}

# Map a country to its "home" storefront, used to pick a default when a profile
# has in-store history but no online history.
COUNTRY_TO_STOREFRONT = {v["country"]: k for k, v in STOREFRONTS.items()}

# --------------------------------------------------------------------------
# FX — static table, deliberately. See "Assumptions" in the one-pager: spend has
# to be compared in a single unit to rank stores, but a marketing pipeline is the
# wrong place to own FX. In production this reads Aurora's Snowflake FX table.
# --------------------------------------------------------------------------
FX_TO_GBP = {"GBP": 1.00, "EUR": 0.85, "USD": 0.79, "CAD": 0.57}

# --------------------------------------------------------------------------
# Preferred Store scoring parameters
# --------------------------------------------------------------------------
LOOKBACK_DAYS = 365          # transactions older than this are ignored entirely
HALF_LIFE_DAYS = 90          # a visit 90 days ago counts half as much as one today
SPEND_WEIGHT_CAP = 3.0       # caps how much a single big basket can dominate
SPEND_NORMALISER_GBP = 100.0 # £100 of spend ≈ one extra "visit" of weight
MIN_TXNS_FOR_PREFERENCE = 2  # one visit is a tourist, not a preference
# Confidence is the top store's share of total score, so a two-store tie scores
# ~0.50 by construction. The floor therefore has to sit *above* 0.5, or every
# genuinely split shopper gets a coin-flip assignment. Found by the unit test in
# tests/test_preferred_store.py, not by reasoning about it up front.
MIN_CONFIDENCE = 0.55

# Country dial codes used when normalising local-format POS phone numbers.
COUNTRY_DIAL_CODES = {"GB": "44", "DE": "49", "FR": "33", "US": "1", "CA": "1"}

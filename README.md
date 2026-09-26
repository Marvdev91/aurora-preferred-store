# Aurora Home Goods — "Preferred Store" proof-of-concept

A working pipeline that unifies Aurora's online (SFCC) and in-store (Square +
mPOS) purchase data, resolves it to a single customer identity, derives a
**Preferred Store** for each customer, and loads the result into Klaviyo as
profile properties, events and segments.

Built for the Klaviyo Senior Solution Architect case study. See
[`ONE_PAGER.md`](ONE_PAGER.md) for the problem framing, assumptions, trade-offs
and risks.

---

## Quick start

No third-party dependencies — Python 3.10+ standard library only.

```bash
git clone <this repo> && cd aurora-preferred-store

# 1. Generate the synthetic source extracts (SFCC / Square / mPOS / SFDC)
python -m src.pipeline generate

# 2. Normalise, resolve identities, score preferred stores. No network, no API key.
python -m src.pipeline build

# 3. Inspect exactly what would be sent to Klaviyo, without sending it
python -m src.pipeline load --dry-run
python -m src.pipeline segments --dry-run
cat data/dry_run_requests.jsonl | head -1 | python -m json.tool

# 4. Against a real account
export KLAVIYO_PRIVATE_API_KEY="pk_..."          # never commit this
python -m src.pipeline load --max-events 50      # smoke test first
python -m src.pipeline load
python -m src.pipeline segments
python -m src.pipeline verify                    # reads the data back out

# Tests (no key, no network)
python tests/test_preferred_store.py
python tests/test_load_repair.py
```

---

## What it does, in order

| Stage | Module | Responsibility |
|---|---|---|
| Generate | `src/generate_source_data.py` | Synthetic extracts from four source systems, messy in realistic ways |
| Normalise | `src/normalize.py` | Three schemas → one canonical `Transaction`; email/phone/currency hygiene |
| Resolve | `src/identity.py` | Union-find over identifier tokens → one identity per human |
| Score | `src/preferred_store.py` | Recency- and spend-weighted store ranking + localisation properties |
| Load | `src/klaviyo.py`, `src/load.py` | Bulk profile import, in-store purchase events, retries and idempotency |
| Segment | `src/segments.py` | Three segment definitions created from JSON via the Segments API |
| Verify | `src/pipeline.py verify` | Reads profiles back out of Klaviyo to prove the properties landed |

---

## Property registry

Every property written to a Klaviyo profile by this pipeline. Nothing else is
touched — the pipeline is additive and never clears properties it does not own.

**Preferred store (Use Case 2)**

| Property | Type | Example |
|---|---|---|
| `preferred_store_id` | string | `DE-BER-01` |
| `preferred_store_name` | string | `Aurora Berlin Mitte` |
| `preferred_store_city` | string | `Berlin` |
| `preferred_store_country` | string | `DE` |
| `preferred_store_region` | string | `EMEA` |
| `preferred_store_confidence` | number 0–1 | `0.82` |
| `preferred_store_method` | string | `recency_weighted_spend_v1` / `insufficient_data` / `below_confidence_threshold` |
| `preferred_store_calculated_at` | ISO 8601 | `2026-09-19T12:00:00+00:00` |

**Localisation (Use Case 1)**

| Property | Type | Example |
|---|---|---|
| `primary_storefront` | string | `aurora-de` |
| `preferred_currency` | string | `EUR` |
| `preferred_locale` | string | `de-DE` |
| `storefront_base_url` | string | `https://www.aurorahome.de` |

**Channel behaviour (Use Case 3 / exec reporting)**

| Property | Type |
|---|---|
| `orders_in_store_12m`, `orders_online_12m` | number |
| `revenue_in_store_gbp_12m`, `revenue_online_gbp_12m` | number |
| `in_store_revenue_share` | number 0–1 |
| `is_omnichannel` | boolean |
| `identity_signal_count`, `data_sources` | number, list (lineage) |

**Events:** `Placed Order In-Store`, with `store_id`, `store_name`, `store_city`,
`store_country`, `region`, `pos_system`, `source_system`, `order_value_gbp`,
`items`. `unique_id` is the source transaction ID, so re-runs are safe.

---

## Configuration

All business data and tuning lives in `src/config.py`:

- `STORES`, `STOREFRONTS` — store and storefront master data
- `FX_TO_GBP` — static FX table (see Assumptions in the one-pager)
- `HALF_LIFE_DAYS` (90), `LOOKBACK_DAYS` (365), `MIN_TXNS_FOR_PREFERENCE` (2),
  `MIN_CONFIDENCE` (0.55) — the scoring model's tunable parameters
- `KLAVIYO_REVISION` — pinned API revision, overridable by environment variable

## Security

- The private API key is read from `KLAVIYO_PRIVATE_API_KEY` and never written
  to disk, logs, or the dry-run payload file.
- `.env` and `data/` are gitignored.
- Only the public account ID (site ID) is shared with the panel.

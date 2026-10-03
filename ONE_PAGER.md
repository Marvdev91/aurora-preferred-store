# Aurora Home Goods — Omnichannel Preferred Store
### Technical Solution One-Pager · Klaviyo Platform Implementation

**Recommendation:** Establish a cross-channel customer identity layer,
validated through the Preferred Store use case, as reusable infrastructure
for all three of Aurora's candidate use cases — not just a Berlin segment.
**Business value:** immediate localised-campaign activation for Marketing,
plus reduced duplicated integration work on Checkout Abandonment and BI
Reporting, because all three draw on the same identity foundation.

**On synthetic test data**, a live POC against a real Klaviyo account
processed 1,702 transactions, resolved 456 identities, achieved 97.2%
Preferred Store assignment among eligible customers, and surfaced a **61.4%
in-store identity coverage baseline** — Aurora's real extracts would need to
revalidate every one of these figures before they inform a business decision.

---

## The problem

Aurora wants localised, store-based experiences — e.g. a Berlin VIP invite.
The signal needed to decide *which* store lives across systems that describe
a purchase differently and identify the customer differently: SFCC (online,
5 storefronts), Square (AMER in-store, email sometimes), mPOS (EMEA in-store,
phone/loyalty-led, rarely email), Salesforce CRM. **An email-only join loses
most of the in-store history.** The real gap isn't a missing segment — it's
reliable identity across channels.

## Why Preferred Store, of the three

| Use case | Brief's own framing | What it tests |
|---|---|---|
| UC1 — Checkout Abandonment | High Priority, Straightforward | Contained within SFCC + Klaviyo |
| **UC2 — Preferred Store** | Medium Priority, Moderate Complexity | Cross-channel identity + decisioning |
| UC3 — BI Reporting | Low Definition, **High Executive Interest** | Broader data-architecture question |

Read together, these are three questions about one customer journey: UC1 asks
*why an online journey broke*, UC2 asks *where the customer actually
transacts*, UC3 asks *what patterns exist at scale*. A customer can abandon
checkout and later buy in their preferred store — an online-only view calls
that lost; a shared identity layer could eventually tell the difference. **The
POC doesn't prove that relationship** — it contains no abandonment events,
only completed orders — but it builds the identity layer that would make the
question answerable once Aurora's real abandonment events connect to it.
*(Full customer-journey discussion: Appendix A.)*

**Two separate reuse paths, not one:** (1) *now* — the same identity pass
derives `primary_storefront`, `preferred_currency` and `preferred_locale`,
exactly what UC1 needs to localise a send; (2) *later* — once abandonment
events share this identity graph, genuine drop-off becomes distinguishable
from a channel switch. **Preferred Store is the use case; cross-channel
identity is the architecture** — the property doesn't solve UC1 or UC3, the
identity layer under it does.

This second path doesn't need a new waiting-window system, and that's the
differentiator worth stating plainly: Klaviyo's abandoned-checkout flows
already re-check *"has placed order zero times since starting this flow"*
before every send — native, out-of-the-box behaviour. The gap today is
visibility, not timing — that filter only sees online orders, because
Klaviyo has no in-store purchase data until this identity layer supplies it.
Online and in-store purchases should stay distinct metrics, not merge into
one, to keep Aurora's own reporting and UC3 attribution clean. **I'm not
proposing a new abandonment engine; I'm making Aurora's existing one
channel-aware.** *(Worked example and the resulting business-rule question:
Appendix A.)*

**Why not Segment Unify or Klaviyo's native profile merging?** The POC builds
identity resolution independently so the identifier requirements,
assumptions and failure modes are explicit before Aurora commits to a
production pattern. **The custom resolver is a validation mechanism, not a
recommendation to replace capabilities Aurora already owns in Segment.**

**Aurora's stated goals:** unify online/offline data ✓ (identity layer, this
POC) · personalise at scale ✓ (deterministic, so it scales past a handful of
manual VIP lists) · reduce stack complexity ✓ (one identity pass feeds three
use cases instead of three separate integrations) · prove ROI — not yet
quantified from synthetic data; the pilot would measure conversion lift on
the Berlin segment against a control group, using the `in_store_revenue_share`
property this POC already computes.

## Architecture

```
SFCC + Square + mPOS + SFDC → Normalise → Resolve Identity → Score Preferred Store
                                                                      │
                                                    ┌─────────────────┼─────────────────┐
                                                    ▼                 ▼                 ▼
                                            Klaviyo Profile      Purchase Event      Segment
```

Identity resolution is deterministic (identifiers as evidence, connected
components), chosen over probabilistic matching because it's auditable and
safe to operationalise — customers are left unassigned rather than
force-matched. Preferred Store scoring weights recency, frequency and capped
spend into a 0–1 confidence score (≥0.55 to assign, ≥2 visits required).
*(Formula, thresholds, full identity-graph mechanics: Appendix B.)*

## Results (synthetic data — see caveat above)

| Metric | Result |
|---|---|
| Transactions / identities resolved | 1,702 / 456 |
| Preferred Store assignment, eligible customers | 97.2% |
| In-store identity coverage (the key finding) | 61.4% |
| Profiles / events loaded to live Klaviyo account | 433/445 · 549/549 |

The shortfall traces to the *same* 12 customers in both cases — phone-only
identities whose one identifier failed Klaviyo's carrier validation, with no
email or CRM match to fall back on. Dropped from profile load individually
and logged by name; their 17 associated purchase events are quarantined
upstream for the identical reason — skipped before a single API call, not a
second failure mode. **Of the 549 events actually eligible, all 549 loaded
successfully: zero failures on the live API.** *(Full detail: Appendix C.)*

## Non-functional requirements

**Volume/throughput** — Klaviyo's Bulk Import Profiles endpoint accepts
10,000 profiles/request at 150 requests/minute steady; Bulk Create Events,
1,000/request at the same rate. A nightly batch of low hundreds of thousands
of profiles or events fits comfortably inside a standard overnight window —
verified against Klaviyo's published limits, not estimated. **Compliance** —
GDPR/UK-GDPR data residency for EU/UK customer data, PII handling in transit
and at rest, secrets management for the API key (never in code, never
logged), and a defined retention policy all need Aurora sign-off before
production. **Operations** — a named monitoring owner for the "unassigned"
canary segment and the nightly batch job.

## Production recommendation

**Snowflake + reverse ETL.** Preferred Store should be maintained as an
Aurora-owned, governed customer attribute in the enterprise data layer and
made available to Klaviyo and other downstream consumers — the business
decision shouldn't be coupled to whichever activation platform happens to
consume it. Segment remains valuable for event collection and routing, and
may contribute resolved identity signals into that layer.

**What would change this:** if Aurora's existing Segment implementation
already provides sufficiently governed cross-channel identity across email,
phone, loyalty and CRM identifiers, that capability should be reused rather
than duplicated in Snowflake.

| Approach | Verdict |
|---|---|
| Most-recent-store-wins | Rejected — ~70% as good, fails multi-store customers |
| Postcode-nearest-store | Rejected — location isn't shopping behaviour |

## Top risks

| Risk | Control |
|---|---|
| 39% of in-store transactions lack an identifier | Measured explicitly; it's a POS capture initiative, not a matching-algorithm problem |
| Consent contamination | Transaction data never infers marketing consent; consent migration is separate and audited |
| One bad record sinking a batch load | Structured error parsing repairs field-by-field — found and fixed against the live account |

*(Full 8-item risk register: Appendix C.)*

## Delivery path (mapped to the 120-day implementation window)

| Phase | Window | Milestone |
|---|---|---|
| 1 — Validate | Weeks 1–2 | Real Aurora extracts confirm/kill every assumption above |
| 2 — Pilot | Weeks 3–4 | Controlled Berlin cohort live in Klaviyo, Marketing-usable |
| 3 — Production | Weeks 5–10 | Snowflake-owned, monitored (pending the Segment-capability check above) |
| 4 — Reuse | Weeks 11+ | Same identity layer extended to UC1 localisation, UC3 reporting |

## Decisions needed from Aurora

Identifier quality & loyalty-ID uniqueness (Retail Ops) · consent state by
region (Marketing/Legal) · production transformation owner (CTO/Eng) ·
Preferred Store business-rule validation (Marketing).

## Key assumptions (validate against real data in Phase 1)

Loyalty ID is unique per customer · mPOS timestamps are naive and treated as
UTC pending confirmation · 12-month lookback / 90-day half-life are POC
defaults, not tuned to Aurora's purchase cycle · FX is a static table, not
Aurora's live rates. *(Full list: Appendix B.)*

## Setup & execution

Python 3.10+, stdlib only, against a free Klaviyo account:

```
python -m src.pipeline generate && python -m src.pipeline build   # no network
python -m src.pipeline load && python -m src.pipeline segments    # live account
python -m src.pipeline verify                                     # confirms it landed
```

Full instructions, property registry and 17 tests: repo `README.md`.

## Use of AI

Used Claude to accelerate Klaviyo API/data-model research, pressure-test
design decisions, and assist with code generation and review. Architectural
decisions, trade-offs, and validation against the live account remained my
responsibility.

---

*Repo:* `https://github.com/Marvdev91/aurora-preferred-store` (see
`APPENDIX.md` for extended architecture, risks and the customer-journey
discussion in full) · *Klaviyo Public API Key / account ID:* `YiCPjg` · *Live
segments:* Berlin VIPs (`RdMdbR`), EMEA Omnichannel (`YpuKdM`), Ops monitoring
(`TtXCCb`). A fourth, `DEMO ONLY — Berlin candidates` (`XqgmGe`), exists only
as a presentation aid with no consent filter — not part of the recommended
architecture, removed after the live walkthrough.

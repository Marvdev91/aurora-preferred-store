# Aurora Home Goods — Preferred Store & Omnichannel Identity
### Solution one-pager · Klaviyo Platform Implementation · Technical work stream

---

## The problem

Aurora asked for localised in-store campaigns — "In-store VIP event in Berlin."
To send that, Klaviyo needs to know which store each customer belongs to. Aurora
has no such field anywhere. The signal exists, but it is scattered across three
systems that describe a purchase three different ways and identify the customer
three different ways:

- **SFCC** (5 storefronts) knows an account email.
- **Square** (AMER stores) captures an email at the till, roughly half the time,
  in whatever casing the customer typed.
- **mPOS** (EMEA stores) is phone- and loyalty-card-led and almost never captures
  an email.

Join those on email and you lose most of the in-store history. Which means the
real deliverable is not a segment. It is **an identity and data foundation** —
and the Berlin segment is the proof that it works.

## What I built

A pipeline that generates realistic source extracts, normalises them to one
canonical transaction shape, resolves identities, scores a preferred store, and
loads the result into a live Klaviyo account as profile properties, events and
segments. It runs end to end in about a minute.

```
SFCC ─┐
Square┼─► normalise ─► identity resolution ─► preferred-store ─► Klaviyo
mPOS ─┤   (one shape)   (union-find over        scoring          profiles
SFDC ─┘                  identifier tokens)                      + events
                                                                 + segments
```

**Identity resolution.** Every identifier (email, E.164 phone, loyalty number,
CRM ID) is a node; every transaction and CRM record is evidence that its
identifiers belong to the same person. Take the connected components. Simple,
deterministic, auditable, and it recovers the mPOS transactions that an email
join throws away.

**Preferred store scoring.**

```
score(store) = Σ  0.5^(days_ago / 90)  ×  (1 + min(spend_gbp / 100, 3))
confidence   = top store's share of total score
```

Recency decay so last year's loyalty doesn't outvote this month's. Spend
weighted but capped so one sofa can't beat a year of weekly visits. Below 0.55
confidence, or fewer than two visits, **we assign nothing** — a blank is
recoverable, a wrong city invitation is a bad customer experience.

It is a closed-form formula Marketing Ops can re-derive by hand from a customer
record. That is worth more than a couple of points of accuracy from a model
nobody can explain when a customer complains.

## Results on the synthetic dataset

| Metric | Value |
|---|---|
| Transactions processed | 1,702 (760 online, 942 in-store) |
| **In-store transactions carrying any identifier** | **61.4%** |
| Identities resolved | 456 (445 addressable) |
| `external_id` coverage | 78.9% |
| Preferred store assigned | 139 profiles |
| Assignment rate among eligible customers (2+ in-store visits) | **97.2%** |
| In-store purchase events loaded | 566 |

The number I would put in front of the executive team is **61.4%**. That is the
ceiling on in-store personalisation today, and it is a store-operations problem,
not a Klaviyo problem. Every point of identity capture at the till is worth more
than any tuning of the model.

## Why this use case, over the other two

Sales flagged Abandoned Checkout as the high-priority item, and it is — but it
is mostly SFCC integration plus template logic, and it is blocked on Aurora's
engineering team, not on me. It is also the use case most likely to be built
correctly by default.

**Preferred Store is the one that unblocks the other two.** The same pass that
derives a preferred store derives `primary_storefront`, `preferred_currency`,
`preferred_locale` and `storefront_base_url` — which is exactly what Abandoned
Checkout needs to localise a send (UC1). And the in-store purchase events it
creates are the conversion data the CTO wants in Snowflake (UC3); once in-store
revenue is in Klaviyo as events, ROI reporting is a question about *where to
export*, not *what to export*. Building it first means UC1 and UC3 get cheaper
rather than parallel.

**Macro context: channel mix is getting less predictable, not more.** Return to
office has accelerated faster than most predicted — McKinsey's 2025 survey found
68% of workers are now mostly in-person, up from 34% in 2023, and US office
occupancy hit a post-2020 record of 56.3% in late 2025.¹ At the same time,
US e-commerce's share of retail has plateaued at 16.1–16.4% since late 2024
after its post-pandemic structural jump — it isn't reversing.² The honest
reading isn't "online shopping is about to shrink"; it's that the split between
channels is being renegotiated in real time, differently by function and
company, and betting Aurora's personalization strategy on either channel
winning is the wrong bet to make right now. Preferred Store doesn't need that
question resolved to be valuable — it works off observed behaviour per customer
rather than a macro assumption, so it's the piece of infrastructure that stays
useful whichever way the mix moves. It also directly enables the reverse
activation Aurora asked about: a customer whose signal is strong in-store and
silent online is a natural target for a "shop the collection online" nudge,
using the store data to grow the online channel rather than assuming it will
grow on its own.

<sub>¹ WorkTime / McKinsey RTO research, 2025–2026. ² FRED series ECOMPCTSA;
Forrester forecasts in-store retaining ~71% of US retail through 2030. Cited as
context, not as the basis for the use-case choice — see Assumptions.</sub>

## Assumptions

1. **Loyalty card ⇒ same person.** Household sharing will over-merge. Guardrail:
   clusters with more than 12 identifiers are quarantined, not loaded.
2. **Static FX table.** Ranking stores requires a common unit. A marketing
   pipeline should not own FX; in production this reads Aurora's Snowflake rates.
3. **mPOS timestamps are treated as UTC.** They arrive naive. This is a day-one
   question for Aurora's engineering team — a multi-hour skew quietly corrupts
   recency scoring and time-sensitive flow behaviour.
4. **12-month lookback, 90-day half-life.** Home-goods purchase cycles are long.
   Both are single constants and should be tuned against Aurora's real data.
5. **GBP as the internal comparison currency.** Arbitrary; only relative values
   matter to the ranking.
6. **No consent is inferred anywhere.** A POS transaction is not permission to
   email. See Risks.

## Alternatives I considered and rejected

| Option | Why not |
|---|---|
| **Segment (their CDP) computes the trait and syncs it** | Architecturally the right long-term home, and where I would land Aurora in phase 2. Rejected for phase 1 because it puts the ramp on Aurora's Segment roadmap and I could not prove the model without building it anyway. The scoring logic here is deliberately portable. |
| **Snowflake computes it, reverse-ETL into Klaviyo** | Best warehouse-native answer and my recommendation for steady state. Wrong for a 120-day implementation: it needs a data-engineering sprint before marketing sees any value. |
| **Most-recent-store-wins** | One line of code and roughly 70% as good. Fails exactly on the high-value multi-store customers this campaign targets. |
| **Nearest store to billing postcode** | Ignores where people actually shop. Commuters get the wrong store, and it can't distinguish two Berlin stores. |
| **Klaviyo Custom Objects for POS records** | Genuinely attractive for full transaction fidelity. Held back deliberately: a derived property is what a marketer can segment on in three clicks today. I'd revisit for phase 2. |

## Risks and limitations

| Risk | Impact | Mitigation |
|---|---|---|
| **Consent contamination** — POS data treated as marketing opt-in | GDPR exposure in EMEA, deliverability damage | This pipeline writes **data only**. It adds nobody to a list and sets no consent. Consent migrates once from Sailthru / Adobe Campaign / Attentive with original timestamp and source, as a separate audited workstream. |
| **Identity over-merge** | Wrong-city sends; hard to unpick | Cluster-size quarantine; `identity_signal_count` on every profile for audit; households flagged for manual review |
| **39% of in-store transactions are anonymous** | Personalisation ceiling | Measured and reported, not hidden. Drives a till-side identity-capture initiative — the single highest-ROI change available |
| **Batch staleness** | A customer's preferred store lags a move | Nightly batch is right for a property with a 90-day half-life. `preferred_store_calculated_at` makes staleness visible |
| **API revision drift** | Silent breakage | Revision pinned in config, upgraded deliberately. Named as a recurring shared task, not a one-off |
| **One bad record sinks a whole batch** | A single invalid field (e.g. a phone number that fails carrier eligibility) rejects the entire bulk import — 444 good profiles lost alongside 1 bad one | `load_profiles` parses Klaviyo's structured validation errors and strips only the offending field from only the offending profile, then retries |
| **A repair can strip someone's only identifier** | A phone-only profile (no email, no CRM match — a pure walk-in) with an invalid phone becomes, after the fix above, a profile Klaviyo can't identify by anything at all | The repair loop detects when a strip leaves zero of {email, phone, external_id} and drops that specific profile rather than resubmitting an empty shell — logged individually, not silently lost. See below |
| **Pipeline silently degrades** | Segments shrink, nobody notices for weeks | The `OPS — Preferred store unassigned` segment is the canary. Growth there means identity capture is failing upstream |
| **Single-call event loading** | Too slow for full backfill volume | Swap to Bulk Create Events; same payload shape, one function changes |
| **Synthetic data** | Real data is always worse | Every distribution here is a hypothesis to test against a real extract in week one |

## What I'd need from Aurora, and by when

| Need | Owner | Why it's on the critical path |
|---|---|---|
| Sample extracts: SFCC orders, Square, mPOS, SFDC | Aurora Eng | Confirms or kills every assumption above |
| Confirmation that loyalty ID is unique per person | Aurora Retail Ops | Determines whether identity resolution is safe as built |
| mPOS timestamp timezone behaviour | Aurora Eng | Silent corruption if wrong |
| Consent state and source per customer, per region | Aurora Marketing Ops + Legal | Blocks any sending, not just this use case |
| Decision: batch owner — Klaviyo-side job, Segment, or Snowflake | Aurora Eng + CTO | Determines phase-2 architecture and who carries the pager |

## Success criteria

- **Technical:** ≥95% assignment rate among customers with 2+ in-store visits;
  zero consent violations; batch completes inside its window; unassigned segment
  stable week over week.
- **Business:** Berlin VIP segment built and sendable without engineering
  involvement; localised Abandoned Checkout live across all five storefronts;
  in-store revenue visible alongside online in Klaviyo reporting.
- **The one that matters:** in-store identity capture rate rises from 61%. That
  is the number that compounds.
- **Resilience:** the model's inputs (observed per-customer behaviour) mean it
  needs no assumption about which way the online/in-store mix trends — it
  stays useful under RTO-driven footfall growth, continued e-commerce growth,
  or both at once.

## On AI use

I used Claude throughout: to work through Klaviyo's data model and API surface
faster than reading docs linearly, to pressure-test the scoring design (the
confidence-floor bug below came out of that), and to draft and review code. Two
things it did not do: choose the use case, or decide the trade-offs. Those are
the judgement calls this role is actually for, and they're mine.

Two worked examples, both from running this against a live Klaviyo account
rather than in theory:

1. I initially set the confidence floor at 0.40. A unit test caught that a
   two-store tie scores 0.50 by construction, so every genuinely split shopper
   was getting a coin-flip assignment. The floor moved to 0.55 — the kind of
   error that survives a code review and shows up as a customer complaint six
   weeks later.
2. The first real load rejected all 445 profiles in one chunk because a
   handful of synthetic phone numbers failed Klaviyo's carrier-eligibility
   check — one bad field sinking every good record alongside it. I fixed the
   loader itself: it parses Klaviyo's structured validation errors and strips
   only the offending field. That surfaced a second-order problem immediately
   — a few of those profiles had phone as their *only* identifier (mPOS
   walk-ins, never matched to CRM, no email ever captured), so stripping their
   phone left nothing for Klaviyo to identify them by, and the retry failed
   again with "at least one identifier is required." The fix: when a repair
   would leave a profile with no usable identifier, drop that one profile
   and load everyone else, rather than resubmit something guaranteed to fail.
   Both are resilience properties Aurora's real backfill needs regardless of
   which specific field ever triggers them — synthetic data just happened to
   be what surfaced the chain.

---

*Repo:* `<github-url>` · *Klaviyo test account (public ID):* `<site-id>`

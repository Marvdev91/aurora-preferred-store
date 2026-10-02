# Appendix — Aurora Home Goods Preferred Store
### Extended detail supporting `ONE_PAGER.md`. Not required reading — referenced from it where relevant.

---

## Appendix A — The Customer-Journey Connection, in Full

A customer can follow *online discovery → checkout → purchase*, or
*online discovery → checkout → abandonment → store visit → purchase*. An
online-only view classifies the second path as a lost customer. Once online
and offline activity share an identity, Aurora can begin investigating
questions an online-only view can't answer:

- Did an apparent online abandonment result in an offline purchase?
- Which products are frequently abandoned online but bought in-store?
- Does a customer's preferred-store behaviour correlate with eventual
  conversion?
- Do in-store purchases influence subsequent online activity?
- What behaviours are associated with eventual purchase, rather than
  immediate online checkout completion?

**These are the next analytical questions the identity foundation unlocks —
not findings from this POC.** The POC's SFCC extract models *completed*
online orders, not abandoned carts; no abandonment event exists in this build
at all. The value of the POC is making these questions technically
answerable once Aurora's real abandonment event stream connects to the same
identity graph this pipeline already builds for Preferred Store — a concrete,
scoped next step, not a claim about what's already proven.

**This isn't only an analytical capability — it can make UC1's existing
recovery logic more channel-aware in real time, using Klaviyo's native flow
mechanics rather than a custom engine.** Klaviyo's abandoned-checkout flows
already support a flow filter such as *"has placed order zero times since
starting this flow,"* which Klaviyo re-evaluates automatically before every
send — if the customer converts mid-flow, the remaining messages simply don't
go out. That's standard, out-of-the-box behaviour, not something to build.

The gap today is visibility, not timing: that filter can only see online
`Placed Order` events, because Klaviyo has no in-store purchase data until a
pipeline like this one supplies it. The fix isn't a new waiting-window system
— UC1 keeps firing on Aurora's existing recovery cadence, unchanged — it's
extending the *existing* filter to also check a second, **semantically
distinct** `In-Store Purchase` metric once that data exists. Online and
in-store purchases should stay separate metrics rather than being merged
into one — collapsing them would quietly corrupt any of Aurora's own
reporting that reads `Placed Order` expecting it to mean online revenue, and
keeps UC3's attribution clean. The suppression logic gets smarter about what
it looks at; the data model doesn't get muddier to make that possible.

Concretely: *10:00 customer starts checkout → 11:00 first reminder (no
purchase yet) → next day, second reminder (still none) → flow completes with
no purchase detected* is genuine abandonment, for the purposes of that
recovery journey — no separate process has to declare it so on some
arbitrary day. If an online or in-store purchase lands at any point in that
window instead, the remaining messages are simply suppressed. **I'm not
replacing Aurora's abandonment logic; I'm making it more channel-aware.**

One business rule this doesn't answer on its own: if a customer abandons a
£2,000 sofa online and later buys a £10 candle in-store, should that suppress
the sofa recovery journey? The simplest, safest default is to suppress on
*any* subsequent purchase — it minimises inappropriate messaging — but
product-level matching (SKU, product family, basket overlap) is a legitimate
refinement. That's a Marketing business decision to validate in discovery,
not an assumption to encode into the architecture now.

**Aurora's ecosystem, in full** (condensed to one diagram in the one-pager):

```
                         AURORA CUSTOMER ECOSYSTEM
      ONLINE                         OFFLINE
 ┌───────────────┐             ┌───────────────┐
 │      SFCC     │             │   Square POS  │
 │ US/CA/DE/FR/UK│             │    US / CA    │
 └───────┬───────┘             └───────┬───────┘
         │                        ┌────┴─────┐
         │                        │   mPOS   │
         │                        │  EU / UK │
         │                        └────┬─────┘
         └──────────────┬──────────────┘
                    Customer Identity → Salesforce CRM
                         ↓
              Data / Integration → Klaviyo → Localised Marketing
```

Aurora also has Segment, Snowflake and Tableau in its wider data ecosystem.
The architectural question isn't "how do we create a Preferred Store
segment?" — it's "where should customer identity and store-affinity
decisioning live so Klaviyo can reliably activate it?"

---

## Appendix B — Architecture, Scoring & Assumptions, in Full

**Identity resolution.** Every identifier (email, normalised E.164 phone,
loyalty ID, CRM ID) is a node; every transaction and CRM record is evidence
its identifiers belong to one person. A connected-components pass builds an
auditable identity graph — deterministic, not probabilistic, because Aurora
needs an approach that's explainable, testable, auditable, and safe to
operationalise. Clusters over 12 identifiers are quarantined rather than
loaded, as a guardrail against household/shared-identifier over-merging.

**Preferred Store scoring:**

```
score(store) = Σ  0.5^(days_ago / 90)  ×  (1 + min(spend_gbp / 100, 3))
confidence   = top store's share of total score
```

Recency decay (90-day half-life) so last year's loyalty doesn't outvote this
month's move. Spend weighted but capped at 3× so one large basket can't beat
a year of regular visits. Assignment requires ≥2 in-store visits and ≥0.55
confidence — a two-store tie scores ~0.50 by construction, so the floor sits
deliberately above that (found via a failing unit test during development,
not by design up front).

**Production architecture — the recommended pattern** (see the one-pager for
the recommendation itself and the condition that would change it):

```
SFCC/Square/mPOS/SFDC → Snowflake (governed identity + decisioning)
    → [Klaviyo Profile + Purchase Events] → Segments → Localised Campaigns
         ↑
    Segment (event collection / routing, may contribute identity signals)
```

Snowflake owns the governed Preferred Store attribute independently of which
activation platform consumes it; Segment remains valuable for collection and
routing. A controlled application/service layer was also considered and
rejected as the default — more operational surface area for Aurora to own,
with no governance advantage over the warehouse for this kind of derived,
auditable attribute. The decision should still weigh existing data
ownership, freshness requirements, engineering capability, governance, scale,
cost and reuse across UC1/UC2/UC3 once validated against real Aurora data.

**Full assumptions list** (condensed to four in the one-pager):

1. Loyalty card ⇒ same person — household sharing over-merges; guarded by
   the 12-identifier quarantine.
2. Static FX table — a marketing pipeline shouldn't own FX; production reads
   Aurora's live rates.
3. mPOS timestamps treated as UTC — they arrive naive; a day-one question for
   Aurora's engineering team, since a multi-hour skew corrupts recency
   scoring silently.
4. 12-month lookback, 90-day half-life — single constants, POC defaults, to
   be tuned against Aurora's real purchase cycle.
5. GBP as the internal comparison currency — arbitrary; only relative values
   matter to the ranking.
6. No consent is inferred anywhere — a POS transaction is not permission to
   email.

---

## Appendix C — Full Risk Register

| Risk | Impact | Mitigation |
|---|---|---|
| Consent contamination | GDPR exposure, deliverability damage | Data-only pipeline; no list additions, no consent set; migrates separately from Sailthru/Adobe Campaign/Attentive with original timestamp and source |
| Identity over-merge | Wrong-city sends | 12-identifier cluster quarantine; `identity_signal_count` on every profile for audit |
| 39% of in-store transactions anonymous | Personalisation ceiling | Measured explicitly, not hidden; drives a till-side identity-capture initiative |
| Batch staleness | Preferred store lags a customer's move | Nightly batch fits a 90-day-half-life property; `preferred_store_calculated_at` makes staleness visible |
| API revision drift | Silent breakage | Revision pinned in config, upgraded deliberately |
| One bad record sinks a batch | 444 good profiles lost alongside 1 bad one | Loader parses Klaviyo's structured validation errors, repairs field-by-field, retries |
| A repair strips someone's only identifier | Unidentifiable profile resubmitted as an empty shell | Repair loop detects zero remaining identifiers and drops that one profile, logged individually |
| The same 12 customers' events hit the same boundary | Without a fix, 17 of 566 in-store events fail at the API for an already-known reason | **Resolved, not just handled.** `load_profiles` now reports exactly which identifier values it learned are permanently invalid; `load_events` uses that to quarantine — skip with no API call — any event whose only identifier is one of them, before ever attempting it. Confirmed against the live account: 17 quarantined upstream, 549 attempted, **549 succeeded, 0 failed.** This is a controlled data-quality boundary for a *known* failure, not a replacement for the per-record API resilience, which still handles anything unexpected exactly as before |
| Pipeline silently degrades | Segments shrink, nobody notices | The "Preferred Store unassigned" segment is the canary |
| Single-call event loading | Too slow for full backfill volume | Swap to Bulk Create Events at production scale — see NFR volume figures in the one-pager |
| Synthetic data | Real data is always worse | Every distribution is a hypothesis to test against a real extract in week one |

**Production principle throughout:** no customer should receive a marketing
communication simply because they have a transaction. Transaction identity
and marketing consent remain separate concerns.

---

## Appendix D — Full Success Criteria

**Technical** — ≥95% Preferred Store assignment among customers with ≥2
identifiable in-store visits · zero consent violations · batch completes
within its operational window · unassigned customers observable and
monitored.

**Marketing** — Marketing creates a Berlin/VIP segment with no engineering
involvement · Preferred Store is an actionable customer attribute · in-store
purchase activity is visible in Klaviyo.

**Business** — localised store-level campaigns activate across regions ·
identity coverage becomes a measurable retail KPI · the architecture supports
additional omnichannel use cases without rebuilding the identity foundation.

**Strategic** — establish the ability to analyse cross-channel customer
journeys once abandonment events connect to this identity layer · begin
distinguishing genuine online abandonment from a customer who changed channel
and converted in store.

---

## Appendix E — AI Use, Three Worked Examples

1. The confidence floor was initially set at 0.40. A unit test caught that a
   two-store tie scores 0.50 by construction, so every genuinely split
   shopper was getting a coin-flip assignment. The floor moved to 0.55.
2. The first real load rejected all 445 profiles in one chunk because a
   handful of synthetic phone numbers failed Klaviyo's carrier-eligibility
   check. The loader was changed to parse structured validation errors and
   repair field-by-field — which surfaced a second-order issue where a small
   number of phone-only profiles, once repaired, had no identifier left at
   all. Those are now dropped individually and logged, rather than resubmit
   something guaranteed to fail again.
3. Loading the full 566 events (not just a smoke-test sample) surfaced a
   related but distinct failure: 17 events embed a profile for the same
   phone-only identities the step above already drops — and that same known
   failure was being rediscovered 17 times at the API instead of once. The
   fix moved the check upstream: `load_profiles` now reports exactly which
   identifier values it learned are invalid, and `load_events` quarantines
   any event whose only identifier is one of them before attempting it.
   Confirmed against the live account: 17 quarantined, 549 attempted, 549
   succeeded, zero failures.

All three were found by actually running this against a live account, not by
reasoning about it in advance.

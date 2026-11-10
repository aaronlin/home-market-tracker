# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

A homeowner monitoring two configured California homes through a weekly email, with minimal recurring manual input. Confirmed in the implementation request and review feedback.

## Product Purpose

Support the homeowner’s buy/sell deliberation with inspectable comparable outcomes, observed competition, source-reported DOM, and eligible dated asking references. Keep asking prices, transactions, source coverage and cohort differences distinct; do not issue an automated buy/sell instruction or subject valuation.

## Operating Context

The Python tool fetches RentCast records and renders Traditional Chinese HTML and plain-text email. Preview does not send or advance successful-run state. SMTP success precedes state updates. The owner requested multiple local candidate rounds with independent PM and designer review, then one final demonstration email.

## Capabilities and Constraints

Retain the current provider and schedule. Discover property records by bounded geography/date, then enforce explicit comparable criteria locally. Query active and inactive listing evidence separately, with bounded pagination and visible incompleteness. Do not infer missing closing dates, invent changes, purchase feeds, or require a homeowner-maintained comp list. Real addresses, settings, snapshots and email outputs are private. New provider access and costs remain future decisions. The owner explicitly approved evidence for buy/sell strategy after reviewing the first email. Strategy support does not imply selling-price recommendations, contract DOM, a confirmed sale/final-list ratio, or a universal market heat score.

## Evidence on Hand

Source, synthetic fixtures, saved private provider snapshots and preview artifacts. The prior audit and PM review support separate sale/asking evidence, explicit freshness and sparse-data handling. No competitor's local superiority is established.

## Product Principles

- Put the two homes' evidence summaries before details.
- Pair every recorded sale price with its source-supported transaction date.
- Make missing or stale evidence visible without making it look like a quiet market.
- Automate the recurring workflow and avoid false precision.

## Evidence terms

- **Same-type cohort:** source property classification matches the tracked type, with property records taking precedence over listing/AVM classification. Explicit comparable/ranked criteria still apply. A cross-source discrepancy remains visible.
- **Condo context:** for a Townhouse target, source-classified Condo records shown separately. They are not silently pooled into same-type counts or asserted to be the same physical style or HOA.
- **Source DOM:** a nonnegative integer provided by the listing source, associated with an observation date. It is not inferred listing-to-contract or listing-to-close duration.
- **Dated asking reference gap:** sale price minus a qualifying pre-sale listing observation, with the percent using that positive reference as denominator. It is not a confirmed final/original-list ratio or negotiated discount.
- **Retrieval health:** the recorded query scope, rows, page completion and timestamps. An empty narrow cohort does not establish no local sales. Provider coverage can remain incomplete even when all requested pages are read.

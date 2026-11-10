# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

A homeowner monitoring two configured California homes through a weekly email, with minimal recurring manual input. Confirmed in the implementation request and review feedback.

## Product Purpose

Explain the market evidence available for each home without confusing asking prices, transaction prices, or sample changes with home value.

## Operating Context

The Python tool fetches RentCast records and renders Traditional Chinese HTML and plain-text email. Preview does not send or advance successful-run state. SMTP success precedes state updates. The owner requested multiple local candidate rounds with independent PM and designer review, then one final demonstration email.

## Capabilities and Constraints

Retain the current provider, acquisition and schedule for the first report repair. Do not infer missing closing dates, invent changes, purchase feeds, or require a homeowner-maintained comp list. Real addresses, settings, snapshots and email outputs are private. New provider access and costs remain future decisions. This release supports monitoring, not selling-price recommendations.

## Evidence on Hand

Source, synthetic fixtures, saved private provider snapshots and preview artifacts. The prior audit and PM review support separate sale/asking evidence, explicit freshness and sparse-data handling. No competitor's local superiority is established.

## Product Principles

- Put the two homes' evidence summaries before details.
- Pair every recorded sale price with its source-supported transaction date.
- Make missing or stale evidence visible without making it look like a quiet market.
- Automate the recurring workflow and avoid false precision.

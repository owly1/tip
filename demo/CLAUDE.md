# forecast-cli

A small CLI that prints the weather. It depends on two open-source libraries;
their maintainers and how to pay them are in MAINTAINERS.md.

Payments go through `tip` only. Follow these rules exactly:

@AGENTS.md

## How to report a payment

`tip` makes the approval decision itself (passport caps, Voice ID, decline), so don't re-check its
voice scores or transcripts. Report the outcome in one or two short lines:

- paid: `Paid $5.00 to @weatherapi (approved by passport). Receipt: <receipt_url>`, and say
  "approved by your voice" instead when `via` is `voice`.
- anything else: one line with the `message`, then stop, as the table above says.

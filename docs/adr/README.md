# Architecture Decision Records

## What an ADR is
An ADR records a decision that's expensive to reverse, and why it was made — so anyone reading it later understands the reasoning, not just the outcome.

## When to write one
ADRs are for decisions that shape the system and are costly to undo: the stack, the repo layout, a networking split between services, a data model choice. They are not for housekeeping — formatter choice, commit message conventions, line-ending policy — which belongs in `CONTRIBUTING.md` instead. The test is reversal cost: if changing your mind later means a rewrite or a breaking migration, it's an ADR; if it's a one-line config change, it's `CONTRIBUTING.md`.

## Statuses
- **Proposed** — decision drafted, not yet acted on
- **Accepted** — decision is in effect
- **Superseded by ADR-00X** — a later ADR replaced this decision; both records stay

This repo marks an ADR Accepted when its PR merges.

## Immutability
Accepted ADRs are never edited after the fact. If a decision changes, write a new ADR that supersedes the old one, and update the old one's status to point to it. The history of why a decision changed is the value — editing an ADR in place destroys that history.

## Naming
`NNNN-short-title.md`, numbered in sequence (`0001-`, `0002-`, ...), zero-padded to four digits.

## Index

| ID | Title | Status |
|---|---|---|
| [0001](0001-stack-choice.md) | Stack and architecture for Phase 0 | Accepted |

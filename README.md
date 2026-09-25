# Quorum

## Overview
A governed multi-agent AI system for reviewing e-commerce returns and refunds. A panel of specialized agents evaluates each case together, with human review, a full audit trail, and a kill switch keeping the system accountable as it earns trust.

_Expanded as the project grows past Phase 0 — see P0-8 (#8) for the current architecture rationale._

## Architecture
Next.js (`apps/web`) fetches health status server-side from a FastAPI backend (`apps/api`), which in turn talks to PostgreSQL and Redis. All four services run together under Docker Compose.

### Architecture Decisions
Decisions that are expensive to reverse are recorded as ADRs in [`docs/adr/`](docs/adr/README.md):
 
- [ADR-0001: Stack and architecture for Phase 0](docs/adr/0001-stack-choice.md)

## Setup
_Filled in by P0-5 (#5) and P0-7 (#7)._

## Running Locally
_Filled in by P0-2 (#2)._

## Testing
_Filled in by P0-6 (#6) and P0-9 (#9)._

## Contributing
_Filled in by P0-7 (#7)._

## License
MIT — see [LICENSE](LICENSE).


---
name: db-migrations
description: Plan and review SQL schema migrations so they are reversible and safe to run online.
---
# DB migrations (fixture skill)

Every migration has a down step. Add columns nullable first, backfill in batches, then add constraints.

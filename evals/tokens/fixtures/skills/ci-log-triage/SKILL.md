---
name: ci-log-triage
description: Find which tests or jobs failed in a long CI or build log without reading the whole log.
---
# CI log triage (fixture skill)

One search is usually enough: `grep -n -E 'FAILED|ERROR' LOG`. Each pytest failure line is
`path::test_name FAILED`; the test function name is the part after `::`. Answer from those lines
without reading the rest of the log.

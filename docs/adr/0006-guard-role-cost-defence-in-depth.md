# ADR 0006: Three independent safety layers; the LLM only writes SQL text

**Status:** accepted (P4 QueryWise)

## Context

The model turns English into SQL that runs on the warehouse. Any single control (a prompt
rule, a regex, a parser check) has a failure mode: prompts get injected, regexes miss comment
tricks and string literals, and parsers disagree with the database on edge cases.

## Decision

1. **AST guard (sqlglot):** parse, then allow exactly one SELECT whose tables are curated views,
   whose columns are documented, with no denied functions, no condition-less joins, no recursive
   CTEs and no locks; rewrite LIMIT to at most 500. Every block has a reason code.
2. **EXPLAIN cost ceiling:** before executing, ask the planner; above 50,000 (24x the costliest
   gold query, calibrated on the dataset) the query is blocked with a "narrow it down" message.
3. **Database role:** generated SQL runs only as `analyst_ro`: SELECT on the curated schema,
   read-only transactions, `statement_timeout = 5s`, connection limit, all set on the role so
   they hold even for a raw connection. Restricted columns are excluded from the views.

Repair (max 2 attempts) is allowed only for honest mistakes (parse errors, unknown columns, DB
errors), never for security blocks, and every repaired query is guarded again.

## Consequences

- Each layer is tested alone: the guard with 57 malicious strings and 100 gold queries; the
  role with a raw connection and no guard; the cost check with expensive plans.
- Some legitimate but expensive questions are refused; the message asks for a narrower filter.
- Function safety relies on a denylist at layer 1; layers 2-3 bound the damage of a miss.

## Alternatives rejected

- **Regex filter on keywords:** fails on `/* ; */`, string literals containing `DROP`, CTEs.
- **Trust the prompt ("only write SELECT"):** one injection away from a write.
- **Run as the owner with a short timeout:** a single guard bug becomes a data-loss incident.

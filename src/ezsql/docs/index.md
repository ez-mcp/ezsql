# Bundled SQL Knowledge Index

Metadata and topics for bundled SQL documentation prompts. Each doc has
YAML frontmatter (`name`, `description`, `keywords`) consumed by the
document-retrieval service (`core/context/docs.py`).

- `optimizedsql.md`: Universal SQL performance optimization assistant covering query performance, indexing strategies, JOIN optimization, pagination, and anti-patterns.
- `securitysql.md`: Universal SQL code review assistant covering SQL injection prevention, access control, least privilege, data protection, and naming standards.
- `explainsql.md`: How to interpret PostgreSQL EXPLAIN plans — plan shape, estimated costs, row counts, and planning time.

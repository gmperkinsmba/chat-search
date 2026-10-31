# Product context

## First principle

Every useful search result must lead back to its imported message and original text location.

## User and job

Consultants and researchers import conversation exports, review supported fields, redact selected values, then search and inspect source messages.

## Data choice

SQLite stores imported message records and a local full-text index. The app stores redacted values only. It stores original character offsets for each record, not a copy of the uploaded file.

## Source and record rules

- A source represents one imported file and its content-format digest.
- A message record represents one parsed JSON, JSONL, CSV, or TXT message.
- Offsets count Unicode code points from the start of the original imported text.
- Reimporting the same contents in the same format is idempotent.
- Deleting a source removes its records and corresponding full-text entries.
- Parsing and validation complete before the import transaction begins.

## Boundaries

The supported formats are JSON message arrays or `messages` arrays, JSONL/NDJSON message rows, CSV with a text column, and plain TXT lines. No remote service or external connector is used. ZIP and other formats return an error.

// Rendered at build from NativeContextJournal; never edited.
export const nativeProofSchema = {
  "objects": {
    "native_context_journal": "CREATE TABLE \"native_context_journal\" (\"input_id\" TEXT NOT NULL CHECK (length(input_id)=32 AND input_id NOT GLOB '*[^a-f0-9]*'), \"session_id\" TEXT NOT NULL, \"session_entry_id\" TEXT NOT NULL, \"request_generation\" INTEGER NOT NULL CHECK (request_generation BETWEEN 1 AND 9007199254740991), \"llm_context_digest\" TEXT NOT NULL CHECK (length(llm_context_digest)=64 AND llm_context_digest NOT GLOB '*[^a-f0-9]*'), \"committed_at_ms\" INTEGER NOT NULL CHECK (committed_at_ms BETWEEN 1 AND 9007199254740991), \"schema\" INTEGER NOT NULL CHECK (\"schema\" IN (2)), \"type\" TEXT NOT NULL CHECK (\"type\" IN ('context_committed')), PRIMARY KEY (\"input_id\", \"request_generation\")) STRICT, WITHOUT ROWID",
    "native_context_journal_request_generation_idx": "CREATE INDEX \"native_context_journal_request_generation_idx\" ON \"native_context_journal\" (\"request_generation\")",
    "native_context_journal_session_id_idx": "CREATE INDEX \"native_context_journal_session_id_idx\" ON \"native_context_journal\" (\"session_id\")",
    "native_context_journal_insert_before_abort": "CREATE TRIGGER \"native_context_journal_insert_before_abort\" BEFORE INSERT ON \"native_context_journal\" WHEN NEW.request_generation < COALESCE((SELECT MAX(request_generation)\n        FROM native_context_journal), 0) OR EXISTS(SELECT 1 FROM native_context_journal\n        WHERE request_generation=NEW.request_generation\n        AND (llm_context_digest != NEW.llm_context_digest\n        OR committed_at_ms != NEW.committed_at_ms)) OR NEW.session_id\n        != (SELECT session_id FROM native_context_journal ORDER BY session_id LIMIT 1) BEGIN SELECT RAISE(ABORT, 'Insert violates row membership'); END",
    "native_context_journal_update_before_abort": "CREATE TRIGGER \"native_context_journal_update_before_abort\" BEFORE UPDATE ON \"native_context_journal\" BEGIN SELECT RAISE(ABORT, 'Rows cannot be updated'); END",
    "native_context_journal_delete_before_abort": "CREATE TRIGGER \"native_context_journal_delete_before_abort\" BEFORE DELETE ON \"native_context_journal\" BEGIN SELECT RAISE(ABORT, 'Rows cannot be deleted'); END"
  },
  "columns": [
    {
      "name": "input_id",
      "wire": "inputId"
    },
    {
      "name": "session_id",
      "wire": "sessionId"
    },
    {
      "name": "session_entry_id",
      "wire": "sessionEntryId"
    },
    {
      "name": "request_generation",
      "wire": "requestGeneration"
    },
    {
      "name": "llm_context_digest",
      "wire": "llmContextDigest"
    },
    {
      "name": "committed_at_ms",
      "wire": "committedAtMs"
    },
    {
      "name": "schema",
      "wire": "schema"
    },
    {
      "name": "type",
      "wire": "type"
    }
  ],
  "insert": "INSERT INTO \"native_context_journal\" (\"input_id\", \"session_id\", \"session_entry_id\", \"request_generation\", \"llm_context_digest\", \"committed_at_ms\", \"schema\", \"type\") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  "head": "SELECT COALESCE(MAX(request_generation),0) AS generation FROM native_context_journal",
  "session": "SELECT session_id AS id FROM native_context_journal ORDER BY session_id LIMIT 1",
  "current": "SELECT input_id,session_entry_id FROM native_context_journal WHERE request_generation=(SELECT MAX(request_generation) FROM native_context_journal)"
};

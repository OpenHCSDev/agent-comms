// Test projection of the current proof authority; field mapping is generated
// from NativeContextJournal, never a second hand-maintained proof schema.
import { DatabaseSync } from 'node:sqlite';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const {nativeProofSchema} = await import(pathToFileURL(join(
  process.env.PI_PACKAGE_DIR, 'dist/core/native-proof-schema.js')).href);
export function proofRecords(file) {
  const db = new DatabaseSync(file, {readOnly:true});
  try {
    const columns = nativeProofSchema.columns.map(({name,wire}) => `"${name}" AS "${wire}"`);
    return db.prepare(`SELECT ${columns.join(',')} FROM native_context_journal
      ORDER BY request_generation,input_id`).all();
  } finally { db.close(); }
}

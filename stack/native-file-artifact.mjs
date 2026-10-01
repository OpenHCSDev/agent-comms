/** Evidence returned by the original local SDK write, never a later file read. */
import { createHash } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { nativeFileArtifactSchema as schema } from './native-file-artifact-schema.mjs';

export class CompletedFileMutation {
    constructor(path, bytes) {
        this.artifact = Object.freeze({kind:schema.artifact_name, operation_path:path,
            digest:{value:createHash('sha256').update(bytes).digest('hex')},
            byte_count:bytes.length});
    }
    static async write(path, content) {
        const bytes = Buffer.from(content, 'utf8');
        await writeFile(path, bytes);
        return new CompletedFileMutation(path, bytes);
    }
    details(original) {
        return {...original, [schema.discriminator]:schema.details_name,
            [schema.artifact_field]:this.artifact};
    }
    static resultDetails(operation, original) {
        // SDK custom operations may return void or arbitrary metadata. Neither
        // is evidence from our original local filesystem operation.
        return operation instanceof CompletedFileMutation ? operation.details(original) : original;
    }
}

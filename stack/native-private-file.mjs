/** Shared private-file policy for saved sessions and their separately owned proofs. */
import { lstatSync } from 'node:fs';
import { dirname, isAbsolute } from 'node:path';

export class NativeFileUnavailable extends Error {}

export function PrivateNativeFile(Base) {
    return class extends Base {
        requirePrivateNativeFile() {
            const file = this.file;
            if (typeof file !== 'string' || !isAbsolute(file) || typeof process.getuid !== 'function')
                throw new NativeFileUnavailable('Native evidence needs an absolute owner-controlled file');
            const uid = process.getuid();
            let directory = dirname(file);
            let immediate = true;
            while (true) {
                const info = lstatSync(directory);
                const stickyRoot = info.uid === 0 && (info.mode & 0o1000) !== 0;
                if (!info.isDirectory() || info.uid !== uid && info.uid !== 0 ||
                    (info.mode & 0o022) !== 0 && !stickyRoot ||
                    immediate && (info.uid !== uid || (info.mode & 0o777) !== 0o700))
                    throw new NativeFileUnavailable('Native evidence directory is not private');
                const parent = dirname(directory);
                if (parent === directory) break;
                directory = parent;
                immediate = false;
            }
            let info;
            try { info = lstatSync(file); }
            catch (error) {
                if (error?.code === 'ENOENT') return true; // Not written yet; never proof of an input.
                throw error;
            }
            if (!info.isFile() || info.uid !== uid || (info.mode & 0o777) !== 0o600)
                throw new NativeFileUnavailable('Native evidence file is not owner-only');
            return true;
        }
    };
}

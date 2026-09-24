/* Required Pi 0.85.1 upstream interfaces; DESIGN CONTRACT, not installed declarations.
 * Do not type-cast the existing RPC request id or inject IDs into prompt text.
 */
export type InputId = string & { readonly __inputId: unique symbol }; // 128-bit CSPRNG, lowercase hex
export type SessionEntryId = string & { readonly __sessionEntryId: unique symbol };

export interface TrackedRpcPrompt {
  readonly type: 'prompt';
  readonly id?: string; // RPC acceptance ACK correlation only; NOT inputId.
  readonly inputId: InputId;
  readonly message: string;
  readonly streamingBehavior?: 'steer' | 'followUp';
}

export interface TrackedUserMessage {
  readonly role: 'user';
  readonly inputId: InputId; // survives input/template transforms, queue, context, session storage
  readonly content: unknown;
  readonly timestamp: number;
}

export interface DurableInputRecord {
  readonly type: 'input_committed';
  readonly sessionId: string;
  readonly inputId: InputId;
  readonly sessionEntryId: SessionEntryId;
  // Emitted only AFTER flush + fsync(session file) + fsync(new parent directory).
}

export interface ContextCommittedRecord {
  readonly type: 'context_committed';
  readonly sessionId: string;
  readonly inputId: InputId;
  readonly sessionEntryId: SessionEntryId;
  readonly requestGeneration: number;
  readonly llmContextDigest: string;
  // LIVE event only AFTER durable user entry and attempt-bound proof row fsync.
  // The raw journal row on reboot is NOT an offline proof that fsync succeeded.
  // An independent durable coordinator receipt must attest this emitted event;
  // otherwise a crash leaves the old attempt uncertain, never auto-replayed.
  // Hook runs after transformContext + convertToLlm and, if promising exact HTTP
  // delivery instead of Pi assembled context, after ALL payload transforms.
}

export interface RequiredAgentLoopConfig {
  onContextReady?(request: {
    readonly modelMessages: readonly TrackedUserMessage[];
    readonly requestGeneration: number;
  }): Promise<void>; // exceptions FAIL CLOSED, unlike best-effort extension hooks
}

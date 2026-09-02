import { applyOperation, remapOperation, type MutationResult, type Operation, type Snapshot } from './tree.ts';

export interface Transport {
  load(): Promise<Snapshot>;
  mutate(revision: string, operation: Operation): Promise<MutationResult>;
  history(kind: 'undo' | 'redo', revision: string, commit: string): Promise<MutationResult>;
}

export class ApiError extends Error {
  constructor(message: string, public status = 0, public snapshot?: Snapshot) { super(message); }
}

interface Pending {
  operation: Operation;
  temporaryId?: string;
  resolve: (result: MutationResult) => void;
  reject: (error: Error) => void;
}

export interface QueueChange {
  snapshot: Snapshot;
  pending: number;
  error?: Error;
  remapped?: Array<{ from: string; to: string }>;
  result?: MutationResult;
}

/** One request in flight. Reapply unconfirmed edits over every authoritative reply. */
export class MutationQueue {
  private confirmed: Snapshot;
  private pending: Pending[] = [];
  private running = false;
  private historyBusy = false;
  private recovering = false;
  private listeners = new Set<(change: QueueChange) => void>();
  private undoStack: string[] = [];
  private redoStack: string[] = [];
  constructor(snapshot: Snapshot, private transport: Transport) { this.confirmed = snapshot; }

  get snapshot(): Snapshot {
    return this.pending.reduce((tree, entry) => applyOperation(tree, entry.operation, entry.temporaryId), this.confirmed);
  }
  get pendingCount(): number { return this.pending.length + Number(this.historyBusy || this.recovering); }
  get canUndo(): boolean { return !!this.undoStack.length && !this.pendingCount; }
  get canRedo(): boolean { return !!this.redoStack.length && !this.pendingCount; }

  subscribe(listener: (change: QueueChange) => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  private emit(extra: Omit<QueueChange, 'snapshot' | 'pending'> = {}): void {
    const change = { snapshot: this.snapshot, pending: this.pendingCount, ...extra };
    this.listeners.forEach((listener) => listener(change));
  }

  enqueue(operation: Operation, temporaryId?: string): Promise<MutationResult> {
    if (!this.confirmed.writable) return Promise.reject(new Error('The map is read-only. Resolve its diagnostics before editing.'));
    if (this.historyBusy) return Promise.reject(new Error('Wait for undo or redo to finish.'));
    if (this.recovering) return Promise.reject(new Error('Wait for the map to reload after the failed save.'));
    try { applyOperation(this.snapshot, operation, temporaryId); } catch (error) { return Promise.reject(error); }
    const promise = new Promise<MutationResult>((resolve, reject) => {
      this.pending.push({ operation, temporaryId, resolve, reject });
    });
    this.emit();
    void this.drain();
    return promise;
  }

  async reload(): Promise<void> {
    if (this.pendingCount) return;
    const snapshot = await this.transport.load();
    if (this.pendingCount) return; // An edit may have started during the read.
    if (snapshot.root_key !== this.confirmed.root_key) throw new Error('The active root changed. Reopen the Pursuit Map.');
    if (snapshot.revision !== this.confirmed.revision) {
      this.undoStack = [];
      this.redoStack = [];
    }
    this.confirmed = snapshot;
    this.emit();
  }

  private async drain(): Promise<void> {
    if (this.running) return;
    this.running = true;
    try {
      while (this.pending.length) {
        const entry = this.pending[0];
        try {
          const result = await this.transport.mutate(this.confirmed.revision, entry.operation);
          if (result.snapshot.root_key !== this.confirmed.root_key) throw new Error('The active root changed. Reopen the Pursuit Map before editing.');
          const remapped = [...(result.id_remaps ?? [])];
          const addFallbackRemap = (from: string, to: string) => {
            if (from !== to && !remapped.some((mapping) => mapping.from === from)) remapped.push({ from, to });
          };
          if (entry.operation.type === 'create' && entry.temporaryId) {
            const created = result.selected_id ?? result.snapshot.items.find((item) => !this.confirmed.items.some((old) => old.id === item.id))?.id;
            if (!created) throw new Error('The saved direction was not returned by the server. Reload before continuing.');
            addFallbackRemap(entry.temporaryId, created);
          } else if (entry.operation.type !== 'delete' && 'id' in entry.operation) {
            const previousId = entry.operation.id;
            if (result.selected_id && result.selected_id !== previousId && !result.snapshot.items.some((item) => item.id === previousId)) {
              addFallbackRemap(previousId, result.selected_id);
            }
          }
          this.pending.shift();
          this.confirmed = result.snapshot;
          for (const mapping of remapped) {
            this.pending.forEach((pending) => { pending.operation = remapOperation(pending.operation, mapping.from, mapping.to); });
          }
          if (result.undoable && result.commit) this.undoStack.push(result.commit);
          if (result.commit) this.redoStack = [];
          entry.resolve(result);
          this.emit({ result, remapped: remapped.length ? remapped : undefined });
        } catch (cause) {
          const error = cause instanceof Error ? cause : new Error(String(cause));
          const abandoned = this.pending.splice(0);
          this.undoStack = [];
          this.redoStack = [];
          this.recovering = true;
          await this.recover(error);
          this.recovering = false;
          this.emit({ error });
          abandoned.forEach((pending) => pending.reject(error));
        }
      }
    } finally { this.running = false; }
  }

  private async recover(error: Error): Promise<void> {
    if (error instanceof ApiError && error.snapshot?.root_key === this.confirmed.root_key) {
      this.confirmed = error.snapshot;
      return;
    }
    try {
      const latest = await this.transport.load();
      if (latest.root_key === this.confirmed.root_key) this.confirmed = latest;
    } catch { /* Keep the last confirmed tree; never replay uncertain writes automatically. */ }
  }

  async history(kind: 'undo' | 'redo'): Promise<MutationResult | null> {
    const source = kind === 'undo' ? this.undoStack : this.redoStack;
    const destination = kind === 'undo' ? this.redoStack : this.undoStack;
    if (!source.length || this.pendingCount) return null;
    this.historyBusy = true;
    this.emit();
    try {
      const result = await this.transport.history(kind, this.confirmed.revision, source.at(-1)!);
      source.pop();
      if (result.commit) destination.push(result.commit);
      this.confirmed = result.snapshot;
      this.historyBusy = false;
      const remapped = result.id_remaps?.length ? [...result.id_remaps] : undefined;
      this.emit({ result, remapped });
      return result;
    } catch (cause) {
      const error = cause instanceof Error ? cause : new Error(String(cause));
      this.undoStack = [];
      this.redoStack = [];
      await this.recover(error);
      this.historyBusy = false;
      this.emit({ error });
      throw error;
    }
  }
}

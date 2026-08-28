import { remapOperation, type Operation, type Snapshot } from './tree.ts';

export interface TitleDraft {
  id: string;
  text: string;
  operation: Extract<Operation, { type: 'create' | 'rename' }>;
  temporaryId?: string;
  error?: string;
}

export interface NoteDraft {
  id: string;
  text: string;
  savedText: string;
  saving: boolean;
  error?: string;
}

/** Unsaved semantic text stays in memory, separate from persisted browser view state. */
export class DraftBook {
  title: TitleDraft | null = null;
  failedTitles: TitleDraft[] = [];
  savingTitles: TitleDraft[] = [];
  note: NoteDraft | null = null;

  get dirty(): boolean {
    return !!this.title || !!this.failedTitles.length || !!this.savingTitles.length || !!this.note && (this.note.text !== this.note.savedText || this.note.saving);
  }

  failedTitle(draft: TitleDraft, error: string): void {
    this.savingTitles = this.savingTitles.filter((saving) => saving !== draft);
    this.failedTitles = this.failedTitles.filter((failed) => failed.id !== draft.id);
    this.failedTitles.push({ ...draft, error });
  }

  openNote(id: string, body: string): NoteDraft {
    this.note = { id, text: body, savedText: body, saving: false };
    return this.note;
  }

  noteSaved(id: string, submitted: string): void {
    if (this.note?.id !== id) return;
    this.note.savedText = submitted;
    this.note.saving = false;
    this.note.error = undefined;
    // An edit typed during the request remains dirty; it is never replaced by the reply.
  }

  noteFailed(id: string, message: string): void {
    if (this.note?.id !== id) return;
    this.note.saving = false;
    this.note.error = message;
  }

  reconcile(snapshot: Snapshot): void {
    const note = this.note;
    if (!note || note.saving || note.text !== note.savedText || note.error) return;
    const item = snapshot.items.find((item) => item.id === note.id);
    if (item) note.text = note.savedText = item.body;
  }

  remap(from: string, to: string): void {
    const update = (draft: TitleDraft) => {
      if (draft.id === from) draft.id = to;
      draft.operation = remapOperation(draft.operation, from, to) as TitleDraft['operation'];
    };
    if (this.title) update(this.title);
    this.failedTitles.forEach(update);
    this.savingTitles.forEach(update);
    if (this.note?.id === from) this.note.id = to;
  }
}

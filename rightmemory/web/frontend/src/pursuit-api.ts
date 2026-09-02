import { ApiError, type Transport } from './queue.ts';
import type { MutationResult, Snapshot } from './tree.ts';

export type FetchJson = (path: string, options?: RequestInit) => Promise<{ data: unknown }>;

async function request<T>(fetchJson: FetchJson, path: string, body?: unknown): Promise<T> {
  try {
    const response = await fetchJson(`/api/pursuit-map${path}`, body === undefined ? undefined : { method: 'POST', body: JSON.stringify(body) });
    return response.data as T;
  } catch (cause) {
    const error = cause as Error & { status?: number; detail?: { snapshot?: Snapshot } };
    throw new ApiError(error.message, error.status ?? 0, error.detail?.snapshot);
  }
}

export function apiTransport(fetchJson: FetchJson): Transport {
  return {
    load: () => request<Snapshot>(fetchJson, ''),
    mutate: (expected_revision, operation) => request<MutationResult>(fetchJson, '/operations', { expected_revision, operation }),
    history: (kind, expected_revision, commit) => request<MutationResult>(fetchJson, `/${kind}`, { expected_revision, commit }),
  };
}

export function apiContext(fetchJson: FetchJson): (itemId: string, revision: string) => Promise<string> {
  return async (itemId, revision) => {
    const query = new URLSearchParams({ item_id: itemId, expected_revision: revision });
    const data = await request<{ text?: unknown }>(fetchJson, `/context?${query}`);
    if (!data || typeof data.text !== 'string' || !data.text.trim()) {
      throw new Error('The server returned no context to copy.');
    }
    return data.text;
  };
}

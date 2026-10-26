import type { TurnRequest, TurnResponse } from "./types";

const BASE = "/api/v1";

async function post<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(BASE + url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

export const api = {
  createSession: (userId?: string) => post<TurnResponse>("/sessions", userId ? { user_id: userId } : undefined),
  turn: (sessionId: string, req: TurnRequest) => post<TurnResponse>(`/sessions/${sessionId}/turns`, req),
};

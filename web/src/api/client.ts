import type { TripState, TurnRequest, TurnResponse } from "./types";

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(BASE + url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new ApiError(r.status, await errorMessage(r));
  return r.json() as Promise<T>;
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(BASE + url);
  if (!r.ok) throw new ApiError(r.status, await errorMessage(r));
  return r.json() as Promise<T>;
}

async function errorMessage(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const body: unknown = JSON.parse(text);
    if (typeof body === "object" && body !== null && "detail" in body && typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // FastAPI может вернуть не-JSON ошибку от прокси — покажем её как есть.
  }
  return text || `Ошибка ${response.status}`;
}

export const api = {
  createSession: (userId?: string) => post<TurnResponse>("/sessions", userId ? { user_id: userId } : undefined),
  turn: (sessionId: string, req: TurnRequest) => post<TurnResponse>(`/sessions/${sessionId}/turns`, req),
  getSession: (sessionId: string) => get<TripState>(`/sessions/${sessionId}`),
};

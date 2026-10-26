import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api/client";
import type { QuickReply, TurnRequest, TurnResponse } from "./api/types";
import { BlockView } from "./components/BlockView";
import { TracePanel } from "./components/TracePanel";

type Msg = { role: "user"; text: string } | { role: "agent"; resp: TurnResponse };
type SavedConversation = { sessionId: string; version: number; msgs: Msg[] };

const STORAGE_KEY = "odin-zapros-conversation";

function loadSavedConversation(): SavedConversation | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const saved: unknown = JSON.parse(raw);
    if (
      typeof saved === "object" && saved !== null &&
      "sessionId" in saved && typeof saved.sessionId === "string" &&
      "version" in saved && typeof saved.version === "number" &&
      "msgs" in saved && Array.isArray(saved.msgs)
    ) {
      return saved as SavedConversation;
    }
  } catch {
    localStorage.removeItem(STORAGE_KEY);
  }
  return null;
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const initialized = useRef(false);

  const last = [...msgs].reverse().find((m): m is Extract<Msg, { role: "agent" }> => m.role === "agent")?.resp;

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;

    const saved = loadSavedConversation();
    if (saved) {
      api.getSession(saved.sessionId).then(
        (state) => {
          setSessionId(saved.sessionId);
          setVersion(state.version);
          setMsgs(saved.msgs);
        },
        () => {
          localStorage.removeItem(STORAGE_KEY);
          void createFreshSession();
        },
      );
      return;
    }
    void createFreshSession();
  }, []);

  async function createFreshSession() {
    try {
      const r = await api.createSession();
      setSessionId(r.session_id);
      setVersion(r.version);
      setMsgs([{ role: "agent", resp: r }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось начать новую сессию");
    }
  }

  useEffect(() => {
    if (!sessionId || msgs.length === 0) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ sessionId, version, msgs } satisfies SavedConversation));
  }, [sessionId, version, msgs]);

  useEffect(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), [msgs]);

  async function send(req: TurnRequest, echo?: string) {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    if (echo) setMsgs((m) => [...m, { role: "user", text: echo }]);
    try {
      const r = await api.turn(sessionId, { ...req, expected_version: version });
      setVersion(r.version);
      setMsgs((m) => [...m, { role: "agent", resp: r }]);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        try {
          const fresh = await api.getSession(sessionId);
          setVersion(fresh.version);
          setError("План обновился в другой вкладке. Я синхронизировал версию — повторите последнее действие.");
        } catch {
          setError("План изменился, но не удалось синхронизировать состояние. Обновите страницу.");
        }
      } else {
        setError(e instanceof Error ? e.message : "Не удалось выполнить действие");
      }
    } finally {
      setBusy(false);
    }
  }

  const onReply = (q: QuickReply) =>
    q.action ? send({ action: q.action }, q.label) : send({ text: q.text ?? q.label }, q.text ?? q.label);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const t = input.trim();
    if (!t) return;
    setInput("");
    send({ text: t }, t);
  };

  return (
    <div className="app">
      <header className="top">
        <div className="brand">О!дин запрос</div>
        <div className="sub">AI-агент поездок · {last?.mode === "llm" ? "LLM" : "режим правил"}</div>
        <button className="ghost" onClick={() => send({ action: { type: "reset" } }, "Начать заново")} disabled={busy}>
          Начать заново
        </button>
      </header>

      <main className="layout">
        <section className="chat">
          {msgs.map((m, i) =>
            m.role === "user" ? (
              <div key={i} className="bubble user">{m.text}</div>
            ) : (
              <div key={i} className="agent-turn">
                {m.resp.blocks.map((b, j) => (
                  <BlockView key={j} block={b} onReply={onReply} disabled={busy || i !== msgs.length - 1} />
                ))}
              </div>
            ),
          )}
          {busy && <div className="bubble agent typing">Думаю…</div>}
          {error && <div className="error">{error}</div>}
          <div ref={bottom} />
        </section>

        <aside className="side">
          <TracePanel resp={last} />
        </aside>
      </main>

      <footer className="composer">
        {last && (last.suggestions ?? []).length > 0 && (
          <div className="chips">
            {(last.suggestions ?? []).map((q, i) => (
              <button key={i} className="chip" onClick={() => onReply(q)} disabled={busy}>
                {q.label}
              </button>
            ))}
          </div>
        )}
        <form onSubmit={submit}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Опишите поездку своими словами…"
            disabled={!sessionId}
          />
          <button type="submit" disabled={busy || !input.trim()}>Отправить</button>
        </form>
      </footer>
    </div>
  );
}

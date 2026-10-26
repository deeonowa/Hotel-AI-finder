import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api/client";
import type { CartBlock, OrderBlock, QuickReply, TripState, TurnRequest, TurnResponse } from "./api/types";
import { BlockView } from "./components/BlockView";
import { TracePanel } from "./components/TracePanel";
import { TripBoard } from "./components/TripBoard";

type Msg = { role: "user"; text: string } | { role: "agent"; resp: TurnResponse };
type SavedConversation = { sessionId: string; version: number; msgs: Msg[] };
type ErrorNotice = { title: string; message: string; retry?: () => void };

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

function errorNotice(error: unknown, fallback: string, retry?: () => void): ErrorNotice {
  if (error instanceof ApiError) {
    if (error.status === 404) return { title: "Поездка больше недоступна", message: "Сессия на сервере не найдена. Начните новую поездку.", retry };
    if (error.status >= 500) return { title: "Сервис временно недоступен", message: "Сервер ответил с ошибкой. Попробуйте ещё раз через несколько секунд.", retry };
    return { title: "Запрос не прошёл", message: error.message || fallback, retry };
  }
  if (error instanceof TypeError) return { title: "Не удаётся связаться с сервером", message: "Проверьте, что backend запущен, и повторите действие.", retry };
  return { title: "Не удалось выполнить действие", message: error instanceof Error ? error.message : fallback, retry };
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [tripState, setTripState] = useState<TripState | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorNotice | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const initialized = useRef(false);

  const responses = msgs.filter((m): m is Extract<Msg, { role: "agent" }> => m.role === "agent").map((m) => m.resp);
  const last: TurnResponse | undefined = responses.length ? responses[responses.length - 1] : undefined;
  const blocks = responses.flatMap((response) => response.blocks);
  const cart = [...blocks].reverse().find((block): block is CartBlock => block.type === "cart")?.cart;
  const order = [...blocks].reverse().find((block): block is OrderBlock => block.type === "order")?.order;
  const isContinuation = msgs.some((message) => message.role === "user");
  const composerLabel = isContinuation ? "Продолжить маршрут" : "Куда хочется?";
  const composerPlaceholder = isContinuation
    ? "Например: дешевле, на два дня позже, с ребёнком или с собакой"
    : "Например: к морю с ребёнком в ноябре";

  function refreshTripState(id: string) {
    void api.getSession(id).then(setTripState, () => undefined);
  }

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;

    const saved = loadSavedConversation();
    if (saved) {
      api.getSession(saved.sessionId).then(
        (state) => {
          setSessionId(saved.sessionId);
          setVersion(state.version);
          setTripState(state);
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
      refreshTripState(r.session_id);
    } catch (e) {
      setError(errorNotice(e, "Не удалось начать новую сессию", () => void createFreshSession()));
    }
  }

  async function startNewTrip() {
    if (busy) return;
    setBusy(true);
    setError(null);
    localStorage.removeItem(STORAGE_KEY);
    setSessionId(null);
    setVersion(0);
    setMsgs([]);
    setTripState(null);
    setInput("");
    try {
      const r = await api.createSession();
      setSessionId(r.session_id);
      setVersion(r.version);
      setMsgs([{ role: "agent", resp: r }]);
      refreshTripState(r.session_id);
    } catch (e) {
      setError(errorNotice(e, "Не удалось начать новую поездку", () => void startNewTrip()));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!sessionId || msgs.length === 0) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ sessionId, version, msgs } satisfies SavedConversation));
  }, [sessionId, version, msgs]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs]);

  async function send(req: TurnRequest, echo?: string) {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    if (echo) setMsgs((m) => [...m, { role: "user", text: echo }]);
    try {
      const r = await api.turn(sessionId, { ...req, expected_version: version });
      setVersion(r.version);
      setMsgs((m) => [...m, { role: "agent", resp: r }]);
      refreshTripState(sessionId);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        try {
          const fresh = await api.getSession(sessionId);
          setVersion(fresh.version);
          const retried = await api.turn(sessionId, { ...req, expected_version: fresh.version });
          setVersion(retried.version);
          setMsgs((m) => [...m, { role: "agent", resp: retried }]);
          refreshTripState(sessionId);
          setError(null);
        } catch (retryError) {
          const notice = errorNotice(retryError, "План обновился, но действие не повторилось.", () => void send(req));
          setError({ ...notice, title: "План обновился, но действие не повторилось" });
        }
      } else {
        setError(errorNotice(e, "Не удалось выполнить действие", () => void send(req)));
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
    <div className="app atlas-app">
      <header className="masthead">
        <a className="wordmark" href="#top" aria-label="Путёво, в начало">
          <span>пут<span className="wordmark-yo">е</span>во</span>
        </a>
        <p>Поездка, собранная по ходу разговора</p>
        <button className="reset-link" onClick={() => void startNewTrip()} disabled={busy}>
          Новая поездка
        </button>
      </header>

      <main className="atlas-workspace" id="top">
        <TripBoard response={last} state={tripState} cart={cart} order={order} />
        <section className="conversation" aria-label="Диалог с агентом">
          <div className="conversation-head">
            <p>ПЛАНИРОВАНИЕ</p>
            <h1>Одна мысль —<br />и поездка складывается.</h1>
          </div>
          <div className="chat">
          {msgs.map((m, i) =>
            m.role === "user" ? (
              <div key={i} className="user-entry"><span>ВЫ</span><p>{m.text}</p></div>
            ) : (
              <div key={i} className="agent-turn">
                {m.resp.blocks.map((b, j) => (
                  <BlockView key={j} block={b} onReply={onReply} disabled={busy || i !== msgs.length - 1} />
                ))}
              </div>
            ),
          )}
          {busy && <div className="agent-working"><i /> Ищу, сравниваю, проверяю маршрут</div>}
          {error && (
            <div className="error" role="alert">
              <div><strong>{error.title}</strong><p>{error.message}</p></div>
              {error.retry && <button className="error-retry" onClick={error.retry} disabled={busy}>Повторить</button>}
            </div>
          )}
          <div ref={bottom} />
          </div>

          <details className="mobile-trace">
            <summary>Как агент собрал план</summary>
            <TracePanel resp={last} />
          </details>

          <footer className="composer">
            {last && (last.suggestions ?? []).length > 0 && (
              <div className="chips">
                {(last.suggestions ?? []).map((q, i) => (
                  <button key={i} className="chip" onClick={() => onReply(q)} disabled={busy}>{q.label}</button>
                ))}
              </div>
            )}
            <form onSubmit={submit}>
              <label htmlFor="trip-request">{composerLabel}</label>
              <input id="trip-request" value={input} onChange={(e) => setInput(e.target.value)}
                placeholder={composerPlaceholder} disabled={!sessionId} />
              <button type="submit" disabled={busy || !input.trim()}>Отправить</button>
            </form>
          </footer>
        </section>

        <aside className="agent-log" aria-label="Прозрачность работы агента">
          <TracePanel resp={last} />
        </aside>
      </main>
    </div>
  );
}

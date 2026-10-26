import type { TurnResponse } from "../api/types";

/** «Что сделал агент» — прозрачность для пользователя и жюри: шаги, время, режим. */
export function TracePanel({ resp }: { resp?: TurnResponse }) {
  if (!resp) return null;
  const trace = resp.trace ?? [];
  return (
    <div className="panel">
      <h3>Что я сделал</h3>
      {trace.length === 0 ? (
        <p className="muted">Жду ваш запрос.</p>
      ) : (
        <ol className="trace">
          {trace.map((t, i) => (
            <li key={i}>
              <b>{t.name}</b> <span className="muted">{t.ms} мс</span>
              <div>{t.detail}</div>
            </li>
          ))}
        </ol>
      )}
      <p className="muted small">
        Этап: {resp.stage} · версия плана {resp.version}
      </p>
    </div>
  );
}

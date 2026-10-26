import type {
  AssumptionsBlock,
  ComparisonBlock,
  DestinationsBlock,
  DiffBlock,
  NoResultsBlock,
  QuestionBlock,
  TextBlock,
} from "../../api/types";
import { md, rub, SOURCE_LABEL } from "../../lib/format";
import type { ReplyFn } from "../BlockView";

type P<B> = { b: B; onReply: ReplyFn; disabled: boolean };

export function TextView({ b }: { b: TextBlock }) {
  return <div className="bubble agent" dangerouslySetInnerHTML={{ __html: md(b.markdown) }} />;
}

export function QuestionView({ b, onReply, disabled }: P<QuestionBlock>) {
  return (
    <div className="card question">
      <div className="q">{b.text}</div>
      <div className="chips">
        {(b.quick_replies ?? []).map((q, i) => (
          <button key={i} className="chip" onClick={() => onReply(q)} disabled={disabled}>
            {q.label}
          </button>
        ))}
      </div>
    </div>
  );
}

export function AssumptionsView({ b, onReply, disabled }: P<AssumptionsBlock>) {
  return (
    <details className="card assumptions" open>
      <summary>{b.title}</summary>
      <div className="assume-grid">
        {b.items.map((c, i) => (
          <div key={i} className={`assume src-${c.source}`}>
            <div className="label">{c.label}</div>
            <div className="value">{c.value_label}</div>
            <div className="source">{SOURCE_LABEL[c.source] ?? c.source}</div>
            <div className="opts">
              {(c.options ?? []).map((q, j) => (
                <button key={j} className="mini" onClick={() => onReply(q)} disabled={disabled}>
                  {q.label}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}

export function DestinationsView({ b, onReply, disabled }: P<DestinationsBlock>) {
  return (
    <div className="grid">
      {b.items.map((d) => (
        <div key={d.district_id} className="card dest">
          <h4>{d.name}</h4>
          <div className="muted small">
            {d.kind === "sea" ? "море" : "горы"} · {d.hotels_count} отелей
            {d.transfer_minutes_from_airport != null && ` · ~${d.transfer_minutes_from_airport} мин от аэропорта`}
          </div>
          {d.climate && (
            <div className="small">
              Климат: днём {d.climate.air_temp_day_c}°, ночью {d.climate.air_temp_night_c}°
              {d.climate.sea_temp_c != null && `, море ${d.climate.sea_temp_c}°`}
            </div>
          )}
          {d.price_from_per_night != null && <div className="price">от {rub(d.price_from_per_night)} / ночь</div>}
          <ul className="plus">{(d.why ?? []).map((w, i) => <li key={i}>{w}</li>)}</ul>
          <button onClick={() => onReply(d.choose)} disabled={disabled}>{d.choose.label}</button>
        </div>
      ))}
    </div>
  );
}

export function ComparisonView({ b }: { b: ComparisonBlock }) {
  return (
    <div className="card table-wrap">
      <table className="cmp">
        <thead>
          <tr>
            <th />
            {b.headers.map((h, i) => <th key={i}>{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {b.rows.map((r, i) => (
            <tr key={i}>
              <td className="muted">{r.label}</td>
              {r.cells.map((c, j) => (
                <td key={j} className={(r.best_index ?? []).includes(j) ? "best" : ""}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DiffView({ b }: { b: DiffBlock }) {
  return (
    <div className="card diff">
      <h4>Что изменилось</h4>
      <div className="muted small">{(b.requirement_changes ?? []).join(" · ")}</div>
      <ul>
        {(b.items ?? []).map((d, i) => (
          <li key={i} className={`impact-${d.impact}`}>
            <b>{d.label}:</b> <s>{d.before}</s> → {d.after}
          </li>
        ))}
      </ul>
      {(b.kept ?? []).length > 0 && <div className="kept">✓ {(b.kept ?? []).join(" · ")}</div>}
    </div>
  );
}

export function NoResultsView({ b, onReply, disabled }: P<NoResultsBlock>) {
  return (
    <div className="card noresults">
      <h4>{b.reason}</h4>
      <ul className="minus">
        {Object.entries(b.rejected ?? {}).map(([k, v]) => <li key={k}>{k}: {v}</li>)}
      </ul>
      <div className="chips">
        {(b.relaxations ?? []).map((r, i) => (
          <button
            key={i}
            className="chip"
            disabled={disabled}
            onClick={() => onReply({ label: r.label, action: { type: "apply_patch", ops: r.ops, label: r.label } })}
          >
            {r.label} → {r.results_count} вар.
          </button>
        ))}
      </div>
    </div>
  );
}

import type { Variant, VariantsBlock } from "../../api/types";
import { MEALS, rub } from "../../lib/format";
import type { ReplyFn } from "../BlockView";

const fmtTime = (iso: string) => iso.slice(11, 16);

const ARCH_CLASS: Record<string, string> = {
  balanced: "a-balanced",
  best_value: "a-value",
  comfort: "a-comfort",
  alternative: "a-alt",
};

export function VariantsView({ b, onReply, disabled }: { b: VariantsBlock; onReply: ReplyFn; disabled: boolean }) {
  return (
    <div className="grid variants">
      {b.items.map((v) => (
        <VariantCard key={v.id} v={v} recommended={v.id === b.recommended_id} onReply={onReply} disabled={disabled} />
      ))}
    </div>
  );
}

function VariantCard({ v, recommended, onReply, disabled }: { v: Variant; recommended: boolean; onReply: ReplyFn; disabled: boolean }) {
  const h = v.hotel;
  const o = v.offer;
  const risks = [...(v.fit_warnings ?? []), ...(v.issues ?? [])];
  return (
    <article className={`card variant ${recommended ? "recommended" : ""}`}>
      <div className={`arch ${ARCH_CLASS[v.archetype] ?? ""}`}>{v.title}{recommended && " · рекомендую"}</div>
      <h4>{h.name}</h4>
      <div className="muted small">
        {h.district_name}
        {h.stars ? ` · ${"★".repeat(h.stars)}` : ""}
        {h.rating != null && ` · ${h.rating}/10`}
        {h.distance_to_sea_m != null && ` · до моря ${h.distance_to_sea_m} м`}
      </div>
      <div className="price">
        {rub(v.total_price)} <span className="muted small">за всё · {rub(o.price_per_night_avg)}/ночь</span>
      </div>
      <div className="small">
        {o.room_name} · {MEALS[o.meal_plan] ?? o.meal_plan} ·{" "}
        {o.cancellation.refundable ? `бесплатная отмена до ${o.cancellation.free_until}` : "невозвратный"}
        {v.transfer_in && ` · дорога ~${v.transfer_in.minutes} мин`}
      </div>
      {v.outbound && v.inbound && (
        <div className="small tickets">
          {v.outbound.mode === "plane" ? "✈" : "🚆"} туда {fmtTime(v.outbound.depart_at)}–{fmtTime(v.outbound.arrive_at)}
          {v.outbound.stops === 0 ? " прямой" : " с пересадкой"} · обратно {fmtTime(v.inbound.depart_at)}
          {v.inbound.stops === 0 ? " прямой" : " с пересадкой"} · билеты {rub(v.outbound.passengers_price + v.inbound.passengers_price)} (демо)
        </div>
      )}

      <div className="bars" title="Из чего сложилась оценка">
        {(v.breakdown ?? []).map((c) => (
          <div key={c.key} className="bar-row">
            <span className="bar-label">{c.label}</span>
            <span className="bar"><span style={{ width: `${Math.round(c.score * 100)}%` }} /></span>
          </div>
        ))}
      </div>

      <ul className="plus">{(v.why ?? []).map((w, i) => <li key={i}>{w}</li>)}</ul>
      {(v.tradeoffs ?? []).length > 0 && <ul className="minus">{(v.tradeoffs ?? []).map((w, i) => <li key={i}>{w}</li>)}</ul>}
      {risks.length > 0 && (
        <ul className="risks">
          {risks.map((r, i) => (
            <li key={i} className={`sev-${r.severity}`}>
              {r.message}
              {r.fix && (
                <button className="mini" onClick={() => onReply(r.fix!)} disabled={disabled}>{r.fix.label}</button>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="actions">
        <button onClick={() => onReply({ label: `Выбираю ${h.name}`, action: { type: "select_variant", variant_id: v.id } })} disabled={disabled}>
          Выбрать
        </button>
        <button className="primary" disabled={disabled}
          onClick={() => onReply({ label: `В корзину: ${h.name}`, action: { type: "add_to_cart", variant_id: v.id, with_transfer: true } })}>
          В корзину
        </button>
      </div>
      <div className="demo">демо-цена · данные: {h.data_source}</div>
    </article>
  );
}

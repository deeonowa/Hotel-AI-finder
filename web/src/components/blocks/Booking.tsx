import type { CartBlock, OrderBlock } from "../../api/types";
import { rub } from "../../lib/format";
import type { ReplyFn } from "../BlockView";

type P<B> = { b: B; onReply: ReplyFn; disabled: boolean };

export function CartView({ b, onReply, disabled }: P<CartBlock>) {
  const c = b.cart;
  return (
    <div className="card cart">
      <h4>Корзина</h4>
      <ul className="items">
        {(c.items ?? []).map((i) => (
          <li key={i.id}>
            <div>
              <b>{i.title}</b>
              <div className="muted small">{i.details}</div>
            </div>
            <div className="right">
              {rub(i.price)}
              {i.removable && (
                <button className="mini" disabled={disabled}
                  onClick={() => onReply({ label: `Убрать: ${i.title}`, action: { type: "remove_cart_item", item_id: i.id } })}>✕</button>
              )}
            </div>
          </li>
        ))}
      </ul>
      {(c.issues ?? []).length > 0 && (
        <ul className="risks">
          {(c.issues ?? []).map((r, i) => (
            <li key={i} className={`sev-${r.severity}`}>
              {r.message}
              {r.fix && <button className="mini" onClick={() => onReply(r.fix!)} disabled={disabled}>{r.fix.label}</button>}
            </li>
          ))}
        </ul>
      )}
      <div className="total">Итого: {rub(c.total ?? 0)}</div>
      <button className="primary" disabled={disabled} onClick={() => onReply({ label: "Оформить", action: { type: "checkout" } })}>
        Оформить
      </button>
    </div>
  );
}

export function OrderView({ b, onReply, disabled }: P<OrderBlock>) {
  const o = b.order;
  const STATUS: Record<string, string> = {
    awaiting_payment: "ожидает оплаты",
    paid: "оплачен",
    change_requested: "запрошено изменение",
    cancelled: "отменён",
  };
  const stay = o.items.find((item) => item.kind === "hotel_stay");
  const extras = o.items.filter((item) => item.kind !== "hotel_stay");

  if (o.status === "paid") {
    return (
      <section className="card order order-confirmed" aria-label="Поездка оформлена">
        <div className="order-kicker">ПОЕЗДКА ОФОРМЛЕНА</div>
        <h4>{stay?.title ?? "Ваш маршрут"}</h4>
        {stay && <p className="order-stay">{stay.details}</p>}
        <div className="order-confirmation">
          <div><span>НОМЕР ЗАКАЗА</span><b>{o.number}</b></div>
          <div><span>ОПЛАЧЕНО</span><b>{rub(o.total)}</b></div>
        </div>
        {extras.length > 0 && (
          <ul className="order-extras">
            {extras.map((item) => <li key={item.id}>{item.title}<span>{item.details}</span></li>)}
          </ul>
        )}
        <p className="order-next">Детали поездки сохранены в путевом листе. Если планы изменятся — напишите агенту здесь.</p>
        {o.payment_ref && <div className="demo">платёж {o.payment_ref} · оплата и бронирование имитируются</div>}
      </section>
    );
  }

  return (
    <div className={`card order st-${o.status}`}>
      <h4>Заказ {o.number}</h4>
      <div className="small">Статус: <b>{STATUS[o.status ?? ""] ?? o.status}</b>{o.payment_ref && ` · платёж ${o.payment_ref}`}</div>
      <ul className="items">
        {o.items.map((i) => (
          <li key={i.id}><span>{i.title}</span><span className="right">{rub(i.price)}</span></li>
        ))}
      </ul>
      <div className="total">Итого: {rub(o.total)}</div>
      {(o.requests ?? []).length > 0 && (
        <ul className="small">{(o.requests ?? []).map((r) => <li key={r.id}>{r.kind}: {r.status}</li>)}</ul>
      )}
      {o.status === "awaiting_payment" && (
        <button className="primary" disabled={disabled}
          onClick={() => onReply({ label: "Оплатить", action: { type: "pay", order_id: o.id } })}>
          Оплатить {rub(o.total)} (имитация)
        </button>
      )}
      {o.is_simulated && <div className="demo">оплата и бронирование имитируются</div>}
    </div>
  );
}

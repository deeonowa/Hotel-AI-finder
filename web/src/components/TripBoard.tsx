import type { Cart, Order, Requirements, TripState, TurnResponse } from "../api/types";
import { rub } from "../lib/format";

const MONTHS = [
  "январе", "феврале", "марте", "апреле", "мае", "июне",
  "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
];

function tripDates(req: Requirements): string {
  const dates = req.dates;
  if (!dates) return "Когда поедем?";
  if (dates.check_in && dates.check_out) {
    const format = (value: string) => {
      const [year, month, day] = value.split("-");
      return `${day}.${month}.${year.slice(2)}`;
    };
    return `${format(dates.check_in)} — ${format(dates.check_out)}`;
  }
  if (dates.month) return `В ${MONTHS[dates.month - 1]}`;
  return "Когда поедем?";
}

function guests(req: Requirements): string {
  const adults = req.guests?.adults;
  const children = req.guests?.children_ages?.length ?? 0;
  if (adults == null && children === 0) return "Кто поедет?";
  const people = [
    adults != null ? `${adults} ${adults === 1 ? "взрослый" : "взрослых"}` : "",
    children ? `${children} ${children === 1 ? "ребёнок" : "детей"}` : "",
  ].filter(Boolean);
  return people.join(" · ");
}

const STEPS = [
  ["collecting", "Запрос", "Собираю важное"],
  ["proposing", "Выбор", "Варианты готовы"],
  ["selected", "Проверка", "Проверяю дорогу"],
  ["in_cart", "Поездка", "Собрана в корзину"],
  ["ordered", "Готово", "Поездка оформлена"],
] as const;

type Props = { response?: TurnResponse; state: TripState | null; cart?: Cart; order?: Order };

export function TripBoard({ response, state, cart, order }: Props) {
  const req = state?.requirements ?? response?.requirements;
  const stage = state?.stage ?? response?.stage;
  const picked = state?.variants?.find((variant) => variant.id === state.selected_variant_id)
    ?? state?.variants?.find((variant) => variant.id === cart?.variant_id)
    ?? state?.variants?.find((variant) => variant.id === order?.variant_id)
    ?? state?.variants?.find((variant) => variant.id === state.recommended_variant_id);
  const stageIndex = Math.max(0, STEPS.findIndex(([step]) => step === stage));
  const hasCart = (stage === "in_cart" || stage === "ordered") && cart;
  const hasOrder = stage === "ordered" && order;

  return (
    <aside className="trip-board" aria-label="Ваша поездка">
      <div className="board-head">
        <h2>Путевой лист</h2>
        <span className="board-state">{response?.mode === "llm" ? "агент онлайн" : "демо-режим"}</span>
      </div>

      <ol className="route-line">
        {STEPS.map(([stage, name, description], index) => (
          <li key={stage} className={index < stageIndex ? "done" : index === stageIndex ? "current" : "future"}>
            <span className="route-dot" aria-hidden="true" />
            <div><b>{name}</b><small>{index === stageIndex ? description : index < stageIndex ? "пройдено" : "впереди"}</small></div>
          </li>
        ))}
      </ol>

      <section className="trip-facts" aria-label="Параметры поездки">
        {response?.location && (
          <div>
            <span>Куда</span>
            <strong>{response.location.abroad ? `${response.location.name}, ${response.location.country}` : response.location.name}</strong>
          </div>
        )}
        <div><span>Даты</span><strong>{req ? tripDates(req) : "Собираем запрос"}</strong></div>
        <div><span>Кто едет</span><strong>{req ? guests(req) : "—"}</strong></div>
        <div><span>Бюджет</span><strong>{req?.budget?.amount ? rub(req.budget.amount) : "Пока не задан"}</strong></div>
      </section>

      {picked ? (
        <section className="chosen-hotel">
          <span>ОСНОВНОЙ ВАРИАНТ</span>
          <h3>{picked.hotel.name}</h3>
          <p>{picked.hotel.district_name} · {rub(picked.total_price)} за всё</p>
          <div className="hotel-mark">{picked.hotel.name.slice(0, 1)}</div>
        </section>
      ) : (
        <section className="board-promise">
          <p>Здесь появится ваш маршрут.</p>
        </section>
      )}

      {(hasCart || hasOrder) && (
        <section className="board-booking" aria-label="Статус бронирования">
          {hasCart && <p><span>КОРЗИНА</span><strong>{rub(cart.total)} · {cart.items?.length ?? 0} поз.</strong></p>}
          {hasOrder && <p><span>ЗАКАЗ</span><strong>{order.number} · {order.status === "paid" ? "оплачен" : "ждёт оплаты"}</strong></p>}
        </section>
      )}
    </aside>
  );
}

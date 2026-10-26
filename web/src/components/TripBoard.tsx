import type { Requirements, TurnResponse, Variant } from "../api/types";
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
  const adults = req.guests?.adults ?? 1;
  const children = req.guests?.children_ages?.length ?? 0;
  const people = [
    `${adults} ${adults === 1 ? "взрослый" : "взрослых"}`,
    children ? `${children} ${children === 1 ? "ребёнок" : "детей"}` : "",
  ].filter(Boolean);
  return people.join(" · ");
}

function recommendation(response?: TurnResponse): Variant | undefined {
  const block = response?.blocks.find((item) => item.type === "variants");
  if (!block || block.type !== "variants") return undefined;
  return block.items.find((item) => item.id === block.recommended_id) ?? block.items[0];
}

const STEPS = [
  ["collecting", "Запрос", "Собираю важное"],
  ["proposing", "Выбор", "Варианты готовы"],
  ["selected", "Проверка", "Проверяю дорогу"],
  ["in_cart", "Поездка", "Собрана в корзину"],
  ["ordered", "Готово", "Поездка оформлена"],
] as const;

export function TripBoard({ response }: { response?: TurnResponse }) {
  const req = response?.requirements;
  const picked = recommendation(response);
  const stageIndex = Math.max(0, STEPS.findIndex(([stage]) => stage === response?.stage));

  return (
    <aside className="trip-board" aria-label="Ваша поездка">
      <div className="board-head">
        <p>ВАША ПОЕЗДКА</p>
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
          <span>СЛЕДУЮЩИЙ ШАГ</span>
          <p>Расскажите о поездке — маршрут начнёт собираться здесь.</p>
        </section>
      )}
    </aside>
  );
}

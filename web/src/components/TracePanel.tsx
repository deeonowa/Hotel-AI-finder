import type { TurnResponse } from "../api/types";

const STAGE: Record<string, { title: string; hint: string }> = {
  collecting: { title: "Собираю поездку", hint: "Расскажите, что для вас важно — подберу следующий шаг." },
  proposing: { title: "Есть варианты", hint: "Сравните варианты или уточните любое условие в сообщении." },
  selected: { title: "Отель выбран", hint: "Можно спросить про отель, изменить условия или добавить его в корзину." },
  in_cart: { title: "Поездка в корзине", hint: "Проверьте детали — можно менять условия до оформления." },
  ordered: { title: "Поездка оформлена", hint: "Я сохраню план и помогу с изменениями после оформления." },
};

const TRACE_COPY: Record<string, string> = {
  apply_patch: "Учёл изменения в поездке",
  compare_locations: "Сравнил направления",
  inspire: "Подобрал подходящие районы",
  search_rank: "Сравнил отели по вашим условиям",
  validate: "Проверил условия и логистику",
  autofix: "Проверил важные детали",
  cart: "Обновил корзину",
  checkout: "Подготовил оформление",
  pay: "Подтвердил заказ",
};

/** Пользовательский статус вместо внутренних имён шагов оркестратора. */
export function TracePanel({ resp }: { resp?: TurnResponse }) {
  if (!resp) return null;
  const stage = STAGE[resp.stage] ?? STAGE.collecting;
  const updates = [...new Set((resp.trace ?? []).map((step) => TRACE_COPY[step.name]).filter(Boolean))].slice(-3);
  return (
    <section className="panel journey-panel" aria-live="polite">
      <h3>Что с поездкой</h3>
      <div className="journey-status">
        <i aria-hidden="true" />
        <div>
          <b>{stage.title}</b>
          <p>{stage.hint}</p>
        </div>
      </div>
      {updates.length > 0 && (
        <div className="journey-checks">
          <span>Уже проверил</span>
          <ul className="journey-updates" aria-label="Что уже проверено">
            {updates.map((update) => <li key={update}>{update}</li>)}
          </ul>
        </div>
      )}
      <details className="source-note">
        <summary>О данных</summary>
        <p>
          © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap contributors</a>,
          климат — Open-Meteo. Цены, наличие, билеты и оплата — демо.
        </p>
      </details>
    </section>
  );
}

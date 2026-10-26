export const rub = (n: number) => `${n.toLocaleString("ru-RU")} ₽`;

export const MEALS: Record<string, string> = {
  RO: "без питания",
  BB: "завтраки",
  HB: "полупансион",
  FB: "полный пансион",
  AI: "всё включено",
};

export const SOURCE_LABEL: Record<string, string> = {
  user: "вы сказали",
  assumed: "я предположил",
  profile: "из прошлых поездок",
  default: "по умолчанию",
};

/** Мини-markdown: **жирный**, _курсив_, переносы. Сначала экранируем HTML — XSS невозможен. */
export function md(text: string): string {
  const esc = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\s)_(.+?)_(?=\s|$|[.,!?])/g, "$1<em>$2</em>")
    .replace(/\n/g, "<br/>");
}

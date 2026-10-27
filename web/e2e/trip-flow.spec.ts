import { expect, test } from "@playwright/test";

const requirements = { dates: { check_in: "2026-11-10", check_out: "2026-11-17" }, guests: { adults: 2, children_ages: [] }, budget: { amount: 120000 } };
const variant = { id: "v-oliva", title: "Лучший баланс", archetype: "balanced", total_price: 46060, score: .86, hotel: { name: "Отель «Олива»", district_name: "Адлер", data_source: "fixture" }, offer: { room_name: "Стандарт", meal_plan: "none", price_per_night_avg: 6580, cancellation: { refundable: true, free_until: "2026-11-01" } }, why: ["Близко к морю"], tradeoffs: [], fit_warnings: [], issues: [{ code: "arrival_unknown", severity: "info", message: "Во сколько вы прибываете?" }], breakdown: [] };
const session = { session_id: "demo", version: 1, stage: "collecting", requirements, variants: [], selected_variant_id: null };

test("выбор сохраняется в путевом листе после добавления в корзину", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const url = route.request().url();
    if (url.endsWith("/sessions") && route.request().method() === "POST") return route.fulfill({ json: { ...session, blocks: [{ type: "text", markdown: "Расскажите о поездке" }], suggestions: [], trace: [], mode: "rules" } });
    if (url.endsWith("/sessions/demo") && route.request().method() === "GET") return route.fulfill({ json: { ...session, stage: "in_cart", variants: [variant], selected_variant_id: variant.id } });
    const body = route.request().postDataJSON() as { text?: string; action?: { type: string } };
    if (body.text) return route.fulfill({ json: { ...session, version: 2, stage: "proposing", blocks: [{ type: "variants", items: [variant], recommended_id: variant.id }], suggestions: [], trace: [], mode: "rules" } });
    return route.fulfill({ json: { ...session, version: 3, stage: "in_cart", blocks: [{ type: "cart", cart: { id: "c1", session_id: "demo", variant_id: variant.id, items: [{ id: "h1", title: "Отель «Олива»", details: "7 ночей", price: 46060, removable: true }], total: 46060, issues: [], updated_at: "2026-10-26T00:00:00Z" } }], suggestions: [], trace: [], mode: "rules" } });
  });
  await page.goto("/");
  await page.getByLabel("Куда хочется?").fill("К морю в ноябре");
  await page.getByRole("button", { name: "Отправить" }).click();
  await page.getByRole("button", { name: /В корзину/ }).click();
  await expect(page.getByLabel("Ваша поездка")).toContainText("Отель «Олива»");
  await expect(page.getByLabel("Статус бронирования")).toContainText("46 060");
  await expect(page.getByText("Во сколько вы прибываете?")).not.toBeVisible();
});

test("выбранный вариант нельзя выбрать повторно, но можно добавить в корзину", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const url = route.request().url();
    if (url.endsWith("/sessions") && route.request().method() === "POST") return route.fulfill({ json: { ...session, blocks: [{ type: "text", markdown: "Расскажите о поездке" }], suggestions: [], trace: [], mode: "rules" } });
    if (url.endsWith("/sessions/demo") && route.request().method() === "GET") return route.fulfill({ json: { ...session, stage: "selected", variants: [variant], selected_variant_id: variant.id } });
    return route.fulfill({ json: { ...session, version: 2, stage: "selected", blocks: [{ type: "variants", items: [variant], recommended_id: variant.id, selected_id: variant.id }], suggestions: [], trace: [{ name: "validate", detail: "", ms: 0 }], mode: "rules" } });
  });
  await page.goto("/");
  await page.getByLabel("Куда хочется?").fill("К морю в ноябре");
  await page.getByRole("button", { name: "Отправить" }).click();
  await expect(page.getByRole("button", { name: "Выбрано" })).toBeDisabled();
  await expect(page.getByRole("button", { name: /В корзину/ })).toBeEnabled();
  await expect(page.getByLabel("Прозрачность работы агента").getByText("Проверил условия и логистику")).toBeVisible();
});

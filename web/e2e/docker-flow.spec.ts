import { expect, test } from "@playwright/test";

const realApiOnly = !process.env.E2E_REAL_API;

test.describe("полный путь через Docker API", () => {
  test.skip(realApiOnly, "запускается отдельно: npm run test:e2e:docker");

  test.beforeAll(async ({ request }) => {
    await expect.poll(
      async () => request.get("/api/v1/health").then((response) => response.status()).catch(() => 0),
      { timeout: 30_000, intervals: [250, 500, 1_000] },
    ).toBe(200);
  });

  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.evaluate(() => localStorage.clear());
    await page.reload();
  });

  test("направление → отель → корзина → оплата → восстановление после refresh", async ({ page }) => {
    await expect(page.getByLabel("Куда хочется?")).toBeEnabled();

    await page.getByLabel("Куда хочется?").fill("Вдвоём к морю с 10 по 17 ноября");
    await page.getByRole("button", { name: "Отправить" }).click();

    await expect(page.getByText("Выберите направление")).toBeVisible();
    await page.getByRole("button", { name: /Смотреть / }).first().click();

    const addToCart = page.getByRole("button", { name: "В корзину" }).first();
    await expect(addToCart).toBeEnabled();
    await addToCart.click();
    await expect(page.getByRole("heading", { name: "Корзина" })).toBeVisible();

    await expect(page.getByRole("button", { name: "Оформить", exact: true })).toHaveCount(1);
    await page.getByRole("button", { name: "Оформить", exact: true }).click();
    const pay = page.getByRole("button", { name: /Оплатить/ });
    await expect(pay).toBeVisible();
    await pay.click();
    await expect(page.getByLabel("Поездка оформлена")).toContainText("ПОЕЗДКА ОФОРМЛЕНА");

    await page.reload();
    await expect(page.getByLabel("Поездка оформлена")).toBeVisible();
    await expect(page.getByLabel("Ваша поездка")).toContainText("ЗАКАЗ");
  });

  test.describe("mobile", () => {
    test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

    test("не имеет горизонтального overflow и проходит первые шаги с клавиатуры", async ({ page }) => {
      const request = page.getByLabel("Куда хочется?");
      await expect(request).toBeEnabled();
      await expect(page.evaluate(() => window.innerWidth)).resolves.toBe(390);
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);

      await request.fill("Вдвоём к морю с 10 по 17 ноября");
      await request.focus();
      await page.keyboard.press("Tab");
      await expect(page.getByRole("button", { name: "Отправить" })).toBeFocused();
      await page.getByRole("button", { name: "Отправить" }).click();
      await expect(page.getByText("Выберите направление")).toBeVisible();
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    });
  });
});

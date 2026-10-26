// Типы берём из сгенерированной OpenAPI-схемы (src/api/schema.d.ts) — руками НЕ дублируем.
// Обновить: python scripts/export_openapi.py && npm run gen:types
import type { components } from "./schema";

type S = components["schemas"];

export type TurnResponse = S["TurnResponse"];
export type TurnRequest = S["TurnRequest"];
export type QuickReply = S["QuickReply"];
export type Action = NonNullable<TurnRequest["action"]>;
export type Variant = S["Variant"];
export type Issue = S["Issue"];
export type Cart = S["Cart"];
export type Order = S["Order"];
export type Requirements = S["Requirements"];

export type TextBlock = S["TextBlock"];
export type AssumptionsBlock = S["AssumptionsBlock"];
export type QuestionBlock = S["QuestionBlock"];
export type DestinationsBlock = S["DestinationsBlock"];
export type VariantsBlock = S["VariantsBlock"];
export type ComparisonBlock = S["ComparisonBlock"];
export type DiffBlock = S["DiffBlock"];
export type NoResultsBlock = S["NoResultsBlock"];
export type CartBlock = S["CartBlock"];
export type OrderBlock = S["OrderBlock"];
export type UIBlock = TurnResponse["blocks"][number];

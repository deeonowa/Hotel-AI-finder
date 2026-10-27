import type { QuickReply, UIBlock } from "../api/types";
import { CartView, OrderView } from "./blocks/Booking";
import {
  AssumptionsView,
  ComparisonView,
  DestinationsView,
  DiffView,
  NoResultsView,
  QuestionView,
  TextView,
} from "./blocks/Misc";
import { VariantsView } from "./blocks/Variants";

export type ReplyFn = (q: QuickReply) => void;

/** Единая точка: тип блока → компонент. Новый тип блока в схеме → TS подсветит здесь пропуск. */
export function BlockView({ block, onReply, disabled, compact = false }: {
  block: UIBlock;
  onReply: ReplyFn;
  disabled: boolean;
  compact?: boolean;
}) {
  switch (block.type) {
    case "text":
      return <TextView b={block} />;
    case "question":
      return <QuestionView b={block} onReply={onReply} disabled={disabled} />;
    case "assumptions":
      return <AssumptionsView b={block} onReply={onReply} disabled={disabled} />;
    case "destinations":
      return <DestinationsView b={block} onReply={onReply} disabled={disabled} compact={compact} />;
    case "variants":
      return <VariantsView b={block} onReply={onReply} disabled={disabled} compact={compact} />;
    case "comparison":
      return <ComparisonView b={block} />;
    case "diff":
      return <DiffView b={block} />;
    case "no_results":
      return <NoResultsView b={block} onReply={onReply} disabled={disabled} />;
    case "cart":
      return <CartView b={block} onReply={onReply} disabled={disabled} />;
    case "order":
      return <OrderView b={block} onReply={onReply} disabled={disabled} />;
    default: {
      const never: never = block;
      return <pre>{JSON.stringify(never)}</pre>;
    }
  }
}

# Eval report (rules)

| метрика | значение |
|---|---|
| mode | rules |
| scenarios | 25 |
| completion_rate | 1.0 |
| avg_clarifications | 0.12 |
| avg_turns_to_first_offer | 1.11 |
| avg_ms_to_first_offer | 21 |
| constraint_violations | 0 |
| logistics_errors | 0 |
| crashes | 0 |

| сценарий | цель | ок | вопросов | ход до вариантов | замечания |
|---|---|---|---|---|---|
| family_sea_nov | order_paid | ✅ | 1 | 2 |  |
| family_replan_later | diff | ✅ | 0 | 1 |  |
| couple_romantic_ny | variants | ✅ | 0 | 1 |  |
| pets_december | destinations | ✅ | 0 | — |  |
| pets_hard_filter | variants | ✅ | 0 | 1 |  |
| add_dog_replan | diff_or_no_results | ✅ | 0 | 1 |  |
| solo_weekend_cheap | variants | ✅ | 0 | 1 |  |
| vague_no_dates | variants | ✅ | 1 | 2 |  |
| impossible_budget | no_results | ✅ | 0 | — |  |
| budget_per_night | variants | ✅ | 0 | 1 |  |
| cheaper_replan | diff_or_no_results | ✅ | 0 | 1 |  |
| all_inclusive_must | variants_or_no_results | ✅ | 0 | 1 |  |
| breakfast_cancel | variants_or_no_results | ✅ | 0 | 1 |  |
| ski_february | variants | ✅ | 0 | 1 |  |
| wellness_sanatorium | variants_or_no_results | ✅ | 0 | 1 |  |
| train_arrival | selected | ✅ | 0 | 1 |  |
| compare_select | in_cart | ✅ | 0 | 1 |  |
| ask_info | selected | ✅ | 0 | 1 |  |
| inject_price | no_crash | ✅ | 0 | — |  |
| inject_system | no_crash | ✅ | 0 | — |  |
| offtopic | no_crash | ✅ | 0 | — |  |
| typos | any_progress | ✅ | 1 | — |  |
| night_arrival | in_cart | ✅ | 0 | 1 |  |
| accessibility | variants_or_no_results | ✅ | 0 | 1 |  |
| reset_flow | variants | ✅ | 0 | 1 |  |

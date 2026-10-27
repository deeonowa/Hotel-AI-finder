# Eval report (rules)

| метрика | значение |
|---|---|
| mode | rules |
| scenarios | 54 |
| completion_rate | 1.0 |
| avg_clarifications | 0.06 |
| avg_turns_to_first_offer | 1.07 |
| avg_ms_to_first_offer | 250 |
| constraint_violations | 0 |
| logistics_errors | 0 |
| crashes | 0 |
| interpretation_errors | 0 |

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
| wellness_sanatorium | variants | ✅ | 0 | 1 |  |
| train_arrival | selected | ✅ | 0 | 1 |  |
| compare_select | selected | ✅ | 0 | 1 |  |
| ask_info | selected | ✅ | 0 | 1 |  |
| inject_price | no_crash | ✅ | 0 | — |  |
| inject_system | no_crash | ✅ | 0 | — |  |
| offtopic | no_crash | ✅ | 0 | — |  |
| typos | any_progress | ✅ | 1 | — |  |
| night_arrival | in_cart | ✅ | 0 | 1 |  |
| accessibility | variants_or_no_results | ✅ | 0 | 1 |  |
| reset_flow | variants | ✅ | 0 | 1 |  |
| confirm_gate | order_paid | ✅ | 0 | 1 |  |
| pinned_shift_cart | in_cart_kept | ✅ | 0 | 1 |  |
| pinned_dog_replaced | diff_or_no_results | ✅ | 0 | 1 |  |
| autofix_night_lazarevskoe | autofix | ✅ | 0 | 1 |  |
| autofix_hilly_family | variants | ✅ | 0 | 1 |  |
| autofix_keep | in_cart | ✅ | 0 | 1 |  |
| u_friend_october | variants | ✅ | 0 | 1 |  |
| u_big_family | variants_or_no_results | ✅ | 0 | 1 |  |
| u_ny_family_budget | variants_or_no_results | ✅ | 0 | 1 |  |
| u_english | variants | ✅ | 0 | 1 |  |
| u_pensioners | variants_or_no_results | ✅ | 0 | 1 |  |
| u_numeric_dates | variants_or_no_results | ✅ | 0 | 1 |  |
| u_landing_late | in_cart | ✅ | 0 | 1 |  |
| u_per_night_march | variants_or_no_results | ✅ | 0 | 1 |  |
| u_shift_forward | diff | ✅ | 0 | 1 |  |
| u_extend | diff | ✅ | 0 | 1 |  |
| u_injection_discount | no_crash | ✅ | 0 | — |  |
| tickets_full_trip | order_paid | ✅ | 0 | 1 |  |
| tickets_direct_only | diff_or_no_results | ✅ | 0 | 1 |  |
| geo_dubai_beach | variants | ✅ | 0 | 1 |  |
| geo_antalya_ai_family | variants_or_no_results | ✅ | 0 | 1 |  |
| geo_tbilisi_old_town | variants | ✅ | 0 | 1 |  |
| geo_phuket_district | variants | ✅ | 0 | 1 |  |
| geo_switch_city | diff | ✅ | 0 | 1 |  |
| geo_where_warm | destinations | ✅ | 0 | — |  |
| geo_where_warm_then_pick | order_paid | ✅ | 0 | 2 |  |
| geo_cold_sea_hint | variants | ✅ | 0 | 1 |  |
| geo_unknown_city | destinations | ✅ | 0 | — |  |
| geo_tickets_phuket | variants_or_no_results | ✅ | 0 | — |  |

# Official-rule conflicts and unresolved details

Checked 2026-08-14. The simulator uses the stricter interpretation whenever a precise official rule cannot be established.

| Topic | Official statements | Resolution used by Lab |
|---|---|---|
| News trading | `Other Activities` allows news on Flex, Pro and Direct but says it is not allowed on Daily. The Daily funded page calls the ±1 minute USD high-impact window a hard breach. Black is not named. | Pro/Flex may trade news under rules, but the selected strategy filters high-impact windows for execution risk. Daily blocks the stated window. Black is marked ambiguous and conservatively blocks it. |
| Trading cutoff | `Allowed Trading Times` explicitly names Pro, Flex, Direct and Live, not Black/Daily/Maxx. | All strategies flatten by 16:00 ET, well before the 16:45 verified cutoff. Black/Daily/Maxx cutoff metadata remains `ambiguous`; the simulator never assumes later access. |
| Consistency cushion | Flex/Daily articles say the cushion varies with actual profit and is not the displayed fixed dollar amount, but do not publish the exact calculation. | Ignore the favorable cushion. Require the exact largest-positive-day / net-profit ratio to be at or below the headline percentage. |
| Drawdown lock boundary | Drawdown pages say “once the account exceeds” the trail balance and also describe a lock after the account reaches the trail. | Strict version: lock only after an EOD close is one cent above the trigger. Exact trigger remains on the trailing formula. |
| MLL breach wording | Pages use both “reached” and “exceeded.” | Strict version: equality breaches. Tests cover one cent above, equal and one cent below. |
| Pro evaluation consistency | Pro evaluation table has no consistency column, while Pro funded has 40% payout consistency. | No consistency rule is applied to Pro evaluation. Funded consistency is not imported into evaluation. |
| Daily DLL | Daily can be purchased DLL on or off. | Explicit selector. The initial default is DLL on for conservatism; evidence must identify the variant it used. |
| Daily drawdown | Daily evaluation can be EOD or intraday; funded is always intraday. | Explicit selector. The selected historical artifact uses EOD only and cannot be reused as evidence for intraday. |
| Same-account mini/micro opposite positions | Hedging article unusually says same-account mini/micro opposing trades “can” be used while broader integrity rules prohibit hedging. | Strategy never holds opposing correlated positions. No simulator exemption is implemented. |
| Exchange/platform/data fees | Official product page labels numbers as commission per side but does not itemize whether every platform/data/exchange fee is included. | Charge the full official commission and separately disclose that any unlisted platform/data fee is outside trade P&L. No zero-fee claim. |
| Early closes/ad-hoc closures | Cutoff article requires flattening before a holiday market close but does not publish a calendar. | Research excludes incomplete 390-minute RTH sessions. Runtime operating plan requires checking the exchange calendar; no trade on missing/incomplete sessions. |

The official pages are mutable. Every displayed rule includes retrieval date and link; re-check before buying an account.

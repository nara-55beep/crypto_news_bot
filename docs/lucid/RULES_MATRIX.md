# Rules matrix

Rules last checked: **2026-08-14**. Dollar values below are evaluation-stage values. The centralized executable copy lives in `lucid_lab/rules.py`.

| Program | Public evaluation | Sizes | Target / MLL (25/50/100/150K) | Eval consistency | DLL | Drawdown | Maximum size |
|---|---|---|---|---|---|---|---|
| LucidPro | Yes | 25/50/100/150K | 1250/1000; 3000/2000; 6000/3000; 9000/4500 | None | none; 1200; 1800; 2700 | EOD trailing, locks +100 after exceeding start+MLL+100 | 2/20; 4/40; 6/60; 10/100 mini/micro |
| LucidFlex | Yes | 25/50/100/150K | same | 50%, variable cushion | None | EOD trailing/lock | same |
| LucidBlack | Yes | 25/50/100K | same first three | 60% | None | EOD trailing/lock | same first three |
| LucidDaily | Yes | 25/50/100/150K | same | 50%, variable cushion | optional: none or Pro values | selectable EOD/intraday in evaluation | same |
| LucidMaxx | Invite only | 25/50/100/150K | same | 40% | None | EOD | same; 5 trading days |
| LucidDirect | No evaluation | 25/50/100/150K | n/a | n/a | funded rules only | funded rules only | funded rules only |

## Rules shared by the Lab's evaluation model

- The MLL is breached when balance/equity **reaches** the active floor, not only after falling below it.
- An EOD floor advances only from a higher qualifying session close and never retreats.
- Once the qualifying close **exceeds** the initial trail balance, the floor locks at starting balance + $100.
- A DLL is a soft stop: reaching it blocks new trades until the next session but is not itself account failure unless the MLL is also reached.
- Evaluation contract limits are an aggregate mini-or-micro cap. One mini is treated as ten micros for cap accounting.
- All modeled positions are forced flat by 16:00 New York—45 minutes earlier than the verified 16:45 cutoff—to avoid relying on the boundary or holiday auto-liquidation.
- The official Pro/Flex session can span the 18:00 reopen through the next session cutoff. That availability is kept separate from the selected strategy's stricter flat-intraday policy; the page no longer mislabels the strategy choice as an official overnight prohibition. Black/Daily remain conservative where the general timing article does not name them.
- MES/MNQ/MCL commission is $0.50 per side ($1.00 round turn) from the official product table.
- News trading is allowed for Pro and Flex. The selected strategy still avoids scheduled high-impact windows because execution cost is unstable, not because Pro forbids it.
- Automated systems are permitted if they obey all other rules. HFT, simulated-fill exploitation, hedging and microscalping are not.

## Current model selection

The page offers only program/stage/size combinations represented in the verified configuration. It excludes Maxx from ordinary selection because it is invite-only, and Direct because it has no evaluation. Funded rules are visible for research but the pass-probability artifact is specifically an evaluation study.

The default candidate is 25K LucidPro. Its target-to-drawdown ratio (1.25) is mechanically better than 50K (1.50), 100K (2.00) and 150K (2.00). This does not by itself prove higher profitability; it only makes the evaluation objective less demanding per dollar of loss buffer. The historical account-size comparison is displayed as post-hoc and does not upgrade the strategy to validated.

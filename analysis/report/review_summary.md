# Review summary

> **Draft.** Numbers are computed from `analysis/state/` as of commit `e272c1c`. Review the wording and the assessments before submitting.

I reviewed Cartwheel support sessions from Module 1, wrote a free-form note for the first failure in each, grouped the notes into binary failure modes, and applied every final mode to every reviewed session. The review interface is `analysis/review_app/`; the state files are in `analysis/state/`.

## Sample

I reviewed **114 distinct sessions**. A session is the unit of review, because Cartwheel writes one Langfuse trace per user turn and a follow-up turn cannot be judged without the earlier ones.

| Batch | Selection | Sessions |
|---|---|---|
| `b1_random` | uniform random sample | 15 |
| `b1_cluster` | one representative per cluster on the Map tab | 15 |
| `b2_intent` | spread across the intent dimension, chosen before looking at outcomes | 30 |
| `b3_depth` | depth searches for candidate modes and close negatives | 25 |
| `d_search` | Part D searches for additional instances of confirmed modes | 14 |
| `b4_random` | uniform random sample after the taxonomy draft | 15 |
| **Total** | | **114** |

No session counts toward more than one batch. Composition: 61 shopper, 30 support, 23 merchant sessions; 80 one-turn, 20 two-turn, 12 three-turn and 2 four-turn sessions.

## Method

- **Open coding.** I read each session top to bottom, including narration, tool calls and tool results, until the first failure, wrote a note in my own words beside the evidence, and stopped. A session without a failure got "No failure observed". 56 sessions received a failure note.
- **Axial coding.** After each batch I grouped the notes under shared binary decision rules. I used the likely product change to decide merges and splits. Edits keep the original note in the history.
- **Structured labeling.** Once the taxonomy was stable, every session received one Pass/Fail judgment per final mode. Judgments are stored in `analysis/state/labels/<mode>.jsonl` (append-only; a changed judgment marks the old row `superseded_by`) and as Langfuse scores. A sync dry run shows 114 judgments per mode in both stores, nothing to write or delete.

## Final taxonomy

| Mode | Binary definition (short) | Requirement | Evaluator | Fail | Sample fraction |
|---|---|---|---|---|---|
| `write_without_confirmation` | A write tool is called without first stating the action and getting confirmation. Merchant and support callers who explicitly name the action and the order confirm that write only. | RESP-6 | code | 34 | 29.8% |
| `wrong_world_date` | Date reasoning uses a "today" other than 2026-07-01, or contradicts `refund_eligible`, so a deadline or eligibility claim is wrong. | RESP-7 | judge | 10 | 8.8% |
| `ungrounded_claim` | Facts or instructions about Cartwheel's interface or features that no tool result or policy supports. | RESP-1, RESP-3 | judge | 6 | 5.3% |
| `unverified_order_match` | The agent treats a found order as the one the user meant, without confirmation, when the description fits several orders or does not fit the chosen one. | RESP-8 | judge | 4 | 3.5% |
| `asks_instead_of_looking_up` | The agent asks for an order number or store id its tools could find, without offering the lookup. | RESP-9 | judge | 4 | 3.5% |
| `false_not_available_claim` | The reply says no matching product exists although matching products exist, because the search was too narrow. | TOOL-3 contract | code + judge | 3 | 2.6% |

51 of 114 sessions (44.7%) have at least one Fail. The full definitions, boundaries, positives, close negatives and originating annotations are in `analysis/state/patterns.json`.

These are **sample fractions, not prevalence estimates**. Clustering, the intent dimension and the depth searches deliberately changed the composition of the sample. Homework 5 will estimate prevalence on the full Module 1 trace store.

## Saturation

**0 of the final 15 random sessions (`b4_random`) produced a previously unseen consequential mode.** 4 of the 15 had a failure, and each fits an existing mode (`3ef73ebc` false_not_available_claim, `ae106891` write_without_confirmation, `a3947927` write_without_confirmation and ungrounded_claim, `d2344a18` ungrounded_claim). The last new candidate mode, `inappropriate_tone`, first appeared in `b3_depth`.

A final batch of 15 only detects failures that are reasonably common. A failure present in 5% of sessions has about a 46% chance of not appearing at all in 15 random sessions.

## Taxonomy revision: role exception for write_without_confirmation

The boundary of `write_without_confirmation` originally made every unconfirmed write a Fail, including an exact request such as "refund order 123". While labeling, I reached `24134ec7`, where a support agent writes "Process rfd for #5155, Harbor Knits wool socks…". For staff callers, asking for confirmation of an explicit instruction is not the behavior I want. Exempting merchant and support callers entirely would have cleared sessions where nobody asked for a write (account changes, password resets, hedged mentions), so I narrowed the exception:

- An explicit request that names both the write action and the order confirms **that write only**.
- A different write than the one requested (asked for a refund, got a ticket) stays Fail.
- An additional write nobody asked for (a ticket next to a requested refund) stays Fail. `24134ec7` itself therefore stays Fail.
- A dispute request naming the order counts as an explicit request for the ticket (ESC-3).

Four sessions changed from Fail to Pass and became close negatives: `65dc9af4`, `1b62be49` (explicit cancellations), `4187829e`, `5c02c48c` (explicit dispute requests). The mode went from 38 to 34 Fails. The revision is recorded in the mode's history and in the `revisions` list of `patterns.json`.

## Rejected search suggestion

In Part D the coding agent searched for more instances of `unverified_order_match`. Of the returned suggestions I rejected 8, among them **`df108d23`**: the shopper has 19 orders, but names the store "Bright Socket Electronics", and only one order (#5571) is from that store. The agent verified it with `get_store_info` and `get_order`. The boundary excludes it: "Pass: only one order fits the user's description." It is now a close negative. In total I rejected 14 suggestions and accepted 8 (`analysis/state/suggestions.json`).

## SPEC.md revisions

I added **RESP-6** to `SPEC.md`: before calling a write tool (TOOL-7, TOOL-8, TOOL-9), state the action and its effect and wait for confirmation; for merchant and support callers, an explicit request naming the action and the order confirms that write only. ESC-1 to ESC-4 decide whether to escalate; RESP-6 decides when the write happens.

- Motivating annotation for RESP-6: `ann-mvsclh15-irkp` on `a00fd7a5` ("…the agent executes a write action without…"), the first note behind `write_without_confirmation`.
- The role exception came from the labeling decision on `24134ec7`, described above.

I then applied the remaining proposals, so every confirmed mode except `ungrounded_claim` and `false_not_available_claim` (already covered by RESP-1, RESP-3 and the TOOL-3 contract) now cites a dedicated requirement:

| Change | Mode | Motivating annotation |
|---|---|---|
| **RESP-7**: reason about dates from the platform date (`WORLD_ASOF` in `seed/generate.py`, 2026-07-01); `refund_eligible` is authoritative | `wrong_world_date` | `ann-mvseglxu-8xhk` on `48713c37` ("it guessed today.") |
| **RESP-8**: without an order number and with several matching orders, list the candidates and let the user choose | `unverified_order_match` | `ann-mw4qdj6t-javk` on `f33c4276` (assumed the most recent Juniper order although there are 3) |
| **RESP-9**: do not ask for what the tools can look up; look it up or offer the lookup | `asks_instead_of_looking_up` | `ann-mvscyoar-6n6n` on `fb159de8` ("it could have looked anyway and presented a list…") |
| **ESC-2** rewritten: account changes go to a human, except payment-card and password changes, which are pointed to account settings citing `cw-account-security` | (conflict, no mode) | `ann-mvyab8qj-kohi` on `9c10732e` ("the SPEC conflict…"), with `ann-mvya6jde-p2ul` on `ade20d50` |

The old ESC-2 ("account changes always go to a human") contradicted SCOPE-2 and `cw-account-security`, which send payment and password changes to self-service. `requirement_source` in `patterns.json` now names the new IDs, and the change is the last entry in its `revisions` list.

RESP-7 states a requirement the current agent probably cannot meet: nothing in the traces shows that the agent is told the platform date. Changing the agent is later work (Homework 8); this homework only records the requirement.

## Failures not covered by a final mode

7 sessions with a failure note have no Fail on any final mode. Their notes stay in `annotations.json`.

| Sessions | Reason |
|---|---|
| `1d1b66dc` | parked mode `refund_options_mismatch_policy` (1 positive) |
| `4f4c0a57`, `c6e1f571` | parked mode `inappropriate_tone` (2 positives) |
| `041682ac`, `f9caad49` | judged as tool gaps, not agent behavior (`invalid_catalog_records_returned`, `search_products_needs_title_token`) |
| `65dc9af4`, `5c02c48c` | Pass after the role revision above |

Two further parked modes (`redundant_escalation`, `unverified_product_identity`) have no session in this list. Five tool gaps are recorded under `tool_gaps` in `patterns.json`.

## How the judgments were produced

Of the 684 judgments, 34 were set directly by hand and 650 were accepted from agent suggestions in the Labels tab. A suggestion was never a label until accepted.

| Suggestion basis | Fail | Pass |
|---|---|---|
| set by hand | 6 | 28 |
| copied from my own taxonomy positives and close negatives | 49 | 34 |
| code check (write tool without confirmation) | 6 | 70 |
| agent reading (sessions from `b3_depth`, `b4_random`, `d_search`) | 0 | 228 |
| keyword and tool scan, not read in full | 0 | 263 |

Every Fail comes from my own reading or from the deterministic code check. Before accepting, I re-checked the sessions the triage flagged (agent-only Fails, uncovered notes, boundary cases). The keyword scan cannot detect `ungrounded_claim`, so I read 10 of its 44 unread Pass suggestions, drawn at random with a fixed seed: 0 Fails.

## Comparison with AgentDebug

I compared the taxonomy with the AgentDebug AgentErrorTaxonomy (arXiv:2509.25370). Its benchmarks have no human user, so it has no user-interaction dimension; `write_without_confirmation`, `unverified_order_match` and `inappropriate_tone` are domain-specific additions. The comparison exposed one unclear name: `search_query_misuse` named a cause, while the definition describes the user-visible outcome, so I renamed it `false_not_available_claim`. No mode was added.

## Part C

I did not complete the optional Raindrop Workshop part, so there is no `workshop_notes.md`.

## Preparing for Homework 5

Homework 5 needs at least 30 Pass and 30 Fail labels per mode. Only `write_without_confirmation` reaches that (34 Fail, 80 Pass). The others need synthetic scenarios: `wrong_world_date` 20 more Fails, `ungrounded_claim` 24, `unverified_order_match` 26, `asks_instead_of_looking_up` 26, `false_not_available_claim` 27.

A first comparison of a regex code check for `write_without_confirmation` with my labels (39 sessions labeled at the time) agreed on 38. The miss was a hedged support request ("…dispute the charge now I think?") that a keyword match reads as explicit. This was not a held-out test.

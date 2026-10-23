# Coverage and limits

| Scenario | Requirements | Observable behavior covered |
|---|---|---|
| H01 | R2 | Invalid `Europe/Istambul` with scheduler enabled serves `/` and `/health`. |
| H02 | R3 | Topic pill count equals filtered `story-SUMMARY_ID` count; irrelevant cloned summary contributes no story or phantom tag. |
| H03 | R4 | Two digest runs cost 0.104 and 0.004; report says product spend `$0.1080`, eval spend remains separate, and `--bulletin` narrows detail only. |
| H04 | R5 | Persisted running collect and digest each refuse both prune modes without deleting fixture data. |
| H05 | R5 | Real prune deletes an old unsummarized article and preserves summaries, bulletins, items, verdicts, and eval rows. |
| H06 | R7 | Rank-stability is absent and invocation fails; a legacy eval row remains; missing production agreement appears as `agreement n/a` in report and English run detail. |
| H07 | R8 | `ok` and unknown reason receive HTTP 422; all four reason slugs succeed; repeated bare `wrong` preserves annotation; old `ok` stays stored without an active positive control. |
| H08 | R9 | New Markdown report and run detail contain the actionable keep-list and omit named retired diagnostics, precision, and TNR. |
| H09 | R11 | `ms` and `mr` select `gpt-5.6-terra`, identified by full model title and `aria-current="true"`. |
| H10 | R11 | Confirmation selection is visible and GET leaves run count unchanged. |

## Boundaries in the contract

- R5 transaction interleaving has no stable public pause/inspection surface. H04 and H05 verify externally observable refusal, dry-run preservation, real deletion, and history preservation; neither claims to prove internal SQLite serialization under a race.
- R11 outbound request parameters have no no-provider-call public inspection surface. H09/H10 cover visible selection; they do not assert transport fields. Built-in default selection is covered by the focused configuration checks described in the contract, because the holdout deliberately sets model environment values to isolate operator configuration.
- R8 active UI assertion checks that an old `ok` story has no form control offering `verdict=ok`. It does not infer product calibration claims from stored historical rows.
- R9 report/detail assertions use the exact keep-list and forbidden labels supplied by the contract. Existing dated records remain outside the generated report fixture and are not rewritten.

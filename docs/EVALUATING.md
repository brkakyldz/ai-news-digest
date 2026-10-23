# Evaluating this digest

`ainews eval report` reads recorded data and appends an immutable dated section
to `docs/evals.md`. It calls no model. The run-detail page shows the free checks
stored when its bulletin was published. Old records remain evidence of the code
and prompts used then; they are never rewritten to resemble today's output.

## Commands and costs

| Command | Purpose | Spending |
|---|---|---|
| `ainews eval record --bulletin latest` | Record a bulletin as an offline fixture | None |
| `ainews eval report --since 30d` | Append actionable checks and recorded spend | None |
| `ainews eval report --bulletin ID --no-write` | Inspect one bulletin without appending | None |
| `ainews eval judge --bulletin latest` | Sample factual grounding against article text | Estimates first; bounded by `--max-cost` |
| `ainews eval judge --labelled` | Inspect known factual errors against the judge | Estimates first; bounded by `--max-cost` |
| `ainews eval corpus --size 40` | Freeze article bodies into ignored local data | None |
| `ainews eval compare --prompt-b candidate.md` | Compare prompts on identical frozen bodies | Estimates first; bounded by `--max-cost` |

The standalone paid rank-stability command is removed. Production still makes
three shuffled rank calls and stores their agreement on each bulletin. The
application never starts a paid evaluation; the CLI is its only entry point.

## Signals and what to do with them

| Signal | Meaning | Action or reason to monitor |
|---|---|---|
| Key fact named | Share of summaries naming a concrete figure, version, name or date | Inspect a low value on a day of releases; an opinion-heavy day can reasonably score low |
| Key fact kept in the writing | Share of named facts retained in headline or summary | Inspect omissions; tighten the summary prompt if useful facts repeatedly disappear |
| Numeral recall | Share of articles with figures whose summary retains an article figure | Check whether compression is discarding the substance of news |
| Ungrounded numerals | Figures in the output not supported by the article text shown to the summarizer | Open every flag; fix factual errors or an over-permissive prompt |
| Word budgets | Headline, summary and consequence exceeding their prompt limits | Inspect overruns and adjust the prompt when they repeat |
| Preferred-tag share | Tags within the preferred vocabulary | Inspect drift; extend vocabulary for a useful new topic or tighten inconsistent naming |
| Tag singleton share | Distinct tags used by just one story | Inspect whether topic filters group useful stories; a high share alone does not prove poor quality |
| Production agreement | Consistency of shuffled production rankings | Inspect the selected order when low; missing agreement needs investigation |
| Categorized wrong feedback | Reader-reported factual, relevance, dedupe or placement problems | Fix the corresponding component using concrete examples and notes |
| Recorded product/eval spend | Money attributed to recorded operations in the reporting window | Investigate unexpected increases and verify model choices or retries |

Word-budget checks use the limits in the shipped prompts: headline 10 words,
summary 55, and consequence 20. A compliant length is not evidence that a summary
is informative. The content-floor checks provide a separate, imperfect signal.
Summary sentence histograms, raw importance distributions, fallback overlap,
unrepresented-five counts, tier shape and editorial-note shape are absent from
the default quality output. They do not establish a better bulletin by themselves.

### Grounding and translation

Numeral checks compare numeric values, including scales: a source figure of
12.9 billion does not support rounding it to 13 billion. They inspect only the
article portion shown to the model, not unseen body text or third-party search
context. A flag is an invitation to inspect the story, not a final verdict.

New digests are canonical English with complete stored Turkish views. Free
quality checks and the grounding judge read canonical English against the
article. They do not measure the accuracy of Turkish translation. A language
switch changes neither the story IDs nor the stored quality measurement.

### Production agreement

Each production ranking makes three shuffled calls and aggregates their picks
by Borda count. Agreement is the lower of mean Kendall tau and chance-corrected
Jaccard. The two measure different failures: inconsistent ordering and different
story membership. Chance correction removes the overlap expected just from the
pool and selection sizes. The existing diagnostic floor is 0.6; it is not a
human quality score and it does not block publication.

A missing value means there were too few valid readings to establish agreement.
The fallback can still publish, but there is no measured editorial consensus.
The report and run detail show that absence explicitly instead of substituting
zero or a successful score.

### Wrong-only reader feedback

A story offers a wrong action, a reason and an optional note. There is no generic
Right action. Existing old positive rows stay in storage; active counts and
comparisons ignore them. No unreported story becomes a positive example.

| Reason | Component to inspect |
|---|---|
| `wrong_fact` | Factual grounding of the summary |
| `not_news` | Summarizer relevance classification |
| `duplicate` | Dedupe and the selected representative |
| `wrong_place` | Editorial membership, order and tier |

Only `wrong_fact` can be compared with the grounding judge. A known-error catch
count describes those examples; it cannot establish precision, specificity or
trustworthy calibration. Relevance, duplicate and placement reports must never
count against a judge that only checks facts. Repeatedly marking a story wrong
without a new note or reason preserves the existing annotation.

Judge results retain their prompt hash. A changed prompt is a new measurement
condition; do not combine old and new results into a claim of prompt improvement.
A sample whose article provenance is unknown is identified separately because
its apparent grounding may come from unattributed context.

### Spend windows and bulletin versions

Product totals sum recorded digest Run costs once per run in the `--since`
window, including partial or failed runs where paid work was recorded. They do
not add the same summary cost for each bulletin version. `--bulletin` narrows
which detail section is printed; it does not narrow the spend window. Eval
spending is summed separately from recorded evaluation rows.

A new bilingual run records summary, ranking and newly paid translation work.
Cached story translations and language-switch reads add no new cost. Reported
cost is a token-price estimate; provider caching can make the billed amount lower.
Historical records cannot reconstruct usage they never stored. Manual repairs
without a digest Run are outside this recorded digest-run total.

## Compare prompts on the same bodies

Freeze a local corpus once, then compare the shipped English summarization prompt
with a candidate on identical bodies and the same model:

```bash
ainews eval corpus --size 40
cp src/ainews/pipeline/prompts/en/summarize.md /tmp/candidate.md
# Edit the candidate, then:
ainews eval compare --prompt-b /tmp/candidate.md
```

The command estimates before spending and names no winner. Check factual and
content changes alongside word-budget changes. The explicit comparison can show
additional descriptive diagnostics; those are not the default report's quality
claims. Corpus bodies remain in ignored local data, not in the public repository.

## Add a useful check

Add a pure function to `src/ainews/quality/checks.py`, cover a concrete failure
with a small offline test, then include it in `quality/stories.py` only if its
result can trigger an action or explain a product-facing risk. Update the report,
run-detail projection and this manual together. Keep paid evaluation code out of
the pipeline and web imports. Old checks_json and dated report sections remain
unchanged; readers ignore retired keys.

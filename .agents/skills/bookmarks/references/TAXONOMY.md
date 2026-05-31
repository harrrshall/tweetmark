# TAXONOMY.md: Johnny.Decimal areas, tags, and how to extend them

The KB is organized by **Johnny.Decimal**: at most 10 areas (`00-09`, `10-19`,
… `90-99`), at most 10 categories per area, item ids `CC.NN` (two-digit category
`CC`, zero-padded counter `NN`). This file is the source of truth for the area
labels the enricher chooses from. The code lives in `scripts/lib/jdid.py`
(`DEFAULT_TAXONOMY`); this doc and that list must agree.

## The 10 default areas

| Area    | Folder              | Use it for                                                    |
|---------|---------------------|---------------------------------------------------------------|
| `00-09` | `00-09_inbox`       | Unsorted / read-later / meta. The fallback when nothing fits. |
| `10-19` | `10-19_ai-tech`     | Models, training, agents, RAG, inference, coding.             |
| `20-29` | `20-29_tools`       | Apps, datasets, templates, extensions, APIs/CLIs.             |
| `30-39` | `30-39_business`    | Startups, strategy, GTM, fundraising, hiring.                 |
| `40-49` | `40-49_money`       | Investing, personal finance, crypto, markets.                 |
| `50-59` | `50-59_productivity`| Workflow, habits, focus, note-taking / PKM.                   |
| `60-69` | `60-69_health`      | Fitness, nutrition, sleep, mental health.                     |
| `70-79` | `70-79_design`      | UI/UX, branding, typography, inspiration.                     |
| `80-89` | `80-89_learning`    | Tutorials, explainers, courses, references, career.           |
| `90-99` | `90-99_archive`     | Decayed/done items (keeps the active set small).              |

The enricher returns a **category label** of the form `CC_slug` (e.g.
`11_llm-training`). `build_kb.py` calls `jdid.code_for_label(label)` to get `CC`
and `jdid.assign(label, existing_ids)` to get the final `CC.NN` id + folder path.
An empty or unrecognized label routes to area `00` (Inbox).

## Example category subtags (the `CC_slug` labels)

These are the labels the deterministic mock enricher already emits
(`scripts/enrich.py` `_CATEGORY_RULES`). Treat them as the canonical category
set; agent/API enrichment should reuse them rather than invent near-duplicates.

- **10-19 AI & Tech**: `11_llm-training`, `12_llm-models`, `13_agents`,
  `14_rag`, `15_audio-ml`, `16_diffusion`, `17_inference`, `18_coding`
- **20-29 Tools**: `21_tools`, `22_datasets`, `23_apis-cli`
- **30-39 Business**: `31_strategy`, `32_gtm`, `33_fundraising`, `34_hiring`
- **40-49 Money**: `41_investing`, `42_crypto`, `43_personal-finance`
- **50-59 Productivity**: `51_productivity`, `52_note-taking`, `53_habits`
- **60-69 Health**: `61_fitness`, `62_nutrition`, `63_health`
- **70-79 Design**: `71_design`, `72_typography`, `73_branding`
- **80-89 Learning**: `81_tutorial`, `82_explainer`, `83_reference`
- **00-09 / 90-99**: `00_inbox` (catch-all), `90_archive` (managed by
  `doctor.py --decay`, never assigned at enrich time)

## When to use which area

- Pick the **primary home**: one category, the single best fit. Cross-cutting
  topics get tags, not a second folder.
- A model/training/agent/RAG/inference/coding tweet is `10-19`. A *thing you can
  install or download* (app, dataset, extension, API/CLI) is `20-29`, even if
  it's AI-related; the distinction is "idea/technique" (10s) vs "artifact you
  use" (20s).
- Money advice and markets are `40-49`; *building a company* is `30-39`.
- "How to X" / "explained" / "primer" content is `80-89` even when the subject
  is AI; the genre wins when the value is the teaching, not the claim.
- When two areas tie, prefer the more specific category; when nothing is a clear
  fit, use `00_inbox` and let a later pass (or the user) re-home it. Never force
  a bad category just to avoid the Inbox.

## Tags vs categories

- **Category** = the one folder (where it lives). **Tags** = 2–5 cross-cutting
  facets (how you'll search for it). A note has exactly one category and several
  tags.
- Tags come from the controlled vocabulary in `<KB>/tags.txt` (seeded from
  `assets/tags.seed.txt`). See `ENRICHMENT.md` for the tag-selection rules.

## How to add a category (or change the taxonomy)

The taxonomy is configurable but **frozen by code**, so changing it means
editing one list, not scattered strings:

1. **Add a category to an existing area.** Pick the next free `CC` slug in the
   decade (e.g. a new AI category becomes `19_xyz`, since `11`–`18` are taken).
   Enrichment just needs to start emitting the new `CC_slug` label; `jdid`
   already maps any `CC` to the right area folder, so no code change is required
   to *store* it. To make the **mock** enricher route to it, add a rule to
   `_CATEGORY_RULES` in `scripts/enrich.py` (keyword list + preferred tags),
   ordered most-specific-first.
2. **Add or rename an area.** Edit `DEFAULT_TAXONOMY` in `scripts/lib/jdid.py`
   (the `(decade_lo, decade_hi, slug, desc)` tuple) and the matching
   `_AREA_ALIASES` keyword hints. Keep the 10-area cap and keep this doc's table
   in sync. Existing notes are **not** moved automatically; re-home them with the
   UI/editor or by re-running `enrich` + `build_kb` after clearing their cache.
3. **Document it here.** A category that isn't listed in this file or in
   `jdid.py` is effectively undiscoverable to the enricher.

Do not exceed 10 areas or 10 categories per area; that's the Johnny.Decimal
invariant the id scheme (`CC.NN`) and the folder layout depend on.

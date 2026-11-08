# Data licences before workflow AI use

Before live dataset-based inference or embeddings, read the generated licence
register (`docs/DATA_LICENSES_AND_AI_USE.md`) and run the source-bound license
preflight. Use lenient by default: it blocks the AI criterion only for an explicit
applicable prohibition. Restrictive remains available and requires explicit AI
approval. Select it through DATA_HUNTER_LICENSE_MODE and record it in the run
report. Both require underlying access/use rights, current evidence, source
identities and compliance with non-AI licence/provider/privacy conditions. An
AI-silent licence is not an explicit AI approval. Missing underlying permission
still blocks lenient execution. Do not disable the gate or infer permission from
parser readiness, a software licence, or another dataset/version. Source
descriptions, dictionaries, codebooks and terminology are covered as well as
patient rows. Local inference, remote inference, embeddings, training and
redistribution need separate decisions. Gathering licence notices and
deterministic inventory for rights review does not authorize using the
underlying records with AI. Never upload data to a remote service without the
applicable data and provider decisions.

# Licence-first dataset intake

New-source intake checks the exact data licence/access notices first and retains
readable evidence with URL, retrieval date and hash. Initial intake is review-only:
necessary public notices/documentation within a 5 MB total budget, no patient or
benchmark payloads. Metadata is not exempt from the AI preflight. Select actual
downloads later from the explored catalogue after applicable permission review.
Manual licence decisions must come from the user, name purpose/operations and
exact source scope, and be validated and registered through
`agents/dataset-license-review-agent.md`. Never infer approval from a browser
selection, replace missing agreements, override explicit bans, or promote AI-silent
terms into explicit AI approval. Keep licensing, acquisition completeness and
benchmark readiness separate.

# Research studies

A new research study is built from its own internet search, even when its brief
resembles an existing study: search first without de-duplicating against the
catalogue, then match results to existing catalogue IDs, and annotate the study
only from that search (see `agents/dataset-intake-agent.md`, "New research
study"). Keep each study's reports as tracked Markdown in
`docs/research-studies/<study-id>/`, registered with the study, and record every
important study action in its append-only `actions.jsonl`: registration,
discovery runs, licence decisions and revisions, downloads (dry, partial,
minimal or full) and file operations. The registration, licence-review and
download scripts log automatically; any other action is logged with
`Scripts/log_study_action.py`. Held files are shared holdings; holding a file is
never permission for a study.

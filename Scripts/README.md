# Scripts

Entry points for the Data Hunter workflow. All of them run offline unless a row
says otherwise; none calls a model.

| Script | What it does |
|---|---|
| `build_dataset_browser.py` | Regenerate the active study's site (`studies/<id>/docs/dataset-browser/`) from its registries and catalogue receipts. With no study yet it builds the bootstrap workspace. |
| `serve.py` | Serve the active study's site on localhost; follows `study.py use` without a restart. |
| `study.py` | `list` study folders, `use <id>` to switch the active study, `current`. |
| `init_empty_workspace.py` | Write the empty validation and selection reports, receipts and catalogue documents for a workspace with no sources. Refuses to run once datasets are registered. |
| `register_research_study.py` | Validate and register an exported study JSON (`--request FILE --apply`): a new study gets its own folder `studies/<id>/` with empty registries and becomes active. `--remove STUDY_ID --apply` retires a study (folder kept). Appends to the study's action log. Grants no data permission. |
| `log_study_action.py` | Append one action to a registered study's `actions.jsonl`. |
| `run_dataset_intake_agent.py` | Prepare a single-source or discovery intake request and prompt for the intake agent; optionally launch the agent with `--execute`: Claude Code when installed, otherwise the Codex CLI. |
| `run_download_agent.py` | Execute the download task saved by the site (CSV or JSON); dry run by default; honours scope, licence mode and byte limits. Network access when executing. |
| `acquire_research_resources.py` | Hash-verified acquisition of reviewed resources into the study's `data/acquisitions/`. Network access. |
| `review_dataset_license.py` | Validate and register a manual licence decision saved by a dataset page. |
| `audit_data_licenses.py` | Gather licence notices, render the licence register and check exact source and operation permissions. Collection does not approve use. |
| `audit_dataset_downloads.py` | Fresh retained-lock and prepared-original integrity checks. Never starts downloads. |
| `validate_dataset_workspace.py` | Organise registry entries under local folders, run integrity and format checks and the source-bound permission preflight, and write the validation report the catalogue reads. |
| `document_dataset_selection.py` | Recheck artifact identities and build the catalogue documents and the selection receipt the site reads. |
| `resolve_full_release.py` | Resolve a `download_scope=full` request into exact-artifact jobs from the providers' own manifests (PhysioNet, Zenodo, GitHub, Hugging Face, OpenML, UCI), apply the size, identity and licence-binding gates, `--register` the bindable jobs and `--execute` them (identity-backed files into the locked holdings, acknowledged-risk files into quarantine; `--workers` concurrent transfers per dataset, default 4; `--datasets ID…` restricts a retry to named datasets of the saved instructions while the others keep their reservations against the total cap). Honours the instruction options `acquire_unresolved_rights` (open-access files without a licence record, kept AI-blocked) and `direct_holdings_for_acknowledged_risks` (no quarantine; uncertainty recorded as dataset `data_caveats`). Network access. |
| `review_quarantine.py` | `list` what the download agent left in quarantine with the reason per file; `--request FILE --apply` releases the deliveries ticked on the Quarantine page into the holdings after re-verification and format checks (`--reverify-download` compares a second download) and discards ticked superseded duplicates. |
| `report_dataset_status.py` | Write `docs/DATASET_STATUS.md` (rendered as the Status & actions page): one state per dataset (automatic, partial, action needed, no route), the one action each needs, quarantine contents and licence records awaiting confirmation. `--report` also saves a dated tracked copy under `reports/` and registers it with the study. Also run by `document_dataset_selection.py`. |
| `report_dataset_structure.py` | Write `docs/DATASET_STRUCTURE.md` (rendered as the Data structure page): for every held dataset the tables found in its verified files (CSV/TSV, gzip tables, ARFF, SQLite, NDJSON, JSON record lists, tables inside ZIP archives) with columns, declared or inferred types, missingness, distinct counts, numeric and date ranges and the labels of low-cardinality columns; for the datasets that are not held, the access route, the held releases of the same family (a provider demo shows the layout to expect), the retained documentation and what stays unknown until access. Deterministic, no model calls; `--datasets` restricts profiling, `--report` saves a dated tracked copy under `reports/` and registers it with the study. The document grants nothing and is itself covered by the AI preflight. |
| `audit_dataset_browser_links.py` | Audit internal links and anchors and public external destinations with bounded requests. Network access. |

`dataset_browser/` holds the site sources: the JavaScript and CSS copied into
the built site's `assets/`, the filter metadata and help text, the licence
review form, the acquisition-date index and the study pages.

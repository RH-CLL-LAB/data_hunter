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
| `run_dataset_intake_agent.py` | Prepare a single-source or discovery intake request and prompt for the intake agent; optionally launch a Codex CLI job with `--execute`. |
| `run_download_agent.py` | Execute the download task saved by the site (CSV or JSON); dry run by default; honours scope, licence mode and byte limits. Network access when executing. |
| `acquire_research_resources.py` | Hash-verified acquisition of reviewed resources into the study's `data/acquisitions/`. Network access. |
| `review_dataset_license.py` | Validate and register a manual licence decision saved by a dataset page. |
| `audit_data_licenses.py` | Gather licence notices, render the licence register and check exact source and operation permissions. Collection does not approve use. |
| `audit_dataset_downloads.py` | Fresh retained-lock and prepared-original integrity checks. Never starts downloads. |
| `validate_dataset_workspace.py` | Organise registry entries under local folders, run integrity and format checks and the source-bound permission preflight, and write the validation report the catalogue reads. |
| `document_dataset_selection.py` | Recheck artifact identities and build the catalogue documents and the selection receipt the site reads. |
| `audit_dataset_browser_links.py` | Audit internal links and anchors and public external destinations with bounded requests. Network access. |

`dataset_browser/` holds the site sources: the JavaScript and CSS copied into
the built site's `assets/`, the filter metadata and help text, the licence
review form, the acquisition-date index and the study pages.

# Data storage and evidence

Use the [dataset catalogue](../docs/DATASET_SELECTION.md) for holdings, sizes, completeness, readiness, relevance, sources and citations. Use the [action/access plan](../docs/DATASET_ACTION_PRIORITIES.md) for priorities, requests, registration and acquisition/MCP commands. These are the two maintained dataset documents.

| Location | Purpose |
|---|---|
| `datasets/<category>/<ID>/manifest.json` | Exact original locators, file identities, scoped inputs and validation state. Folder READMEs are navigation pointers only. |
| `raw/public_reference_collection/downloaded_data/` | Original starter/provider downloads; preserve recorded paths and bytes. |
| `raw/public_reference_collection/acquisitions/<batch>/` | Follow-up original downloads, provider/MCP metadata, receipts and retained inspection evidence. |
| `raw/public_reference_collection/prepared/` | Separate `inputs/`, `schema_inputs/`, `references/`, `documentation/` and `metadata/`; never ingest the whole root. |
| `incoming_manual/<dataset>/<release>/` | Authorized manual deliveries with dictionaries, release/source details, checksums and applicable terms. |
| `interim/dataset_validation/` and `interim/dataset_selection/` | Versioned deterministic validation/selection facts and code/config snapshots; `latest.json` points to current evidence. |
| `interim/documentation_cleanup/` | Checksum-bound recovery copies of superseded documentation; not a current dataset catalogue. |
| `processed/` | Derived workflow outputs; never substitute them for original input or independent truth. |
| `../runs/` | Versioned analytical inputs, prompts, code, metrics, outputs and provenance ledger. |

Source data, generated evidence and model artifacts stay out of Git. Before live inference or embeddings, read the [mandatory licence policy](../docs/DATA_LICENSES_AND_AI_USE.md) and run the source-bound preflight. Neither public access nor parser readiness grants permission or validates clinical mappings.

# Run archive

[Local run index](INDEX.md) · [Versioned input index](inputs/README.md)

Each run contains:

- `metrics.md` and `metrics.csv`: status, quality, protocol, timing and explicit input/output locations.
- `run.json`, `specification.json`, `inputs.json`, `outputs.json`: versioned settings, source/derived hashes and artifact identities.
- `code/`, `code_manifest.json`, `environment/`: exact source snapshots, dependencies and host/model inventory.
- `instructions/`: the applicable AGENTS.md, model instructions and evaluation rules.
- `prompts/`: actual outgoing request JSON and backend replies, including bounded retries; historical reconstruction is labelled separately.
- `outputs/`: native results, independent validation, extraction, scorer reports and failures.

Inputs live under `inputs/vNNN_*/`. Originals are preserved in v000; subsequent
versions include their parent manifest hash and a correction ledger. No patient
rows are stored in the Markdown/CSV metrics tables. Local source and generated
artifacts remain excluded from Git.

Runs and completed input versions are sealed by a file-hash manifest. A run never
overwrites another run. Do not compare different case sets or combine successful
cases across models/strategies. Missing historic prompts are reported as gaps;
reconstructed prompts are never labelled as captured requests.

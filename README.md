# Data Hunter

Explore, validate, select and download research datasets, licence first.

Data Hunter is a static site plus a small set of scripts for the datasets a
research study considers. A study writes its brief, searches for candidate
datasets licence-first, reads what is actually held and permitted, builds a
shortlist, and hands review, download and file tasks to a coding agent
(Claude Code or Codex) working from reviewed agent definitions. The site runs
entirely in the browser, makes no network requests and grants no data or
AI-use permission.

Developed by **Nikola Tom**; group leader **Carsten Utoft Niemann**.
Licence: [Apache 2.0](LICENSE). Citation: see [How to cite](#how-to-cite).

## Contents

1. [What is in this repository](#what-is-in-this-repository)
2. [Installation](#installation)
3. [Open Data Hunter](#open-data-hunter)
4. [The workflow](#the-workflow)
5. [Run with Docker](#run-with-docker)
6. [Development and tests](#development-and-tests)
7. [Data, licences and AI use](#data-licences-and-ai-use)
8. [Repository layout](#repository-layout)
9. [How to cite](#how-to-cite)
10. [Licence](#licence)

## What is in this repository

| Part | Where | What it does |
|---|---|---|
| The site | `docs/dataset-browser/` (generated), `Scripts/build_dataset_browser.py`, `Scripts/dataset_browser/` | Research studies, licence-first dataset search, catalogue with filters and shortlists, dataset records, licence decisions, download and file tasks, tutorial. |
| Workflow scripts | `Scripts/` | Study registration and logging, intake and download agents, licence review and audit, workspace validation, catalogue documentation, link audit, release export. See `Scripts/README.md`. |
| Agent definitions | `agents/` | The roles a coding agent follows for intake, licence review, downloads and file operations. |
| Registries | `configs/` | Dataset selection, study usage, validation, research studies, licence decisions and acquisition locks. They start empty. |
| Shared modules | `src/datahunter/` | The licence registry (`data_rights.py`) and the research-study registry (`research_studies.py`). |

This is the code-only release. The workspace starts empty: no datasets,
studies, searches, licence or access reviews, download records or acquired
holdings. Those are created by your own studies, searches and reviews and stay
in your workspace.

## Installation

### Prerequisites

- **Python 3.11 or newer**.
- **Git**.
- **Node.js** (any current LTS), only for the JavaScript tests. The site
  itself needs no build tooling.

The commands are for macOS and Linux shells. On Windows, replace
`.venv/bin/python` with `.venv\Scripts\python`.

### 1. Clone

```bash
git clone https://github.com/RH-CLL-LAB/data_hunter.git
```

```bash
cd data_hunter
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
```

If `python3 --version` reports anything older than 3.11, use the newer
interpreter explicitly, for example `python3.12 -m venv .venv`.

```bash
.venv/bin/python -m pip install -r requirements.txt
```

The only third-party dependency is `httpx`, used by the link audit. Everything
else is the Python standard library.

### 3. Verify

```bash
.venv/bin/python -m unittest discover -s tests
```

```bash
node --test tests/*.cjs
```

Both suites use fixtures only; they call no model and download nothing. Tests
that check registered catalogue entries are skipped while the catalogue is
empty.

## Open Data Hunter

The site is prebuilt in `docs/dataset-browser/`. Serve it locally (opening the
files directly also works, but browser storage for research studies is then
isolated per page):

```bash
python3 -m http.server 8767 --bind 127.0.0.1 --directory docs/dataset-browser
```

Then open <http://127.0.0.1:8767/> and start with **Tutorial** in the
navigation.

Rebuild the site after registries change:

```bash
.venv/bin/python Scripts/build_dataset_browser.py
```

When no catalogue receipts exist yet, the build creates the empty workspace
state itself (`Scripts/init_empty_workspace.py`). Once sources have been
acquired, `Scripts/validate_dataset_workspace.py` and
`Scripts/document_dataset_selection.py` produce the receipts the build reads.

## The workflow

1. **Create a research study** on the Research studies page: ID, question,
   intended use, population, targets, inclusion and exclusion.
2. **Search for its datasets**: the Add datasets page generates a licence-first
   discovery prompt; pass it to Claude Code or Codex with
   `agents/dataset-intake-agent.md`. The agent retains public licence notices
   with URL, date and hash, adds entries and annotations, and downloads no data.
3. **Explore the catalogue**: filter, including the reasons a dataset is not
   ready, open records, tick datasets and save the study shortlist.
4. **Record licence decisions** on dataset pages; the agent validates and
   registers them with `Scripts/review_dataset_license.py`.
5. **Prepare downloads and file tasks** from the selection bar as prompts, JSON
   or CSV for `agents/dataset-download-agent.md` and
   `agents/dataset-operations-agent.md`. Downloading grants no AI-use permission.
6. **Export and register the study** with
   `Scripts/register_research_study.py --request study.json --apply`; remove
   one with `--remove STUDY_ID --apply`. Every important action is appended to
   the study's log under `docs/research-studies/<id>/`.

## Run with Docker

`Dockerfile` and `compose.yaml` provide the same environment without a local
Python. Every service mounts this checkout at `/app` and runs as your user.

```bash
docker compose run --rm build-test
```

rebuilds the site and runs both test suites with networking disabled.

```bash
docker compose up site
```

serves the site at <http://127.0.0.1:8767/> from a read-only mount.
`download-agent` (profile `acquire`) is the one service with network access,
for reviewed acquisition jobs. Claude Code or Codex work on the checkout
itself, not inside a container.

## Development and tests

- Site sources live in `Scripts/dataset_browser/`; `docs/dataset-browser/assets/`
  is a generated copy and a test fails if the two differ. Edit the sources, run
  the build, then run the tests.
- `Scripts/export_public_release.py --target DIR` cuts a code-only release
  like this one from a working checkout: it copies the tool's code, empties the
  registries, builds the empty site and runs the tests in the target.

## Data, licences and AI use

Before any dataset-based inference or embeddings, read the licence register
the audit script generates (`docs/DATA_LICENSES_AND_AI_USE.md`) and run the
source-bound licence preflight. Lenient mode is the default and blocks only an
explicit applicable AI prohibition; restrictive mode also requires explicit AI
approval. Both require underlying access and use rights with current
evidence. Source descriptions, dictionaries and terminology are covered as
well as patient rows, and local inference, remote inference, embeddings,
training and redistribution are separate decisions. An AI-silent licence is
not explicit approval. Holding a file is never permission to use it.

## Repository layout

```text
agents/                    agent role definitions
configs/                   registries (empty until your studies fill them)
docs/dataset-browser/      the generated site
docs/research-studies/     one folder per registered study: reports, actions.jsonl
Scripts/                   workflow, audit, build and release scripts
Scripts/dataset_browser/   site sources (JS, CSS, Python helpers)
src/datahunter/            shared licence and study registry modules
tests/                     Python and JavaScript tests
data/, runs/               local workspace (ignored except their README files)
```

## How to cite

If you use Data Hunter or results produced with it, please cite the authors
and the tool. [CITATION.cff](CITATION.cff) has the machine-readable form:

> Tom N, Niemann CU. Data Hunter: licence-first exploration, validation,
> selection and download of research datasets. 2026.
> https://github.com/RH-CLL-LAB/data_hunter

## Licence

Code, configuration and documentation are released under the
[Apache License 2.0](LICENSE). Attribution notices in [NOTICE](NOTICE) must be
retained in redistributions and derivative works. The licence does not cover
third-party datasets, dictionaries, terminologies, licence notices or
publications that the tool catalogues or references; those keep their own
licences and access conditions.

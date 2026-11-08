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
| The site | `studies/<id>/docs/dataset-browser/` (generated per study), `Scripts/build_dataset_browser.py`, `Scripts/dataset_browser/` | Research studies, licence-first dataset search, catalogue with filters and shortlists, dataset records, licence decisions, download and file tasks, tutorial. |
| Workflow scripts | `Scripts/` | Study registration and switching, logging, intake and download agents, licence review and audit, workspace validation, catalogue documentation, link audit, local site server. See `Scripts/README.md`. |
| Agent definitions | `agents/` | The roles a coding agent follows for intake, licence review, downloads and file operations. |
| Study folders | `studies/<id>/` | Everything one study produces, in one place: its registries (`configs/`), retained notices, quarantine and acquired data (`data/`), intake, download and review runs (`runs/`), reports, action log and generated site (`docs/`). Created empty when a study is registered; never committed. |
| Registry templates | `templates/study/configs/` | The empty registries a new study folder starts from. |
| Shared modules | `src/datahunter/` | The licence registry (`data_rights.py`) and the research-study registry (`research_studies.py`). |

This is the code-only release. There are no studies yet: no datasets,
searches, licence or access reviews, download records or acquired holdings.
Each study you create gets its own folder under `studies/`, and everything it
produces stays there.

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

Build the site, then serve it:

```bash
.venv/bin/python Scripts/build_dataset_browser.py
```

```bash
.venv/bin/python Scripts/serve.py
```

Then open <http://127.0.0.1:8767/> and start with **Tutorial** in the
navigation. With no study yet, the build uses a bootstrap workspace
(`studies/.bootstrap/`) so the tutorial and the New-study form are available.

The server shows the **active study**. Registering a study makes it active;
switch with `Scripts/study.py use <id>` and list studies with
`Scripts/study.py list`. The server follows the switch without a restart.
The **Study** picker in the site header also lists the other registered studies
whose sites have been built and opens the chosen study's site; rebuild a study's
site after registering a new study so its picker knows about it.

Rebuild the site after the active study's registries change:

```bash
.venv/bin/python Scripts/build_dataset_browser.py
```

Once sources have been acquired, `Scripts/validate_dataset_workspace.py` and
`Scripts/document_dataset_selection.py` produce the receipts the build reads;
for a study without sources the build creates the empty state itself.

## The workflow

1. **Create a research study** on the Research studies page: ID, question,
   intended use, population, targets, inclusion and exclusion. Export it and
   register it with `Scripts/register_research_study.py --request study.json
   --apply`: this creates `studies/<id>/` with empty registries and makes it
   the active study.
2. **Search for its datasets**: the Search datasets page generates a licence-first
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
6. **Re-export and register the study** after changing its shortlist or
   annotations; retire one with `--remove STUDY_ID --apply` (its folder is
   kept). Every important action is appended to `studies/<id>/actions.jsonl`.

## Run with Docker

`Dockerfile` and `compose.yaml` provide the same environment without a local
Python. Every service mounts this checkout, study folders included, at `/app`
and runs as your user.

```bash
docker compose run --rm build-test
```

rebuilds the site and runs both test suites with networking disabled.

```bash
docker compose up site
```

serves the active study's site at <http://127.0.0.1:8767/> from a read-only mount.
`download-agent` (profile `acquire`) is the one service with network access,
for reviewed acquisition jobs. Claude Code or Codex work on the checkout
itself, not inside a container.

## Development and tests

- Site sources live in `Scripts/dataset_browser/`; the `assets/` folder of a
  built site is a generated copy and a test fails if the two differ. Edit the
  sources, run the build, then run the tests.
- Study folders are ignored by Git, so a checkout is always the code-only
  release: nothing a study searched, reviewed or downloaded is committed.
- `DATA_HUNTER_STUDY=<id>` selects a study for one command;
  `DATA_HUNTER_WORKSPACE=<folder>` points the scripts at any other workspace.

## Data, licences and AI use

Before any dataset-based inference or embeddings, read the licence register
the audit script generates (`studies/<id>/docs/DATA_LICENSES_AND_AI_USE.md`) and run the
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
Scripts/                   workflow, audit, build, serve and study scripts
Scripts/dataset_browser/   site sources (JS, CSS, Python helpers)
src/datahunter/            shared modules: workspace, licence registry, study registry
templates/study/configs/   empty registries a new study starts from
tests/                     Python and JavaScript tests
studies/<id>/              one folder per study (ignored by Git):
  study.json               marker and creation record
  actions.jsonl            append-only action log
  reports/                 tracked Markdown reports
  configs/                 this study's registries
  data/                    notices, quarantine, acquisitions, receipts
  runs/                    intake, download, review and registration runs
  docs/                    generated catalogue documents and dataset-browser site
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

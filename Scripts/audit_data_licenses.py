"""Collect licence evidence, render the register, or gate a workflow source package.

Collection never grants permission or rewrites reviewed decisions. No model calls.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root
ROOT = workspace_root(default=CODE_ROOT)
from datahunter.data_rights import LicenseRegistry, OPERATIONS, MODES, DataRightsError

AUDIT = ROOT / 'data/interim/license_audit'
DOCUMENT = ROOT / 'docs/DATA_LICENSES_AND_AI_USE.md'


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + '\n')


def collect(registry, fetch=False):
    resources_by_url = {}
    for resource in registry.resources.values():
        for url in resource.get('sources', []):
            resources_by_url.setdefault(url, []).append(resource['id'])
    directory = AUDIT / 'publisher_pages'
    directory.mkdir(parents=True, exist_ok=True)
    previous_path = AUDIT / 'collected_sources.json'
    previous = {p['url']: p for p in json.loads(previous_path.read_text()).get('pages', [])} if previous_path.exists() else {}

    def download(item):
        url, resources = item
        parts = urlsplit(url)
        record = {'url': url, 'resources': resources, 'status': 'not_fetched'}
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            record['status'] = 'rejected_url'
            return record
        if not fetch:
            return record
        name = sha256(url.encode()).hexdigest() + '.html'
        path = directory / name
        old = previous.get(url, {})
        if path.is_file() and old.get('sha256') == sha256(path.read_bytes()).hexdigest():
            record.update(status='retrieved_not_approved', snapshot=str(path.relative_to(ROOT)),
                          bytes=path.stat().st_size, sha256=old['sha256'], cached=True)
            return record
        temporary = path.with_suffix('.part')
        command = ['curl', '--fail', '--location', '--silent', '--show-error',
                   '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '5',
                   '--connect-timeout', '10', '--max-time', '25', '--max-filesize', '5000000',
                   '--output', str(temporary), url]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode:
                record.update(status='unavailable', curl_code=result.returncode)
            else:
                temporary.replace(path)
                payload = path.read_bytes()
                record.update(status='retrieved_not_approved', snapshot=str(path.relative_to(ROOT)),
                              bytes=len(payload), sha256=sha256(payload).hexdigest())
        except (OSError, subprocess.TimeoutExpired):
            record['status'] = 'unavailable'
        finally:
            temporary.unlink(missing_ok=True)
        print(f"{record['status']}: {parts.hostname}", flush=True)
        return record

    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(download, sorted(resources_by_url.items())))
    report = {'collected_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'public licence notices/provider pages only; retrieval is not approval',
              'pages': pages, 'policy_sha256': registry.policy_hash, 'model_calls': 0}
    write(AUDIT / 'collected_sources.json', report)
    return report


INTRO = '''# Data licenses and allowed/restricted AI use

This is the source-bound runtime permission register; review dates and exact scopes
are recorded per resource below. The broader [dataset catalogue](DATASET_SELECTION.md)
also includes unacquired and unregistered sources, which are not cleared by appearing
in documentation. Public access and download success are not permission.

Before a new account/application or transfer, use the [pre-registration licensing
screen](DATASET_ACTION_PRIORITIES.md#license-checks-before-registration-or-download).
It records public terms, missing agreements and platform/component restrictions for
planned access routes. That preliminary review does not grant access or alter the
operation permissions in this runtime register.

## Licence-first intake and manual review

Adding a source starts with licence/access evidence, then only necessary public
notices and documentation (5,000,000 bytes total by default). Initial intake does
not acquire patient tables or benchmark payloads. Metadata still needs its own
source-bound preflight before AI use. Dataset acquisition is a later catalogue
selection after source exploration and permission review.

The Explorer separates four display states: approved recorded local AI scope;
source approved but exact files pending; licence/permission review required; and
prohibited intended local use. Download completeness and benchmark readiness are
independent. The associated package can remain blocked when a narrower prepared
input scope is allowed. Always read the exact scope.

Each dataset's Access & licensing section links to applicable official terms and
retained notices when available. Explicitly verified public legal notices
marked `public_notice` are copied as inert text attachments into the generated
Explorer. Private agreements are never copied into public pages automatically. Missing evidence is shown as missing. The manual
review form records the reviewer, exact release, whole selected artifact scope,
purpose, operations, conditions, dates and retained evidence path/hash. A human
interpretation is not a new publisher grant. Required agreements and explicit
prohibitions cannot be overridden. Strict mode still needs explicit source AI
approval; recording an AI-silent interpretation does not provide it.

The static page only prepares a pending decision. Copy its prompt or attach the
saved JSON in Claude Code or Codex using [the licence review agent](../agents/dataset-license-review-agent.md).
The agent first validates it with `Scripts/review_dataset_license.py --request`
and applies the supplied decision with `--apply` only after validation succeeds.
An exact current catalogue/registry identity and complete selected-resource scope
are required. The importer preserves review requests and before/after registries
under `runs/license_reviews/`; history is indexed by
`configs/dataset_license_reviews.json`. Browser storage is never a runtime grant.

Approval before acquisition creates a reviewed source resource but clears no
unidentified input. Download jobs still require exact-version rights binding,
access checks and budgets. After delivery, register exact file identities, refresh
audit/validation/catalogue evidence and rerun the normal preflight. Prepared files
and later derivatives need their own exact scope. Manual approval does not mark a
dataset complete or certify its benchmark/reference quality.

## Separate research studies

Use the Research study selector to keep a separate question, intended use,
dataset shortlist, saved filters and paper/dataset annotations for each project.
Dataset files and published-study records remain shared. Existing default-study
priority, relevance and technical-readiness ratings are a baseline, not an
assessment for every new study. Annotation filters do not grant data rights.

Browser profiles and annotations can be exported/imported from Research studies.
Pass the exported JSON to Claude Code or Codex to register it with
`Scripts/register_research_study.py --request /path/to/study.json --apply`.
The normalized brief and its revision hash identify the purpose. A new study has
no inherited runtime data clearance. Set `DATA_HUNTER_STUDY_ID` to its registered ID
before workflow use; the normal preflight requires a current study-specific
review. Changing a brief invalidates reviews tied to its previous revision.
The default study profile is retained; create a separate study for another
research purpose.

Manual licence decisions include the active research brief. The importer verifies
that it matches the registered brief, retains the decision for that study and
cannot override an applicable explicit source prohibition. A paper may link to
several differently licensed datasets. Published-study filtering therefore shows
linked dataset permission coverage and review routes, not a licence inferred from
the article. A recorded manual decision can also be a refusal or clarification;
combine that filter with permission status and read the source scope.

## Rule enforced at workflow startup

Two versions implement the requested AI criteria:

| Mode | AI criterion | Silence / broad MIT, Apache, CC BY or ODbL grant |
|---|---|---|
| [Restrictive](#restrictive-policy) | The intended AI use must be explicitly approved | Blocks; a general grant is not AI-specific approval |
| [Lenient](#lenient-policy), default | The intended AI use must not be explicitly banned | Passes the AI criterion; this is an inference from silence, not publisher approval |

Both preserve underlying access/use rights, attribution, modification restrictions,
privacy, disclosure/provider checks, source identity and evidence freshness. Missing
data rights can therefore block a lenient run even when its AI criterion passes.
Choose a mode explicitly with `DATA_HUNTER_LICENSE_MODE=restrictive|lenient`;
audit commands also accept `--mode`. The selected mode is recorded in every report
and pinned for a running license session. Redistribution keeps its separate
non-AI permission check in both modes.

Every source artifact must match the versioned SHA-256 asset register. Its license
evidence must exist, match its recorded hash, remain within the review period, and
permit every requested operation. **Restricted, missing, ambiguous, expired or
unregistered permissions stop the AI workflow before a model request.** There is
no command-line override or fallback to an online provider. A renamed file or a
metadata-only prompt does not remove its source license. If identical bytes have
multiple applicable decisions, the most restrictive decision wins.

`ALLOWED` below means both the selected AI criterion and the separate underlying
use check pass under the listed conditions. It is not institutional
approval, clinical certification or permission for every future product use.
`BLOCKED: UNREVIEWED` identifies missing/ambiguous permission; it does **not** claim
the publisher explicitly bans AI. `BLOCKED: PROHIBITED` is reserved for an explicit
restriction. A modification restriction or uncertain component license is enough
to stop the project while its application to the proposed operation is unresolved.

Local inference, local embeddings, remote inference, remote embeddings, training,
and redistribution have independent decisions. Remote providers, training and
redistribution are currently unreviewed and blocked, even where a broad data
license might permit them. This project choice is not a claim that MIT, Apache,
CC BY or ODbL inherently prohibits those operations. Generating embeddings is AI
use. Clinical rows, descriptions, dictionaries and codebooks all remain subject to
their source terms. Current stage disclosure/terminology checks still apply.

## Public-source reassessment, 30 October 2026

The scoped review records before/after policy and asset hashes at `data/interim/license_audit/public_review_20261030/decision.json`. No source data bytes were changed. A changed permission assessment does not retroactively modify earlier sealed blocked-run results. The publisher statement for Synthea and the HEAL publication notice were retained; an empty HTTP response for HEAL was rejected as evidence, and the actually retrieved browser/search text is preserved instead.

## Main findings

- Open MIMIC native/OMOP/FHIR demos, the official eICU demo and ricu demo data carry
  ODbL 1.0. Keep attribution/notices; assess adapted-database publication duties.
  Their open-demo terms do not grant use of the full credentialed databases.
- The CTN-0094 public data package declares MIT; ReMatch has an MIT license.
  Neither grants rights to separate original studies or third-party vocabularies.
- TCIA explicitly licenses the acquired ACRIN ancillary XLSX under CC BY 4.0.
  Its collection/publication citations must be retained.
- OMAP has no explicit license in the pinned repository. AI use is blocked.
- The CDISC pilot's custom terms restrict modification, competitive use and
  distribution. Its applicability to transformations/AI is unresolved: block it,
  its prepared derivatives and the inherited OHDSI trial examples. Previously
  prepared files remain present; parser readiness is not permission to use them.
- Native Synthea CSV records are cleared for lenient local inference/embeddings after checking the publisher generated-record permission statement. Bundled vocabulary distributions and the supplied OMOP reference retain separate decisions.
- Pharmaverse remains mixed: three self-contained generated response CSVs (rs_onco_irecist, rs_onco_ca125, supprs_onco_ca125) are cleared as a hash-scoped subset. Pilot-derived data remain unresolved. The qs_metabolic CoEQ example carries an explicit instrument-specific purpose limit and is excluded from MedScrape use without a separate grant.
- The HEAL publication is CC BY. Its 66 source-term strings are separately cleared for local exploration with citation; published vocabulary candidate outputs remain under their separate unresolved decision. The source list is not a clinical event dataset or a complete 76-term gold standard.
- Athena is a collection of separately licensed vocabularies, not an umbrella
  AI grant. LOINC includes additional-rights content, and UCUM restricts changes
  and derivatives. Whole-distribution use and derived artifacts remain subject to
  scoped rights decisions. Lenient mode permits local inference over unmodified
  UCUM reference content for interoperability; restrictive mode still requires
  explicit AI approval. Existing deterministic lookup/conversion is unchanged.
  NCI terminology is distinct from CDISC Library publications and Pilot data.
- UKB explicitly prohibits incorporating participant-level data into publicly
  available generative AI, including indirect exposure through searchable public
  repositories. It also prohibits sharing trained models that expose or recreate
  participant data or equivalent access. Responsible research AI is supported
  subject to its agreements; this is not a blanket ban on all AI. These specific
  prohibitions apply in both modes. See the
  [official AI guidance, sections 2–3](https://community.ukbiobank.ac.uk/hc/en-gb/articles/21922841787293-Use-of-Artificial-Intelligence-AI-applications-and-models).
- UKB and unacquired controlled studies have no project-specific agreements here.
  They are blocked. No signed agreement or researcher approval is inferred from
  a public study page. UKB access is currently paused; see the dataset guide.

Sources: [ODbL legal text](https://opendatacommons.org/licenses/odbl/1-0/),
[PhysioNet restricted-data/online-service notice](https://physionet.org/news/post/gpt-responsible-use/),
[TCIA ACRIN terms](https://www.cancerimagingarchive.net/collection/acrin-6698/),
[CDISC Pilot terms](https://github.com/cdisc-org/sdtm-adam-pilot-project),
[LOINC license](https://loinc.org/license/), [UCUM license](https://ucum.org/license),
[SNOMED licensing](https://www.snomed.org/licensing),
[UMLS agreement](https://www.nlm.nih.gov/research/umls/knowledge_sources/metathesaurus/release/license_agreement.html),
[NCI CDISC publication statement](https://www.cancer.gov/about-nci/organization/cbiit/vocabulary/cdisc).

## Resource-by-resource decisions

The table is rendered from [reviewed decisions](../configs/data_licenses.json).
Evidence links point to retained local license notices or public provider pages.
The [asset register](../configs/data_license_assets.json) contains identities and
paths only, with no patient rows or terminology records. Unknown/unacquired
releases remain blocked even if a general provider policy is public.

'''

FOOTER = '''
## Operation and evidence details

The next table preserves the specific reason, conditions and exact local evidence
for every decision. Copies are under ignored `data/interim/license_audit/` so
restricted notices/provider pages are not republished as project data. Licence
files copied from the original acquisitions preserve their acquisition provenance;
their SHA-256 identifies the exact reviewed bytes. Fetching a new page does not
replace an approved snapshot or automatically grant permission. Provider pages may
contain template agreements rather than a signed/approved project agreement.

'''

RUNBOOK = '''
## Startup check and audit commands

```sh
# Check the complete selected input package before a local inference/embedding run.
.venv/bin/python Scripts/audit_data_licenses.py check \\
  --mode restrictive \\
  --source data/raw/public_reference_collection/prepared/inputs/eicu_2_0_1 \\
  --operation local_inference --operation local_embedding

# Inspect decisions, missing evidence and policy expiry for every resource.
.venv/bin/python Scripts/audit_data_licenses.py report --mode lenient

# Lenient is the default; select it explicitly for reproducible workflow commands.
export DATA_HUNTER_LICENSE_MODE=lenient

# Gather public terms/provider-page snapshots; never auto-approve their content.
.venv/bin/python Scripts/audit_data_licenses.py collect --fetch

# Re-render this document after a reviewed policy change.
.venv/bin/python Scripts/audit_data_licenses.py render
```

The check exits nonzero on a blocked package and writes
`data/interim/license_audit/preflight.json`, including denied operations/reasons,
policy/asset hashes and conditions. It makes zero model calls. The legacy pipeline
runs this gate before profiling/stages and records a startup report under its
output directory. Production proposal and embedding workflows check their native
source identities; local HTTP inference/embedding requests also require an active
licence session. Legacy external SDK model calls are guarded independently.
The proposal CLI checks the entire ingestion artifact census before discovery,
embeddings or model-client setup; unsupported/unregistered artifacts cannot be
silently omitted to obtain permission. Source dictionaries count as inputs.

All adapters for a future OpenCode/cloud benchmark must enter the same licensed
session and satisfy a separate provider decision. Running OpenCode manually
outside the default study is outside this software gate. The existing caller disclosure
authorization is additional to, and cannot override, data licence permission.

The deterministic public reader wrapper records an AI-eligibility preflight before
ingestion; a blocked decision stops it. Low-level deterministic inventory/compiler
utilities remain usable for inspecting already-held artifacts and rights evidence
without AI. Permission for conversion/derivative publication must be assessed
separately; this gate does not retroactively authorize prior file preparation.

The 30-day review interval is a project control, not a licence expiration date.
If a release/terms/provider/purpose changes, review the new evidence and content
hashes before enabling that operation. Do not solve a denial by relabelling input,
deleting the gate, changing a model's answer or treating a transport failure as
permission. Files remain retained for audit; blocking AI does not delete data.

No live model comparison, remote dataset transmission, fine-tuning or training was
performed for this audit. Runtime checks are deterministic; ordinary tests retain
their test doubles. This gate is an application control, not an operating-system
sandbox or a substitute for project/custodian agreements.
'''


def render_mode(registry):
    mode = registry.mode
    lines = [f'# Data licences — {mode} policy', '',
             '[Consolidated evidence, conditions and sources](DATA_LICENSES_AND_AI_USE.md).', '',
             ('Requires explicit approval of the intended AI operation. General licence grants alone do not qualify.'
              if mode == 'restrictive' else
              'The AI criterion passes unless the intended use is explicitly banned. Silence is not publisher approval.'), '',
             'Overall workflow eligibility also requires underlying access/use rights and valid evidence; provider, privacy and disclosure checks remain in force.', '',
             f'Use `DATA_HUNTER_LICENSE_MODE={mode}` or audit `--mode {mode}`. Project default: **{registry.policy.get("default", "lenient")}**. Reviewed 30 October 2026.', '',
             '| Resource | AI criterion (local inference) | Local inference workflow | Local embedding workflow | Remote inference workflow | Training workflow | Reason (local inference) |',
             '|---|---|---|---|---|---|---|']
    for r in registry.resources.values():
        ds = {op: registry._decision(r['id'],op) for op in OPERATIONS}
        label = lambda op: 'ALLOWED' if ds[op]['state']=='allowed' else 'BLOCKED: '+ds[op]['state'].upper()
        name = f"[{r['name']}]({r['sources'][0]})" if r.get('sources') else r['name']
        values = [name, ds['local_inference']['ai_criterion'].upper(), label('local_inference'),
                  label('local_embedding'), label('remote_inference'), label('training'),ds['local_inference']['reason']]
        lines.append('| '+' | '.join(v.replace('|','\\|') for v in values)+' |')
    lines += ['', 'Remote embeddings and redistribution have independent entries in the machine-readable report.',
              'UKB public-generative-AI and participant-data-equivalent model-sharing prohibitions apply in both modes. No UKB participant data or project agreement is held here.', '']
    return '\n'.join(lines).replace(f'# Data licences — {mode} policy', f'## {mode.capitalize()} policy', 1)


def render(registry):
    clean = lambda value: str(value).replace('|', '\\|').replace('\n', ' ')
    counts = f"Register: **{len(registry.resources)} resource/component decisions**, **{len(registry.assets)} artifact identities**, covering all **48 catalog IDs**.\n"
    receipt = AUDIT / 'collected_sources.json'
    if receipt.exists():
        pages = json.loads(receipt.read_text())['pages']
        counts += f"Public evidence collection: **{sum(p['status']=='retrieved_not_approved' for p in pages)}/{len(pages)} pages retrieved**; unavailable pages are recorded in the [collection receipt](../data/interim/license_audit/collected_sources.json). Retrieval is not licence approval.\n"
    lines = [INTRO, counts, f'Table mode: **{registry.mode}**. Both modes are included below for comparison.\n', '| Resource / catalog IDs | Licence evidence | Local inference | Local embeddings | Remote inference / embeddings | Training / redistribution |',
             '|---|---|---|---|---|---|']
    for resource in registry.resources.values():
        states = {op: registry._decision(resource['id'], op)['state'] for op in OPERATIONS}
        label = lambda op: 'ALLOWED' if states[op] == 'allowed' else 'BLOCKED: ' + states[op].upper()
        name = resource['name'] + (' (' + ', '.join(resource['catalog_ids']) + ')' if resource.get('catalog_ids') else '')
        license_text = '[' + resource['license'] + '](' + resource['sources'][0] + ')' if resource.get('sources') else resource['license']
        lines.append('| ' + ' | '.join(map(clean, [name, license_text, label('local_inference'), label('local_embedding'),
                    label('remote_inference') + ' / ' + label('remote_embedding'),
                    label('training') + ' / ' + label('redistribution')])) + ' |')
    lines.append(FOOTER)
    for resource in registry.resources.values():
        lines.extend(['### ' + resource['name'], '',
                      '- **Reason if blocked:** ' + resource['reason'] + '.',
                      ('- **Conditions:** ' + ' '.join(resource.get('conditions', []))).rstrip(),
                      '- **AI-specific evidence assessment:** ' + resource.get('ai_review_note', 'Not recorded.'),
                      '- **Reviewed / next check:** ' + resource['reviewed_on'] + ' / ' + resource['review_due'] + '.'])
        if resource.get('sources'):
            lines.append('- **Publisher / terms links:** ' + ', '.join(
                '[source ' + str(i+1) + '](' + url + ')' for i,url in enumerate(resource['sources'])) + '.')
        for item in resource.get('evidence', []):
            lines.append('- **Retained evidence:** [' + Path(item['snapshot']).name + '](../' + item['snapshot']
                         + '); SHA-256 `' + item['sha256'] + '`.')
        if not resource.get('evidence'):
            lines.append('- **Missing:** no artifact-specific approved license/agreement snapshot is registered. AI remains blocked.')
        for prohibition in resource.get('conditional_prohibitions', []):
            lines.append('- **Explicit conditional prohibition (both modes):** ' + prohibition['activity']
                         + ' [Source, section ' + prohibition['section'] + '](' + prohibition['source'] + ').')
        lines.append('')
    lines.append(RUNBOOK)
    for mode in sorted(MODES):
        lines.append(render_mode(LicenseRegistry(registry.policy_path, registry.assets_path, root=registry.root, mode=mode)))
    DOCUMENT.write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['collect', 'report', 'render', 'check'])
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--source', type=Path, action='append')
    parser.add_argument('--operation', choices=sorted(OPERATIONS), action='append')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--mode', choices=sorted(MODES))
    args = parser.parse_args()
    registry = LicenseRegistry(mode=args.mode)
    if args.command == 'collect':
        result = collect(registry, args.fetch)
        print(json.dumps({'pages': len(result['pages']), 'retrieved': sum(
            p['status'] == 'retrieved_not_approved' for p in result['pages']), 'model_calls': 0}))
        return 0
    if args.command == 'render':
        render(registry)
        print(DOCUMENT)
        return 0
    if args.command == 'report':
        result = {'mode': registry.mode, 'policy_sha256': registry.policy_hash, 'assets_sha256': registry.assets_hash,
                  'decisions': [registry._decision(r, op) for r in registry.resources for op in sorted(OPERATIONS)],
                  'model_calls': 0}
    else:
        if not args.source:
            parser.error('check requires at least one --source')
        result = registry.paths(args.source, args.operation or ['local_inference', 'local_embedding'])
    write(args.output or AUDIT / ('preflight.json' if args.command == 'check' else 'report.json'), result)
    print(json.dumps(result, indent=2))
    return 2 if result.get('status') == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())

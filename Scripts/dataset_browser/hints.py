"""One sentence of help on every control of the generated site.

`hinted(html)` gives each button, select, summary, text input and marked link a `title` (the browser's hover
tooltip, read by screen readers as a description) from HINTS, keyed by an attribute the control already carries.
`help_button(id, label, text)` is the ⓘ affordance the filter panel uses, for headings and forms: a toggle that
shows a short explanation in place. Every text says what the control does and what it never does: nothing on the
site transfers data or grants permission; each action saves a task or a file for you or the agent.
"""
from html import escape
import re

# What each page is for; the navigation links carry these as tooltips and the home page lists them.
PAGE_BLURBS = {
    'Home': 'Overview: what the site holds, how the workflow runs, where to start.',
    'Tutorial': 'The workflow step by step: study, search, explore, decide, download, register.',
    'Research studies': 'Create or switch the active study; its brief, search results, reports and action log live here.',
    'Search datasets': 'Prepare the licence-first dataset search for a study, or a single-source review of a dataset or paper.',
    'Dataset explorer': 'Search, filter, tick and shortlist datasets; export and start download or file tasks.',
    'Activity': 'What is running now, what each study has done and what is still missing.',
    'Priorities & access': 'The shared plan: priority, required action and official access route for every dataset.',
    'Licensing': 'Lenient and strict rules, registered resource decisions, notices and the AI-use gate.',
    'Status & actions': 'One state per dataset: obtained automatically, partial, or the one action it needs from you; quarantine and licence confirmations.',
    'Data structure': 'Tables, columns, types and descriptive statistics of the held files; for datasets you still need to apply for, what the held releases of the same family and the retained documentation show.',
    'Published studies & tools': 'Which papers and comparator tools used each source, with annotation and permission filters.',
    'Quarantine': 'Staged deliveries that are not held yet: tick what to release or discard and save the request for the agent.',
    'Evidence & guide': 'Definitions of readiness, sizes, leakage and validation limits; how the catalogue is maintained.',
    'Download guide': 'How scoped downloads, file actions, size limits and acquisition dates work.',
    'How to cite': 'How to cite the tool and the catalogued datasets.',
}

# Control → one sentence. The key is an attribute (or attribute=value pair) the control carries in the generated HTML.
HINTS = {
    # Header
    'data-study-switch': 'Switch the study whose shortlist, annotations and reports you see. Another registered study opens its own site.',
    'data-new-study-link': 'Go to the form that starts a new research study.',
    'data-theme-choice="light"': 'Light appearance.',
    'data-theme-choice="dark"': 'Dark appearance.',
    # Explorer: search, sort, filters, table
    'data-filter="q"': 'Search names, IDs, studies, topics and tags; every word you type must match.',
    'data-sort': 'Order the table by priority, name or retained size.',
    'data-filter-toggle': 'Show or hide the filter panel; the number is how many filters are active.',
    'data-reset': 'Clear every filter and the search. Ticks are kept.',
    'data-expand-groups': 'Open every filter group.',
    'data-collapse-groups': 'Close every filter group.',
    'data-pick-all': 'Tick or untick every dataset on this page.',
    'data-previous': 'Previous page of the table.',
    'data-next': 'Next page of the table.',
    'data-detail-link': 'Open this dataset’s page: held files, terms, licence review and annotations.',
    'data-back-selection': 'Back to the explorer with the filters and ticks you had.',
    # Explorer: selection bar
    'data-pick-filtered': 'Tick every dataset that matches the current filters, so the actions apply to all of them.',
    'data-clear-picks': 'Untick every dataset. Filters stay as they are.',
    'data-save-study-selection': 'Keep the ticked datasets as this study’s shortlist in this browser. Export the study to register it; a shortlist grants no data access.',
    'data-export': 'Download a CSV of the ticked datasets (or of every matching dataset when nothing is ticked) with status, access and licence columns.',
    'data-copy-filter': 'Copy a link that reopens this view with the same filters and ticks.',
    'data-save-agent': 'Open the download options for the ticked datasets: scope, licensing, options and limits. Nothing is transferred until the saved task reaches the agent.',
    'data-action-menu': 'File tasks for ticked datasets whose files are held. Each saves a task for the agent; none changes files by itself.',
    'data-dataset-action="verify"': 'Save a task that re-checks the held files’ presence, sizes and SHA-256. It repairs nothing.',
    'data-dataset-action="copy"': 'Save a task that makes a versioned working copy of the held files. Originals stay unchanged.',
    'data-dataset-action="prepare_inputs"': 'Save a task that prepares source-only workflow inputs from the held files after format and rights review.',
    'data-dataset-action="relocate"': 'Save a plan to move held files. Nothing moves until the plan is confirmed.',
    'data-dataset-action="remove"': 'Save a removal plan only. The agent lists the exact files and asks for confirmation before deleting anything.',
    'data-file-inventory': 'Download a CSV of every retained file of the ticked datasets: path, size, SHA-256 and the datasets that share it.',
    # Download dialog
    'name="agent-download-scope" value="review_only"': 'Investigate and register the sources only; no payload is transferred.',
    'name="agent-download-scope" value="minimal"': 'Acquire the reviewed minimal useful package of each dataset.',
    'name="agent-download-scope" value="full"': 'Resolve and acquire each complete provider release, within the saved limits.',
    'name="agent-license-mode" value="lenient"': 'Underlying rights are required; an explicit applicable AI prohibition blocks use.',
    'name="agent-license-mode" value="restrictive"': 'The same rights checks, plus an explicit AI approval before local inference or embeddings.',
    'data-agent-unresolved': 'Also take open-access files that no licence record covers yet. They stay blocked for AI use until a licence decision is registered.',
    'data-agent-reuse-held': 'Copy files another study of this checkout already holds, re-verified under this study’s own gates. No permission crosses.',
    'data-agent-risk value="missing_provider_checksum"': 'Accept files whose provider publishes no checksum. They are identified by local SHA-256 only and quarantined.',
    'data-agent-risk value="unknown_advertised_size"': 'Accept files whose size the provider does not state, within the byte limits.',
    'data-agent-direct-holdings': 'Full releases only: files taken under an accepted exception go straight into the holdings, with their uncertainty recorded, instead of quarantine.',
    'data-agent-dataset': 'Cap per dataset in decimal GB; 20 GB is the ceiling.',
    'data-agent-max': 'Cap per file in decimal GB.',
    'data-agent-total': 'Cap for the whole selection in decimal GB.',
    'data-generate-agent': 'Build the agent prompt and task from the choices above. Nothing is transferred.',
    'data-copy-agent': 'Copy the generated prompt to paste into Claude Code or Codex.',
    'data-confirm-agent': 'Save the task as a CSV to attach in Claude Code or Codex. The agent validates it before any transfer.',
    'data-cancel-agent': 'Close without saving.',
    # File-task dialog
    'data-operation-destination': 'Folder inside the project where copies or prepared inputs go.',
    'data-confirm-operation': 'Save the file task as a CSV for the operations agent. Nothing changes until the agent runs it.',
    'data-cancel-operation': 'Close without saving.',
    # Search datasets (intake)
    'name="intake-task-type" value="single_source"': 'Investigate one named source: licence, documentation, duplicates. No payload.',
    'name="intake-task-type" value="discovery"': 'Run a licence-first search for datasets that match the study brief.',
    'name="intake-mode" value="lenient"': 'Underlying rights are required; an explicit applicable AI prohibition blocks use.',
    'name="intake-mode" value="restrictive"': 'The same rights checks, plus an explicit AI approval.',
    'name="public_metadata_only"': 'Confirm that the first phase reads public notices and documentation only, within 5 MB, no patient or benchmark payloads.',
    'data-generate-intake': 'Build the intake prompt from the form. Nothing is downloaded.',
    'data-copy-intake': 'Copy the prompt for Claude Code or Codex.',
    'data-save-intake': 'Save the task as JSON to attach in Claude Code or Codex.',
    'data-copy-prompt': 'Copy this example prompt.',
    # Research studies
    'data-study-search': 'Filter the paper register by title, DOI, dataset ID or study name.',
    'data-study-search-link': 'Open the dataset search prepared for this study.',
    'data-export-study': 'Download the study JSON (brief, shortlist, annotations, browser log) for the agent to register. It grants no data permission.',
    'data-import-study': 'Load an exported study JSON into this browser. No permissions are imported.',
    'data-archive-study': 'Hide this browser’s draft of the study. The registered study is untouched.',
    'data-remove-study': 'Retire a registered study through the agent (its folder is kept), or delete a browser-only draft.',
    'data-restore-archived-study': 'Bring back the archived browser draft.',
    'data-create-study': 'Create the study in this browser and open its dataset search. Export and register it later to make it durable.',
    'name="relevance"': 'How relevant this is to the active study.',
    'name="role"': 'How the study would use it: development, benchmark, reference, or exclude.',
    'data-save-annotation': 'Keep relevance, role, tags and note for the active study in this browser. Export the study to register them.',
    'data-merge-source': 'The registered study whose facts are copied.',
    'data-merge-preview': 'Ask the local server for the dry-run plan: what would be added, what conflicts. Nothing is written.',
    'data-merge-reviewed': 'Confirm that you read the plan.',
    'data-merge-save': 'Download the merge request JSON for the agent. The merge happens only when the command runs with --apply.',
    # Licence review (dataset pages)
    'name="verdict"': 'Your reading of the terms for the selected use. A prohibition can never be overridden.',
    'name="retained_notice"': 'Fill the evidence fields from a notice the agent retained.',
    'name="review_operations"': 'Tick only the operations you reviewed; each needs its own decision.',
    'name="ai_evidence"': 'What the source states about AI use. Silence is not approval.',
    'name="evidence_url"': 'The official page that states the data licence or agreement.',
    'name="evidence_path"': 'The retained notice this decision rests on, as a project-relative path.',
    'name="reviewed_on"': 'The day you read the terms.',
    'name="review_due"': 'When the terms should be read again.',
    'name="review_assets"': 'The exact held files this decision covers.',
    'data-generate-license-review': 'Build the decision from the form. It is registered only after the agent validates it.',
    'data-copy-license-review': 'Copy the decision as a prompt for Claude Code or Codex.',
    'data-save-license-review': 'Save the decision as JSON for the agent, which validates and registers it. Saving grants nothing by itself.',
    # Quarantine
    'data-quarantine-item': 'Include this staged file in the request.',
    'name="reviewed"': 'Confirm that you reviewed the ticked files and their recorded risks.',
    'data-quarantine-save': 'Download the release request for the agent. Files move only when review_quarantine.py applies it.',
    'data-quarantine-copy': 'Copy the request as a prompt.',
    # Cite
    'data-copy-text="citation-text"': 'Copy the citation text.',
    'data-copy-text="citation-bibtex"': 'Copy the BibTeX entry.',
}

TAG = re.compile(r'<(button|select|summary|input|textarea|a)\b([^>]*)>')
TITLED_INPUTS = ('type="search"', 'type="number"', 'type="url"', 'type="date"', 'type="file"', 'type="checkbox"', 'type="radio"')


def e(value):
    return escape(str(value), quote=True)


def hint_for(attributes):
    """The HINTS text whose key appears, whole, among the tag's attributes; None when there is none."""
    padded = ' ' + attributes + ' '
    for key, text in HINTS.items():
        if re.search(r'(?<![\w-])' + re.escape(key) + r'(?![\w-])', padded):
            return text
    return None


def hinted(html):
    """Add `title` to every control HINTS knows and that has no title yet. Other tags are returned unchanged."""
    def add(match):
        tag, attributes = match.group(1), match.group(2)
        if ' title=' in attributes or (tag == 'input' and not any(t in attributes for t in TITLED_INPUTS) and 'data-' not in attributes):
            return match.group(0)
        text = hint_for(attributes)
        if text is None:
            return match.group(0)
        return f'<{tag} title="{e(text)}"{attributes}>'  # first, so the attributes other code and tests look for keep their order
    return TAG.sub(add, html)


def help_button(identifier, label, text):
    """The ⓘ toggle of the filter panel, for a heading or form: shows `text` in place; app.js wires every [data-help]."""
    return (f'<button type="button" class="infobutton" data-help="{e(identifier)}" aria-label="Explain {e(label)}" aria-controls="{e(identifier)}" '
            f'aria-expanded="false" title="What this does">ⓘ</button><div class="filterhelptext helpnote" id="{e(identifier)}" hidden>{e(text)}</div>')


def missing_hints(html):
    """Controls of a page without a title (and no explaining aria-label): what a test reports."""
    found = []
    for match in TAG.finditer(html):
        tag, attributes = match.group(1), match.group(2)
        if tag in ('input', 'textarea', 'a') or (tag == 'summary' and 'plainbutton' not in attributes):  # a plain disclosure heading explains itself
            continue
        if ' title=' in attributes or 'aria-label="Explain' in attributes or 'infobutton' in attributes:  # a ⓘ toggle is itself the explanation
            continue
        found.append(match.group(0)[:120])
    return found

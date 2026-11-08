"""Small HTML components for research projects and human annotations."""
from html import escape

def annotations(kind, identifier):
    e = lambda v: escape(str(v), quote=True)
    result = '<details class="manual-license-review"><summary>Annotate for the active research study</summary><form data-annotation-form data-annotation-kind="'+e(kind)+'" data-annotation-id="'+e(identifier)+'">'
    for name, label, choices in [('relevance', 'Relevance to this research study', ['unassessed', 'high', 'moderate', 'low']),
                                 ('role', 'Intended study role', ['unassessed', 'development', 'benchmark', 'reference', 'exclude'])]:
        result += '<label>'+label+'<select name="'+name+'">'+''.join('<option value="'+v+'">'+v.capitalize()+'</option>' for v in choices)+'</select></label>'
    result += '<label>Topic tags (comma separated)<input name="tags" placeholder="semantic-mapping, units, clinical-extraction"></label><label>Notes / selection reasoning<textarea name="note"></textarea></label><button class="plainbutton" type="submit">Save study annotation</button><p class="small" role="status" data-annotation-status></p><p class="small">Saved in this browser for the active research study. Export the study to retain/share these annotations. An annotation is not a licence approval or independent benchmark certification.</p></form></details>'
    return result

def projects():
    result = '<section class="panel"><h2>Active study: <span data-active-study-name></span></h2><pre data-active-study-brief></pre><div class="selectionbuttons"><a class="plainbutton" href="#new-study">Start a new study</a><a class="plainbutton" data-study-search-link href="add-datasets.html?discover=1">Search datasets for this study</a><a class="plainbutton" href="catalogue.html">Explore the shared catalogue</a><button type="button" class="plainbutton" data-export-study>Export study, shortlist & annotations</button><button type="button" class="plainbutton" data-archive-study>Archive browser draft</button><button type="button" class="plainbutton" data-restore-archived-study>Restore last archived draft</button><button type="button" class="plainbutton remove-action" data-remove-study>Remove study…</button></div><p>Research studies have separate questions, shortlists, filter views and annotations. Dataset files and paper records are shared. Source permissions must be reviewed for each study’s purpose.</p><p class="small">Removing a study: a browser draft is deleted here for good (export it first if you want to keep it). A registered study is removed from the registry with <code>Scripts/register_research_study.py --remove STUDY_ID --apply</code>; its reports and action log stay in <code>docs/research-studies/STUDY_ID/</code>. The default study cannot be removed.</p><p class="small">Use an HTTP preview for study switching across pages; file:// browser storage may be isolated per page. Browser drafts are local to this browser/origin. Export a backup and pass it to Claude Code or Codex to register the study with <code>Scripts/register_research_study.py --request /path/to/study.json --apply</code>. Registration grants no data permissions. Imported/exported annotations do not grant licences.</p><label>Import an exported study<input type="file" accept=".json,application/json" data-import-study></label></section>'
    result += '<section class="panel" id="new-study" style="margin:24px 0;scroll-margin-top:24px"><h2>Start a new research study</h2><form class="intake-form" data-new-study-form>'
    fields = [('id', 'Study ID', 'e.g. diabetes-lab-harmonization'), ('name', 'Study name', 'Human-readable project name'),
              ('question', 'Research question', 'What should the dataset search establish?'), ('intended_use', 'Intended data and AI use', 'Purpose, research/commercial scope and intended compute environment'),
              ('population', 'Population / domain', 'e.g. diabetes clinical studies'), ('targets', 'Target variables / tasks', 'e.g. laboratory concepts, units and event extraction'),
              ('inclusion', 'Include', 'Required data, reference mappings, comparable methods or date window'), ('exclusion', 'Exclude', 'Irrelevant modalities, missing references or unacceptable access conditions')]
    for name, label, placeholder in fields:
        result += '<label>'+label+('<input' if name in {'id', 'name'} else '<textarea')+' name="'+name+'" maxlength="'+('80' if name=='id' else '160' if name=='name' else '4000')+'" placeholder="'+placeholder+'"'+(' required' if name in {'id', 'name', 'question', 'intended_use'} else '')+('>' if name in {'id', 'name'} else '></textarea>')+'</label>'
    result += '<button class="agent-save" type="submit">Create study and start dataset search</button><p role="status" data-new-study-status></p></form></section>'
    records = ('<section class="panel" style="margin:24px 0" data-study-records><h2>Reports &amp; action log</h2>'
               '<p class="small">Reports and registered actions come from the project registry (<code>docs/research-studies/&lt;study&gt;/</code>). '
               'Actions taken in this browser are listed separately until you export the study and register it. The log records what happened; it grants no data permission.</p>'
               '<h3>Reports</h3><ul data-study-reports></ul><h3>Action log</h3><ol class="studylog" reversed data-study-log></ol></section>')
    result += records
    return result

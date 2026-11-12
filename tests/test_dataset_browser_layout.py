"""The generated Explorer keeps its layout and safe-action rules after a rebuild."""
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import re
import unittest

import subprocess
CODE_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(CODE_ROOT/'src'))
from datahunter.workspace import workspace_root
SITE=workspace_root(bootstrap=True)/'docs/dataset-browser'  # the active study's site, or the bootstrap site of a fresh install
SOURCE=CODE_ROOT/'Scripts/dataset_browser'
if not (SITE/'catalogue.html').exists():
    subprocess.run([sys.executable,str(CODE_ROOT/'Scripts/build_dataset_browser.py')],check=True,capture_output=True)


class Elements(HTMLParser):
    def __init__(self):
        super().__init__();self.elements=[]
    def handle_starttag(self,tag,attrs):
        self.elements.append((tag,dict(attrs)))


def parse(path):
    parser=Elements();html=path.read_text();parser.feed(html);return html,parser.elements


class ExplorerLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html,cls.elements=parse(SITE/'catalogue.html')
        cls.datasets=json.loads((SITE/'build-manifest.json').read_text())['datasets']

    def test_search_selection_and_table_come_before_the_filters(self):
        order=[self.html.index(marker) for marker in ['data-filter="q"','data-selection-bar','data-filter-panel','data-catalogue']]
        self.assertEqual(order,sorted(order))
        self.assertLess(self.html.index('data-facet="license_status"'),self.html.index('More filters'))

    def test_filter_panel_has_a_keyboard_accessible_resize_handle(self):
        handles=[a for tag,a in self.elements if tag=='div' and 'data-filter-resizer' in a]
        self.assertEqual(len(handles),1)
        handle=handles[0]
        self.assertEqual((handle.get('role'),handle.get('aria-orientation'),handle.get('tabindex'),handle.get('aria-controls')),('separator','vertical','0','filters'))
        self.assertTrue(int(handle['aria-valuemin'])<=int(handle['aria-valuenow'])<=int(handle['aria-valuemax']))
        self.assertLess(self.html.index('data-filter-panel'),self.html.index('data-filter-resizer'))
        self.assertLess(self.html.index('data-filter-resizer'),self.html.index('data-catalogue'))

    def test_rows_start_with_a_tick_box_and_have_no_duplicate_open_link(self):
        self.assertNotIn('Open →',self.html)
        rows=re.findall(r'<tr data-id="([^"]+)"[^>]*><td data-label="Tick"><label class="rowpick"><input type="checkbox" data-pick="([^"]+)"',self.html)
        self.assertEqual(len(rows),self.datasets)
        self.assertTrue(all(a==b for a,b in rows))
        self.assertIn('data-pick-all',self.html[:self.html.index('<tbody>')])

    def test_not_ready_states_have_nested_reasons_and_rows_link_to_the_problems_section(self):
        subs=[a for tag,a in self.elements if tag=='fieldset' and 'data-suboptions' in a]
        self.assertEqual({a['data-suboptions'] for a in subs},{'ready:needs_preparation_or_permissions','ready:incomplete'})
        for parent in ['needs_preparation_or_permissions','incomplete']:
            start=self.html.index(f'data-facet="ready" value="{parent}"');block=self.html[start:self.html.index('</fieldset>',start)]
            self.assertIn(f'data-suboptions="ready:{parent}"',block);self.assertGreaterEqual(block.count('data-facet="blocker"'),5)
        self.assertNotIn('data-facet-group="blocker"',self.html)
        links=re.findall(r'<a data-detail-link="gaps" href="datasets/([^"]+)\.html#gaps">',self.html)
        self.assertEqual(len(links),len(set(links)));self.assertLessEqual(len(links),self.datasets)
        for link in links[:1]:self.assertIn('id="gaps"',(SITE/'datasets'/(link+'.html')).read_text())

    def test_readiness_states_share_one_colour_set(self):
        if not self.datasets:self.skipTest('empty catalogue: no readiness badges to colour')
        tones=set(re.findall(r'<span class="badge (good|warn|bad|ref|off)">',self.html))
        self.assertTrue(tones,'readiness badges use the shared tone classes');self.assertTrue(tones<={'good','warn','bad','ref','off'})
        styles=self.html+''.join(p.read_text() for p in (SITE/'assets').glob('*.css'))
        for token in ['--ok','--warn','--bad','--ref']:self.assertIn(token,styles,token)  # one colour per readiness state, shared by badges and the readiness bar
        first=next(iter(sorted((SITE/'datasets').glob('*.html'))),None)
        if first:self.assertIn('class="license-status tone-',first.read_text())

    def test_rows_carry_only_their_id(self):
        rows=[a for tag,a in self.elements if tag=='tr' and 'data-id' in a]
        self.assertEqual(len(rows),self.datasets)
        self.assertEqual({k for a in rows for k in a if k.startswith('data-')},{'data-id'} if rows else set())
        self.assertNotIn('data-default-today" value=',self.html)

    def test_download_scope_is_a_single_choice(self):
        scopes=[a for tag,a in self.elements if tag=='input' and a.get('name')=='agent-download-scope']
        self.assertEqual({a['type'] for a in scopes},{'radio'})
        self.assertEqual(sum('checked' in a for a in scopes),1)

    def test_file_actions_sit_in_a_menu_and_removal_is_only_planned(self):
        menu=self.html[self.html.index('<details class="actionmenu">'):]
        menu=menu[:menu.index('</details>')]
        self.assertEqual(menu.count('data-held-action'),5)
        self.assertIn('Plan removal…',menu)
        self.assertNotIn('Remove dataset',self.html)

    def test_dataset_pages_name_their_single_dataset(self):
        pages=sorted((SITE/'datasets').glob('*.html'))
        if not pages:self.skipTest('empty catalogue: no dataset pages')
        html=pages[0].read_text()
        self.assertIn('Actions for this dataset',html)
        self.assertNotIn('Actions for selected datasets',html)

    def test_pages_load_versioned_shared_scripts_in_order(self):
        for page in SITE.rglob('*.html'):
            html,elements=parse(page)
            scripts=[a['src'].split('/')[-1] for tag,a in elements if tag=='script' and 'src' in a]
            names=[s.split('?')[0] for s in scripts]
            self.assertTrue(all('?v=' in s for s in scripts),page)
            self.assertEqual(names[:2],['theme.js','download.js'],page)
            self.assertEqual(names[-1],'app.js',page)
            self.assertEqual('selection.js' in names,'data-save-agent' in html,page)
            self.assertEqual('intake.js' in names,'data-intake-form' in html,page)

    def test_every_element_the_catalogue_script_queries_exists(self):
        # `$` is only defined inside the catalogue block of app.js, so each static `$('…')` must resolve or the page dies on load.
        script=(SOURCE/'app.js').read_text()
        selectors={s for s in re.findall(r"\$\('([^']+)'\)",script) if "'+" not in s and '+' not in s}
        self.assertGreater(len(selectors),10)
        for selector in sorted(selectors):
            tag,attr,value,cls=re.fullmatch(r"([a-z]*)(?:\[([a-z-]+)(?:=\"([^\"]*)\")?\])?(?:\.([a-z-]+))?",selector).groups()
            found=[a for t,a in self.elements if (not tag or t==tag) and (not attr or (attr in a and (value is None or a[attr]==value))) and (not cls or cls in (a.get('class') or '').split())]
            self.assertTrue(found,selector)
        payload=json.loads(re.search(r'<script type="application/json" id="selection-data">(.*?)</script>',self.html,re.S).group(1))
        counters={a['data-option-count'] for t,a in self.elements if 'data-option-count' in a}
        for record in payload['records']:
            # Study-scoped facets (relevance, role, shortlist, permission, review route) are added per study in the browser.
            for key in payload['facets']:
                if key not in record or key=='project_tags':continue
                values=record[key]
                for value in (values if isinstance(values,list) else [values]):
                    self.assertIn(f'{key}:{value}',counters,(record['id'],key))

    def test_every_control_explains_itself(self):
        """Every button, select and menu summary carries a title (or is a ⓘ toggle), and every navigation link says what its page is for."""
        import importlib.util
        spec=importlib.util.spec_from_file_location('hints',SOURCE/'hints.py');hints=importlib.util.module_from_spec(spec);spec.loader.exec_module(hints)
        for page in SITE.rglob('*.html'):
            html,elements=parse(page)
            self.assertEqual(hints.missing_hints(html),[],f'{page.name}: controls without a hint')
            nav=[a for tag,a in elements if tag=='a' and a.get('href','').endswith(('catalogue.html','research-studies.html','activity.html')) and 'aria-current' in a or (tag=='a' and a.get('aria-current')=='page')]
            self.assertTrue(all(a.get('title') for a in nav),f'{page.name}: navigation links without a title')
        self.assertIn('data-help="help-selection"',self.html,'the selection bar explains itself')
        self.assertIn('data-help="help-download-dialog"',self.html,'the download dialog explains itself')

    def test_published_assets_match_their_source_and_pages_load_nothing_external(self):
        for published in sorted(p for p in (SITE/'assets').rglob('*') if p.is_file()):  # tutorial screenshots sit in assets/tutorial/
            relative=published.relative_to(SITE/'assets')
            self.assertEqual(published.read_bytes(),(SOURCE/relative).read_bytes(),str(relative))
        for page in SITE.rglob('*.html'):
            html,elements=parse(page)
            for tag,a in elements:
                for attr in ('src','href'):
                    if tag in ('script','link','img','iframe') and attr in a:
                        self.assertFalse(re.match(r'^(https?:)?//',a[attr]),(page.name,tag,a[attr]))

    def test_tutorial_is_linked_everywhere_and_walks_the_workflow_in_order(self):
        html,elements=parse(SITE/'tutorial.html')
        steps=re.findall(r'<li class="panel tutorialstep" id="step-(\d+)">',html)
        self.assertEqual(steps,[str(i) for i in range(1,len(steps)+1)]);self.assertGreaterEqual(len(steps),8)
        for needle in ['research-studies.html','catalogue.html','add-datasets.html','downloads.html','studies.html','register_research_study.py']:self.assertIn(needle,html)
        self.assertIn('grants no data',html)
        for needle in ['resolve_full_release.py','merge_study_facts.py','structure.html','status.html','--import-from','Merge studies into a new study','id="before-you-start"']:self.assertIn(needle,html)
        shots=re.findall(r'<img src="(assets/tutorial/[^"?]+)\?v=[0-9a-f]{12}" alt="[^"]+"',html)
        self.assertGreaterEqual(len(shots),10,'the walk-through shows the pages it describes')
        for shot in shots:self.assertTrue((SITE/shot).is_file(),shot)
        self.assertIn('<svg class="schema"',html);self.assertIn('role="img"',html)
        for lane in ['In your browser','Hand-off · Claude Code or Codex','In the repository']:self.assertIn(lane,html)
        for page in SITE.rglob('*.html'):
            text=page.read_text();self.assertIn('· Data Hunter</title>',text,page);self.assertNotIn('<span>MedScrape Dataset Explorer</span>',text,page)
        # The walk-through starts with a new study and its dataset search, before the catalogue.
        order=[html.index(t) for t in ['Create a research study','Search for datasets for the study','Explore the catalogue','Prepare a download','Read the data structure report','Merge studies into a new study','Export and register the study']]
        self.assertEqual(order,sorted(order))
        mainnav=re.search(r'<nav class="wrap mainnav".*?</nav>',self.html,re.S).group(0)
        nav=[n.split('?')[0] for n in re.findall(r'href="([^"]+)"',mainnav)]
        self.assertEqual(nav[:5],['index.html','tutorial.html','research-studies.html','add-datasets.html','catalogue.html'])
        home=(SITE/'index.html').read_text()
        self.assertEqual(home.count('tutorial.html'),3,'nav, hero button and overview link; no duplicate how-to button')
        for page in SITE.rglob('*.html'):
            _,els=parse(page)
            nav=[a for tag,a in els if tag=='a' and a.get('href','').endswith('tutorial.html')]
            self.assertTrue(nav,page)
        # The header keeps the study picker labelled and the controls grouped.
        self.assertIn('id="new-study"',(SITE/'research-studies.html').read_text())
        self.assertIn('research-studies.html#new-study">New study</a>',self.html)
        self.assertIn('research-studies.html#new-study',(SITE/'index.html').read_text())
        self.assertIn('<svg class="brandlogo"',self.html);self.assertIn('rel="icon" type="image/svg+xml" href="assets/logo.svg?v=',self.html)
        self.assertIn('<label for="study-switch">',self.html);self.assertIn('class="headcontrols"',self.html);self.assertIn('class="brandblock"',self.html)

    def test_counts_are_not_hardcoded(self):
        html=(SITE/'methods.html').read_text()
        self.assertIn(f'{self.datasets} entries are not {self.datasets} independent cohorts',html)


if __name__=='__main__':unittest.main()

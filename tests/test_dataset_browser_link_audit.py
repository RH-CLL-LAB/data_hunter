"""Reachability labels must not confuse ordinary scripts with access challenges."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

CODE_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(CODE_ROOT/'src'))
from datahunter.workspace import workspace_root, resolve_snapshot
ROOT=workspace_root(default=CODE_ROOT)  # the active study folder; the code root when no study exists
HAS_CATALOGUE=(ROOT/'configs/dataset_study_usage.json').is_file() and bool(json.loads((ROOT/'configs/dataset_study_usage.json').read_text()).get('datasets'))  # empty in a fresh workspace
WORKSPACE=(ROOT/'data/interim/dataset_selection/latest.json').exists()  # local acquisition workspace, not in Git
spec=importlib.util.spec_from_file_location('browser_links',CODE_ROOT/'Scripts/audit_dataset_browser_links.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


@unittest.skipUnless(WORKSPACE,'needs the local acquisition workspace (data/interim receipts and licence evidence)')
@unittest.skipUnless(HAS_CATALOGUE,'needs a populated catalogue (these check the registered MedScrape entries)')
class LinkAuditTests(unittest.TestCase):
    def test_captcha_widget_on_ordinary_page_is_not_a_block(self):
        self.assertEqual(audit.classify(200,'Clinical data repository','<script src="captcha.js"></script>','https://example.org'),'reachable')

    def test_github_not_found_page_is_not_mislabelled_as_bot_gate(self):
        self.assertEqual(audit.classify(404,'Page not found · GitHub','captcha','https://github.com/example/missing'),'not_found')

    def test_challenge_and_login_pages_have_distinct_labels(self):
        self.assertEqual(audit.classify(200,'Checking your browser - reCAPTCHA','','https://example.org'),'access_or_bot_restricted')
        self.assertEqual(audit.classify(200,'Log in','','https://example.org/login'),'login_page')

    def test_internal_html_and_help_anchors_are_all_resolvable(self):
        pages,external,internal,issues=audit.scan()
        self.assertGreaterEqual(len(pages),10)  # home, tutorial, catalogue, studies, licences and the dataset pages
        self.assertTrue(external)
        self.assertTrue(internal)
        self.assertEqual(issues,[])


if __name__=='__main__':unittest.main()

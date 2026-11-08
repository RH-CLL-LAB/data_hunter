"""Public notice attachments and embedded study briefs retain their boundaries."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'Scripts'))
spec=importlib.util.spec_from_file_location('study_browser',ROOT/'Scripts/build_dataset_browser.py')
browser=importlib.util.module_from_spec(spec);spec.loader.exec_module(browser)

class StudyBrowserTests(unittest.TestCase):
    def test_embedded_research_brief_cannot_close_its_data_script(self):
        question='Threshold < 10; literal </script> stays text'
        study={'id':'test','name':'Fixture','question':question,'intended_use':'Interface test'}
        with patch.object(browser,'RESEARCH_STUDIES',{'studies':[study]}):
            html=browser.study_shell_data(browser.OUT/'research-studies.html')
        self.assertEqual(html.count('</script>'),1)
        payload=re.search(r'<script[^>]+>(.*?)</script>',html,re.S).group(1)
        self.assertEqual(json.loads(payload)['studies'][0]['question'],question)

    def test_only_explicitly_public_notices_are_attached_to_the_site(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();out=root/'docs/dataset-browser';out.mkdir(parents=True)
            notice=root/'notice.txt';notice.write_text('Synthetic fixture notice; no actual data grant.')
            evidence={'snapshot':'notice.txt','sha256':hashlib.sha256(notice.read_bytes()).hexdigest()}
            resource={'id':'rights','name':'Fixture rights','license':'Fixture terms','sources':[],'evidence':[evidence]}
            dataset={'id':'TEST','input_scopes':[],'associated_download_permissions':{'decisions':[{'resource':'rights'}]},'obtained_artifacts':[]}
            with patch.object(browser,'ROOT',root),patch.object(browser,'OUT',out),patch.object(browser,'LICENSES',{'resources':[resource]}):
                browser.PUBLIC_NOTICE_FILES.clear();browser.PUBLIC_NOTICE_SOURCES.clear()
                browser.dataset_license_links(dataset,out/'dataset.html')
                self.assertEqual(list(out.glob('license-notices/*')),[])
                evidence['public_notice']=True
                browser.dataset_license_links(dataset,out/'dataset.html')
                attached=list(out.glob('license-notices/*'))
                self.assertEqual(len(attached),1)
                self.assertEqual(attached[0].suffix,'.txt')
                self.assertEqual(attached[0].read_bytes(),notice.read_bytes())
                notice.write_text('Changed notice')
                with self.assertRaisesRegex(ValueError,'public_license_notice_missing_changed_or_unsafe'):
                    browser.dataset_license_links(dataset,out/'dataset.html')
            browser.PUBLIC_NOTICE_FILES.clear();browser.PUBLIC_NOTICE_SOURCES.clear()

if __name__=='__main__':unittest.main()

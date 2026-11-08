"""The repository licence and citation files stay present and consistent."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]


class ProjectLicenseTests(unittest.TestCase):
    def test_apache_license_notice_and_citation_are_present_and_agree(self):
        license_text=(ROOT/'LICENSE').read_text()
        self.assertIn('Apache License',license_text);self.assertIn('Version 2.0',license_text);self.assertIn('Copyright 2026 Nikola Tom',license_text)
        notice=(ROOT/'NOTICE').read_text()
        for needle in ['Nikola Tom','Carsten Utoft Niemann','CITATION.cff','does not cover third-party datasets']:self.assertIn(needle,notice)
        citation=(ROOT/'CITATION.cff').read_text()
        for needle in ['cff-version: 1.2.0','family-names: Tom','family-names: Niemann','license: Apache-2.0','title: "Data Hunter:']:self.assertIn(needle,citation)
        readme=(ROOT/'README.md').read_text()
        self.assertIn('## How to cite',readme);self.assertTrue('## Licence and citation' in readme or '## Licence' in readme);self.assertIn('(LICENSE)',readme);self.assertIn('(CITATION.cff)',readme)
        # The credit and licence line come before the table of contents; the full citation section exists further down.
        self.assertLess(readme.index('Developed by **Nikola Tom**'),readme.index('## Contents'));self.assertIn('Tom N, Niemann CU.',readme)

    def test_generated_site_points_at_the_licence_and_citation(self):
        html=(ROOT/'docs/dataset-browser/index.html').read_text()
        self.assertIn('Apache-2.0',html);self.assertIn('CITATION.cff',html);self.assertNotIn('id="cite"',html)
        self.assertIn('href="cite.html"',html)
        cite=(ROOT/'docs/dataset-browser/cite.html').read_text()
        self.assertIn('id="cite"',cite);self.assertIn('Tom N, Niemann CU. Data Hunter:',cite);self.assertIn('data-copy-text="citation-bibtex"',cite)
        for text in [cite,(ROOT/'CITATION.cff').read_text(),(ROOT/'NOTICE').read_text()]:self.assertIn('RH-CLL-LAB/data_hunter',text)
        health=ROOT/'docs/dataset-browser/link-health.html'
        if health.exists():self.assertNotIn('127.0.0.1',health.read_text())
        for page in (ROOT/'docs/dataset-browser').rglob('*.html'):self.assertIn('developed by Nikola Tom',page.read_text(),page)


if __name__=='__main__':unittest.main()

"""Acquisition timestamps must bind source identities and keep uncertainty explicit."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('dates',Path(__file__).parents[1]/'Scripts/dataset_browser/acquisition_dates.py')
dates=importlib.util.module_from_spec(spec);spec.loader.exec_module(dates)

class AcquisitionDatesTests(unittest.TestCase):
    def test_no_files_and_missing_dates_are_separate(self):
        self.assertEqual(dates.summarize([],{} )['label'],'Not obtained')
        r=dates.summarize([{'path':'data/2026-10-29/a','sha256':'a'}],{})
        self.assertEqual(r['unknown_files'],1)
        self.assertIn('Unknown',r['label'])

    def test_timezone_range_and_partial_coverage(self):
        files=[{'path':s,'sha256':s} for s in ['a','b','c']]
        index={(s,s):[{'at':t,'basis':'artifact_acquisition_timestamp','evidence':'lock.json'}]
               for s,t in [('a','2026-10-30T23:30:00+00:00'),('b','2026-11-02T09:00:00+00:00')]}
        r=dates.summarize(files,index)
        self.assertEqual(r['label'],'31 Oct 2026 – 02 Nov 2026 · partial dates')
        self.assertEqual(r['unknown_files'],1)

    def test_checked_receipt_is_not_an_acquisition_timestamp(self):
        index={('a','sha'):[{'at':'2026-10-29T14:37:00+00:00','basis':'receipt_held_by_only','evidence':'report.json'}]}
        r=dates.summarize([{'path':'a','sha256':'sha'}],index)
        self.assertIsNone(r['first_at'])
        self.assertEqual(r['held_by_only_files'],1)
        self.assertIn('Date unknown · recorded held by 29 Oct 2026',r['label'])

    def test_receipt_records_require_exact_locked_path_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();base=root/'data';base.mkdir()
            lock=root/'lock.json';lock.write_text(json.dumps({'files':[{'path':'a','sha256':'right'}]}))
            receipt=base/'download_manifest.json'
            receipt.write_text(json.dumps({'downloaded_at':'2026-10-29T13:33:14+00:00','files':[{'path':'a','sha256':'wrong'}]}))
            index,sources=dates.date_index(root,{'lock.json':base})
            self.assertEqual(index,{})
            receipt.write_text(json.dumps({'downloaded_at':'2026-10-29T13:33:14+00:00','files':[{'path':'a','sha256':'right'}]}))
            index,sources=dates.date_index(root,{'lock.json':base})
            self.assertEqual(index[('data/a','right')][0]['basis'],'download_batch_timestamp')
            self.assertIn(receipt,sources)

    def test_unknown_timezone_and_invalid_dates_are_not_invented(self):
        self.assertIsNone(dates.timestamp('2026-10-29T12:00:00'))
        self.assertIsNone(dates.timestamp('invalid'))

if __name__=='__main__':unittest.main()

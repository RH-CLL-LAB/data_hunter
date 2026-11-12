#!/usr/bin/env python3
"""Shrink a study's download-review file by storing each approval evidence list once.

Full-release registrations used to copy the release's whole evidence list onto every approved file, so a study holding
25,000 files had a configs/dataset_download_reviews.json of about 1 GB. This rewrites it in the shared form the
registration scripts now write: each distinct list is kept once under `evidence_sets` (named by its SHA-256) and each
approval names its set. Nothing else changes; every approval is checked to rest on exactly the evidence it rested on
before the file is replaced. Changes no approval, licence record or permission.

Select the study as for any script (DATA_HUNTER_WORKSPACE or the active study). --dry-run reports without writing.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_resources import approval_evidence, share_evidence, write_json  # noqa: E402

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, require_workspace, workspace_study_id  # noqa: E402
from datahunter.research_studies import append_log  # noqa: E402

ROOT = workspace_root(default=CODE_ROOT)


def compact(root, write=True):
    """Rewrite the review file with shared evidence; returns sizes and counts. Refuses if any approval would change."""
    path = Path(root) / 'configs/dataset_download_reviews.json'
    if not path.is_file():
        return {'file': None}
    before = path.stat().st_size
    reviews = json.loads(path.read_text())
    approvals = reviews.get('approved_artifacts', [])
    original = [approval_evidence(reviews, a) for a in approvals]
    share_evidence(reviews)
    if [approval_evidence(reviews, a) for a in approvals] != original:
        raise ValueError('shared_evidence_differs_from_the_original_approvals')
    result = {'file': str(path.relative_to(root)), 'approvals': len(approvals), 'evidence_sets': len(reviews.get('evidence_sets', {})), 'bytes_before': before}
    if write:
        write_json(path, reviews)
        result['bytes_after'] = path.stat().st_size
    return result


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true', help='report what would change without writing')
    args = parser.parse_args(argv)
    result = compact(root, write=not args.dry_run)
    study = workspace_study_id(root)
    if result.get('bytes_after') is not None and result['bytes_after'] != result['bytes_before'] and study:
        append_log(root, study, [{'at': datetime.now(timezone.utc).isoformat(), 'action': 'note', 'source': 'compact_download_reviews.py',
                                  'detail': f"Download-review file stored with shared evidence: {result['approvals']:,} approvals now name {result['evidence_sets']} evidence lists "
                                            f"({result['bytes_before']:,} → {result['bytes_after']:,} bytes); no approval, licence record or permission changed."}])
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    require_workspace(ROOT)
    main()

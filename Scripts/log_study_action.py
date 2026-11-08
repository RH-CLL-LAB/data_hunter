#!/usr/bin/env python3
"""Append one action to a registered research study's log (studies/<id>/actions.jsonl).

Agents call this after an intake/discovery run, a licence-review registration or any other
study-scoped action. The log records what happened; it grants no data permission."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, require_workspace
ROOT = workspace_root(default=CODE_ROOT)
from datahunter.research_studies import LOG_ACTIONS, append_log, registered


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True)
    parser.add_argument('--action', required=True, choices=sorted(LOG_ACTIONS))
    parser.add_argument('--detail', required=True)
    parser.add_argument('--run', help='Run folder or evidence path for this action')
    parser.add_argument('--at', help='ISO time of the action (defaults to now, UTC)')
    args = parser.parse_args(argv)
    if not registered(root, args.study):
        raise SystemExit('register_the_research_study_first')
    event = {'at': args.at or datetime.now(timezone.utc).isoformat(), 'action': args.action,
             'detail': args.detail, 'source': 'log_study_action.py'}
    if args.run:event['run'] = args.run
    added = append_log(root, args.study, [event])
    print(json.dumps({'study': args.study, 'added': len(added)}))
    return added


if __name__ == '__main__':
    require_workspace(ROOT)  # a clear message instead of a missing-file error when no study is active
    main()

#!/usr/bin/env python3
"""List study workspaces and choose the active one.

    python Scripts/study.py list
    python Scripts/study.py use harmonization-test
    python Scripts/study.py current

Each study lives in studies/<id>/ with its own registries, runs, data, reports,
action log and generated site. Studies are created by registering an exported
brief with Scripts/register_research_study.py; this command only switches.
"""
import argparse
import json
from pathlib import Path
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import active_study_id, list_studies, set_active, study_path, CODE_ROOT  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='List study workspaces')
    use = sub.add_parser('use', help='Make a study the active workspace')
    use.add_argument('study_id')
    sub.add_parser('current', help='Show the active study and its folder')
    args = parser.parse_args(argv)
    if args.command == 'list':
        active = active_study_id()
        print(json.dumps({'studies': [{'id': s, 'path': str(study_path(s).relative_to(CODE_ROOT)), 'active': s == active} for s in list_studies()]}, indent=2))
    elif args.command == 'use':
        folder = set_active(args.study_id)
        print(json.dumps({'active_study': args.study_id, 'workspace': str(folder)}))
    else:
        active = active_study_id()
        print(json.dumps({'active_study': active, 'workspace': str(study_path(active)) if active else None}))


if __name__ == '__main__':
    main()

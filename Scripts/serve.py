#!/usr/bin/env python3
"""Serve the Data Hunter site of the active study on localhost.

    python Scripts/serve.py            # http://127.0.0.1:8767/
    python Scripts/serve.py 8080 --bind 0.0.0.0

The repository folder is served so that evidence links (notices, receipts, licence
files) resolve; "/" redirects to the active study's site, and /api/panel answers the
site's Activity page with the running scripts and every study's milestones; /api/merge-plan?source=ID&target=ID
answers the Research studies page's merge control with the dry-run plan of a facts merge, and
/api/merge-plan?sources=A,B with what each study would bring to a merged study (nothing is written). The active study is
resolved on every request, so switching studies with `Scripts/study.py use <id>`
needs no restart. With no study yet, the bootstrap site is served.
"""
import argparse
import http.server
from pathlib import Path
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import CODE_ROOT, site_dir, workspace_root  # noqa: E402
from datahunter.workspace import valid_id  # noqa: E402
import json  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402
sys.path.insert(0, str(CODE_ROOT / 'Scripts'))
import importlib  # noqa: E402
import merge_study_facts  # noqa: E402
import datahunter.panel as panel  # noqa: E402

_LOADED = {}


def fresh(module):
    """The module, reloaded when its source changed since it was loaded: an edit to the merge or panel code
    reaches the API without restarting the server (a change to this file itself still needs a restart)."""
    path = Path(module.__file__)
    stamp = path.stat().st_mtime_ns
    if _LOADED.setdefault(module.__name__, stamp) != stamp:
        module = importlib.reload(module)
        _LOADED[module.__name__] = stamp
    return module


def site_location():
    root = workspace_root(bootstrap=True)
    site = site_dir(root)
    if not site.is_dir():
        raise SystemExit('site_not_built_run_Scripts/build_dataset_browser.py_first: ' + str(site))
    if root.is_relative_to(CODE_ROOT):
        return CODE_ROOT, '/' + site.relative_to(CODE_ROOT).as_posix() + '/'
    return root, '/' + site.relative_to(root).as_posix() + '/'


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.base, self.site = site_location()
        super().__init__(*args, directory=str(self.base), **kwargs)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self.send_response(302)
            self.send_header('Location', self.site)
            self.end_headers()
            return
        parts = urlsplit(self.path)
        if parts.path == '/api/panel':  # the site's Activity page: running scripts, study milestones, what is missing
            return self.reply_json(fresh(panel).panel_model())
        if parts.path == '/api/merge-plan':  # the Research studies page's Merge control: the dry run of a facts merge between two registered studies
            query = parse_qs(parts.query)
            source, target = (query.get('source') or [''])[0], (query.get('target') or [''])[0]
            sources = [x for x in (query.get('sources') or [''])[0].split(',') if x]
            try:
                if sources:  # “Merge studies into a new study”: each source's inventory and the ids they share
                    if not all(valid_id(x) for x in sources):
                        raise ValueError('invalid_study_id')
                    return self.reply_json(fresh(merge_study_facts).merged_study_preview(sources))
                if not (valid_id(source) and valid_id(target)):
                    raise ValueError('invalid_study_id')
                return self.reply_json(fresh(merge_study_facts).plan_json(source, target))
            except (ValueError, SystemExit) as error:
                return self.reply_json({'error': str(error)}, 400)
        super().do_GET()

    def reply_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('port', nargs='?', type=int, default=8767)
    parser.add_argument('--bind', default='127.0.0.1')
    args = parser.parse_args(argv)
    base, site = site_location()
    print(f'Serving {base} at http://{args.bind}:{args.port}{site}', flush=True)
    http.server.ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()

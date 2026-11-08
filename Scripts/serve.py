#!/usr/bin/env python3
"""Serve the Data Hunter site of the active study on localhost.

    python Scripts/serve.py            # http://127.0.0.1:8767/
    python Scripts/serve.py 8080 --bind 0.0.0.0

The repository folder is served so that evidence links (notices, receipts, licence
files) resolve; "/" redirects to the active study's site. The active study is
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
        super().do_GET()

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

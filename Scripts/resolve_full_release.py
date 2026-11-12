#!/usr/bin/env python3
"""Resolve a scope=full download request into exact-artifact jobs, register them and, when asked, acquire them.

For every dataset in the saved instructions the resolver first applies the access route recorded in the catalogue:
registration, agreement or no-route datasets are reported as blocked without touching the provider. For the others it
reads every supported source URL's own release manifest and keeps it as evidence: PhysioNet (SHA256SUMS.txt with HEAD
sizes, or the public file index when no checksums are published), Zenodo (record files with MD5 and size), GitHub
(latest release assets with SHA-256 digests plus the tagged tree with git blob identities; the default-branch head when
there is no release), Hugging Face dataset repositories (per-file SHA-256 or blob identities at a pinned revision), OpenML
(dataset descriptions with MD5 and HEAD sizes, suites expanded to their datasets) and UCI (dataset pages with their zip
downloads and licence statement; no checksums). Source URLs on other hosts (Kaggle, portals behind a login, cloud-drive
links) are listed as unsupported components; a release with such components can never be labelled complete.

Each resolved release then passes the same gates as any acquisition: the complete package must fit the saved per-dataset,
per-artifact and total byte limits (a larger release is size-blocked, never trimmed); every job needs a provider identity
and an exact size, or the matching technical-risk acknowledgement; and each file needs an allowed licence record for the
dataset in the saved mode and study (a record may limit itself to exact URLs with `covers_urls`; software-only records
bind nothing; code releases travel as bounded references). `--register` writes the bindable jobs, relevance decisions,
exact-artifact approvals and licence-asset identities into the registries and labels the dataset partial when files
stay unbound or unsupported; `--execute` transfers the registered jobs: files with provider identities go straight into
the locked holdings, acknowledged-risk files are staged in quarantine for the human release step. Every run is kept under
runs/full_release/<run>/ and logged to the study. Downloading grants no AI-use permission.

A release the user obtained themselves (after credentialing, an agreement, or from a provider without an automated route)
enters through `--import-from ID=FOLDER`: the folder stands in for the provider manifest, identities come from the
SHA256SUMS or MD5SUMS file the download brought along (without one the files need the missing-checksum acknowledgement),
and the files are copied in under the same approval, identity, size and licence gates as a download.

Exact files of a provider without an automated route, read by hand from its own pages, enter through
`--file-list ID=FILE`: a JSON list of the provider's download URLs (with the provider's sizes and checksums where it
publishes them) stands in for the provider manifest. The files keep their real URLs and are downloaded and checked like
any resolved file; the list and the provider pages it names are kept as evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from urllib.parse import quote, urlsplit
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_resources import contained, digest, write_json, write_reviews  # noqa: E402
import acquire_research_resources as acquisition  # noqa: E402
import run_download_agent as agent  # noqa: E402
from review_quarantine import refresh_dataset_entries  # noqa: E402

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT / 'src'))
from datahunter.workspace import workspace_root, require_workspace, study_path  # noqa: E402
from datahunter.data_rights import LicenseRegistry  # noqa: E402
from datahunter.research_studies import default_study, append_log  # noqa: E402

ROOT = workspace_root(default=CODE_ROOT)
USER_AGENT = 'data-hunter-full-release-resolver/1.0'
MANIFEST_LIMIT = 16 * 1024**2        # a provider manifest or listing larger than this is not read
INDEX_PAGE_LIMIT = 1000              # bounded crawl of a public file index
REFERENCE_LIMIT = 5 * 1024**2        # the bounded-reference path for code releases
REFERENCE_CATEGORIES = {'standards_and_methods'}
CHECKS = ['clinical_content', 'subject_and_grain', 'value_units_codes', 'time_semantics', 'format_and_size', 'duplicate_overlap', 'test_gap', 'access']
ROUTE_BLOCKS = ('registration', 'no_public_access_route')


def size_cap(advertised):
    """A job's transfer cap from the provider's advertised size; the acquisition gate classifies by this cap."""
    return int(advertised * 1.2) + 4096


def now():
    return datetime.now(timezone.utc).isoformat()


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def read(path):
    return json.loads(Path(path).read_text())


def slug(value):
    return re.sub(r'[^a-z0-9]+', '_', str(value).lower()).strip('_')


def ext(name):
    base = name.rsplit('/', 1)[-1]
    return base.rsplit('.', 1)[-1].lower().replace('gz', 'gzip') if '.' in base else 'bin'


# ----------------------------------------------------------------------------------------------------- network
TRANSIENT_STATUSES = {429, 502, 503, 504}


def fetch(url, *, limit=MANIFEST_LIMIT, method='GET', attempts=3):
    """Bounded HTTPS request; returns (status, final_url, headers, body or None).

    A provider's momentary 5xx, 429 or dropped connection is retried a few times with a short pause, so one
    unlucky request does not fail a release manifest of a hundred; a persistent error is returned or raised as before.
    """
    if urlsplit(url).scheme != 'https':
        raise ValueError('https_required: ' + url)
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json, text/html, text/plain, */*'}
    if urlsplit(url).netloc == 'api.github.com' and os.environ.get('GITHUB_TOKEN'):  # optional: raises the API rate limit; never written anywhere
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    request = urllib.request.Request(url, method=method, headers=headers)
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = None if method == 'HEAD' else response.read(limit + 1)
                if body is not None and len(body) > limit:
                    raise ValueError('provider_manifest_exceeds_limit: ' + url)
                return response.status, response.geturl(), dict(response.headers), body
        except urllib.error.HTTPError as error:
            if error.code not in TRANSIENT_STATUSES or attempt == attempts:
                return error.code, url, dict(error.headers or {}), None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts:
                raise
        time.sleep(5 * attempt)


def head_sizes(urls, workers=3):
    """Exact Content-Length per URL (None when the provider does not say or encodes the body)."""
    def one(url):
        status, final, headers, _ = fetch(url, method='HEAD')
        length = headers.get('Content-Length')
        return url, (int(length) if status == 200 and length and not headers.get('Content-Encoding') else None)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, urls))


# ---------------------------------------------------------------------------------------------------- providers
def detect(urls):
    """The first supported provider target among the URLs, or None."""
    targets, _ = detect_all(urls)
    return targets[0] if targets else None


REFERENCE_HOSTS = {'arxiv.org', 'www.arxiv.org', 'www.nature.com', 'dl.acm.org', 'ieeexplore.ieee.org', 'pubmed.ncbi.nlm.nih.gov', 'www.biorxiv.org', 'www.medrxiv.org'}


def is_reference(url):
    """Publication and notice links listed beside a dataset: not release components, never counted as unsupported."""
    u = urlsplit(url)
    host, path = u.netloc.lower(), u.path.rstrip('/')
    if host in REFERENCE_HOSTS:
        return True
    if host == 'doi.org' and not re.fullmatch(r'/10\.5281/zenodo\.\d+', path, re.I):
        return True
    return host == 'physionet.org' and '/view-license/' in path


def detect_all(urls):
    """Every supported provider target among the URLs (deduplicated), the URLs no resolver supports, and publication links set aside."""
    targets, unsupported, seen = [], [], set()
    for url in urls:
        if is_reference(url):
            continue
        u = urlsplit(url)
        host, path = u.netloc.lower(), u.path.rstrip('/')
        target = None
        if host == 'doi.org' and (m := re.fullmatch(r'/10\.5281/zenodo\.(\d+)', path, re.I)):
            target = {'provider': 'zenodo', 'record': m[1]}
        elif host == 'physionet.org' and (m := re.fullmatch(r'/content/([^/]+)/([^/]+)', path)):
            target = {'provider': 'physionet', 'project': m[1], 'version': m[2]}
        elif host in {'zenodo.org', 'www.zenodo.org'} and (m := re.fullmatch(r'/(?:records?|record)/(\d+)', path)):
            target = {'provider': 'zenodo', 'record': m[1]}
        elif host in {'github.com', 'www.github.com'} and (m := re.fullmatch(r'/([^/]+)/([^/]+)', path)):
            target = {'provider': 'github', 'owner': m[1], 'repo': m[2].removesuffix('.git')}
        elif host in {'huggingface.co', 'www.huggingface.co'} and (m := re.fullmatch(r'/datasets/([^/]+)/([^/]+)', path)):
            target = {'provider': 'huggingface', 'org': m[1], 'name': m[2]}
        elif host in {'www.openml.org', 'openml.org'} and (m := re.fullmatch(r'/d/(\d+)', path)):
            target = {'provider': 'openml', 'kind': 'data', 'id': m[1]}
        elif host in {'www.openml.org', 'openml.org'} and (m := re.fullmatch(r'/s/(\d+)', path)):
            target = {'provider': 'openml', 'kind': 'study', 'id': m[1]}
        elif host == 'archive.ics.uci.edu' and (re.fullmatch(r'/dataset/\d+(/[^/]*)?', path) or path.startswith('/ml/datasets/')):
            target = {'provider': 'uci', 'page': url}
        if target is None:
            unsupported.append(url)
            continue
        key = json.dumps({k: v for k, v in target.items()}, sort_keys=True)
        if key not in seen:
            seen.add(key)
            targets.append({**target, 'source': url})
    return targets, unsupported


def file_entry(path, url, size, fmt=None, **identity):
    return {'path': path, 'url': url, 'bytes': size, 'format': fmt or ext(path), **{k: v for k, v in identity.items() if v}}


def resolve_physionet(target, evidence):
    base = f"https://physionet.org/files/{target['project']}/{target['version']}/"
    name = f"PhysioNet {target['project']} {target['version']}"
    status, final, headers, body = fetch(base + 'SHA256SUMS.txt')
    if status in {401, 403} or '/login' in final or (status == 200 and body and b'<html' in body[:300].lower()):
        return {'blocked': 'blocked_access_credentialed', 'release': name}
    if status == 200 and body:
        evidence.append((f"physionet_{target['project']}_{target['version']}_SHA256SUMS.txt", body, base + 'SHA256SUMS.txt'))
        entries = [tuple(reversed(line.split(None, 1))) for line in body.decode('utf-8', 'replace').splitlines() if line.strip()]
        entries = [(p.strip(), c) for p, c in entries]
        sizes = head_sizes([base + quote(p) for p, _ in entries])
        files = [file_entry(p, base + quote(p), sizes.get(base + quote(p)), provider_sha256=c) for p, c in entries]
        return {'release': name, 'manifest': 'SHA256SUMS.txt', 'files': files, 'folder': f"{slug(target['project'])}_{slug(target['version'])}"}
    if status != 404:
        return {'blocked': f'manifest_unavailable_http_{status}', 'release': name}
    # Open release without published checksums: list the public index recursively; sizes are exact, identities are not.
    listed, pages, queue = [], 0, ['']
    while queue:
        rel = queue.pop(0)
        st, _, _, page = fetch(base + rel)
        pages += 1
        if st != 200 or not page:
            return {'blocked': f'file_index_unavailable_http_{st}', 'release': name}
        evidence.append((f"physionet_{target['project']}_{target['version']}_index_{slug(rel) or 'root'}.html", page, base + rel))
        for href, size in re.findall(r'<a href="([^"]+)">[^<]*</a>\s+\d{2}-\w{3}-\d{4} \d{2}:\d{2}\s+(\d+|-)', page.decode('utf-8', 'replace')):
            if href == '../':
                continue
            if href.endswith('/'):
                queue.append(rel + href)
            else:
                listed.append((rel + urllib.request.unquote(href), int(size)))
        if pages > INDEX_PAGE_LIMIT:
            return {'blocked': 'file_index_too_large_to_list', 'release': name}
    return {'release': name, 'manifest': 'public file index (no checksums published)', 'files': [file_entry(p, base + quote(p), s) for p, s in listed],
            'folder': f"{slug(target['project'])}_{slug(target['version'])}"}


def resolve_zenodo(target, evidence):
    url = f"https://zenodo.org/api/records/{target['record']}"
    name = f"Zenodo record {target['record']}"
    status, _, _, body = fetch(url)
    if status != 200 or not body:
        return {'blocked': f'record_unavailable_http_{status}', 'release': name}
    evidence.append((f"zenodo_{target['record']}_record.json", body, url))
    record = json.loads(body)
    if record.get('metadata', {}).get('access_right') != 'open':
        return {'blocked': 'blocked_access_' + str(record.get('metadata', {}).get('access_right')), 'release': name}
    files = [file_entry(f['key'], f"https://zenodo.org/api/records/{target['record']}/files/{quote(f['key'])}/content", f.get('size'),
                        provider_md5=f['checksum'].split(':', 1)[1] if str(f.get('checksum', '')).startswith('md5:') else None) for f in record.get('files', [])]
    version = record.get('metadata', {}).get('version')
    return {'release': name + (f' version {version}' if version else ''), 'manifest': 'Zenodo record file list', 'files': files, 'folder': f"zenodo_{target['record']}",
            'licence_label': (record.get('metadata', {}).get('license') or {}).get('id')}


def resolve_github(target, evidence):
    api = 'https://api.github.com/repos/'
    status, _, headers, body = fetch(f"{api}{target['owner']}/{target['repo']}")
    if status in {403, 429} and headers.get('X-RateLimit-Remaining') == '0':
        reset = headers.get('X-RateLimit-Reset')
        when = datetime.fromtimestamp(int(reset), timezone.utc).isoformat() if reset and reset.isdigit() else 'later'
        return {'blocked': f'github_api_rate_limited_retry_after_{when}', 'release': f"GitHub {target['owner']}/{target['repo']}"}
    if status != 200 or not body:
        return {'blocked': f'repository_unavailable_http_{status}', 'release': f"GitHub {target['owner']}/{target['repo']}"}
    repo = json.loads(body)
    full = repo['full_name']
    evidence.append((f"github_{slug(full)}_repo.json", body, f"{api}{target['owner']}/{target['repo']}"))
    status, _, _, body = fetch(f'{api}{full}/releases/latest')
    assets, tag, commit = [], None, None
    if status == 200 and body:
        release = json.loads(body)
        evidence.append((f'github_{slug(full)}_release_latest.json', body, f'{api}{full}/releases/latest'))
        tag = release['tag_name']
        st, _, _, ref = fetch(f'{api}{full}/git/ref/tags/{quote(tag)}')
        if st == 200 and ref:
            ref = json.loads(ref)
            commit = ref['object']['sha']
            if ref['object']['type'] == 'tag':  # annotated tag: dereference to the commit
                st2, _, _, tagobj = fetch(ref['object']['url'])
                if st2 == 200 and tagobj:
                    commit = json.loads(tagobj)['object']['sha']
        for a in release.get('assets', []):
            digest_value = str(a.get('digest') or '')
            assets.append(file_entry(f"release_assets/{a['name']}", a['browser_download_url'], a.get('size'), provider_sha256=digest_value.split(':', 1)[1] if digest_value.startswith('sha256:') else None))
    if commit is None:
        st, _, _, head = fetch(f"{api}{full}/commits/{quote(repo['default_branch'])}")
        if st != 200 or not head:
            return {'blocked': f'commit_unavailable_http_{st}', 'release': f'GitHub {full}'}
        commit = json.loads(head)['sha']
    status, _, _, body = fetch(f'{api}{full}/git/trees/{commit}?recursive=1')
    if status != 200 or not body:
        return {'blocked': f'tree_unavailable_http_{status}', 'release': f'GitHub {full} {tag or commit[:12]}'}
    tree = json.loads(body)
    if tree.get('truncated'):
        return {'blocked': 'tree_truncated_by_provider', 'release': f'GitHub {full} {tag or commit[:12]}'}
    evidence.append((f"github_{slug(full)}_tree_{commit[:12]}.json", body, f'{api}{full}/git/trees/{commit}?recursive=1'))
    files = assets + [file_entry(f"source/{t['path']}", f"https://raw.githubusercontent.com/{full}/{commit}/{quote(t['path'])}", t.get('size'), git_blob_sha1=t['sha'])
                      for t in tree['tree'] if t['type'] == 'blob']
    return {'release': f'GitHub {full} ' + (f'release {tag}' if tag else 'default branch head') + f' (commit {commit[:12]})', 'manifest': 'release assets and tagged tree',
            'files': files, 'folder': f"{slug(full)}_{slug(tag) if tag else commit[:12]}", 'licence_label': (repo.get('license') or {}).get('spdx_id')}


def resolve_huggingface(target, evidence):
    url = f"https://huggingface.co/api/datasets/{target['org']}/{target['name']}?blobs=true"
    name = f"Hugging Face {target['org']}/{target['name']}"
    status, _, _, body = fetch(url)
    if status in {401, 403}:
        return {'blocked': 'blocked_access_gated', 'release': name}
    if status != 200 or not body:
        return {'blocked': f'repository_unavailable_http_{status}', 'release': name}
    info = json.loads(body)
    if info.get('gated') or info.get('private'):
        return {'blocked': 'blocked_access_gated', 'release': name}
    evidence.append((f"huggingface_{slug(target['org'])}_{slug(target['name'])}_{info['sha'][:12]}.json", body, url))
    files = []
    for s in info.get('siblings', []):
        lfs = s.get('lfs') or {}
        files.append(file_entry(s['rfilename'], f"https://huggingface.co/datasets/{target['org']}/{target['name']}/resolve/{info['sha']}/{quote(s['rfilename'])}",
                                lfs.get('size') or s.get('size'), provider_sha256=lfs.get('sha256') or lfs.get('oid'), git_blob_sha1=None if lfs else s.get('blobId')))
    return {'release': f"{name} revision {info['sha'][:12]}", 'manifest': 'repository file list at the pinned revision', 'files': files,
            'folder': f"hf_{slug(target['org'])}_{slug(target['name'])}", 'licence_label': (info.get('cardData') or {}).get('license')}


def resolve_openml(target, evidence):
    ids = [target['id']]
    name = f"OpenML {'suite' if target['kind'] == 'study' else 'dataset'} {target['id']}"
    if target['kind'] == 'study':
        url = f"https://www.openml.org/api/v1/json/study/{target['id']}"
        status, _, _, body = fetch(url)
        if status != 200 or not body:
            return {'blocked': f'suite_unavailable_http_{status}', 'release': name}
        evidence.append((f"openml_study_{target['id']}.json", body, url))
        ids = [str(i) for i in json.loads(body)['study']['data']['data_id']]
    files, labels = [], Counter()
    for did in ids:
        url = f'https://www.openml.org/api/v1/json/data/{did}'
        status, _, _, body = fetch(url)
        if status != 200 or not body:
            return {'blocked': f'dataset_{did}_unavailable_http_{status}', 'release': name}
        evidence.append((f'openml_data_{did}.json', body, url))
        d = json.loads(body)['data_set_description']
        labels[d.get('licence')] += 1
        files.append(file_entry(f"openml_{did}_{slug(d['name'])}_v{d['version']}.{str(d.get('format', 'arff')).lower()}", d['url'], None, provider_md5=d.get('md5_checksum')))
    sizes = head_sizes([f['url'] for f in files])
    for f in files:
        f['bytes'] = sizes.get(f['url'])
    return {'release': f'{name} ({len(files)} datasets)', 'manifest': 'OpenML dataset descriptions', 'files': files, 'folder': f"openml_{target['kind']}_{target['id']}", 'licence_label': dict(labels)}


def resolve_uci(target, evidence):
    status, final, _, body = fetch(target['page'])
    if status != 200 or not body:
        return {'blocked': f'dataset_page_unavailable_http_{status}', 'release': 'UCI ' + target['page']}
    m = re.search(r'/dataset/(\d+)(?:/([^/?#]*))?', urlsplit(final).path)
    if not m:
        return {'blocked': 'uci_dataset_id_not_resolved', 'release': 'UCI ' + target['page']}
    dataset_id = m[1]
    evidence.append((f'uci_{dataset_id}_page.html', body, final))
    text = body.decode('utf-8', 'replace')
    links = sorted({l for l in re.findall(r'href="(/static/public/' + dataset_id + r'/[^"]+)"', text)})
    if not links:
        return {'blocked': 'uci_download_link_not_found', 'release': f'UCI dataset {dataset_id}'}
    urls = ['https://archive.ics.uci.edu' + l for l in links]
    sizes = head_sizes(urls)
    licence = re.search(r'licensed under a ([^.<]+?) license', text)
    files = [file_entry(urllib.request.unquote(l.rsplit('/', 1)[-1]), u, sizes.get(u)) for l, u in zip(links, urls)]
    return {'release': f'UCI dataset {dataset_id}', 'manifest': 'UCI dataset page download links (no checksums published)', 'files': files, 'folder': f'uci_{dataset_id}',
            'licence_label': licence[1].strip() if licence else None}


RESOLVERS = {'physionet': resolve_physionet, 'zenodo': resolve_zenodo, 'github': resolve_github, 'huggingface': resolve_huggingface, 'openml': resolve_openml, 'uci': resolve_uci}


def resolve_all(targets, unsupported, evidence):
    """Resolve every supported source of a dataset; files carry their component folder, unsupported sources stay listed.

    A source that fails (a provider error or the GitHub rate limit) while others resolve is listed in `unresolved_sources`:
    the release is then partial, never labelled complete from the sources that happened to answer."""
    components = []
    for target in targets:
        try:
            result = RESOLVERS[target['provider']](target, evidence)
        except Exception as error:
            result = {'blocked': 'provider_error: ' + str(error)[:200], 'release': target.get('source')}
        components.append({'target': target, **result})
    resolved = [c for c in components if not c.get('blocked')]
    files = [{**f, 'folder': c['folder']} for c in resolved for f in c['files']]
    return {'components': [{k: v for k, v in c.items() if k != 'files'} for c in components], 'files': files, 'unsupported_urls': unsupported,
            'release': '; '.join(c['release'] for c in resolved) or None, 'manifest': '; '.join(c['manifest'] for c in resolved) or None,
            'licence_label': {c['target'].get('source'): c.get('licence_label') for c in resolved if c.get('licence_label') is not None},
            'unresolved_sources': [{'source': c['target'].get('source'), 'reason': c['blocked']} for c in components if c.get('blocked')] if resolved else [],
            'blocked': None if resolved else (components[0]['blocked'] if components else 'unsupported_provider_manual_route')}


CHECKSUM_FILES = {'sha256sums.txt': 'provider_sha256', 'sha256sums': 'provider_sha256', 'md5sums.txt': 'provider_md5', 'md5sums': 'provider_md5'}


def resolve_folder(d, folder, evidence):
    """A release the user obtained themselves, read from a local folder instead of the provider.

    Every regular file is a release file; provider identities come from a SHA256SUMS or MD5SUMS file at the folder root
    (retained as evidence), so the copy is verified against the provider's own hashes. Without one the files carry local
    identities only and need the missing-checksum acknowledgement, exactly like an index-only release. A file the checksum
    file lists but the folder lacks blocks the import: a full release is complete or it is not imported.
    """
    folder = Path(folder)
    files = sorted(p for p in folder.rglob('*') if p.is_file() and not p.is_symlink())
    identities, manifest, key = {}, None, None
    for p in files:
        if p.parent == folder and p.name.lower() in CHECKSUM_FILES:
            manifest, key = p, CHECKSUM_FILES[p.name.lower()]
            length = 64 if key == 'provider_sha256' else 32
            for line in p.read_text('utf-8', 'replace').splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and re.fullmatch(r'[0-9a-fA-F]{%d}' % length, parts[0]):
                    identities[parts[1].strip().lstrip('*').removeprefix('./')] = parts[0].lower()
            evidence.append((f"import_{slug(d['id'])}_{p.name}", p.read_bytes(), 'file://' + str(p)))
            break
    present = {p.relative_to(folder).as_posix(): p for p in files if p != manifest}
    missing = sorted(set(identities) - set(present))
    name = f'user-obtained release imported from {folder}'
    if not present:
        return {'blocked': 'import_folder_empty', 'release': name}
    if missing:
        return {'blocked': f'import_folder_incomplete_{len(missing)}_listed_files_missing', 'release': name, 'missing_files': missing[:20]}
    target = detect(d.get('source_urls', []))
    if target and target['provider'] == 'physionet':
        base, component = f"https://physionet.org/files/{target['project']}/{target['version']}/", f"{slug(target['project'])}_{slug(target['version'])}"
    else:
        source = next((u for u in d.get('source_urls', []) if u.startswith('https://')), None)
        if not source:
            return {'blocked': 'import_needs_an_https_source_url_in_the_catalogue', 'release': name}
        base, component = source.rstrip('/') + '/', slug(d['id'])
    entries, sources = [], {}
    for rel, p in present.items():
        url = base + quote(rel)
        entries.append({**file_entry(rel, url, p.stat().st_size, **({key: identities[rel]} if rel in identities else {})), 'folder': component})
        sources[url] = {'path': p, 'bytes': p.stat().st_size, 'sha256': digest(p), 'folder': str(folder)}
    return {'release': f'{name} ({len(entries)} files)', 'manifest': f'{manifest.name} supplied with the files' if manifest else 'none: local SHA-256 identities only (no checksum file supplied)',
            'files': entries, 'folder': component, 'unsupported_urls': [], 'components': None, 'licence_label': None, 'imported_from': str(folder), 'import_sources': sources,
            'checksum_file_entries': len(identities), 'files_without_provider_identity': sum(1 for rel in present if rel not in identities)}


FILE_LIST_LIMIT = 5000      # files in one hand-prepared list
FILE_LIST_PAGES = 100       # provider pages one list may cite
IDENTITY_FORMATS = {'md5': ('provider_md5', 32), 'sha256': ('provider_sha256', 64), 'git_blob_sha1': ('git_blob_sha1', 40)}


def resolve_file_list(d, path, evidence):
    """Exact provider files a person listed by hand, for a provider without an automated route.

    The list is JSON: `dataset_id`, a `release` label, `prepared_from` (the provider pages the list was read from) and
    `files`, each with the provider's https `url`, the `path` it is kept under and, where the provider publishes them,
    `bytes` and one of `md5`, `sha256` or `git_blob_sha1`; optionally `left_out`, sources of the release the list cannot
    cover (a member-only file, say), each with its `source` URL and `reason`, which keep the dataset partial. It stands in
    for the provider manifest: the list is kept as
    evidence and the `prepared_from` pages are fetched again and kept beside it, so the record shows what it was read
    from. Each file keeps its real URL and goes through the same gates as any resolved file: a listed checksum must
    match the downloaded bytes, a size the list omits comes from the provider's HEAD response, and a file the provider
    publishes no checksum for needs the missing-checksum acknowledgement.
    """
    path = Path(path)
    name = f'hand-prepared file list {path.name}'
    try:
        listing = json.loads(path.read_text())
    except (OSError, ValueError):
        return {'blocked': 'file_list_unreadable', 'release': name}
    if not isinstance(listing, dict) or listing.get('dataset_id') != d['id']:
        return {'blocked': 'file_list_names_another_dataset', 'release': name}
    rows, pages = listing.get('files'), listing.get('prepared_from')
    if not isinstance(rows, list) or not 0 < len(rows) <= FILE_LIST_LIMIT:
        return {'blocked': 'file_list_empty_or_too_long', 'release': name}
    if not isinstance(pages, list) or not 0 < len(pages) <= FILE_LIST_PAGES or any(urlsplit(str(u)).scheme != 'https' for u in pages):
        return {'blocked': 'file_list_needs_the_https_provider_pages_it_was_prepared_from', 'release': name}
    files, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            return {'blocked': 'file_list_entry_is_not_an_object', 'release': name}
        url, rel = str(row.get('url', '')), str(row.get('path', ''))
        try:
            contained(Path('/release'), rel)
        except ValueError:
            return {'blocked': 'file_list_unsafe_path: ' + rel[:120], 'release': name}
        if urlsplit(url).scheme != 'https' or not urlsplit(url).netloc:
            return {'blocked': 'file_list_url_must_be_https: ' + url[:120], 'release': name}
        if rel in seen or url in seen:
            return {'blocked': 'file_list_duplicate_path_or_url: ' + rel[:120], 'release': name}
        seen.update({rel, url})
        identity = {}
        for field, (key, length) in IDENTITY_FORMATS.items():
            if row.get(field) is not None:
                if not re.fullmatch(r'[0-9a-f]{%d}' % length, str(row[field])):
                    return {'blocked': f'file_list_malformed_{field}: ' + rel[:120], 'release': name}
                identity[key] = row[field]
        size = row.get('bytes')
        if size is not None and (type(size) is not int or size < 0):
            return {'blocked': 'file_list_malformed_bytes: ' + rel[:120], 'release': name}
        files.append(file_entry(rel, url, size, **identity))
    left_out = listing.get('left_out') or []
    if not isinstance(left_out, list) or any(not isinstance(x, dict) or urlsplit(str(x.get('source', ''))).scheme != 'https' or not x.get('reason') for x in left_out):
        return {'blocked': 'file_list_left_out_needs_https_source_and_reason', 'release': name}
    body = path.read_bytes()  # evidence is named by its content, so a list edited later the same day is kept beside the first
    evidence.append((f"file_list_{slug(d['id'])}_{hashlib.sha256(body).hexdigest()[:12]}_{path.name}", body, 'file://' + str(path.resolve())))
    for page in pages:
        status, _, _, body = fetch(page)
        if status != 200 or not body:
            return {'blocked': f'file_list_source_page_unavailable_http_{status}: ' + page[:120], 'release': name}
        kind = '.json' if body.lstrip()[:1] in (b'{', b'[') else '.html' if body.lstrip()[:1] == b'<' else '.txt'
        evidence.append((f"file_list_{slug(d['id'])}_source_{hashlib.sha256(body).hexdigest()[:12]}_{slug(urlsplit(page).path)[-60:]}{kind}", body, page))
    sizes = head_sizes([f['url'] for f in files if f['bytes'] is None])
    files = [{**f, 'bytes': sizes.get(f['url'])} if f['bytes'] is None else f for f in files]
    return {'release': str(listing.get('release') or name), 'manifest': f'{path.name}: {len(files)} files listed by hand from {len(pages)} provider pages',
            'files': files, 'folder': slug(d['id']), 'unsupported_urls': [x['source'] for x in left_out], 'left_out': left_out, 'components': None, 'licence_label': None, 'file_list': str(path.resolve())}


def file_lists_for(spec, file_lists):
    """Datasets whose exact files a person listed by hand: id -> list file, checked before any provider is contacted."""
    lists = {}
    for dataset_id, path in (file_lists or {}).items():
        if dataset_id not in spec['dataset_ids']:
            raise ValueError('file_list_dataset_not_in_instructions: ' + dataset_id)
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise ValueError('file_list_missing: ' + str(path))
        lists[dataset_id] = path
    return lists


def import_folders(root, spec, import_from):
    """Datasets whose release the user obtained themselves: id -> folder, checked before any provider is contacted."""
    folders = {}
    for dataset_id, folder in (import_from or {}).items():
        if dataset_id not in spec['dataset_ids']:
            raise ValueError('import_dataset_not_in_instructions: ' + dataset_id)
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError('import_folder_missing: ' + str(folder))
        if folder.is_relative_to(Path(root).resolve() / 'data'):
            raise ValueError('import_folder_inside_the_workspace_data: ' + str(folder))
        folders[dataset_id] = folder
    return folders


# -------------------------------------------------------------------------------------------------------- gates
def bindings_for(dataset_id, url, registry):
    """Licence records allowed for the dataset in the saved mode and study that cover this URL; software-only records bind nothing."""
    allowed = []
    for resource in registry.resources.values():
        if dataset_id not in resource.get('catalog_ids', []) or resource.get('scope') == 'software_only':
            continue
        if resource.get('covers_urls') is not None and url not in resource['covers_urls']:
            continue
        if all(registry._decision(resource['id'], op)['state'] == 'allowed' for op in ('local_inference', 'local_embedding')):
            allowed.append(resource['id'])
    return allowed


def committed_bytes(root, dataset_ids):
    """What the selection already commits against the total cap: bytes held for its datasets, and per dataset the
    budgets of registered jobs not held yet (a reservation that a run replaces by its own plan when it re-plans the dataset).

    The saved total cap is a ceiling for the selection, not for one run: a release that fits only because another
    registered release is temporarily unavailable from its provider must not slip in.
    """
    root = Path(root)
    reviews_path, lock_path, manifest_path = root / 'configs/dataset_download_reviews.json', root / 'configs/research_resources.lock.json', root / 'configs/research_resources.json'
    owner = {alias: r['id'] for r in (read(reviews_path).get('reviews', []) if reviews_path.exists() else []) for alias in r.get('resource_aliases', [])}
    locked = {f['id']: f for f in read(lock_path)['files']} if lock_path.exists() else {}
    registered = read(manifest_path)['resources'] if manifest_path.exists() else []
    held = sum(f['bytes'] for i, f in locked.items() if owner.get(i) in dataset_ids)
    reserved = Counter()
    for r in registered:
        if r['id'] not in locked and owner.get(r['id']) in dataset_ids:
            reserved[owner[r['id']]] += r.get('max_bytes', 0)
    return held, dict(reserved)


def withdraw_unheld_jobs(root, dataset_id):
    """Remove a dataset's registered jobs that are not held: a size-blocked release keeps no reservation against the cap.

    Held files, their lock entries and the dataset's review entry stay; only the manifest resources, scope decisions,
    exact-artifact approvals and alias list of the unheld jobs are withdrawn. Returns the withdrawn job ids.
    """
    root = Path(root)
    reviews_path, lock_path = root / 'configs/dataset_download_reviews.json', root / 'configs/research_resources.lock.json'
    if not reviews_path.exists():
        return []
    reviews = read(reviews_path)
    entry = next((r for r in reviews['reviews'] if r['id'] == dataset_id), None)
    locked = {f['id'] for f in read(lock_path)['files']} if lock_path.exists() else set()
    unheld = [a for a in (entry or {}).get('resource_aliases', []) if a not in locked]
    if not unheld:
        return []
    gone = set(unheld)
    manifest = read(root / 'configs/research_resources.json')
    manifest['resources'] = [r for r in manifest['resources'] if r['id'] not in gone]
    manifest['updated_at'] = now()
    scope = read(root / 'configs/resource_scope.json')
    scope['decisions'] = [x for x in scope['decisions'] if x['id'] not in gone]
    reviews['approved_artifacts'] = [a for a in reviews['approved_artifacts'] if a['id'] not in gone]
    entry['resource_aliases'] = [a for a in entry['resource_aliases'] if a not in gone]
    entry['full_release'] = {**entry.get('full_release', {}), 'withdrawn_unheld_jobs': len(gone), 'withdrawn_on': date.today().isoformat(), 'complete': False}
    write_json(root / 'configs/research_resources.json', manifest)
    write_json(root / 'configs/resource_scope.json', scope)
    write_reviews(reviews_path, reviews)
    return sorted(gone)


def held_keys(root):
    """Paths and URLs of files already held and locked, under whatever folder name they were acquired."""
    lock_path = Path(root) / 'configs/research_resources.lock.json'
    locked = read(lock_path)['files'] if lock_path.exists() else []
    return {('path', f['path']) for f in locked} | {('url', f.get('url')) for f in locked}


def plan_dataset(d, resolution, spec, registry, manifest_paths, held, acknowledgements):
    """Turn a resolved release into jobs with budgets and decide the dataset outcome without transferring anything.

    `held` is the set from held_keys(): a release file already held under any folder name counts as held. Jobs without a
    covering licence record stay unregistered and are counted, so a partially bindable release is labelled partial.
    `manifest_paths` maps every registered path to its job id: a job that is registered but not held (a failed or interrupted
    transfer) is resumed under its own id with its current definition; a path registered under another job blocks."""
    if resolution.get('blocked'):
        return {'outcome': resolution['blocked'], 'jobs': [], 'held': 0, 'release_files': None, 'blockers': [resolution['blocked']], 'unsupported_urls': resolution.get('unsupported_urls', [])}
    files = resolution['files']
    unsupported = resolution.get('unsupported_urls', [])
    if not files:
        return {'outcome': 'empty_release_manifest', 'jobs': [], 'held': 0, 'release_files': 0, 'blockers': ['empty_release_manifest'], 'unsupported_urls': unsupported}
    reference = d.get('category') in REFERENCE_CATEGORIES
    route = d.get('access_route', '')
    open_route = not ('registration' in route and 'no_registration' not in route) and route != 'no_public_access_route' and d.get('category') != 'excluded'
    unresolved_option = spec.get('acquire_unresolved_rights') is True and open_route
    seen, jobs, unbound, unacknowledged, held_count, risks, unresolved = set(), [], [], [], 0, Counter(), 0
    for f in files:
        path = f"datasets/{f.get('folder') or resolution.get('folder')}/{f['path']}"
        if path in seen:
            continue
        seen.add(path)
        if ('path', path) in held or ('url', f['url']) in held:
            held_count += 1
            continue
        job = {'id': f"{slug(f.get('folder') or resolution.get('folder'))}_{slug(f['path'])}", 'kind': 'repository_source' if reference and type(f.get('bytes')) is int and size_cap(f['bytes']) <= REFERENCE_LIMIT else 'dataset',
               'acquisition': 'direct', 'url': f['url'], 'path': path, 'format': f['format'],
               'workflow_use': f"Complete provider release of {d['id']} ({resolution['release']}), requested as a full release."}
        for key in ('provider_sha256', 'provider_md5', 'git_blob_sha1'):
            if f.get(key):
                job[key] = f[key]
        job_risks = []
        if not any(k in job for k in ('provider_sha256', 'provider_md5', 'git_blob_sha1')):
            job_risks.append('missing_provider_checksum')
        if type(f.get('bytes')) is int:
            job['expected_bytes'] = f['bytes']
            job['max_bytes'] = size_cap(f['bytes'])
        else:
            job_risks.append('unknown_advertised_size')
            job['max_bytes'] = spec['max_bytes_per_artifact']
        if job['format'] in {'zip', 'tar', 'tar.gz'}:
            job['max_uncompressed_bytes'] = spec.get('max_bytes_per_dataset', agent.MAX_DATASET_BYTES)  # archive members are inspected (hashed, never extracted) up to the saved dataset ceiling
        job['technical_risks'] = job_risks
        job['download_license_resources'] = bindings_for(d['id'], f['url'], registry)
        if not job['download_license_resources'] and job['kind'] != 'repository_source':
            if not unresolved_option:
                unbound.append(job)
                continue
            job['rights_unresolved'] = True  # explicit option: acquire the open-access file, create no licence record, keep AI use blocked
            unresolved += 1
        if any(r not in acknowledgements for r in job_risks):
            unacknowledged.append(job)
            continue
        for r in job_risks:
            risks[r] += 1
        jobs.append(job)
    repeated = {i for i, n in Counter(j['id'] for j in jobs).items() if n > 1}
    for j in jobs:  # deterministic, collision-safe ids for paths that slug to the same text
        if j['id'] in repeated:
            j['id'] += '_' + hashlib.sha1(j['path'].encode()).hexdigest()[:8]
    everything = jobs + unbound + unacknowledged
    sized = [j for j in everything if 'expected_bytes' in j]
    unknown_size = [j for j in everything if 'expected_bytes' not in j]  # provider states no size: the per-artifact cap bounds the transfer, nothing more is known
    package = sum(j['max_bytes'] for j in sized)
    unaccepted = sorted({r for j in unacknowledged for r in j['technical_risks'] if r not in acknowledgements})
    blockers = []
    if package > spec.get('max_bytes_per_dataset', agent.MAX_DATASET_BYTES) or any(j['max_bytes'] > spec['max_bytes_per_artifact'] for j in sized):
        blockers.append('blocked_dataset_size_limit')  # the complete release is what must fit; a partial transfer never hides an oversized release
    registered_as = manifest_paths if isinstance(manifest_paths, dict) else dict.fromkeys(manifest_paths)  # path -> registered job id (None: unknown)
    if any(j['path'] in registered_as and registered_as[j['path']] != j['id'] for j in jobs):
        blockers.append('path_already_registered_under_another_job')
    if not jobs:
        if unbound:
            blockers.append('blocked_license_binding_missing')
        if unacknowledged:
            blockers.append('needs_technical_risk_acknowledgement')
    failed = resolution.get('unresolved_sources', [])  # a source that did not answer this run: its files are unknown, so the release is not complete
    complete = not unbound and not unacknowledged and not unsupported and not failed
    if blockers:
        outcome = blockers[0]
    elif not jobs:
        outcome = 'already_complete' if complete else 'held_part_complete_rest_unbound_or_unsupported'
    else:
        outcome = 'ready' if complete else 'ready_partial'
    return {'outcome': outcome, 'blockers': blockers, 'jobs': jobs, 'held': held_count, 'release_files': len(seen), 'package_budget': package, 'risks': dict(risks), 'unaccepted': unaccepted,
            'bindings': sorted({b for j in jobs for b in j['download_license_resources']}), 'unbound_files': len(unbound), 'unbound_sample': [j['path'] for j in unbound[:10]],
            'unacknowledged_files': len(unacknowledged), 'unacknowledged_sample': [j['path'] for j in unacknowledged[:10]], 'rights_unresolved_files': unresolved, 'unknown_size_files': len(unknown_size),
            'unsupported_urls': unsupported, 'unresolved_sources': failed, 'reference_release': reference, 'complete_release_registrable': complete}


def copy_only(plan, sources):
    """Keep only the jobs whose bytes another study already holds (`sources`: URL -> holding): a copy-only run transfers
    nothing from the provider. The other release files stay unregistered and counted, so the dataset is partial, never complete."""
    keep = [j for j in plan['jobs'] if j['url'] in sources]
    dropped = len(plan['jobs']) - len(keep)
    if not dropped:
        return plan
    return {**plan, 'jobs': keep, 'not_held_elsewhere_files': dropped, 'complete_release_registrable': False,
            'outcome': 'ready_partial' if keep else 'held_part_complete_rest_unbound_or_unsupported',
            'package_budget': sum(j['max_bytes'] for j in keep if 'expected_bytes' in j),
            'bindings': sorted({b for j in keep for b in j['download_license_resources']}),
            'risks': dict(Counter(r for j in keep for r in j['technical_risks'])),
            'rights_unresolved_files': sum(1 for j in keep if j.get('rights_unresolved'))}


# ----------------------------------------------------------------------------------------------- registration
def register(root, d, resolution, plan, evidence_items, licence_evidence, run_name):
    """Write the bindable jobs, relevance decisions, exact-artifact approvals and provider-hash asset identities."""
    root = Path(root)
    reviewed_at = now()
    manifest = read(root / 'configs/research_resources.json')
    scope = read(root / 'configs/resource_scope.json')
    reviews = read(root / 'configs/dataset_download_reviews.json')
    assets = read(root / 'configs/data_license_assets.json')
    jobs = [{k: v for k, v in j.items() if k not in {'technical_risks', 'download_license_resources', 'rights_unresolved'}} for j in plan['jobs']]
    ids = {j['id'] for j in jobs}
    manifest['resources'] = [r for r in manifest['resources'] if r['id'] not in ids] + jobs  # a registered, unheld job is replaced by its current definition, never duplicated
    manifest['updated_at'] = reviewed_at
    scope['decisions'] = [x for x in scope['decisions'] if x['id'] not in {j['id'] for j in jobs}] + [{'id': j['id'], 'retain': True, 'reason': j['workflow_use']} for j in jobs]
    scope['reviewed_at'] = reviewed_at
    approvals = []
    for j in plan['jobs']:
        approval = {'id': j['id'], 'url': j['url'], 'path': j['path'], 'format': j['format'], 'max_bytes': j['max_bytes'], 'reviewed_at': reviewed_at, 'decision': 'approve_minimal_subset',
                    'use_case': j['workflow_use'], 'file_selection_reason': f"File {j['path'].split('/', 2)[2]} of the complete provider release ({resolution['manifest']}); "
                    + ('provider identity recorded' if not j['technical_risks'] else 'technical risks acknowledged: ' + ', '.join(j['technical_risks']))
                    + (f"; exact size {j['expected_bytes']} bytes" if 'expected_bytes' in j else ''),
                    'checks': dict.fromkeys(CHECKS, True),
                    'check_interpretation': 'Full provider release resolved from the retained provider manifest; metadata-level approval, not validation of content.',
                    'metadata_evidence': evidence_items + licence_evidence}
        if j['download_license_resources']:
            approval['download_license_resources'] = j['download_license_resources']
        if j.get('rights_unresolved'):
            approval['rights_unresolved_download'] = True
            approval['file_selection_reason'] += '; acquired under the explicit unresolved-rights option (open access, no licence decision recorded, AI use blocked until a licence review)'
        approvals.append(approval)
    reviews['approved_artifacts'] = [a for a in reviews['approved_artifacts'] if a['id'] not in {j['id'] for j in jobs}] + approvals
    existing = next((r for r in reviews['reviews'] if r['id'] == d['id']), None)
    lock_path = root / 'configs/research_resources.lock.json'
    release = {f['url'] for f in resolution.get('files', [])} | {f"datasets/{f.get('folder') or resolution.get('folder')}/{f['path']}" for f in resolution.get('files', [])}
    held_ids = [f['id'] for f in (read(lock_path)['files'] if lock_path.exists() else []) if f.get('url') in release or f['path'] in release]  # release files already held, possibly under another entry's jobs
    aliases = list(dict.fromkeys((existing or {}).get('resource_aliases', []) + held_ids + [j['id'] for j in jobs]))
    selection = 'All files (complete provider release)' if plan['complete_release_registrable'] else (
        f"Registrable part of the release: {plan['unbound_files']} files have no covering licence record" + (f"; {plan['unacknowledged_files']} files need a technical-risk acknowledgement" if plan['unacknowledged_files'] else '') + (f"; {len(plan['unsupported_urls'])} source URLs are unsupported" if plan['unsupported_urls'] else '') + '.')
    entry = {**(existing or {}), 'id': d['id'], 'name': d['name'], 'source': (d.get('source_urls') or [''])[0], 'priority': d.get('priority'),
             'evidence_level': 'provider_manifest_resolved', 'observed_schema': 'Provider release manifest retained; contents inspected after acquisition.',
             'medscrape_use': f"Complete provider release ({resolution['release']}).", 'decision': 'approve_public_release_files', 'minimal_selection': selection,
             'remaining_checks': ['Verify provider identities on transfer', 'Inspect structure after transfer'], 'resource_aliases': aliases, 'metadata_evidence': evidence_items + licence_evidence,
             'access_and_AI': 'Bound licence records: ' + (', '.join(plan['bindings']) or 'none') + (f"; {plan['rights_unresolved_files']} files acquired rights-unresolved under the explicit option (no licence record, AI use blocked)" if plan.get('rights_unresolved_files') else '') + '; the saved mode applies; downloading grants no AI use.',
             'full_release': {'manifest': resolution['manifest'], 'release': resolution['release'], 'files': plan['release_files'], 'previously_held': plan['held'], 'jobs_added': len(jobs),
                              'unbound_files': plan['unbound_files'], 'unacknowledged_files': plan['unacknowledged_files'], 'rights_unresolved_files': plan.get('rights_unresolved_files', 0), 'unknown_size_files': plan.get('unknown_size_files', 0), 'unsupported_sources': plan['unsupported_urls'], 'unresolved_sources': plan.get('unresolved_sources', []), 'not_held_elsewhere_files': plan.get('not_held_elsewhere_files', 0), 'complete': plan['complete_release_registrable'],
                              'requested': date.today().isoformat(), 'resolver_run': run_name}}
    reviews['reviews'] = [r for r in reviews['reviews'] if r['id'] != d['id']] + [entry]
    reviews['reviewed_at'] = reviewed_at
    new_assets = [{'path': f"data/acquisitions/{j['path']}", 'sha256': j['provider_sha256'], 'resource': resource}
                  for j in plan['jobs'] if j.get('provider_sha256') for resource in j['download_license_resources']]
    seen = {(a['path'], a['sha256'], a['resource']) for a in assets['files']}
    assets['files'] += [a for a in new_assets if (a['path'], a['sha256'], a['resource']) not in seen]
    assets['as_of'] = date.today().isoformat()
    if jobs:  # recording a release held under another entry's jobs registers nothing new: the job manifest the acquisition report is bound to stays untouched
        write_json(root / 'configs/research_resources.json', manifest)
        write_json(root / 'configs/resource_scope.json', scope)
    write_reviews(root / 'configs/dataset_download_reviews.json', reviews)  # each release's evidence list is stored once, not per file
    if new_assets:
        write_json(root / 'configs/data_license_assets.json', assets)
    return {'jobs': len(jobs), 'approvals': len(approvals), 'assets': len(new_assets)}


def confirm_complete(root, completed, run_name):
    """Record on the review entries that these releases resolved as complete today.

    An earlier partial resolution may have left `complete: false` or stale `unsupported_sources` on the entry, and
    nothing else rewrites it; the dataset registry derives its completeness label from that record.
    """
    if not completed:
        return
    path = root / 'configs/dataset_download_reviews.json'
    reviews = read(path)
    for entry in reviews['reviews']:
        plan = completed.get(entry['id'])
        if plan is not None:
            entry['full_release'] = {**entry.get('full_release', {}), 'files': plan['release_files'], 'previously_held': plan['held'], 'jobs_added': 0, 'withdrawn_unheld_jobs': 0, 'unbound_files': plan.get('unbound_files', 0),
                                     'unacknowledged_files': plan.get('unacknowledged_files', 0), 'unsupported_sources': plan.get('unsupported_urls', []), 'unresolved_sources': [], 'not_held_elsewhere_files': 0, 'complete': True,
                                     'confirmed_complete': date.today().isoformat(), 'resolver_run': run_name}
            entry['minimal_selection'] = 'All files (complete provider release)'
    write_reviews(path, reviews)


# -------------------------------------------------------------------------------------------------- execution
def execute(root, ready, spec, mode, study_id, run_dir, journal, workers=4, reuse=None, reuse_only=False):
    """Acquire registered jobs: identity-backed files into the locked holdings, acknowledged-risk files into quarantine.

    `workers` transfers run at a time per dataset; the calling thread settles each finished transfer (lock, report,
    quarantine record, journal), so the lock never lags a held file by more than the transfers in flight.
    """
    root = Path(root)
    manifest_path = root / 'configs/research_resources.json'
    lock_path = root / 'configs/research_resources.lock.json'
    base = root / 'data/acquisitions'
    base.mkdir(parents=True, exist_ok=True)
    lock = read(lock_path) if lock_path.exists() else {'schema_version': 1, 'scope': read(manifest_path).get('scope'), 'files': []}
    locked = {f['id']: f for f in lock['files']}
    report_path = base / 'acquisition-report.json'
    report = read(report_path) if report_path.exists() else {'results': []}
    results = {r['id']: r for r in report.get('results', [])}
    stage = root / 'data/quarantine/download_agent' / run_dir.name
    staged = []
    direct = spec.get('direct_holdings_for_acknowledged_risks') is True  # explicit option: acknowledged exceptions bypass quarantine, uncertainty is recorded
    license_context = {'mode': mode, 'study_id': study_id}

    def transfer(d_id, job):  # worker thread: network and verification only
        item = {k: v for k, v in job.items() if k not in {'technical_risks', 'download_license_resources', 'rights_unresolved'}}
        allow = job.get('rights_unresolved') is True
        try:
            if job['technical_risks'] and not direct:
                return d_id, job, 'quarantined', acquisition.acquire(item, {}, base=stage, execute=True, journal=journal, license_context=license_context, allow_unresolved_rights=allow, reuse=reuse, reuse_only=reuse_only)
            return d_id, job, 'acquired', acquisition.acquire(item, locked, base=base, execute=True, journal=journal, license_context=license_context, allow_unresolved_rights=allow, reuse=reuse, reuse_only=reuse_only)
        except Exception as error:
            return d_id, job, 'failed', error

    def settle(d_id, job, kind, record):  # calling thread: registries and journal
        allow = job.get('rights_unresolved') is True
        if kind == 'failed':
            results[job['id']] = {'id': job['id'], 'state': 'failed', 'problem': str(record)}
            journal('transfer_failed', {'id': job['id'], 'dataset': d_id, 'problem': str(record)})
            return False
        if kind == 'quarantined':
            staged.append({'id': job['id'], 'status': 'downloaded_quarantined', 'technical_risks': job['technical_risks'], 'warnings': [agent.RISKS[r] for r in job['technical_risks']] + ([agent.UNRESOLVED_RIGHTS_WARNING] if allow else []), 'rights_unresolved': allow,
                           'record': {k: v for k, v in record.items() if k not in {'state', 'inspection'}}, 'dataset': d_id})
            journal('quarantined', {'id': job['id'], 'dataset': d_id, 'risks': job['technical_risks']})
            return False
        held = record['state'] == 'downloaded_verified'
        if held:
            if allow:
                record = {**record, 'rights': 'unresolved_at_acquisition'}
            if job['technical_risks']:
                record = {**record, 'technical_risks': job['technical_risks'], 'identity': 'local_sha256_only' if 'missing_provider_checksum' in job['technical_risks'] else 'provider',
                          'direct_holdings_note': 'Acknowledged technical exception taken straight into the holdings on the user’s explicit option; see data_caveats on the dataset.'}
            locked[job['id']] = {k: v for k, v in record.items() if k not in {'state', 'inspection'}}
            results[job['id']] = record
        journal('acquired', {'id': job['id'], 'dataset': d_id, 'state': record['state']})
        return held

    for d_id, plan in ready.items():
        with ThreadPoolExecutor(max_workers=workers) as pool:
            queue, pending = iter(plan['jobs']), set()
            while True:
                pending.update(pool.submit(transfer, d_id, job) for job in itertools.islice(queue, 2 * workers - len(pending)))
                if not pending:
                    break
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                if any([settle(*future.result()) for future in done]):  # one lock write per batch of settled transfers
                    lock['files'] = list(locked.values())
                    write_json(lock_path, lock)
    report.update(checked_at=now(), manifest_sha256=digest(manifest_path), results=list(results.values()), counts=dict(Counter(r['state'] for r in results.values())),
                  scope='Acquisition and deterministic inventory only; no inference, ETL, repairs or benchmark execution')
    write_json(report_path, report)
    refresh_dataset_entries(root, set(ready))
    if staged:  # a download-agent style run record, so the quarantine listing and the Quarantine page can describe the staged files
        write_json(root / 'runs/downloads' / run_dir.name / 'report.json', {'schema_version': 1, 'created_at': now(), 'mode': 'execute', 'license_mode': mode, 'download_scope': 'full',
                                                                              'source': 'resolve_full_release.py', 'quarantine_directory': str(stage.relative_to(root)),
                                                                              'unique_artifacts': staged, 'dataset_results': [], 'model_calls': 0, 'ai_use_authorized_by_download': False})
    return {'acquired': sum(1 for r in results.values() if r.get('state') == 'downloaded_verified'), 'failed': sum(1 for r in results.values() if r.get('state') == 'failed'),
            'quarantined': len(staged), 'lock_files': len(locked),
            **({'copied_from_other_studies': sum(1 for r in results.values() if (r.get('copied_from') or {}).get('study')),
                'imported_from_folders': sum(1 for r in results.values() if (r.get('copied_from') or {}).get('folder'))} if reuse is not None else {})}


# -------------------------------------------------------------------------------------------------------- run
def reuse_workspaces(root, names):
    """Other study workspaces whose verified holdings this run may copy instead of downloading: ids under studies/ or paths."""
    found = []
    for name in names or []:
        workspace = Path(name) if Path(name).is_dir() else study_path(name)
        if workspace.resolve() == Path(root).resolve():
            raise ValueError('reuse_study_is_this_study: ' + str(name))
        if not (workspace / 'configs/research_resources.lock.json').is_file():
            raise ValueError('reuse_study_has_no_lock: ' + str(name))
        found.append(workspace)
    return found


def run(instructions_path, root=ROOT, register_jobs=False, execute_jobs=False, workers=4, only=None, reuse_from=None, import_from=None, reuse_only=False, file_lists=None):
    root = Path(root).resolve()
    spec = agent.read_instructions(instructions_path)
    receipt = read(root / 'data/interim/dataset_selection/latest.json')
    if digest(root / receipt['selection']) != receipt['selection_sha256']:
        raise ValueError('catalogue_receipt_changed')
    acknowledgements = agent.validate_instructions(spec, receipt['selection_sha256'])
    if spec.get('download_scope') != 'full':
        raise ValueError('full_release_resolver_needs_download_scope_full')
    catalogue = {d['id']: d for d in read(root / receipt['selection'])['datasets']}
    if set(spec['dataset_ids']) - set(catalogue):
        raise ValueError('unknown_dataset_ids')
    study = spec.get('research_study')
    study_id = study['id'] if study else default_study(root)
    if study:
        current = agent.registered_study(root, study['id'])
        if not current or agent.study_fingerprint(current) != agent.study_fingerprint(study):
            raise ValueError('register_current_research_study_before_downloading')
    registry = LicenseRegistry(root / 'configs/data_licenses.json', root / 'configs/data_license_assets.json', root=root, mode=spec['license_mode'], study_id=study_id)
    manifest_paths = {r['path']: r['id'] for r in read(root / 'configs/research_resources.json')['resources']}
    held = held_keys(root)
    run_name = stamp()
    run_dir = root / 'runs/full_release' / run_name
    run_dir.mkdir(parents=True)
    shutil.copyfile(instructions_path, run_dir / ('original-instructions.csv' if str(instructions_path).lower().endswith('.csv') else 'original-instructions.json'))
    write_json(run_dir / 'instructions.json', spec)
    for source in (Path(__file__), CODE_ROOT / 'Scripts/acquire_research_resources.py'):
        shutil.copyfile(source, run_dir / source.name)
    evidence_dir = root / 'data/raw/public_reference_collection/acquisitions' / f"full_release_{study_id}_{date.today().isoformat()}" / 'notices'

    journal_guard = threading.Lock()  # transfers journal from worker threads

    def journal(kind, details):
        with journal_guard, (run_dir / 'operations.jsonl').open('a') as stream:
            stream.write(json.dumps({'at': now(), 'kind': kind, 'details': details}, ensure_ascii=False) + '\n')
    if only and set(only) - set(spec['dataset_ids']):
        raise ValueError('datasets_not_in_instructions: ' + ', '.join(sorted(set(only) - set(spec['dataset_ids']))))
    selected = [i for i in spec['dataset_ids'] if not only or i in only]  # a restricted run re-plans only these; the others keep their reservations against the total
    if reuse_from is None and (spec.get('reuse_held_files') is True or reuse_only):  # the saved option: every other study workspace of this checkout that holds verified files
        reuse_from = [str(p) for p in acquisition.workspaces_with_holdings_near(root)]
    reuse_paths = reuse_workspaces(root, reuse_from)
    reuse = acquisition.holdings_of(reuse_paths) if reuse_paths else None
    if reuse_only and not reuse:
        raise ValueError('reuse_only_needs_another_study_that_holds_files')
    imports = import_folders(root, spec, import_from)  # releases the user obtained themselves: their folders stand in for the provider
    lists = file_lists_for(spec, file_lists)  # exact provider files listed by hand: the list stands in for the provider manifest
    if set(imports) & set(lists):
        raise ValueError('dataset_given_both_an_import_folder_and_a_file_list: ' + ', '.join(sorted(set(imports) & set(lists))))
    import_sources = {}
    journal('run_started', {'reuse_only': reuse_only, 'dataset_ids': spec['dataset_ids'], 'only': sorted(only) if only else None, 'reuse_from': [p.name for p in reuse_paths], 'import_from': {i: str(f) for i, f in imports.items()}, 'file_lists': {i: str(f) for i, f in lists.items()}, 'reuse_held_files': spec.get('reuse_held_files') is True, 'license_mode': spec['license_mode'], 'study_id': study_id, 'register': register_jobs, 'execute': execute_jobs})
    held_bytes, reserved = committed_bytes(root, spec['dataset_ids'])
    outcomes, ready, completed, total_budget = [], {}, {}, held_bytes + sum(reserved.values())
    for dataset_id in selected:
        d = catalogue[dataset_id]
        route = d.get('access_route', '')
        evidence = []
        if dataset_id in imports:  # the user obtained the release through the recorded route; its folder is the manifest
            resolution = resolve_folder(d, imports[dataset_id], evidence)
            import_sources.update(resolution.pop('import_sources', {}))
        elif d.get('category') == 'excluded' or route == 'no_public_access_route':
            resolution = {'blocked': 'no_public_access_route', 'release': None}
        elif 'registration' in route and 'no_registration' not in route:
            resolution = {'blocked': 'blocked_access_' + route, 'release': None}
        elif dataset_id in lists:  # exact files of an open provider without an automated route, listed by hand from its pages
            resolution = resolve_file_list(d, lists[dataset_id], evidence)
        else:
            targets, unsupported = detect_all(d.get('source_urls', []))
            resolution = resolve_all(targets, unsupported, evidence) if targets else {'blocked': 'unsupported_provider_manual_route', 'release': None, 'unsupported_urls': unsupported}
        evidence_items = []
        for name, body, url in evidence:
            path = contained(evidence_dir, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(body)
            evidence_items.append({'path': str(path.relative_to(root)), 'sha256': digest(path), 'url': url, 'retrieved_at': now()})
        plan = plan_dataset(d, resolution, spec, registry, manifest_paths, held, acknowledgements)
        if reuse_only and plan['outcome'] in {'ready', 'ready_partial'}:
            plan = copy_only(plan, reuse)  # copy what other studies hold; the provider is never asked for bytes
        withdrawn = []
        if plan['outcome'] in {'ready', 'ready_partial'}:
            total_budget -= reserved.pop(dataset_id, 0)  # this run re-plans the dataset's unheld files; its package replaces the reservation
            if total_budget + plan['package_budget'] > spec['max_total_bytes']:
                plan['outcome'], plan['blockers'] = 'blocked_total_byte_budget', ['blocked_total_byte_budget'] + plan['blockers']
                if register_jobs:
                    withdrawn = withdraw_unheld_jobs(root, dataset_id)  # a size-blocked release holds no reservation; its held files stay
                    manifest_paths = {p: i for p, i in manifest_paths.items() if i not in set(withdrawn)}
        outcome = {'dataset': dataset_id, 'release': resolution.get('release'), 'manifest': resolution.get('manifest'), 'components': resolution.get('components'),
                   'unsupported_urls': plan.get('unsupported_urls', []), 'unresolved_sources': plan.get('unresolved_sources', []), 'not_held_elsewhere_files': plan.get('not_held_elsewhere_files', 0), 'licence_label_from_provider': resolution.get('licence_label'), 'outcome': plan['outcome'], 'blockers': plan.get('blockers', []),
                   'release_files': plan.get('release_files'), 'previously_held': plan.get('held'), 'new_jobs': len(plan['jobs']), 'unbound_files': plan.get('unbound_files', 0), 'unacknowledged_files': plan.get('unacknowledged_files', 0), 'rights_unresolved_files': plan.get('rights_unresolved_files', 0), 'unknown_size_files': plan.get('unknown_size_files', 0),
                   'unbound_sample': plan.get('unbound_sample', []), 'package_budget_bytes': plan.get('package_budget'), 'selection_committed_bytes_before': total_budget, 'withdrawn_jobs': withdrawn, 'expected_bytes': sum(j.get('expected_bytes', 0) for j in plan['jobs']),
                   'technical_risks': plan.get('risks'), 'unaccepted_risks': plan.get('unaccepted'), 'license_bindings': plan.get('bindings'), 'complete_release_registrable': plan.get('complete_release_registrable'),
                   'imported_from': resolution.get('imported_from'), 'file_list': resolution.get('file_list'), 'missing_files': resolution.get('missing_files'), 'evidence': evidence_items}
        if plan['outcome'] in {'ready', 'ready_partial'}:
            total_budget += plan['package_budget']
            if register_jobs:
                licence_evidence = [{'path': e['snapshot'], 'sha256': e['sha256']} for b in plan['bindings'] for e in registry.resources[b].get('evidence', [])]
                outcome['registered'] = register(root, d, resolution, plan, [{'path': e['path'], 'sha256': e['sha256']} for e in evidence_items], licence_evidence, run_name)
                manifest_paths.update({j['path']: j['id'] for j in plan['jobs']})
                ready[dataset_id] = plan
        elif register_jobs and plan['outcome'] in {'already_complete', 'held_part_complete_rest_unbound_or_unsupported'}:
            if plan.get('held') and not any(r['id'] == dataset_id for r in read(root / 'configs/dataset_download_reviews.json')['reviews']):
                # every file it needs is held under another catalogue entry's jobs (one release, two entries): record that this dataset holds them
                licence_evidence = [{'path': e['snapshot'], 'sha256': e['sha256']} for b in plan['bindings'] for e in registry.resources[b].get('evidence', [])]
                outcome['registered'] = register(root, d, resolution, plan, [{'path': e['path'], 'sha256': e['sha256']} for e in evidence_items], licence_evidence, run_name)
            if plan['outcome'] == 'already_complete':
                completed[dataset_id] = plan
        outcomes.append(outcome)
        journal('dataset_resolved', {k: v for k, v in outcome.items() if k not in {'evidence', 'components'}})
    confirm_complete(root, completed, run_name)
    if completed:
        refresh_dataset_entries(root, set(completed))
    sources = {**(reuse or {}), **import_sources} if reuse is not None or import_sources else None  # bytes to copy instead of downloading: other studies' holdings and the user's folders
    executed = execute(root, ready, spec, spec['license_mode'], study_id, run_dir, journal, workers, sources, reuse_only) if execute_jobs and ready else None
    if register_jobs:
        refresh_dataset_entries(root, set(selected))  # every dataset this run looked at, including size-blocked ones with withdrawn jobs or a stopped earlier run, gets its registry entry in line with the lock
    report = {'schema_version': 1, 'kind': 'datahunter_full_release_resolution', 'created_at': now(), 'run': str(run_dir.relative_to(root)), 'study_id': study_id, 'license_mode': spec['license_mode'], 'only': sorted(only) if only else None,
              'registered': register_jobs, 'executed': execute_jobs, 'outcomes': outcomes, 'counts': dict(Counter(o['outcome'] for o in outcomes)), 'execution': executed,
              'limits': 'Full releases only; a release larger than the saved caps is size-blocked, never trimmed; missing identities need the matching acknowledgement and are quarantined; '
                        'files without a covering licence record stay unregistered and the release is labelled partial; nothing here grants AI use.', 'model_calls': 0}
    write_json(run_dir / 'report.json', report)
    journal('run_finished', {'counts': report['counts'], 'execution': executed})
    if register_jobs or execute_jobs:
        append_log(root, study_id, [{'at': now(), 'action': 'download_run', 'source': 'resolve_full_release.py', 'run': report['run'],
                                     'detail': (f"Full-release resolution ({'executed' if execute_jobs else 'registered'}, licence {spec['license_mode']}{', restricted to ' + ', '.join(sorted(only)) if only else ''}): "
                                                + '; '.join(f"{o['dataset']}: {o['outcome']}" + (f" ({o['new_jobs']} jobs, {o['expected_bytes']:,} bytes" + (f", {o['unbound_files']} unbound" if o['unbound_files'] else '') + (f", {o['unacknowledged_files']} need an acknowledgement" if o['unacknowledged_files'] else '') + (f", {o['rights_unresolved_files']} rights-unresolved" if o['rights_unresolved_files'] else '') + ')' if o['new_jobs'] else '') for o in outcomes)
                                                + (f". Transfers: {executed['acquired']} locked, {executed['quarantined']} quarantined, {executed['failed']} failed" + (f", {executed['copied_from_other_studies']} copied from {', '.join(p.name for p in reuse_paths)}" if executed.get('copied_from_other_studies') else '') + (f", {executed['imported_from_folders']} imported from {', '.join(f'{i} ({f})' for i, f in imports.items())}" if executed.get('imported_from_folders') else '') + '.' if executed else '')
                                                + ' Downloading grants no AI-use permission.')[:1000]}])
    print(json.dumps({'run': report['run'], 'counts': report['counts'], 'execution': executed}))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--instructions', required=True, type=Path, help='Saved download instructions (JSON or CSV) with download_scope=full')
    parser.add_argument('--register', action='store_true', help='Write resolved, bindable jobs into the registries')
    parser.add_argument('--execute', action='store_true', help='Also transfer the registered jobs (implies --register)')
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 9), help='Concurrent transfers per dataset (default 4, as in acquire_research_resources.py)')
    parser.add_argument('--datasets', nargs='+', metavar='ID', help='Only these datasets of the saved instructions (a retry after a provider outage); the others keep their reservations against the total cap')
    parser.add_argument('--reuse-only', action='store_true', help='Copy only the files other study workspaces hold (all of them unless --reuse-from-study names some) and download nothing; the rest of each release stays unregistered and the dataset partial')
    parser.add_argument('--reuse-from-study', nargs='+', metavar='STUDY', help='Copy files another study workspace already holds and verified (study id under studies/ or a workspace path) instead of downloading them; identity checks and this study’s permission gates still apply')
    parser.add_argument('--import-from', nargs='+', metavar='ID=FOLDER', help='A release you obtained yourself (after credentialing, an agreement or from a provider without an automated route): the folder stands in for the provider manifest; keep the provider’s SHA256SUMS or MD5SUMS file in it so the copy is verified against the provider’s hashes; the same approval, size and licence gates apply')
    parser.add_argument('--file-list', nargs='+', metavar='ID=FILE', help='Exact files of a provider without an automated route, listed by hand from its own pages (JSON: dataset_id, release, prepared_from, files with url, path and the provider’s bytes and md5, sha256 or git_blob_sha1 where published): the list stands in for the provider manifest, the files keep their real URLs and are downloaded under the same gates')
    args = parser.parse_args(argv)
    lists = {}
    for item in args.file_list or []:
        if '=' not in item:
            parser.error('--file-list takes ID=FILE')
        dataset_id, path = item.split('=', 1)
        lists[dataset_id] = path
    imports = {}
    for item in args.import_from or []:
        if '=' not in item:
            parser.error('--import-from takes ID=FOLDER')
        dataset_id, folder = item.split('=', 1)
        imports[dataset_id] = folder
    return run(args.instructions, register_jobs=args.register or args.execute, execute_jobs=args.execute, workers=args.workers, only=args.datasets, reuse_from=args.reuse_from_study, import_from=imports or None, reuse_only=args.reuse_only, file_lists=lists or None)


if __name__ == '__main__':
    require_workspace(ROOT)
    main()

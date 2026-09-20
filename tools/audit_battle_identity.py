"""Audit console identity records against a delivered manifest (offline only).

Counts are LOG FILES, not matches or our team's appearances. Resolve teamA/B
ownership from official match metadata before attributing a file to our team.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

IDENTITY = re.compile(r'(?:^|\|\s*)identity\s+(\{.*\})\s*$')
SHA = re.compile(r'^[0-9a-f]{40}$')
DIGEST = re.compile(r'^[0-9a-f]{64}$')


def expected_identity(manifest):
    commit, digest = manifest.get('commit'), manifest.get('source_digest')
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError('Manifest must contain a full source commit, not a delivery SHA')
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError('Manifest source_digest missing or invalid')
    return commit, digest


def scan(path, expected):
    commit, digest = expected
    identities, invalid, error = [], 0, None
    try:
        with path.open('rb') as stream:
            bom = stream.read(2)
        encoding = 'utf-16' if bom in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig'
        with path.open(encoding=encoding) as stream:
            for line_no, line in enumerate(stream, 1):
                match = IDENTITY.search(line.strip())
                if not match:
                    if re.search(r'(?:^|\|\s*)identity\s+', line):
                        invalid += 1
                    continue
                try:
                    row = json.loads(match.group(1))
                    code = row.get('code_commit')
                    if not isinstance(code, str) or not SHA.fullmatch(code):
                        invalid += 1
                        continue
                    manifest = row.get('manifest')
                    manifest = manifest if isinstance(manifest, dict) else {}
                    verified = (row.get('commit_source') == 'manifest'
                                and manifest.get('state') == 'verified'
                                and manifest.get('commit') == code
                                and code == commit and manifest.get('source_digest') == digest)
                    identities.append({'line': line_no, 'source_commit': code,
                                       'manifest_state': manifest.get('state'),
                                       'manifest_source_digest': manifest.get('source_digest'),
                                       'verified_expected': verified})
                except (ValueError, TypeError, AttributeError):
                    invalid += 1
    except (OSError, UnicodeError) as exc:
        error = type(exc).__name__
    sources = sorted({r['source_commit'] for r in identities})
    if error or invalid:
        status = 'UNKNOWN_INCOMPLETE'
    elif len(sources) > 1:
        status = 'MIXED_IDENTITIES'
    elif not identities:
        status = 'UNKNOWN_NO_IDENTITY'
    elif sources == [commit]:
        status = ('EXPECTED_VERIFIED_SOURCE' if all(r['verified_expected'] for r in identities)
                  else 'EXPECTED_SOURCE_UNVERIFIED')
    else:
        status = 'OTHER_REPORTED_SOURCE'
    return {'file': str(path), 'classification': status, 'identities': identities,
            'invalid_identity_lines': invalid, 'read_error': error}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--logs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; choose a new report path')
    if not args.logs.exists():
        parser.error('Log path does not exist')
    manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    expected = expected_identity(manifest)
    paths = ([args.logs] if args.logs.is_file() else
             sorted(p for p in args.logs.rglob('*.log') if p.name in ('teamA.log', 'teamB.log')))
    rows = [scan(p, expected) for p in paths]
    result = {'schema': 'competition-hw-battle-identity/1',
              'expected_source_commit': expected[0], 'expected_manifest_source_digest': expected[1],
              'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              'count_unit': 'log_file', 'team_scope': 'not_resolved',
              'scope': 'Reported source identity, not archive byte identity, platform PASS, or win/loss',
              'files_scanned': len(rows), 'counts': dict(Counter(r['classification'] for r in rows)),
              'files': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'files'}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

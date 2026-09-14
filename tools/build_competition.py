"""Build a flat competition package using the original sample's exact entry.

The existing full viewer bundle remains available via build_submission.py.
This profile has no nested Demo/ bootstrap or web assets. All bytes come from
one Git commit; the official sample archive is read, never rewritten.
"""
import argparse
import io
import json
from pathlib import Path
import tarfile

import build_submission as b


def build(repo, ref, output):
    repo, output = Path(repo).resolve(), Path(output).resolve()
    sidecar = output.with_suffix(output.suffix+'.sha256')
    manifest_path = output.parent/'submission-manifest.json'
    b.preflight([output, sidecar, manifest_path])
    commit = b.resolve_commit(repo, ref)
    paths = b.committed_paths(repo, commit)
    def read(name):
        return b._git_bytes(repo, 'show', f'{commit}:{name}')
    original = read('Demo/CoreGeek.tar.gz')
    if not b.known_original_fingerprint(original):
        raise b.BuildError('official sample archive differs from the reviewed baseline')
    with tarfile.open(fileobj=io.BytesIO(original), mode='r:gz') as archive:
        entry = archive.extractfile('CoreGeek/main3.py').read()
        project = archive.extractfile('CoreGeek/pyproject.toml').read()
    blobs, entries = {}, []
    def add(name, data, source):
        target = 'CoreGeek/'+name
        if target in blobs:
            raise b.BuildError('duplicate package path '+target)
        blobs[target] = data
        entries.append({'path': target, 'sha256': b._sha256(data), 'bytes': len(data), 'source': source})
    # These are byte-for-byte copies of the known-working sample, not wrappers.
    add('main3.py', entry, 'Demo/CoreGeek.tar.gz:CoreGeek/main3.py')
    add('main.py', entry, 'Demo/CoreGeek.tar.gz:CoreGeek/main3.py')
    add('pyproject.toml', project, 'Demo/CoreGeek.tar.gz:CoreGeek/pyproject.toml')
    sources = [p for p in paths if p.startswith('Demo/CoreGeek/src/agent/') and p.endswith(('.py', '.json'))]
    extras = ['submission/server.py','run.sh','tools/trace_tool.py','docs/TRACE_LOGGING.md',
              'docs/request.txt','docs/response.txt']
    b.check_modes(repo, commit, sources+extras)
    for name in sources:
        if name == 'Demo/CoreGeek/src/agent/server.py':
            continue
        add(name.removeprefix('Demo/CoreGeek/'), read(name), name)
    add('src/agent/server.py', read('submission/server.py'), 'submission/server.py')
    for name in extras[1:]:
        add(name, read(name), name)
    manifest = b.build_manifest(commit, entries)
    manifest.update(profile='competition-flat-v1', generated_by='tools/build_competition.py',
                    original_sample_sha256=b._sha256(original),
                    original_entry_sha256=b._sha256(entry))
    manifest['rules_baseline'] = {'version':'v1.0','date':'2026-09-09',
                                 'note':'Official source documents remain in the source repository',
                                 'task_sha256':b._sha256(read('docs/任务书.md')),
                                 'protocol_sha256':b._sha256(read('docs/接口文档.md'))}
    directories = {'CoreGeek'}
    for name in blobs:
        directories.update(str(parent).replace('\\','/') for parent in Path(name).parents if str(parent)!='.')
    raw = b._tar_bytes(manifest, blobs, directories)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    sidecar.write_text(f'{b._sha256(raw)}  {output.name}\n', encoding='utf-8')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    verified = b.verify(output, sidecar=sidecar, expected_paths=set(blobs))
    if not verified['ok']:
        raise b.BuildError('flat package verification failed: '+str(verified['problems']))
    return {'commit':commit,'profile':manifest['profile'],'archive':str(output),
            'archive_sha256':b._sha256(raw),'archive_bytes':len(raw),
            'dirty_working_tree':bool(b.working_tree_dirty(repo)),'verification':verified}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path.cwd())
    parser.add_argument('--ref',default='HEAD')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=build(args.repo,args.ref,args.output)
    except (b.BuildError,OSError,ValueError,tarfile.TarError) as error:
        parser.exit(1,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()

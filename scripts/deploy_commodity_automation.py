"""Explicit Ubuntu-1-only rolling source deployment. No image, database or broker operations.

Default is a dry plan. The caller must stage hash-verified source under the existing
/srv/commodity-acts/releases directory first. No NIFTY service can be targeted.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path('/srv/commodity-acts')
TARGETS = ('crudeoilm', 'natgasmini', 'backend')
SOURCES = ('backend/app/commodity_acts/broker.py', 'backend/app/commodity_acts/config.py',
           'backend/app/commodity_acts/paper_read.py', 'backend/app/commodity_acts/request_budget.py',
           'worker/commodity_acts_paper_worker.py')


def require(condition, reason):
    if not condition: raise RuntimeError(reason)


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=240)
    require(result.returncode == 0, 'COMMODITY_DEPLOY_COMMAND_FAILED')
    return result.stdout


def identity(row):
    return {key: value for key, value in {'id': row['Id'], 'name': row['Name'], 'image': row['Image'],
        'started_at': row['State']['StartedAt'], 'restart_count': row['RestartCount'],
        'running': row['State']['Running']}.items()}


def inventory():
    ids = command(['docker', 'ps', '-q', '--no-trunc']).split()
    return json.loads(command(['docker', 'inspect', *ids])) if ids else []


def model_with_sources(original, release):
    result = copy.deepcopy(original)
    require(set(TARGETS) <= set(original['services']), 'COMMODITY_SERVICES_MISSING')
    for name in TARGETS:
        row = result['services'][name]
        require(row.get('restart') == 'unless-stopped', 'COMMODITY_RESTART_POLICY_CHANGED')
        volumes = row.setdefault('volumes', [])
        def target(value):
            return value.get('target') if isinstance(value, dict) else value.split(':')[1]
        destinations = {'/app/' + path for path in SOURCES}
        row['volumes'] = [value for value in volumes if target(value) not in destinations]
        row['volumes'].extend({'type': 'bind', 'source': str(release / path), 'target': '/app/' + path,
                               'read_only': True} for path in SOURCES)
        # All settings, risk policies, images, commands and limits remain identical.
        before, after = copy.deepcopy(original['services'][name]), copy.deepcopy(row)
        before.pop('volumes', None); after.pop('volumes', None)
        require(before == after, 'NON_MOUNT_SERVICE_CHANGE')
    require(all(result['services'][name] == row for name, row in original['services'].items() if name not in TARGETS),
            'PROTECTED_SERVICE_CONFIG_CHANGED')
    return result


def write_private(path, value):
    # Atomic replacement and private mode; credentials/configuration never printed.
    temporary = path.with_name(path.name + '.automation-tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    require(re.fullmatch('[0-9a-f]{40}', args.commit) is not None, 'INVALID_SOURCE_REVISION')
    release = Path(args.release_dir).resolve()
    require(release.parent == ROOT / 'releases' and release.is_dir(), 'WRONG_RELEASE_DIRECTORY')
    manifest = json.loads((release / 'automation-files.json').read_text())
    require(manifest['commit'] == args.commit and set(manifest['files']) == set(SOURCES), 'SOURCE_MANIFEST_MISMATCH')
    for path, checksum in manifest['files'].items():
        file = release / path
        require(file.is_file() and not file.is_symlink() and hashlib.sha256(file.read_bytes()).hexdigest() == checksum,
                'SOURCE_CHECKSUM_MISMATCH')
    base = ['docker', 'compose', '-p', 'commodity_acts', '--project-directory', str(ROOT), '--env-file',
            str(ROOT / '.env.production'), '-f', str(ROOT / 'compose.commodity.json')]
    rows = inventory()
    mapping = {row['Config']['Labels'].get('com.docker.compose.service'): row for row in rows
               if (row['Config'].get('Labels') or {}).get('com.docker.compose.project') == 'commodity_acts'}
    require(set(TARGETS) | {'postgres', 'frontend', 'gateway'} <= set(mapping), 'WRONG_COMMODITY_HOST_OR_SERVICES')
    for name in TARGETS:
        meta = mapping[name]['Config']['Labels']
        require(meta.get('com.docker.compose.project.working_dir') == str(ROOT)
                and meta.get('com.docker.compose.project.config_files') == str(ROOT / 'compose.commodity.json'), 'COMPOSE_MAPPING_CHANGED')
    original_text = (ROOT / 'compose.commodity.json').read_text()
    original = json.loads(original_text)
    updated = model_with_sources(original, release)
    print(json.dumps({'mode': 'APPLY' if args.apply else 'DRY_PLAN', 'targets': TARGETS,
                      'source_commit': args.commit, 'risk_or_environment_changes': False, 'migrations': False}))
    if not args.apply: return
    require(not (release / 'compose.before.json').exists(), 'DEPLOYMENT_ALREADY_ATTEMPTED')
    write_private(release / 'compose.before.json', original_text)
    changed = []
    try:
        write_private(ROOT / 'compose.commodity.json', json.dumps(updated, indent=2) + '\n')
        command(base + ['config', '--quiet'])
        for name in TARGETS:
            before = inventory()
            protected = [identity(row) for row in before if row['Id'] != mapping[name]['Id']]
            changed.append(name)
            command(base + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180', name])
            after = inventory()
            by_id = {row['Id']: identity(row) for row in after}
            require(all(by_id.get(row['id']) == row for row in protected), 'PROTECTED_CONTAINER_CHANGED')
            new = [row for row in after if (row['Config'].get('Labels') or {}).get('com.docker.compose.project') == 'commodity_acts'
                   and row['Config']['Labels'].get('com.docker.compose.service') == name]
            require(len(new) == 1 and new[0]['Image'] == mapping[name]['Image'], 'IMAGE_ID_CHANGED')
            print(json.dumps({'service': name, 'health': 'PASS', 'image_unchanged': True, 'other_containers_preserved': True}))
        write_private(release / 'deployment-result.json', json.dumps({'commit': args.commit,
            'deployed_at': datetime.now(timezone.utc).isoformat(), 'targets': TARGETS, 'nifty_modified': False}))
    except Exception:
        # Restore old source pointers only. No database downgrade/restore or image pull.
        write_private(ROOT / 'compose.commodity.json', original_text)
        for name in reversed(changed):
            command(base + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180', name])
        raise


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print('COMMODITY_AUTOMATION_DEPLOY_BLOCKED: ' + (str(exc) if type(exc) is RuntimeError else type(exc).__name__), file=sys.stderr)
        raise SystemExit(1)

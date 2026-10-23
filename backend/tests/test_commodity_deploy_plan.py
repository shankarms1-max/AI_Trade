import copy
from pathlib import Path
import pytest
from scripts.deploy_commodity_automation import SOURCES, TARGETS, model_with_sources


def test_plan_changes_only_readonly_source_mounts_of_existing_commodity_targets():
    original = {'services': {name: {'restart': 'unless-stopped', 'image': 'unchanged', 'environment': {'risk': 'unchanged'},
        'volumes': [{'source': '/old', 'target': '/app/' + SOURCES[0], 'read_only': True}, 'cache:/app/cache']} for name in TARGETS}}
    original['services'].update({name: {'image': 'protected', 'volumes': ['data:/var/lib/postgresql/data']} for name in ('postgres', 'frontend', 'gateway')})
    before = copy.deepcopy(original)
    updated = model_with_sources(original, Path('/srv/commodity-acts/releases/test'))
    assert original == before
    for name in ('postgres', 'frontend', 'gateway'):
        assert updated['services'][name] == original['services'][name]
    for name in TARGETS:
        assert updated['services'][name]['environment'] == original['services'][name]['environment']
        assert updated['services'][name]['volumes'][0] == 'cache:/app/cache'
        assert all(row['read_only'] is True for row in updated['services'][name]['volumes'][1:])


def test_missing_services_or_changed_restart_policy_block_plan():
    with pytest.raises(RuntimeError, match='MISSING'):
        model_with_sources({'services': {}}, Path('/tmp'))
    with pytest.raises(RuntimeError, match='RESTART'):
        model_with_sources({'services': {name: {'restart': 'no'} for name in TARGETS}}, Path('/tmp'))

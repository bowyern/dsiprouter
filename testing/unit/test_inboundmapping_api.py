"""HTTP/SQL regression tests for inbound route API parity.

Run: python -m pytest testing/unit/test_inboundmapping_api.py
Dependencies: Flask, SQLAlchemy>=2.0, pytest. No running dSIPRouter required.
"""
import ast
from enum import Enum
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from flask import Flask, jsonify
import pytest
from sqlalchemy import Column, Integer, String, Table, create_engine, exc
from sqlalchemy.orm import registry, sessionmaker
from werkzeug import exceptions


ROOT = Path(__file__).resolve().parents[2]
URL = '/api/v1/inboundmapping'


def load_definitions(path, names, namespace):
    """Execute real model/helper definitions without machine-specific startup."""
    tree = ast.parse((ROOT / path).read_text())
    tree.body = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    exec(compile(tree, str(path), 'exec'), namespace)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(sys, 'path', list(sys.path))

    def module(name, **values):
        mod = ModuleType(name)
        mod.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, mod)
        spec.loader.exec_module(mod)
        return mod

    settings = module('settings', FLT_INBOUND=9000, FLT_OUTBOUND=8000, FLT_FWD_MIN=20000,
                      FLT_CARRIER=8, FLT_PBX=9, FLT_MSTEAMS=17,
                      DID_PREFIX_ALLOWED_CHARS=set('0123456789+#*'))
    shared = module('shared')
    load_definitions('gui/shared.py', {'strFieldsToDict', 'dictToStrFields'}, shared.__dict__)
    database = module('database', Column=Column, String=String, UnsignedInt=Integer(),
                      Enum=Enum, settings=settings, dictToStrFields=shared.dictToStrFields)
    load_definitions('gui/database/__init__.py',
                     {'InboundMapping', 'GatewayGroups', 'dSIPHardFwd', 'dSIPFailFwd'}, database.__dict__)
    mapper = registry()
    # Match production's varchar group IDs, including both forwarding tables.
    routes_table = Table('dr_rules', mapper.metadata,
        Column('ruleid', Integer, primary_key=True), Column('groupid', String(255)),
        Column('prefix', String(64)), Column('gwlist', String(255)),
        Column('description', String(255)), Column('timerec', String(255)),
        Column('routeid', String(64)))
    mapper.map_imperatively(database.InboundMapping, routes_table)
    mapper.map_imperatively(database.GatewayGroups, Table('dr_gw_lists', mapper.metadata,
        Column('id', Integer, primary_key=True), Column('description', String(255)),
        Column('gwlist', String(255))))
    for model, name in ((database.dSIPHardFwd, 'dsip_hardfwd'), (database.dSIPFailFwd, 'dsip_failfwd')):
        mapper.map_imperatively(model, Table(name, mapper.metadata,
            Column('dr_ruleid', String(64), primary_key=True), Column('did', String(64)),
            Column('dr_groupid', String(64))))
    numbers = load('modules.numbers.db.dsip_number', 'gui/modules/numbers/db/dsip_number.py')
    engine = create_engine('sqlite://')
    mapper.metadata.create_all(engine)
    numbers.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    database.startSession = sessions
    database.DummySession = lambda: SimpleNamespace(rollback=lambda: None, close=lambda: None)
    state = {'kam_reload_required': False, 'dsip_reload_required': False}
    module('util.ipc', STATE_SHMEM_NAME='test', getSharedMemoryDict=lambda _: state)
    responses = module('modules.api.api_functions', jsonify=jsonify, sql_exceptions=exc,
        http_exceptions=exceptions, debugException=lambda *a, **kw: None,
        StatusCodes=SimpleNamespace(HTTP_OK=200, HTTP_INTERNAL_SERVER_ERROR=500),
        STATE_SHMEM_NAME='test', getSharedMemoryDict=lambda _: state)
    load_definitions('gui/modules/api/api_functions.py', {'createApiResponse', 'showApiError'}, responses.__dict__)
    functions = load('modules.api.inboundmapping.functions', 'gui/modules/api/inboundmapping/functions.py')
    routes = load('inboundmapping_test_routes', 'gui/modules/api/inboundmapping/routes.py')
    app = Flask(__name__)
    app.add_url_rule(URL, view_func=routes.handleInboundMappingRequest,
                     methods=['GET', 'POST', 'PUT', 'DELETE'])
    client = app.test_client()
    db = sessions()
    for group_id, kind in ((1, 9), (2, 8), (3, 9)):
        group = database.GatewayGroups(f'Group {group_id}', type=kind)
        group.id = group_id
        if kind == 9:
            group.description += ',lb:100,lb_ext:1100'
        db.add(group)
    outbound = database.InboundMapping('8000', '', '#2', 'name:Outbound')
    db.add(outbound)
    db.add(numbers.dSIPNumber('15551000', status='available'))
    db.commit()

    def request(method, *, body=None, query=None):
        response = client.open(URL, method=method, json=body, query_string=query)
        db.expire_all()
        return response

    yield SimpleNamespace(request=request, db=db, models=database, Number=numbers.dSIPNumber,
                          state=state, functions=functions, outbound_id=outbound.ruleid)
    db.close()
    mapper.dispose()
    engine.dispose()


def create(api, **fields):
    response = api.request('POST', body={'did': '15551000', **fields})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['kamreload'] is True
    return response.get_json()['data'][0]


def update(api, ruleid, **fields):
    response = api.request('PUT', query={'ruleid': ruleid}, body=fields)
    assert response.status_code == 200, response.get_json()
    return response.get_json()['data'][0]


def test_legacy_servers_and_query_selectors_remain_supported(api):
    row = create(api, servers=['66', '67'], name='Legacy')
    assert row['servers'] == ['66', '67']
    response = api.request('PUT', query={'did': '15551000'}, body={'name': 'Renamed'})
    assert response.status_code == 200
    data = response.get_json()['data'][0]
    assert data['servers'] == ['66', '67'] and data['name'] == 'Renamed'
    assert api.request('GET', query={'ruleid': row['ruleid']}).get_json()['data'][0] == data
    assert api.request('GET').get_json()['data'] == [data]
    assert api.db.query(api.Number).one().status == 'assigned'


def test_unassigned_and_load_balancing_choices(api):
    row = create(api, gwgroupid='0')
    assert row['gwgroupid'] == '' and row['lb_enabled'] is False
    assert api.db.query(api.Number).one().status == 'available'
    other = create(api, did='15552000', gwgroupid='1')
    row = update(api, row['ruleid'], gwgroupid='lb_1_1100')
    assert row['gwgroupid'] == '1' and row['lb_enabled'] is True
    peer = api.request('GET', query={'ruleid': other['ruleid']}).get_json()['data'][0]
    assert peer['lb_enabled'] is True
    row = update(api, row['ruleid'], name='Name only')
    assert row['lb_enabled'] is True
    update(api, row['ruleid'], lb_enabled=False)
    assert api.request('GET', query={'ruleid': other['ruleid']}).get_json()['data'][0]['lb_enabled'] is False
    update(api, row['ruleid'], gwgroupid=None)
    assert api.db.query(api.Number).one().status == 'available'


@pytest.mark.parametrize('enabled,prefix,model_name', [
    ('hardfwd_enabled', 'hf', 'dSIPHardFwd'), ('failfwd_enabled', 'ff', 'dSIPFailFwd'),
])
def test_forwarding_lifecycle_and_partial_updates(api, enabled, prefix, model_name):
    model = getattr(api.models, model_name)
    row = create(api, gwgroupid=1, **{enabled: True, prefix + '_fwddid': '15559999'})
    rid = row['ruleid']
    assert row[enabled] is True and row[prefix + '_gwgroupid'] == ''
    assert api.db.query(model).one().dr_groupid == '8000'
    row = update(api, rid, **{prefix + '_gwgroupid': 2})
    group_id = api.db.query(model).one().dr_groupid
    forward = api.db.query(api.models.InboundMapping).filter_by(groupid=group_id).one()
    assert int(group_id) >= 20000 and forward.gwlist == '#2'
    row = update(api, rid, **{prefix + '_fwddid': '', prefix + '_gwgroupid': 3})
    assert row[prefix + '_fwddid'] == '' and row[prefix + '_gwgroupid'] == '3'
    assert api.db.query(model).one().dr_groupid == group_id
    row = update(api, rid, name='Unrelated edit')
    assert row[enabled] is True and row[prefix + '_gwgroupid'] == '3'
    row = update(api, rid, **{prefix + '_gwgroupid': ''})
    assert api.db.query(model).one().dr_groupid == '8000'
    assert api.db.query(api.models.InboundMapping).filter_by(groupid=group_id).count() == 0
    row = update(api, rid, **{enabled: False})
    assert row[enabled] is False and api.db.query(model).count() == 0
    assert api.db.query(api.models.InboundMapping).filter_by(ruleid=api.outbound_id).one().gwlist == '#2'


def test_both_forwards_allocate_distinct_groups_and_delete_cleans_up(api):
    # The previous highest group may cross a varchar/numeric sorting boundary.
    api.db.add(api.models.InboundMapping('99999', '', '#2', 'name:Unrelated forward'))
    api.db.commit()
    row = create(api, gwgroupid=1, hardfwd_enabled=True, hf_fwddid='15553000', hf_gwgroupid=2,
                 failfwd_enabled=True, ff_fwddid='15554000', ff_gwgroupid=3)
    hard = api.db.query(api.models.dSIPHardFwd).one()
    fail = api.db.query(api.models.dSIPFailFwd).one()
    assert (hard.dr_groupid, fail.dr_groupid) == ('100000', '100001')
    response = api.request('DELETE', query={'ruleid': row['ruleid']})
    assert response.status_code == 200
    assert api.db.query(api.models.dSIPHardFwd).count() == api.db.query(api.models.dSIPFailFwd).count() == 0
    assert {route.groupid for route in api.db.query(api.models.InboundMapping)} == {'8000', '99999'}
    assert api.db.query(api.Number).one().status == 'available'


def test_hard_forward_outbound_does_not_allocate_failover_in_outbound_range(api):
    create(api, hardfwd_enabled=True, hf_fwddid='15553000',
           failfwd_enabled=True, ff_fwddid='15554000', ff_gwgroupid=2)
    assert api.db.query(api.models.dSIPHardFwd).one().dr_groupid == '8000'
    assert api.db.query(api.models.dSIPFailFwd).one().dr_groupid == '20000'


def test_reads_and_edits_existing_ui_forwarding_records(api):
    route = api.models.InboundMapping('9000', '15551000', '#1', 'name:From UI,lb_enabled:1,custom:keep')
    forward = api.models.InboundMapping('20000', '', '#2', 'name:Hard Forward')
    api.db.add_all([route, forward])
    api.db.flush()
    api.db.add(api.models.dSIPHardFwd(str(route.ruleid), '15559999', '20000'))
    api.db.commit()
    result = api.request('GET', query={'did': '15551000'}).get_json()['data'][0]
    assert result['hardfwd_enabled'] is True and result['hf_gwgroupid'] == '2'
    assert result['hf_fwddid'] == '15559999' and result['lb_enabled'] is True
    update(api, route.ruleid, hardfwd_enabled=False, name='Edited via API')
    assert api.db.query(api.models.dSIPHardFwd).count() == 0
    assert api.db.query(api.models.InboundMapping).filter_by(groupid='20000').count() == 0
    api.db.refresh(route)
    assert 'custom:keep' in route.description and 'lb_enabled:1' in route.description


def test_delete_by_did_preserves_forward_groups_still_used_elsewhere(api):
    row = create(api, hardfwd_enabled=True, hf_gwgroupid=2)
    group = api.db.query(api.models.dSIPHardFwd).one().dr_groupid
    # External integrations may share a forwarding group. Removing one owner
    # must not remove the destination of another owner.
    other = create(api, did='15552000')
    api.db.add(api.models.dSIPFailFwd(str(other['ruleid']), '15559999', group))
    api.db.commit()
    assert api.request('DELETE', query={'did': row['did']}).status_code == 200
    assert api.db.query(api.models.InboundMapping).filter_by(groupid=group).count() == 1
    assert api.db.query(api.models.dSIPFailFwd).count() == 1


def test_rename_by_did_preserves_forwarding_and_releases_old_number(api):
    api.db.add(api.Number('15552000', status='available'))
    api.db.commit()
    row = create(api, gwgroupid=1, hardfwd_enabled=True, hf_fwddid='15559999')
    response = api.request('PUT', query={'did': '15551000'}, body={'did': '15552000'})
    assert response.status_code == 200
    updated = response.get_json()['data'][0]
    assert updated['did'] == '15552000' and updated['hardfwd_enabled'] is True
    assert updated['ruleid'] == row['ruleid']
    assert {n.did: n.status for n in api.db.query(api.Number)} == {
        '15551000': 'available', '15552000': 'assigned'}


def test_invalid_forward_rolls_back_entire_creation(api):
    response = api.request('POST', body={
        'did': '15551000', 'gwgroupid': 1,
        'hardfwd_enabled': True, 'hf_gwgroupid': 2,
        'failfwd_enabled': True, 'ff_gwgroupid': 999,
    })
    assert response.status_code == 400
    assert api.db.query(api.models.InboundMapping).count() == 1
    assert api.db.query(api.models.dSIPHardFwd).count() == 0
    assert api.db.query(api.Number).one().status == 'available'
    assert api.state['kam_reload_required'] is False


def test_duplicate_rename_rolls_back_and_preserves_settings(api):
    row = create(api, hardfwd_enabled=True, hf_gwgroupid=2)
    create(api, did='15552000')
    response = api.request('PUT', query={'ruleid': row['ruleid']},
                           body={'did': '15552000', 'hardfwd_enabled': False})
    assert response.status_code == 400
    saved = api.request('GET', query={'ruleid': row['ruleid']}).get_json()['data'][0]
    assert saved['did'] == '15551000' and saved['hardfwd_enabled'] is True


@pytest.mark.parametrize('body', [
    [], {}, {'name': 'Missing DID'}, {'did': 'bad'}, {'did': '1', 'unknown': True},
    {'did': '1', 'servers': []}, {'did': '1', 'servers': ['abc']},
    {'did': '1', 'servers': ['1'], 'gwgroupid': 1}, {'did': '1', 'gwgroupid': 2},
    {'did': '1', 'gwgroupid': 999}, {'did': '1', 'gwgroupid': False},
    {'did': '1', 'hardfwd_enabled': 'false'}, {'did': '1', 'hf_fwddid': '2'},
    {'did': '1', 'hardfwd_enabled': True, 'hf_fwddid': 'invalid'},
    {'did': '1', 'lb_enabled': True}, {'did': '1', 'gwgroupid': 'lb_1_bad'},
    {'did': '1', 'servers': ['#2'], 'lb_enabled': True},
    {'did': '1', 'gwgroupid': 'lb_1_100', 'lb_enabled': False},
])
def test_bad_json_fields_return_400(api, body):
    assert api.request('POST', body=body).status_code == 400


@pytest.mark.parametrize('method,query,expected', [
    ('GET', {'ruleid': 999}, 404), ('PUT', {'ruleid': 999}, 404),
    ('DELETE', {'did': 'missing'}, 404), ('PUT', None, 400), ('DELETE', None, 400),
    ('GET', {'ruleid': 'bad'}, 400), ('GET', {'ruleid': 0}, 400),
    ('GET', {'ruleid': 1, 'did': '1'}, 400), ('GET', {'unknown': '1'}, 400),
    ('GET', [('did', '1'), ('did', '2')], 400),
])
def test_selectors_and_missing_routes(api, method, query, expected):
    assert api.request(method, query=query, body={'name': 'Update'} if method == 'PUT' else None).status_code == expected


def test_cannot_modify_outbound_routes_through_inbound_api(api):
    assert api.request('DELETE', query={'ruleid': api.outbound_id}).status_code == 404
    assert api.db.query(api.models.InboundMapping).filter_by(ruleid=api.outbound_id).count() == 1

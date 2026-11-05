"""Isolated HTTP/SQL regression tests; no installed dSIPRouter or MySQL needed.

Run: python -m pytest testing/unit/test_numbers_pagination.py
Dependencies: Flask, SQLAlchemy>=2.0, pytest.
"""
import ast
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from flask import Flask, jsonify, request
import pytest
from sqlalchemy import create_engine, event, exc
from sqlalchemy.orm import sessionmaker
from werkzeug import exceptions


ROOT = Path(__file__).resolve().parents[2]


def load_definitions(path, names, namespace):
    """Use actual helpers without importing machine-specific service startup."""
    tree = ast.parse((ROOT / path).read_text())
    tree.body = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    exec(compile(tree, str(path), 'exec'), namespace)


@pytest.fixture
def numbers(monkeypatch):
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

    model = load('modules.numbers.db.dsip_number', 'gui/modules/numbers/db/dsip_number.py')
    engine = create_engine('sqlite://')
    model.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    db = sessions()
    db.add_all(model.dSIPNumber(
        did=f'1313555{index:04}', status='available' if index % 2 else 'assigned',
        carrier='Alpha' if index % 3 else 'Beta', pool='primary' if index < 300 else 'reserve',
        assigned_reference_id=f'customer-{index % 5}', assigned_length='30 days',
    ) for index in range(603))
    db.commit()
    statements = []
    event.listen(engine, 'before_cursor_execute',
                 lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
    module('database', startSession=sessions, DummySession=lambda: None, InboundMapping=object)
    module('settings', DEBUG=False, FLT_INBOUND=9000)
    shared = module('shared', request=request, json=json, debugException=lambda *a, **kw: None,
                    debugEndpoint=lambda: None, showError=lambda **kw: None)
    load_definitions('gui/shared.py', {'getRequestData', 'rowToDict'}, shared.__dict__)
    state = {'kam_reload_required': False, 'dsip_reload_required': False}
    api = module('modules.api.api_functions', jsonify=jsonify, sql_exceptions=exc,
                 http_exceptions=exceptions, debugException=lambda *a, **kw: None,
                 StatusCodes=SimpleNamespace(HTTP_OK=200, HTTP_INTERNAL_SERVER_ERROR=500),
                 STATE_SHMEM_NAME='test', getSharedMemoryDict=lambda _: state,
                 api_security=lambda func: func)
    load_definitions('gui/modules/api/api_functions.py', {'createApiResponse', 'showApiError'}, api.__dict__)
    routes = load('numbers_test_routes', 'gui/modules/numbers/api/routes.py')
    app = Flask(__name__)
    app.register_blueprint(routes.numbers_api)
    yield SimpleNamespace(client=app.test_client(), db=db, Number=model.dSIPNumber,
                          statements=statements)
    db.close()
    engine.dispose()


URL = '/api/numbers/v1/number'


def test_default_is_bounded_and_query_uses_limit(numbers):
    response = numbers.client.get(URL)
    assert response.status_code == 200
    body = response.get_json()
    assert len(body['data']) == body['length'] == 25
    assert body['recordsTotal'] == body['recordsFiltered'] == 603
    assert body['start'] == 0
    assert any('LIMIT' in sql and 'OFFSET' in sql for sql in numbers.statements)


def test_pages_have_stable_order_and_no_overlap(numbers):
    first = numbers.client.get(URL, query_string={'draw': 3, 'length': 10}).get_json()
    second = numbers.client.get(URL, query_string={'draw': 4, 'length': 10, 'start': 10}).get_json()
    assert second['draw'] == 4
    assert [int(row['id']) for row in first['data']] == list(range(1, 11))
    assert [int(row['id']) for row in second['data']] == list(range(11, 21))


def test_global_search_and_exact_filters_count_before_paging(numbers):
    response = numbers.client.get(URL, query_string={
        'source': 'Alpha', 'status': 'available', 'pool': 'primary',
        'search[value]': 'customer-1 30', 'length': 3,
    })
    body = response.get_json()
    assert response.status_code == 200
    assert body['recordsTotal'] == 603
    expected = [i for i in range(300) if i % 3 and i % 2 and i % 5 == 1]
    assert body['recordsFiltered'] == len(expected)
    assert len(body['data']) == 3


def test_pattern_filters_and_multi_column_order(numbers):
    body = numbers.client.get(URL, query_string={
        'starts_with': '1313', 'contains': '555', 'ends_with': '1',
        'order[0][column]': 4, 'order[0][dir]': 'asc',
        'order[1][column]': 1, 'order[1][dir]': 'desc', 'length': 500,
    }).get_json()
    rows = body['data']
    assert len(rows) == body['recordsFiltered'] == 61
    assert all(row['did'].endswith('1') for row in rows)
    assert rows == sorted(rows, key=lambda row: (row['carrier'], -int(row['id'])))


def test_empty_and_out_of_range_pages_keep_counts(numbers):
    empty = numbers.client.get(URL, query_string={'search[value]': 'no match'}).get_json()
    assert empty['data'] == [] and empty['recordsFiltered'] == 0
    beyond = numbers.client.get(URL, query_string={'start': 1000}).get_json()
    assert beyond['data'] == [] and beyond['recordsFiltered'] == 603


def test_wildcards_are_literal_and_page_size_is_capped(numbers):
    body = numbers.client.get(URL, query_string={'search[value]': '%'}).get_json()
    assert body['recordsFiltered'] == 0
    body = numbers.client.get(URL, query_string={'length': 100000}).get_json()
    assert len(body['data']) == body['length'] == 500


@pytest.mark.parametrize('params', [
    {'start': -1}, {'length': -1}, {'length': 0}, {'length': 'abc'},
    {'draw': '<script>'}, {'order[0][column]': 0}, {'order[0][column]': 99},
    {'order[0][column]': 2, 'order[0][dir]': 'DROP TABLE'},
    [('length', '1'), ('length', '2')],
])
def test_invalid_pagination_and_order_return_400(numbers, params):
    assert numbers.client.get(URL, query_string=params).status_code == 400

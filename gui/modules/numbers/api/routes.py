import sys, os
if sys.path[0] != '/etc/dsiprouter/gui':
    sys.path.insert(0, '/etc/dsiprouter/gui')

from flask import Blueprint, session,render_template, request, jsonify
from modules.numbers.db.dsip_number import dSIPNumber
from database import startSession, DummySession, InboundMapping
from modules.api.api_functions import createApiResponse, showApiError, api_security
from shared import getRequestData, rowToDict, showError, debugException, debugEndpoint
from sqlalchemy import or_, cast, String
from werkzeug import exceptions as http_exceptions
import settings
import csv
import io

numbers_api = Blueprint('numbers', __name__, template_folder='../templates', static_folder='../static', static_url_path='/numbers/static')


def _sync_available_inbound_route(db, did):
    normalized_did = did.lstrip('+') if did else ''
    if not normalized_did:
        return

    route = db.query(InboundMapping).filter(
        InboundMapping.groupid == settings.FLT_INBOUND,
        or_(
            InboundMapping.prefix == normalized_did,
            InboundMapping.prefix == '+' + normalized_did,
        )
    ).first()

    if route is None:
        db.add(InboundMapping(settings.FLT_INBOUND, normalized_did, '', f'name:{normalized_did}'))
    else:
        route.prefix = normalized_did
        route.gwlist = ''
        route.description = f'name:{normalized_did}'

@numbers_api.route('/gui/numbers', methods=['GET'])
def numbers_index():
   
    try:
        if (settings.DEBUG):
            debugEndpoint()

        if not session.get('logged_in'):
            return render_template('index.html', version=settings.VERSION)
        else:
            action = request.args.get('action')
            return render_template('numbers.html', show_add_onload=action, version=settings.VERSION)
            
    except http_exceptions.HTTPException as ex:
        debugException(ex)
        return showError(type='http', code=ex.code, msg=ex.description)
    except Exception as ex:
        debugException(ex, log_ex=False, print_ex=True, showstack=False)
        error = "server"
        return showError(type=error)


@numbers_api.route('/api/numbers/v1/number', methods=['GET'])
@api_security
def get_numbers():
    """List a bounded page of numbers using the DataTables server-side protocol."""
    db = DummySession()
    try:
        db = startSession()

        payload = getRequestData() or {}

        def scalar(key, default=None):
            value = payload.get(key, default)
            if isinstance(value, list):
                if len(value) != 1:
                    raise http_exceptions.BadRequest(f'{key} must have one value')
                value = value[0]
            return value

        def integer(key, default, minimum):
            try:
                value = int(scalar(key, default))
            except (TypeError, ValueError):
                raise http_exceptions.BadRequest(f'{key} must be an integer')
            if value < minimum:
                raise http_exceptions.BadRequest(f'{key} must be at least {minimum}')
            return value

        draw = integer('draw', 0, 0)
        start = integer('start', 0, 0)
        length = min(integer('length', 25, 1), 500)

        # Only known database columns can be searched or sorted. Column zero and
        # the action columns in the UI are deliberately absent from this map.
        columns = {
            1: dSIPNumber.id, 2: dSIPNumber.did, 3: dSIPNumber.status,
            4: dSIPNumber.carrier, 5: dSIPNumber.pool,
            6: dSIPNumber.assigned_date, 7: dSIPNumber.assigned_length,
            8: dSIPNumber.assigned_reference_id,
        }

        q = db.query(dSIPNumber)
        total = q.count()

        for key, column in (('source', dSIPNumber.carrier),
                            ('status', dSIPNumber.status), ('pool', dSIPNumber.pool)):
            value = scalar(key)
            if value:
                q = q.filter(column == value)
        for key, match in (('starts_with', dSIPNumber.did.startswith),
                           ('contains', dSIPNumber.did.contains),
                           ('ends_with', dSIPNumber.did.endswith)):
            value = scalar(key)
            if value:
                q = q.filter(match(value, autoescape=True))

        search = scalar('search[value]', '')
        if search:
            # Match each whitespace-separated term across the displayed fields,
            # as the previous client-side global search did.
            for term in search.split():
                q = q.filter(or_(*(cast(column, String).icontains(term, autoescape=True)
                                  for column in columns.values())))
        filtered = q.count()

        ordering = []
        for index in range(len(columns)):
            key = f'order[{index}][column]'
            if key not in payload:
                break
            column_id = integer(key, 1, 0)
            direction = scalar(f'order[{index}][dir]', 'asc')
            if column_id not in columns or direction not in ('asc', 'desc'):
                raise http_exceptions.BadRequest('Invalid sort column or direction')
            column = columns[column_id]
            ordering.append(column.desc() if direction == 'desc' else column.asc())

        # A unique tie-breaker keeps page boundaries stable when values repeat.
        results = q.order_by(*ordering, dSIPNumber.id.asc()).offset(start).limit(length).all()
        data = [rowToDict(r) for r in results]
        count = len(data)

        response, status_code = createApiResponse(msg=str(count) + ' Numbers Retrieved', data=data)
        body = response.get_json()
        body.update(draw=draw, recordsTotal=total, recordsFiltered=filtered,
                    start=start, length=length)
        return jsonify(body), status_code

    except Exception as ex:
        db.rollback()
        db.flush()
        return showApiError(ex)
    finally:
        db.close()


@numbers_api.route('/api/numbers/v1/number', methods=['POST'])
@api_security
def create_number():
    db = DummySession()
    try:
        db = startSession()
        payload = getRequestData()
        did = payload.get('did')
        if not did:
            raise http_exceptions.BadRequest('did is required')

        # CHeck if DID exists
        existing = db.query(dSIPNumber).filter(
            or_(
                dSIPNumber.did == did,
                dSIPNumber.did == "+" + did,
            )).first()
        
        if existing is not None:
            raise http_exceptions.Conflict('DID already exists')
        

        number = dSIPNumber(
            did=did,
            status=payload.get('status'),
            carrier=payload.get('carrier'),
            pool=payload.get('pool'),
            assigned_length=payload.get('assigned_length'),
            assigned_reference_id=payload.get('assigned_reference_id'),
           
        )

        # define an assignd_date if the assigned_length is set
        if payload.get('assigned_length'):
            from datetime import datetime
            number.assigned_date = datetime.utcnow()

        if number.status == 'available':
            _sync_available_inbound_route(db, number.did)

        db.add(number)
        db.flush()
        db.commit()

        return createApiResponse(msg='Number created', data=[rowToDict(number)])
    except Exception as ex:
        db.rollback()
        db.flush()
        return showApiError(ex)
    finally:
        db.close()


@numbers_api.route('/api/numbers/v1/number/<int:number_id>', methods=['PUT'])
@api_security
def update_number(number_id):
    db = DummySession()
    try:
        db = startSession()
        payload = getRequestData()

        number = db.query(dSIPNumber).filter(dSIPNumber.id == number_id).first()
        if number is None:
            raise http_exceptions.NotFound('Number not found')

        # update fields
        for key in ('did', 'status', 'carrier', 'pool', 'assigned_length', 'assigned_reference_id', 'assigned_date'):
            if key in payload:
                setattr(number, key, payload.get(key))
            if key == 'did' and payload.get('did'):
                # Check if DID exists
                existing = db.query(dSIPNumber).filter(
                    or_(
                        dSIPNumber.did == payload.get('did'),
                        dSIPNumber.did == "+" + payload.get('did'),
                    ),
                    dSIPNumber.id != number_id
                ).first()
                
                if existing is not None:
                    raise http_exceptions.Conflict('DID already exists')
                
        
        # define an assignd_date if the assigned_length is set
        if payload.get('assigned_length'):
            from datetime import datetime
            number.assigned_date = datetime.utcnow()

        if number.status == 'available':
            _sync_available_inbound_route(db, number.did)

        db.add(number)
        db.flush()
        db.commit()

        return createApiResponse(msg='Number updated', data=[rowToDict(number)])
    except Exception as ex:
        db.rollback()
        db.flush()
        return showApiError(ex)
    finally:
        db.close()


@numbers_api.route('/api/numbers/v1/number/<int:number_id>', methods=['DELETE'])
@api_security
def delete_number(number_id):
    db = DummySession()
    try:
        db = startSession()
        number = db.query(dSIPNumber).filter(dSIPNumber.id == number_id).first()
        if number is None:
            raise http_exceptions.NotFound('Number not found')

        db.delete(number)
        db.flush()
        db.commit()

        return createApiResponse(msg='Number deleted', data=[{'id': number_id}])
    except Exception as ex:
        db.rollback()
        db.flush()
        return showApiError(ex)
    finally:
        db.close()


@numbers_api.route('/api/numbers/v1/number', methods=['DELETE'])
@api_security
def delete_numbers_by_dids():
    db = DummySession()
    try:
        db = startSession()
        payload = getRequestData()
        did = payload.get('did')
        dids = payload.get('dids', [])
        id = payload.get('id', [])
        if did:
            dids = [did]
        if not dids and not id:
            raise http_exceptions.BadRequest('did or the id of the did is required')

        deleted = []
        # Delete by DIDs
        for d in dids:
            normalized = d.lstrip('+')
            number = db.query(dSIPNumber).filter(
                or_(
                    dSIPNumber.did == normalized,
                    dSIPNumber.did == "+" + normalized,
                )).first()
            
            if number is not None:
                db.delete(number)
                deleted.append({'did': d, 'id': number.id})

        # Delete by IDs
        for entry in id:
            number = db.query(dSIPNumber).filter(dSIPNumber.id == entry).first()
            if number is not None:
                db.delete(number)
                deleted.append({'id': entry, 'did': number.did})   
            
        # If nothing deleted, raise not found    
        if not deleted:
            raise http_exceptions.NotFound('No numbers found to delete')

        db.flush()
        db.commit()

        return createApiResponse(msg=f'{len(deleted)} Numbers deleted', data=deleted)
    except Exception as ex:
        db.rollback()
        db.flush()
        return showApiError(ex)
    finally:
        db.close()


@numbers_api.route('/api/numbers/v1/number/bulk', methods=['POST'])
@api_security
def bulk_create_numbers():
    db = DummySession()
    try:
        db = startSession()
        file = request.files.get('file')
        if not file:
            raise http_exceptions.BadRequest('CSV file is required')

        # Read CSV
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = csv.reader(stream)
        header = next(csv_input)  # Skip header

        numbers = []
        for row in csv_input:
            if len(row) < 1:
                continue
            did = row[0].strip()
            status = row[1].strip() if len(row) > 1 else None
            carrier = row[2].strip() if len(row) > 2 else None
            pool = row[3].strip() if len(row) > 3 else None
            assigned_length = row[4].strip() if len(row) > 4 else None
            assigned_reference_id = row[5].strip() if len(row) > 5 else None

            # Check if DID exists
            existing = db.query(dSIPNumber).filter(
                or_(
                    dSIPNumber.did == did,
                    dSIPNumber.did == "+" + did,
                )).first()
            if existing is not None:
                continue  # Skip existing

            number = dSIPNumber(
                did=did,
                status=status,
                carrier=carrier,
                pool=pool,
                assigned_length=assigned_length,
                assigned_reference_id=assigned_reference_id,
            )
            if assigned_length:
                from datetime import datetime
                number.assigned_date = datetime.utcnow()

            db.add(number)
            numbers.append(number)

        db.flush()
        db.commit()

        data = [rowToDict(n) for n in numbers]
        return createApiResponse(msg=f'{len(numbers)} Numbers created', data=data)
    except Exception as ex:
        db.rollback()
        db.flush()
        return showApiError(ex)
    finally:
        db.close()

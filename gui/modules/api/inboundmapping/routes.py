"""JSON handler registered by the existing API blueprint."""
from flask import request
from werkzeug import exceptions as http_exceptions
from database import startSession, DummySession, InboundMapping
from modules.api.api_functions import createApiResponse, showApiError
from modules.api.inboundmapping.functions import (
    validateInboundRouteBody, serializeInboundRoute, saveInboundRoute, deleteInboundRoute,
)
from util.ipc import STATE_SHMEM_NAME, getSharedMemoryDict
import settings


def handleInboundMappingRequest():
    db = DummySession()
    try:
        if set(request.args) - {'ruleid', 'did'}:
            raise http_exceptions.BadRequest('Request argument not recognized')
        if any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise http_exceptions.BadRequest('Supply each selector only once')
        if 'ruleid' in request.args and 'did' in request.args:
            raise http_exceptions.BadRequest('Select by ruleid or did, not both')
        if request.method == 'POST' and request.args:
            raise http_exceptions.BadRequest('POST fields belong in the JSON body')

        db = startSession()
        query = db.query(InboundMapping).filter(InboundMapping.groupid == settings.FLT_INBOUND)
        selected = bool(request.args)
        if 'ruleid' in request.args:
            try:
                rule_id = int(request.args['ruleid'])
                if rule_id <= 0:
                    raise ValueError()
            except ValueError:
                raise http_exceptions.BadRequest('ruleid must be a positive integer')
            query = query.filter(InboundMapping.ruleid == rule_id)
        elif 'did' in request.args:
            query = query.filter(InboundMapping.prefix == request.args['did'])

        if request.method == 'GET':
            rows = query.order_by(InboundMapping.ruleid).all()
            if selected and not rows:
                raise http_exceptions.NotFound('No matching inbound route found')
            return createApiResponse(msg='Rules Found' if rows else 'No Rules Found',
                                     data=[serializeInboundRoute(db, row) for row in rows])

        if request.method in ('PUT', 'DELETE') and not selected:
            raise http_exceptions.BadRequest('ruleid or did is required')
        row = query.with_for_update().first() if selected else None
        if selected and row is None:
            raise http_exceptions.NotFound('No matching inbound route found')

        if request.method in ('POST', 'PUT'):
            # Query-string selectors must not be merged into the update body:
            # ?did=old with {"did": "new"} must rename the selected route.
            data = validateInboundRouteBody(request.get_json(), creating=request.method == 'POST')
            row = saveInboundRoute(db, data, row)
            result = [serializeInboundRoute(db, row)]
            message = 'Rule Created' if request.method == 'POST' else 'Rule Updated'
        else:
            deleteInboundRoute(db, row)
            result = []
            message = 'Rule Deleted'
        db.commit()
        getSharedMemoryDict(STATE_SHMEM_NAME)['kam_reload_required'] = True
        return createApiResponse(msg=message, data=result, kamreload=True)
    except Exception as ex:
        db.rollback()
        return showApiError(ex)
    finally:
        db.close()

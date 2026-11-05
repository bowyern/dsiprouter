"""Inbound route and forwarding operations. The caller owns the transaction."""
import re

from sqlalchemy import or_, cast, Integer
from werkzeug import exceptions as http_exceptions
from database import InboundMapping, GatewayGroups, dSIPHardFwd, dSIPFailFwd
from modules.numbers.db.dsip_number import dSIPNumber
from shared import dictToStrFields, strFieldsToDict
import settings


FORWARDING = (
    ('hardfwd_enabled', 'hf', dSIPHardFwd, 'Hard'),
    ('failfwd_enabled', 'ff', dSIPFailFwd, 'Failover'),
)
VALID_FIELDS = {
    'did', 'name', 'servers', 'gwgroupid', 'lb_enabled',
    'hardfwd_enabled', 'hf_fwddid', 'hf_gwgroupid',
    'failfwd_enabled', 'ff_fwddid', 'ff_gwgroupid',
}


def _boolean(value, field):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise http_exceptions.BadRequest(f'{field} must be a boolean or 0/1')


def _group_id(value, field):
    if isinstance(value, bool):
        raise http_exceptions.BadRequest(f'{field} must be a group ID, not a boolean')
    if value is None or value == '' or value == 0 or value == '0':
        return ''
    if not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise http_exceptions.BadRequest(f'{field} must be a positive group ID, or empty')
    return str(value)


def validateInboundRouteBody(data, creating=False):
    if not isinstance(data, dict) or not data:
        raise http_exceptions.BadRequest('A nonempty JSON object is required')
    if set(data) - VALID_FIELDS:
        raise http_exceptions.BadRequest('Request data argument not recognized')
    data = dict(data)
    if creating and 'did' not in data:
        raise http_exceptions.BadRequest('did is required')
    if 'servers' in data and 'gwgroupid' in data:
        raise http_exceptions.BadRequest('Use either servers or gwgroupid, not both')
    for field in ('did', 'hf_fwddid', 'ff_fwddid'):
        if field in data:
            value = data[field]
            if not isinstance(value, str) or len(value) > 64 or any(
                    char not in settings.DID_PREFIX_ALLOWED_CHARS for char in value):
                raise http_exceptions.BadRequest(f'{field} is improperly formatted')
    if 'name' in data:
        if not isinstance(data['name'], str) or ',' in data['name']:
            raise http_exceptions.BadRequest('name must be a string without commas')
    for field in ('lb_enabled', 'hardfwd_enabled', 'failfwd_enabled'):
        if field in data:
            data[field] = _boolean(data[field], field)
    for field in ('gwgroupid', 'hf_gwgroupid', 'ff_gwgroupid'):
        if field not in data:
            continue
        value = data[field]
        # Accept the values used by both LB choices in the UI dropdown. The UI
        # stores the group ID and lb_enabled, not the dispatcher-set suffix.
        if field == 'gwgroupid' and isinstance(value, str) and value.startswith('lb_'):
            match = re.fullmatch(r'lb_([1-9][0-9]*)_([1-9][0-9]*)', value)
            if not match or data.get('lb_enabled') is False:
                raise http_exceptions.BadRequest('Invalid or conflicting load-balancing selection')
            value = match.group(1)
            data['lb_enabled'] = True
        data[field] = _group_id(value, field)
    if 'servers' in data:
        servers = data['servers']
        if not isinstance(servers, list) or not 1 <= len(servers) <= 2:
            raise http_exceptions.BadRequest('servers must contain one or two gateway IDs')
        if any(isinstance(server, bool) or not re.fullmatch(r'#?[1-9][0-9]*', str(server))
               for server in servers):
            raise http_exceptions.BadRequest('Invalid server ID')
        data['servers'] = [str(server) for server in servers]
    return data


def _description(row):
    return strFieldsToDict(row.description) if row.description else {}


def _gateway_group(gwlist):
    return gwlist[1:] if re.fullmatch(r'#[1-9][0-9]*', gwlist or '') else ''


def _check_group(db, group_id, field, endpoint_only=False):
    if not group_id:
        return
    group = db.query(GatewayGroups).filter(GatewayGroups.id == int(group_id)).first()
    allowed = {str(settings.FLT_PBX)}
    if not endpoint_only:
        allowed.add(str(settings.FLT_CARRIER))
    if group is None or _description(group).get('type') not in allowed:
        raise http_exceptions.BadRequest(f'{field} does not identify an available '
                                        + ('endpoint group' if endpoint_only else 'carrier/endpoint group'))


def syncNumberStatus(db, did, gwlist):
    normalized = did.lstrip('+') if did else ''
    if normalized:
        number = db.query(dSIPNumber).filter(or_(
            dSIPNumber.did == normalized, dSIPNumber.did == '+' + normalized)).first()
        if number is not None:
            number.status = 'assigned' if gwlist else 'available'


def _forward_route(db, group_id):
    if group_id is None or int(group_id) < settings.FLT_FWD_MIN:
        return None
    return db.query(InboundMapping).filter(InboundMapping.groupid == group_id).first()


def serializeInboundRoute(db, row):
    desc = _description(row)
    data = {
        'ruleid': row.ruleid, 'did': row.prefix, 'name': desc.get('name', ''),
        'servers': (row.gwlist or '').split(','),
        'gwgroupid': _gateway_group(row.gwlist),
        'lb_enabled': desc.get('lb_enabled') == '1',
    }
    for enabled, prefix, model, label in FORWARDING:
        forwarding = db.query(model).filter(model.dr_ruleid == str(row.ruleid)).first()
        route = _forward_route(db, forwarding.dr_groupid) if forwarding else None
        data[enabled] = forwarding is not None
        data[prefix + '_fwddid'] = forwarding.did if forwarding else ''
        data[prefix + '_gwgroupid'] = _gateway_group(route.gwlist) if route else ''
    return data


def _remove_forward_route(db, group_id):
    route = _forward_route(db, group_id)
    if route is None:
        return
    # Never remove shared outbound routing, or a group still referenced by a
    # different forwarding record (including records created outside this API).
    for _, _, model, _ in FORWARDING:
        if db.query(model).filter(model.dr_groupid == str(group_id)).first() is not None:
            return
    db.delete(route)


def _new_forward_route(db, gwlist, description):
    # Use a locking read so concurrent allocators cannot both reuse the highest
    # existing forwarding group. Flush each new route before allocating another.
    numeric_group = cast(InboundMapping.groupid, Integer)
    last = db.query(InboundMapping).filter(
        numeric_group >= settings.FLT_FWD_MIN
    ).order_by(numeric_group.desc()).with_for_update().first()
    group_id = int(last.groupid) + 1 if last else settings.FLT_FWD_MIN
    route = InboundMapping(str(group_id), '', gwlist, description)
    db.add(route)
    db.flush()
    return route


def _update_forwarding(db, row, data):
    for enabled, prefix, model, label in FORWARDING:
        did_key, group_key = prefix + '_fwddid', prefix + '_gwgroupid'
        forwarding = db.query(model).filter(model.dr_ruleid == str(row.ruleid)).first()
        if not ({enabled, did_key, group_key} & data.keys()):
            continue
        active = data.get(enabled, forwarding is not None)
        if not active:
            if forwarding is not None:
                old_group = forwarding.dr_groupid
                db.delete(forwarding)
                db.flush()
                _remove_forward_route(db, old_group)
            elif enabled not in data:
                raise http_exceptions.BadRequest(f'{enabled} must be enabled before setting its destination')
            continue

        old_group = forwarding.dr_groupid if forwarding else None
        route = _forward_route(db, old_group)
        did = data.get(did_key, forwarding.did if forwarding else '')
        group_id = data.get(group_key, _gateway_group(route.gwlist) if route else '')
        _check_group(db, group_id, group_key)
        if group_id:
            description = f'name:{label} Forward from {row.prefix} to DID {did}'
            if route is None:
                route = _new_forward_route(db, '#' + group_id, description)
            else:
                route.gwlist = '#' + group_id
                route.description = description
            target_group = route.groupid
        else:
            target_group = settings.FLT_OUTBOUND

        if forwarding is None:
            forwarding = model(str(row.ruleid), did, str(target_group))
            db.add(forwarding)
        else:
            forwarding.did = did
            forwarding.dr_groupid = str(target_group)
        db.flush()
        if old_group is not None and int(old_group) != int(target_group):
            _remove_forward_route(db, old_group)


def saveInboundRoute(db, data, row=None):
    creating = row is None
    did = data.get('did', row.prefix if row else '')
    duplicate = db.query(InboundMapping).filter(
        InboundMapping.groupid == settings.FLT_INBOUND, InboundMapping.prefix == did)
    if row is not None:
        duplicate = duplicate.filter(InboundMapping.ruleid != row.ruleid)
    if duplicate.first() is not None:
        raise http_exceptions.BadRequest("Duplicate DID's are not allowed")

    desc = _description(row) if row else {}
    if 'name' in data:
        desc['name'] = data['name']
    gwlist = row.gwlist if row else ''
    mapping_changed = 'gwgroupid' in data or 'servers' in data
    if 'gwgroupid' in data:
        _check_group(db, data['gwgroupid'], 'gwgroupid', endpoint_only=True)
        gwlist = '#' + data['gwgroupid'] if data['gwgroupid'] else ''
    elif 'servers' in data:
        gwlist = ','.join(data['servers'])
    if creating or mapping_changed or 'lb_enabled' in data:
        lb = data.get('lb_enabled', False)
        if lb and not _gateway_group(gwlist):
            raise http_exceptions.BadRequest('Load balancing requires a single endpoint group')
        if lb:
            _check_group(db, _gateway_group(gwlist), 'lb_enabled', endpoint_only=True)
        desc['lb_enabled'] = '1' if lb else '0'
    description = dictToStrFields(desc)
    if len(description) > 255:
        raise http_exceptions.BadRequest('Route description exceeds 255 characters')

    if creating:
        row = InboundMapping(str(settings.FLT_INBOUND), did, gwlist, description)
        db.add(row)
    else:
        old_did = row.prefix
        row.prefix, row.gwlist, row.description = did, gwlist, description
        if old_did != did:
            syncNumberStatus(db, old_did, '')
    # Get the actual generated rule ID; never guess the next AUTO_INCREMENT.
    db.flush()
    syncNumberStatus(db, did, gwlist)
    _update_forwarding(db, row, data)

    # Match the UI: changing LB mode affects all inbound routes using that group.
    if gwlist and (creating or mapping_changed or 'lb_enabled' in data):
        for other in db.query(InboundMapping).filter(
                InboundMapping.groupid == settings.FLT_INBOUND,
                InboundMapping.gwlist == gwlist, InboundMapping.ruleid != row.ruleid):
            other_desc = _description(other)
            other_desc['lb_enabled'] = desc['lb_enabled']
            other.description = dictToStrFields(other_desc)
    db.flush()
    return row


def deleteInboundRoute(db, row):
    for _, _, model, _ in FORWARDING:
        forwarding = db.query(model).filter(model.dr_ruleid == str(row.ruleid)).first()
        if forwarding is not None:
            group_id = forwarding.dr_groupid
            db.delete(forwarding)
            db.flush()
            _remove_forward_route(db, group_id)
    syncNumberStatus(db, row.prefix, '')
    db.delete(row)

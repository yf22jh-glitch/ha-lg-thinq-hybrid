"""ThinQ account settings, not local appliance commands.

The gear-menu recommendation toggle is SS11_11, not food-expiration push
notifications. Each write is issued once and confirmed by a fresh read.
"""
from __future__ import annotations
import re

FOOD_MODELS = frozenset(('2REFO1DBN3K_U', '3REK2G03VI230D_2'))
PAIRING_MODEL = 'ST_R_ETH01Y_'
PAIRING_RULE = 'washerToStyler_Pairing01'


def device_path(device_id):
    if not isinstance(device_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,128}',device_id):
        raise ValueError('Device identity is invalid')
    return f'service/fridge/{device_id}'


async def read_food(request, model, device_id):
    if model not in FOOD_MODELS:
        raise ValueError('Food recommendations are not configured for this model')
    saved = await request('GET', device_path(device_id) + '/push/config/expired-food-v2')
    # Current KM server also reports D. The gear menu checks === 'Y', so D
    # displays unchecked. Do not invent meanings for other/missing values.
    if saved.get('recommandUseYn') not in ('Y','N','D'):
        raise ValueError('Food recommendation state is unavailable')
    return 'on' if saved['recommandUseYn'] == 'Y' else 'off'


async def set_food(request, model, device_id, value):
    if value not in ('on','off'):
        raise ValueError('Choose on or off')
    if await read_food(request,model,device_id) == value:
        return value
    await request('PUT',device_path(device_id) + '/stored-food/keep-recommand-use',
                  {'recommandUseYn':'Y' if value == 'on' else 'N','deviceId':device_id})
    after = await read_food(request,model,device_id)
    if after != value:
        raise ValueError('Food recommendation save was not confirmed; inspect ThinQ before retrying')
    return after


def pairing_allowed(context, rule):
    """The Web ST util.isAvailable rule, with its conditions held in the DB."""
    required = rule.get('required')
    if not required or any(context.get(key) != value for key,value in required.items()):
        return False
    if context.get('state') == rule.get('power_off_state'):
        return (context.get('config',{}).get(rule.get('power_off_support_key')) is True
                or rule.get('power_off_content') in context.get('activated',[]))
    return context.get('state') in rule.get('states',[])


async def read_pairing(request, device_id):
    device_path(device_id)
    response = await request('GET',f'service/devices/{device_id}/available-pairing-devices?deviceType=201,221,223')
    rows = response['item']
    if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):
        raise ValueError('Pairing device list is invalid')
    if len({x['deviceId'] for x in rows}) != len(rows) or sum(x.get('pairingYn') == 'Y' for x in rows) > 1:
        raise ValueError('Pairing device list is ambiguous')
    for row in rows:
        device_path(row['deviceId'])
        if row.get('pairingYn') not in ('Y','N','O') or row.get('ownerYn') not in ('Y','N'):
            raise ValueError('Pairing ownership is unknown')
    return rows


async def set_pairing(request, device_id, target_id, expected_current):
    rows = await read_pairing(request,device_id)
    current = next((row['deviceId'] for row in rows if row['pairingYn'] == 'Y'),None)
    if current != expected_current:
        raise ValueError('Pairing changed; refresh before changing it')
    if current == target_id:
        return rows
    if current is not None and target_id is not None:
        raise ValueError('Disconnect the current pairing explicitly before choosing another product')
    selected = next((row for row in rows if row['deviceId'] == (target_id or current)),None)
    if selected is None or selected['ownerYn'] != 'Y' or selected['pairingYn'] not in ('N','Y'):
        raise ValueError('Only an owned, unpaired product or this current pairing can be changed')
    peer = selected['deviceId']
    await request('POST' if target_id else 'DELETE',f'service/devices/{device_id}/pairing-devices/{peer}',
                  {'deviceId':device_id,'pairingDeviceId':peer,'ruleType':PAIRING_RULE})
    after = await read_pairing(request,device_id)
    if next((row['deviceId'] for row in after if row['pairingYn'] == 'Y'),None) != target_id:
        raise ValueError('Pairing save was not confirmed; inspect the pairing before retrying')
    return after

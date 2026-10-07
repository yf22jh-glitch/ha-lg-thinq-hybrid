"""DB-driven recipe drafts never send until an explicit action press."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock
from types import MappingProxyType

from homeassistant.exceptions import HomeAssistantError
from custom_components.my_lg.local_control_entity import MyLgFeatureDraftText, MyLgFeatureDraftButton
from custom_components.my_lg.local_control_contract import (
    LocalControlBindingEligibility, local_control_value_authorized,
    load_local_control_entity_contract,
    load_local_control_entity_contract_from_database,
)
from tests.test_local_control_generic_entities import Coordinator, PrimaryProvider, Router, descriptor

PATTERN = r'(oven|range) [1-9][0-9]{2,3} 00:[0-9]{2}:[0-5]0'
DRAFT = 'oven.recipe.draft'

class DraftRouter(Router):
    def __init__(self):
        super().__init__()
        self.drafts = {}
        self.listeners = set()
    def feature_draft(self, device, cap):
        return self.drafts.get((device,cap))
    def set_feature_draft(self, device, cap, value):
        if value is None: self.drafts.pop((device,cap),None)
        else: self.drafts[(device,cap)] = value
        for listener in tuple(self.listeners): listener()
    def subscribe_feature_draft(self, device, listener):
        self.listeners.add(listener)
        return lambda:self.listeners.discard(listener)
    def value_authorized(self, device, cap, value):
        return value.startswith('oven ')

class FeatureDraftTest(unittest.IsolatedAsyncioTestCase):
    def test_database_loads_draft_and_action_metadata(self):
        artifact=Path(__file__).resolve().parents[1]/'custom_components/my_lg/home-assistant-local-control-entity-contract.v1.json'
        base=next(d for d in json.loads(artifact.read_text())['entities']
                  if d['entityDomain']=='select' and not d['existingHomeAssistantOwner']['exists'])
        draft={**base,'modelId':'WMLJ32RS','capabilityId':DRAFT,'key':'WMLJ32RS|'+DRAFT,
            'homeAssistantEntityKey':'local_oven_recipe_draft','entityDomain':'text',
            'inputDomain':{'kind':'enum','values':['oven 100 00:00:30']},'supportedValues':['oven 100 00:00:30'],
            'valueMappings':[{'homeAssistantValue':'oven 100 00:00:30','localRequestValue':'oven 100 00:00:30'}],
            'draftOnly':True,'parameterPattern':PATTERN}
        action={**base,'modelId':'WMLJ32RS','capabilityId':'oven.operation.start','key':'WMLJ32RS|oven.operation.start',
            'homeAssistantEntityKey':'local_oven_operation_start','entityDomain':'button',
            'inputDomain':{'kind':'boolean','values':[True]},'supportedValues':[True],
            'valueMappings':[{'homeAssistantValue':'press','localRequestValue':'true'}],
            'commandSemantics':'one-shot','parameterless':True,'oneShot':True,
            'requestFrom':DRAFT,'parameterPattern':PATTERN.replace('(oven|range)','oven')}
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'features.sqlite3'
            with closing(sqlite3.connect(path)) as c, c:
                c.executescript('PRAGMA application_id=1279739462; PRAGMA user_version=2; CREATE TABLE features(channel TEXT,model_id TEXT,feature_id TEXT,definition_json TEXT,enabled INTEGER);')
                for d in [draft,action]: c.execute('INSERT INTO features VALUES(?,?,?,?,1)',('control-entity',d['modelId'],d['capabilityId'],json.dumps(d)))
            loaded=load_local_control_entity_contract_from_database(path)
            self.assertEqual(len(loaded.descriptors),2)
            self.assertTrue(next(d for d in loaded.descriptors if d.capability_id==DRAFT).draft_only)
            self.assertEqual(next(d for d in loaded.descriptors if d.capability_id==DRAFT).entity_domain,'text')
            self.assertEqual(next(d for d in loaded.descriptors if d.capability_id==action['capabilityId']).request_from,DRAFT)

    def make(self):
        route=DraftRouter()
        base=descriptor('select')
        draft=replace(base,entity_domain='text',capability_id=DRAFT,draft_only=True,parameter_pattern=PATTERN)
        action=replace(base,entity_domain='button',capability_id='oven.operation.start',
            request_from=DRAFT,parameter_pattern=PATTERN.replace('(oven|range)','oven'),one_shot=True)
        text=MyLgFeatureDraftText(Coordinator(),draft,route,PrimaryProvider(),None)
        button=MyLgFeatureDraftButton(Coordinator(),action,route,PrimaryProvider(),None)
        text.async_write_ha_state=Mock()
        button.async_write_ha_state=Mock()
        return route,text,button

    async def test_draft_is_empty_after_reload_and_edit_never_dispatches(self):
        route,text,button=self.make()
        self.assertEqual(text.native_value,'')
        self.assertFalse(button.available)
        await text.async_set_value('oven 100 00:00:30')
        self.assertEqual(route.calls,[])
        self.assertTrue(button.available)
        await text.async_set_value('range 300 00:00:30')
        self.assertFalse(button.available)
        await text.async_set_value('')
        self.assertEqual(text.native_value,'')
        with self.assertRaises(HomeAssistantError): await text.async_set_value('malformed')
        self.assertEqual(route.calls,[])

    async def test_press_consumes_draft_and_never_replays(self):
        route,text,button=self.make()
        await text.async_set_value('oven 100 00:00:30')
        results=await asyncio.gather(button.async_press(),button.async_press(),return_exceptions=True)
        self.assertEqual(len(route.calls),1)
        self.assertEqual(route.calls[0][1:],('oven.operation.start','oven 100 00:00:30'))
        self.assertTrue(any(isinstance(r,HomeAssistantError) for r in results))
        self.assertFalse(button.available)
        self.assertEqual(text.native_value,'')

    async def test_unavailable_target_preserves_draft_without_dispatch(self):
        route,text,button=self.make()
        await text.async_set_value('oven 100 00:00:30')
        route.target_available=False
        with self.assertRaises(HomeAssistantError): await button.async_press()
        self.assertEqual(route.calls,[])
        self.assertEqual(text.native_value,'oven 100 00:00:30')

    def test_draft_and_action_authorization_are_separate(self):
        _,text,button=self.make()
        draft,action=text._descriptor,button._descriptor
        model=draft.model_id
        contract=replace(load_local_control_entity_contract(),descriptors=(draft,action),
            descriptors_by_model=MappingProxyType({model:(draft,action)}))
        eligible={'synthetic':LocalControlBindingEligibility('synthetic',MappingProxyType({
            draft.capability_id:draft.exact_local_request_values,
            action.capability_id:action.exact_local_request_values}))}
        def allowed(cap,value):
            return local_control_value_authorized(contract,eligible,binding_id='synthetic',
                model_id=model,capability_id=cap,local_request_value=value)
        self.assertFalse(allowed(DRAFT,'oven 100 00:00:30'))
        self.assertTrue(allowed(action.capability_id,'oven 100 00:00:30'))
        self.assertFalse(allowed(action.capability_id,'range 300 00:00:30'))
        self.assertFalse(allowed(action.capability_id,'true'))

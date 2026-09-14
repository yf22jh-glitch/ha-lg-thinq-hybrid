"""Thin HA wiring for the additive, already-confirmed command catalogue.

The producer retains exact target/frame authority and own-device confirmation.
No protocol bytes are interpreted here and no old catalogue/pin is replaced.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from .local_control_contract import (
    LocalControlBindingEligibility,
    LocalControlEntityDescriptor,
    LocalControlValueMapping,
)
from .local_vacuum_reservation import MODEL as VACUUM_MODEL, SCHEDULE, SCHEMA

CATALOGUE_SHA256 = '18e1efa554aeef531bd9f0edee172218bfa85458665b53355b6df7d9196759df'


def load_confirmed_features():
    document = json.loads((Path(__file__).parent / 'local-control-confirmed-features.v1.json').read_text())
    encoded = json.dumps(document['features'], ensure_ascii=False, separators=(',', ':')).encode()
    if document['schema_version'] != 1 or document['catalogue_sha256'] != CATALOGUE_SHA256 or hashlib.sha256(encoded).hexdigest() != CATALOGUE_SHA256:
        raise ValueError('Confirmed feature catalogue does not match this release')
    return document['features']


APPLIANCE_SETTING_MODELS = MappingProxyType({
    feature['capability_id']: feature['model_id'] for feature in load_confirmed_features()
    if feature['model_id'] in ('ST_R_ETH01Y_', '1WPD4CMIDR__3') and feature['domain'] == 'switch'
})


def augment_confirmed_features(contract, eligibility, binding_models):
    """Extend only existing selected bindings; excluded/malformed scopes stay out.

    The base digest still identifies the unmodified base contract. Extension
    identity is independently pinned above and never persisted as a v3 proof.
    """
    features = load_confirmed_features()
    by_model = {model: tuple(rows) for model, rows in contract.descriptors_by_model.items()}
    bindings = dict(eligibility)
    additions = []
    for feature in features:
        model = feature['model_id']
        capability = feature['capability_id']
        selected = [binding for binding in bindings if binding_models[binding] == model]
        if not selected:
            continue
        if any(d.capability_id == capability for d in by_model.get(model, ())):
            raise ValueError('Confirmed feature would replace an existing control')
        values = feature['values']
        parameter_schema = feature.get('parameter_schema')
        if parameter_schema is not None and (model != VACUUM_MODEL or capability != SCHEDULE or parameter_schema != SCHEMA or feature['domain'] != 'text' or values):
            raise ValueError('Unsupported confirmed parameter schema')
        boolean_values = feature['domain'] == 'switch' or capability == 'washer.fresh_care_enabled'
        descriptor = LocalControlEntityDescriptor(
            key=model + '|' + capability, model_id=model, capability_id=capability,
            home_assistant_entity_key=feature['entity_key'], label_ko=feature['label_ko'],
            entity_domain=feature['domain'], input_kind='boolean' if feature['domain'] == 'switch' else 'enum',
            supported_values=tuple(v['value'] == 'true' if boolean_values else v['value'] for v in values),
            value_mappings=tuple(LocalControlValueMapping(v['label'], v['value']) for v in values),
            exact_state_semantic=capability if boolean_values else None,
            factory_eligible=not feature['existing_owner'], existing_owner=feature['existing_owner'],
            # These whole-bundle choices have producer-side state confirmation.
            # Do not guess a selected preset from an old/partial HA snapshot.
            one_shot=feature['domain'] != 'switch',
            parameter_schema=parameter_schema,
        )
        by_model[model] = (*by_model.get(model, ()), descriptor)
        additions.append(descriptor)
        for binding in selected:
            prior = bindings[binding]
            bindings[binding] = LocalControlBindingEligibility(binding, MappingProxyType({
                **prior.values_by_capability, capability: descriptor.exact_local_request_values,
            }))
    return replace(contract, descriptors=(*contract.descriptors, *additions),
                   descriptors_by_model=MappingProxyType(by_model)), MappingProxyType(bindings)

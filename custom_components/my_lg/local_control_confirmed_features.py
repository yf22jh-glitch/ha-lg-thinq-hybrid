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
from .local_water_dnd import MODEL as WATER_MODEL, WINDOW, SCHEMA as WATER_DND_SCHEMA
from .local_water_parameters import SCHEMAS as WATER_PARAMETER_SCHEMAS
from .local_washer_options import MODEL as WASHER_MODEL, CAPABILITY as WASHER_PROGRAM, SCHEMA as WASHER_SCHEMA
from .local_dryer_options import MODEL as DRYER_MODEL, CAPABILITY as DRYER_PROGRAM, SCHEMA as DRYER_SCHEMA
from .local_styler_options import MODEL as STYLER_MODEL, CAPABILITY as STYLER_PROGRAM, SCHEMA as STYLER_SCHEMA

CATALOGUE_SHA256 = '2b3a3d3aee1397433a85b382f51c2c92068297b7dad8a710f00d0fe3c604c76a'


def load_confirmed_features():
    document = json.loads((Path(__file__).parent / 'local-control-confirmed-features.v1.json').read_text())
    encoded = json.dumps(document['features'], ensure_ascii=False, separators=(',', ':')).encode()
    if document['schema_version'] != 1 or document['catalogue_sha256'] != CATALOGUE_SHA256 or hashlib.sha256(encoded).hexdigest() != CATALOGUE_SHA256:
        raise ValueError('Confirmed feature catalogue does not match this release')
    return document['features']


def augment_confirmed_climate_domain(contract):
    """Overlay release-declared preserved-target modes without altering base pins.

    This exposes the existing native climate owner. Per-value admission and
    fresh target preservation remain checked by the producer at wire time.
    """
    capabilities = dict(contract.capabilities)
    for feature in load_confirmed_features():
        modes = feature.get('preserve_setpoint_modes')
        if modes is None:
            continue
        key = (feature['model_id'], feature['capability_id'])
        old = capabilities.get(key)
        if (old is None or modes != ['dry', 'fan_only']
                or feature.get('value_source') != 'exact-model-web-domain'):
            raise ValueError('Preserved climate overlay requires an existing exact owner')
        domain = old.input_domain
        carried_range = domain.target_range('cool')
        if carried_range is None or not all(any(v['value'].startswith(f'{mode}|') for v in feature['values']) for mode in modes):
            raise ValueError('Preserved climate overlay lacks declared values or carrier range')
        capabilities[key] = replace(old, input_domain=replace(domain,
            modes=tuple(dict.fromkeys((*domain.modes, *modes))),
            target_ranges_by_mode=MappingProxyType({**domain.target_ranges_by_mode,
                **{mode: carried_range for mode in modes}}),
            preserve_setpoint_modes=tuple(modes)))
    return replace(contract, capabilities=MappingProxyType(capabilities))


APPLIANCE_SETTING_MODELS = MappingProxyType({
    feature['capability_id']: feature['model_id'] for feature in load_confirmed_features()
    if feature['model_id'] in ('ST_R_ETH01Y_', '1WPD4CMIDR__3', '3REK2G03VI230D_2', 'CST_170004_WW', 'CST_570004_WW', 'DHUM_056905_WW', 'HUM_056905_WW') and feature['domain'] == 'switch'
       and not feature['existing_owner']  # native TLV owners read their own shadow, not this model-specific endpoint
})
APPLIANCE_VALUE_MODELS = MappingProxyType({
    feature['capability_id']: feature['model_id'] for feature in load_confirmed_features()
    if (feature['model_id'] == WASHER_MODEL and feature['capability_id'] == WASHER_PROGRAM)
       or (feature['model_id'] == WASHER_MODEL and feature['capability_id'] == 'washer.sound.volume_level')
       or (feature['model_id'] == DRYER_MODEL and feature['capability_id'] == DRYER_PROGRAM)
       or (feature['model_id'] == WATER_MODEL and feature['capability_id'] in
        ('water.sound.volume_percent', 'water.display.brightness_percent', WINDOW, *WATER_PARAMETER_SCHEMAS))
       or (feature['model_id'] == '3REK2G03VI230D_2' and feature['capability_id'] == 'kimchi.sound.door_melody')
       or (feature['model_id'] == 'ST_R_ETH01Y_' and feature['capability_id'] in
           ('styler.sound.volume_level', 'styler.sound.melody', 'styler.display.startup_image', 'styler.smart_care.night_start_time', 'styler.smart_care.night_end_time'))
       or (feature['model_id'] == 'HUM_056905_WW' and feature['capability_id'] == 'hum.sound.melody')
})
APPLIANCE_VALUE_OPTIONS = MappingProxyType({
    feature['capability_id']: tuple(v['value'] for v in feature['values']) for feature in load_confirmed_features()
    if feature['capability_id'] in APPLIANCE_VALUE_MODELS
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
    replacements = {}
    for feature in features:
        model = feature['model_id']
        capability = feature['capability_id']
        selected = [binding for binding in bindings if binding_models[binding] == model]
        if not selected:
            continue
        # Missing CST570 descriptor, existing native climate owner. This narrow
        # case must not weaken the required-owner check for numeric overlays.
        native_horizontal = (model == 'CST_570004_WW' and capability == 'swing.horizontal_enabled'
                             and feature['existing_owner'] is True and feature['domain'] == 'switch'
                             and feature.get('wire_evidence') == 'exact-model-declared-values-no-own-golden'
                             and tuple(v['value'] for v in feature['values']) == ('false', 'true')
                             and tuple(v.get('reported_value') for v in feature['values']) == (False, True))
        if feature.get('value_source') == 'exact-model-web-domain' and not native_horizontal:
            matches = [d for d in by_model.get(model, ()) if d.capability_id == capability]
            if len(matches) != 1:
                raise ValueError('Numeric extension requires exactly one existing owner')
            old = matches[0]
            values = tuple(v['value'] for v in feature['values'])
            if feature.get('value_kind') == 'boolean':
                reports = tuple(v.get('reported_value') for v in feature['values'])
                if (old.input_kind != 'boolean' or old.entity_domain != 'switch' or feature['domain'] != 'switch'
                        or not old.existing_owner or feature['existing_owner'] is not True
                        or not values or len(set(values)) != len(values)
                        or any(v not in ('false', 'true') for v in values)
                        or any(type(r) is not bool or r != (v == 'true') for v, r in zip(values, reports))
                        or set(values).intersection(old.exact_local_request_values)):
                    raise ValueError('Boolean extension must add exact missing values to an existing switch owner')
                requests = tuple(v for v in ('false', 'true') if v in (*old.exact_local_request_values, *values))
                prior_labels = {v.local_request_value: v.home_assistant_value for v in old.value_mappings}
                descriptor = replace(old, supported_values=tuple(v == 'true' for v in requests),
                    value_mappings=tuple(LocalControlValueMapping(prior_labels.get(v, v == 'true'), v) for v in requests))
                replacements[old.key] = descriptor
                by_model[model] = tuple(descriptor if d.key == old.key else d for d in by_model[model])
                for binding in selected:
                    prior = bindings[binding]
                    allowed = (*prior.values_by_capability.get(capability, ()), *values)
                    bindings[binding] = replace(prior, values_by_capability=MappingProxyType({
                        **prior.values_by_capability, capability: tuple(v for v in ('false', 'true') if v in allowed)}))
                continue
            if feature.get('value_kind') == 'enum':
                reports = tuple(v.get('reported_value') for v in feature['values'])
                labels = tuple(v['label'] for v in feature['values'])
                if (old.input_kind != 'enum' or old.entity_domain != 'select' or not values
                        or any(not isinstance(v, str) or not v for v in (*values, *labels, *reports))
                        or len(set(values)) != len(values) or len(set(reports)) != len(reports)
                        or set(values).intersection(old.exact_local_request_values)
                        or set(reports).intersection(old.supported_values)
                        or set(labels).intersection(v.home_assistant_value for v in old.value_mappings)):
                    raise ValueError('Enum extension requires disjoint exact request and report values')
                descriptor = replace(old, supported_values=(*old.supported_values, *reports),
                    value_mappings=(*old.value_mappings, *(LocalControlValueMapping(label, value)
                        for label, value in zip(labels, values))))
                replacements[old.key] = descriptor
                by_model[model] = tuple(descriptor if d.key == old.key else d for d in by_model[model])
                for binding in selected:
                    prior = bindings[binding]
                    bindings[binding] = replace(prior, values_by_capability=MappingProxyType({
                        **prior.values_by_capability, capability: (
                            *prior.values_by_capability.get(capability, ()), *values)}))
                continue
            if (old.input_kind not in ('number', 'enum') or old.entity_domain not in ('number','select')
                    or not values or len(set(values)) != len(values)
                    or any(not isinstance(v,str) or not v.isascii() or not v.isdecimal() or str(int(v)) != v for v in values)
                    or set(values).intersection(old.exact_local_request_values)):
                raise ValueError('Numeric extension must contain only new canonical values')
            supported = tuple(sorted(int(v) for v in (*old.exact_local_request_values, *values)))
            labels = feature.get('display_labels')
            if labels is not None and (not isinstance(labels, dict)
                    or set(labels) != {str(n) for n in supported}
                    or any(not isinstance(v, str) or not v for v in labels.values())
                    or len(set(labels.values())) != len(labels)):
                raise ValueError('Numeric display labels must cover the exact value domain')
            if labels is not None and old.entity_domain == 'select' and any(
                    labels[v.local_request_value] != v.home_assistant_value for v in old.value_mappings
            ) and feature.get('relabels_existing') is not True:
                raise ValueError('Existing option relabeling requires an explicit migration declaration')
            gaps = {b-a for a,b in zip(supported,supported[1:])}
            # Keep existing select owners; irregular domains must not create
            # an apparently legal min/max/step lattice with unsupported points.
            domain = 'number' if labels is None and old.entity_domain == 'number' and len(gaps) == 1 else 'select'
            # Existing select labels are an automation contract, not display decoration.
            plain_select = old.entity_domain == 'select' and all(
                v.home_assistant_value == v.local_request_value for v in old.value_mappings)
            old_labels = {v.local_request_value: v.home_assistant_value for v in old.value_mappings}
            mappings = tuple(LocalControlValueMapping(
                labels[str(n)] if labels is not None else n if domain == 'number' else (
                    old_labels.get(str(n), str(n) if plain_select else f"{n}{old.unit or ''}")
                    if old.entity_domain == 'select' else f"{n}{old.unit or ''}"), str(n)) for n in supported)
            descriptor = replace(old, supported_values=supported, value_mappings=mappings,
                entity_domain=domain, input_kind='number' if domain == 'number' else 'enum',
                number_min=supported[0] if domain == 'number' else None,
                number_max=supported[-1] if domain == 'number' else None,
                number_step=next(iter(gaps)) if domain == 'number' else None)
            replacements[old.key] = descriptor
            by_model[model] = tuple(descriptor if d.key == old.key else d for d in by_model[model])
            for binding in selected:
                prior = bindings[binding]
                # Preserve any reviewed-value restriction. This extension owns
                # only the added values, not an old command's authorization.
                allowed = tuple(str(n) for n in sorted(int(v) for v in (
                    *prior.values_by_capability.get(capability, ()), *values)))
                bindings[binding] = replace(prior, values_by_capability=MappingProxyType({
                    **prior.values_by_capability, capability: allowed}))
            continue
        if any(d.capability_id == capability for d in by_model.get(model, ())):
            raise ValueError('Confirmed feature would replace an existing control')
        values = feature['values']
        parameter_schema = feature.get('parameter_schema')
        valid_parameter = ((model == VACUUM_MODEL and capability == SCHEDULE and parameter_schema == SCHEMA)
                           or (model == DRYER_MODEL and capability == DRYER_PROGRAM and parameter_schema == DRYER_SCHEMA)
                           or (model == STYLER_MODEL and capability == STYLER_PROGRAM and parameter_schema == STYLER_SCHEMA)
                           or (model == WASHER_MODEL and capability == WASHER_PROGRAM and parameter_schema == WASHER_SCHEMA)
                           or (model == WATER_MODEL and capability == WINDOW and parameter_schema == WATER_DND_SCHEMA)
                           or (model == WATER_MODEL and capability in WATER_PARAMETER_SCHEMAS and parameter_schema == WATER_PARAMETER_SCHEMAS[capability]))
        if parameter_schema is not None and (not valid_parameter or feature['domain'] != 'text' or values):
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
    return replace(contract, descriptors=(*(replacements.get(d.key,d) for d in contract.descriptors), *additions),
                   descriptors_by_model=MappingProxyType(by_model)), MappingProxyType(bindings)

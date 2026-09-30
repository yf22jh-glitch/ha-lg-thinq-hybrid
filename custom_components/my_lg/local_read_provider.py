"""Strict subscriber contract for the complete read-only TLV sensor feed.

Authenticated live presence remains the appliance/publication authority.  The
read feed owns its independent cohort/source cursor, so a publication can be
materialized before a pilot semantic snapshot exists.  It deliberately has no
Home Assistant or MQTT dependency.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

_LOGGER = logging.getLogger(__name__)

TLV_READ_SCHEMA_VERSION = 2
TLV_READ_PUBLICATION_PLAN_REVISION = 2
PER_MODEL_TLV_READ_SCHEMA_VERSION = 3
READ_STATIC_CONTRACT_PROJECTION_VERSION = 2
EXPECTED_TLV_READ_SEMANTICS_REVISION = 34
TLV_READ_TOPIC_PREFIX = "lg_rethink_local/v1/read"
MAX_TLV_READ_PAYLOAD_BYTES = 64 * 1024
MAX_TLV_READ_EVENT_PAYLOAD_BYTES = 16 * 1024
MAX_TLV_READ_FIELDS = 256
MAX_JSON_SAFE_INTEGER = 9_007_199_254_740_991
MAX_COHORT_GENERATION = 1_999_999_999_998
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_CURSOR_HISTORY = 10_000
TLV_READ_ENTITY_CONTRACT_FILENAME = "full-read-entity-contract.v1.json"
TLV_READ_ENTITY_CONTRACT_DIGEST_FILENAME = "full-read-entity-contract.v1.sha256"
TLV_READ_PROFILE_FILENAME = "full-read-sensor-profiles.v1.json"
TLV_READ_PROFILE_DIGEST_FILENAME = "full-read-sensor-profiles.v1.sha256"
TLV_READ_PER_MODEL_AUTHORITY_FILENAME = "ha-per-model-read-authority.v1.json"
TLV_READ_PER_MODEL_AUTHORITY_DIGEST_FILENAME = (
    "ha-per-model-read-authority.v1.sha256"
)
TLV_READ_CONSUMER_STATE_STORE_VERSION = 1
TLV_READ_CONSUMER_STATE_STORE_KEY = "tlv_read_consumer_state_v1"
TLV_READ_CONSUMER_MUTATIONS = (
    "bootstrap-v1",
    "stage-v2",
    "restore-staged-v1",
    "adopt-v2",
    "retire-v1",
    "adopt-successor-v2",
    "restore-predecessor-v2",
    "retire-predecessor-v2",
)
EXPECTED_TLV_READ_DESCRIPTOR_COUNT = 370
EXPECTED_TLV_READ_ENTITY_ROOT_SHA256 = (
    "5bb2be2e4805e092788b786c8106cf3167c61eff916ad20df47caadc408ebd08"
)
EXPECTED_TLV_READ_PROFILE_ROOT_SHA256 = (
    "658865e343ad49917131c0872234b0930315644558da76035b26a533e253255b"
)
EXPECTED_TLV_SOURCE_ENTITY_REVISION = "tlv-read-entities-v1:81fc45c66742929d"
EXPECTED_TLV_SOURCE_ENTITY_ROOT_SHA256 = (
    "81fc45c66742929dd3dea120fa8d504dc531be1d845cc5f4e5575b22f1ec5eab"
)
EXPECTED_TLV_CATALOG_REVISION = "tlv-knowledge-v1:22ba817a3d40043f"
EXPECTED_TLV_CATALOG_SHA256 = (
    "22ba817a3d40043fc94d57162da2f121b19febe55debb2d8750218606b2ee48a"
)
EXPECTED_REVIEWED_NON_TLV_REVISION = "reviewed-non-tlv-read-v1:1b8a17d9cfdeeadb"
EXPECTED_REVIEWED_NON_TLV_SHA256 = (
    "1b8a17d9cfdeeadbebecaa7249ad2659924eaadd3e95376698bf28fbd49bbbdd"
)

# Retained-message compatibility across the reviewed sem31, sem32, sem33 and
# prior sem34 rollout.  Listing complete predecessor generations lets the
# component move first without making local-read bindings unavailable while
# appliance runtimes move independently.  Every candidate below is still
# matched as one complete six-pin generation: mixed roots and all unlisted
# generations remain unauthorized.
_RETAINED_TLV_READ_PUBLICATION_PIN_GENERATIONS = (
    MappingProxyType(
        {
            "profile_revision": "full-read-sensor-profiles-v1:02b9bce3531a1a47",
            "profile_sha256": (
                "02b9bce3531a1a4781a089e7cb4eaf9f1e05c7b2161d76da2003c4c59cf998c2"
            ),
            "read_entity_contract_revision": "full-read-entities-v1:46f5df303ebe5061",
            "read_entity_contract_sha256": (
                "46f5df303ebe50611b069ce68e8c23baa9d1dc5f68e77f0c7a93532c809dbab8"
            ),
            "catalog_sha256": (
                "e555dc723ce6d2e19f8179900406cc91add2a91156518c72fd92188526b279bc"
            ),
            "semantics_revision": 31,
        }
    ),
    MappingProxyType(
        {
            "profile_revision": "full-read-sensor-profiles-v1:19ac432dc133c51e",
            "profile_sha256": (
                "19ac432dc133c51ee146c362f0b34283d9707daadff5d315e9c984d2fe7c833c"
            ),
            "read_entity_contract_revision": "full-read-entities-v1:5d942f007981f8d9",
            "read_entity_contract_sha256": (
                "5d942f007981f8d9fe7d28721b1bdfb2e508b124ba49933dd00eb8c0db1b3fa4"
            ),
            "catalog_sha256": (
                "e555dc723ce6d2e19f8179900406cc91add2a91156518c72fd92188526b279bc"
            ),
            "semantics_revision": 31,
        }
    ),
    # The deployed sem32 generation remains a complete, exact six-pin
    # generation while sem33 removes the context-dependent CST carrier leaf.
    MappingProxyType(
        {
            "profile_revision": "full-read-sensor-profiles-v1:5995b66176492950",
            "profile_sha256": (
                "5995b66176492950f98fa427891ccfac0b67115cd5c03663bd73ce252bfef3b3"
            ),
            "read_entity_contract_revision": "full-read-entities-v1:a255753e08cbad9d",
            "read_entity_contract_sha256": (
                "a255753e08cbad9db1c4d92720709df1eea9ae2b0eae4ce36436d7361a211af5"
            ),
            "catalog_sha256": (
                "07b97efabfc0f059844826ef425d6ad46737bd3f527d57862202d52ac812024b"
            ),
            "semantics_revision": 32,
        }
    ),
    # Keep the immediately preceding sem33 generation exact while the
    # appliance runtimes move independently to the sem34 catalogue.
    MappingProxyType(
        {
            "profile_revision": "full-read-sensor-profiles-v1:b3a87ad4dde6e7ec",
            "profile_sha256": (
                "b3a87ad4dde6e7ec0db0c21328744b96d4416bc1d9d028bf9b8f5182ccb1eb7f"
            ),
            "read_entity_contract_revision": "full-read-entities-v1:0b8bad1be19f6b47",
            "read_entity_contract_sha256": (
                "0b8bad1be19f6b4741a224d0a01d95823bee04aba4bc5a1b3bcdf985cdc53530"
            ),
            "catalog_sha256": (
                "145bd2bbc87cb7e6d14694b5d362a485e8b38d8ceef0607b8e4738daf56f4ce9"
            ),
            "semantics_revision": 33,
        }
    ),
    # The immediately preceding sem34 generation remains exact while only the
    # two CST model contracts add the reviewed auto-dry wind readback.
    MappingProxyType(
        {
            "profile_revision": "full-read-sensor-profiles-v1:3f993fff50fbdeed",
            "profile_sha256": (
                "3f993fff50fbdeed338aa83082bb43d969ff93970924627fe86cf132804d5985"
            ),
            "read_entity_contract_revision": "full-read-entities-v1:7bc022c12410bb75",
            "read_entity_contract_sha256": (
                "7bc022c12410bb75c143b54e9fd93537ae8cad44c8b664f010af811f51f7b8b6"
            ),
            "catalog_sha256": (
                "6a9c918ef5d6f9c07b8fdb4d556d8d0c8f58867dcb1eefebf504d85a5c7fe4f7"
            ),
            "semantics_revision": 34,
        }
    ),
    # The current sem34 generation remains exact while the reviewed fridge
    # adds two read-only compartment status fields. Other model pins are unchanged.
    MappingProxyType(
        {
            "profile_revision": "full-read-sensor-profiles-v1:3f738b7b24ce327a",
            "profile_sha256": (
                "3f738b7b24ce327a94cf873bbdc31e0db4076785402807f69380e27583d3237f"
            ),
            "read_entity_contract_revision": "full-read-entities-v1:b970f1e6c9877653",
            "read_entity_contract_sha256": (
                "b970f1e6c9877653475aeab2a0b6a96618053748163c4bc34f8c557bf915bbff"
            ),
            "catalog_sha256": (
                "22ba817a3d40043fc94d57162da2f121b19febe55debb2d8750218606b2ee48a"
            ),
            "semantics_revision": 34,
        }
    ),
)

# Exact predecessors for reviewed per-model transitions into the current
# sem34 authority.  A predecessor publication is accepted only while that exact
# predecessor remains the binding's durable adopted singleton.  The explicit
# admin transition replaces the accepted singleton with the successor, after
# which predecessor publications fail closed.  Each model has one current
# successor and one predecessor; every other model/hash remains unauthorized.
_REVIEWED_TLV_READ_V2_SUCCESSOR_PREDECESSORS = MappingProxyType(
    {
        "1WPD4CMIDR__3": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "8233e4fae4f422a96de959275363f33062475018955cc5e569bba43274478ca8"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "3a42032b5dac2e6064b33ba879e11a12c086d179f01542c3d38a05071ab239fa"
                ),
            }
        ),
        "2REFO1DBN3K_U": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "da1e2060781d2d3a55a805ea0e1739372f5de0a50b4d9070678b407d63c55849"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "d26de953c793792552c5c65572f0b16bd3e519a055d2c7fae320609101bdf916"
                ),
            }
        ),
        "2REK1D04AR170": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "f40bb3cd566f260130a1d4af9cee9288fd9761a7fbb6f2ba7c74becc0690b1d4"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "a09ae9b7349c33b2d2e5b96e053895b981f607db4564fbf1dc6045e92a39e208"
                ),
            }
        ),
        "3REK2G03VI230D_2": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "9bb2b8f4fd78c3e775156e76037397da8da5f226484d5266067851beb0456973"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "a97d5e5d0866a1d113ed81dcd7e3146986a5224a1f08ca2fab2158d4b3019511"
                ),
            }
        ),
        "AIR_2C0001_WW": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "714d4f061424d973b5db608ba8aa5f8d7de7f592c555228f3510391ff75a1df2"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "50714ceb5962b9d5b7ea9c9a143eb82c209d1e6151631e83494a43448b3dc86f"
                ),
            }
        ),
        "AIR_910604_WW": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "497dd3f8ea1150b094bb825c32e2c8105725ba983913c48ec55f7fe0ef934d36"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "789884a6ccb89fbb679d521a8baee6653fe12ade857c0edc433b3eb22ab39e8b"
                ),
            }
        ),
        "CST_170004_WW": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "0f6dc6bb1746b04cc932faf9bcfb66c7f60771a502e218c1a950e5935245c5ef"
                ),
                "predecessor_semantics_revision": 34,
                "predecessor_model_contract_sha256": (
                    "7a4da8a96e73d7c927899bf64b5cf491dd721233e535c7bacfa9139e5654cb7f"
                ),
            }
        ),
        "CST_570004_WW": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "80f61837676426bb1b99eb8d8aa283bcc0a45d084d7a3e665bc579a310c972d5"
                ),
                "predecessor_semantics_revision": 34,
                "predecessor_model_contract_sha256": (
                    "f9f5e2e5e401c6af0ab653f65ae95e5b215979182e07af37e3fd483165ad93a6"
                ),
            }
        ),
        "D121110": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "3b4cafdcf10b9a49d0277ed45e2b644505538a2a6b4bc1b2443e90f927b97a76"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "72802dacbebd37aef5f686f0c0f2b3da232c420a0656f1a029af86fd65b73eef"
                ),
            }
        ),
        "DHUM_056905_WW": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "d041c74a071fa77db557bc8398a3da2f2aa5f26d49e77b4eba047217022e83d7"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "875450af69ddbea6ae1d1d800010a0d42cb7c846a9af9f7f6d55672af42caf08"
                ),
            }
        ),
        "HUM_056905_WW": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "d0b1955778c5cee97a2b2c73d286bfadaea288702470f006d762d7114775ad3a"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "f4e03100a36ed3164ba0d6eb963c68eff54711c01bc33cdd6fbec39fbe80bd19"
                ),
            }
        ),
        "HWWA9X3C_F2U": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "fbc50fbb17ddec2046efc77f6bfd13c1a12f5cef8e58a13f76d9d7b8e3184677"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "e0d755979da2ce1876d1b797943e82d854a684af05e1aba5bf3bf819022e2530"
                ),
            }
        ),
        "ST_R_ETH01Y_": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "cb92288782510894b516ba062b21402dec3c0abefbbab74bd922895e80238d7e"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "0daae56bb51b50216179f0630c7368084498bf5f7a4f62cb91d1ab5ccbfa3410"
                ),
            }
        ),
        "WBEF3": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "f827fa3bd89a1a25523ac1147a7db67d60809f175f5e837056e012980acf07d2"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "72e3fa192e9c246bc9258126e76042a3583a6fbc03958d7407220948d4746e45"
                ),
            }
        ),
        "WMLJ32RS": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "9661ffb37b5dde4b6754f35997856b10276f9ddef01075772990e85d6f8a2ca5"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "9867af144446c0fd5ddcb82c960baf0167dacefc3b248895ae77e0ab9814b7f2"
                ),
            }
        ),
        "WTL_KPK_BDH_KR_01": MappingProxyType(
            {
                "successor_semantics_revision": 34,
                "successor_model_contract_sha256": (
                    "822a51a8fd1b11a96437fe741b617993024c4b167eb065a646fe6297fea2660b"
                ),
                "predecessor_semantics_revision": 32,
                "predecessor_model_contract_sha256": (
                    "d6ed11f00a73b87dcb19f1b614e6719587a92592a5215f61c50d361b720ccc2a"
                ),
            }
        ),
    }
)
MAX_TLV_READ_ARTIFACT_BYTES = 2 * 1024 * 1024

_BINDING_ID = re.compile(
    r"^(?!shadow-)[a-zA-Z0-9][a-zA-Z0-9_-]{15,127}$", re.IGNORECASE
)
_OPAQUE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,255}$")
_SOURCE_SESSION_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PUBLICATION_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_ISO_TIMESTAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,3}))?(Z|[+-](\d{2}):(\d{2}))$"
)

_V1_PIN_KEYS = frozenset(
    {
        "schema_version",
        "publication_plan_revision",
        "profile_id",
        "profile_contract_revision",
        "profile_revision",
        "profile_sha256",
        "read_entity_contract_revision",
        "read_entity_contract_sha256",
        "catalog_sha256",
        "semantics_revision",
        "binding_id",
        "model_id",
        "platform",
        "binding_generation",
        "pat_device_id_proof_sha256",
        "publication_session_id",
        "cohort_generation",
        "source_session_id",
        "sequence",
        "published_at",
    }
)
_V2_PIN_KEYS = frozenset(
    {
        "schema_version",
        "publication_plan_revision",
        "static_contract_projection_version",
        "profile_id",
        "model_contract_sha256",
        "semantics_revision",
        "binding_id",
        "model_id",
        "platform",
        "binding_generation",
        "pat_device_id_proof_sha256",
        "publication_session_id",
        "cohort_generation",
        "source_session_id",
        "sequence",
        "published_at",
    }
)
_PIN_KEYS_BY_PROJECTION = MappingProxyType({1: _V1_PIN_KEYS, 2: _V2_PIN_KEYS})
_CURRENT_OPTIONAL_KEYS = frozenset({"invalidated_fields"})
_CURRENT_REQUIRED_KEYS_BY_PROJECTION = MappingProxyType(
    {
        projection: keys | {"fields", "diagnostics"}
        for projection, keys in _PIN_KEYS_BY_PROJECTION.items()
    }
)
_EVENT_KEYS_BY_PROJECTION = MappingProxyType(
    {
        projection: keys
        | {"descriptor_key", "semantic_id", "event_type", "field"}
        for projection, keys in _PIN_KEYS_BY_PROJECTION.items()
    }
)
_FIELD_REQUIRED_KEYS = frozenset(
    {"value", "value_type", "observed_at", "confidence", "exposure"}
)
_FIELD_ALLOWED_KEYS = _FIELD_REQUIRED_KEYS | {"unit"}
_INVALIDATION_KEYS = frozenset({"observed_at", "confidence"})
TLV_READ_DIAGNOSTIC_KEYS = (
    "rejected_frames",
    "unresolved_fields",
    "invalid_values",
    "unsupported_frames",
)
_DIAGNOSTIC_KEYS = frozenset(TLV_READ_DIAGNOSTIC_KEYS)
_ENTITY_ARTIFACT_KEYS = frozenset(
    {
        "schemaVersion",
        "revision",
        "rootSha256",
        "authority",
        "access",
        "tlvEntityContractRevision",
        "tlvEntityContractSha256",
        "catalogRevision",
        "catalogSha256",
        "reviewedNonTlvSourceRevision",
        "reviewedNonTlvSourceSha256",
        "entities",
        "aggregateExclusions",
        "stats",
    }
)
_PROFILE_ARTIFACT_KEYS = frozenset(
    {
        "schemaVersion",
        "revision",
        "rootSha256",
        "authority",
        "access",
        "semanticsRevision",
        "readEntityContractRevision",
        "readEntityContractSha256",
        "catalogRevision",
        "catalogSha256",
        "reviewedNonTlvSourceRevision",
        "reviewedNonTlvSourceSha256",
        "profiles",
        "descriptorOnly",
        "aggregateExclusions",
        "stats",
    }
)
_ENTITY_DESCRIPTOR_REQUIRED_KEYS = frozenset(
    {
        "key",
        "modelId",
        "platform",
        "semanticId",
        "domain",
        "domainAuthority",
        "access",
        "direction",
        "sourceKeys",
        "sourceTags",
        "sourceTagHexes",
        "decoderContractIds",
        "valueTypes",
        "semanticKind",
        "exposure",
        "labelKo",
        "owner",
        "isDiagnostic",
        "entityCategory",
        "enabledByDefault",
    }
)
_PROFILE_KEYS = frozenset(
    {
        "profileId",
        "contractRevision",
        "modelId",
        "platform",
        "authority",
        "access",
        "fields",
        "stats",
    }
)
_PROFILE_FIELD_REQUIRED_KEYS = frozenset(
    {
        "descriptorKey",
        "semanticId",
        "domain",
        "valueTypes",
        "exposure",
        "labelKo",
        "owner",
        "entityCategory",
        "enabledByDefault",
        "publicationMode",
    }
)
_PROFILE_STATS_KEYS = frozenset(
    {
        "descriptorCount",
        "retainedCurrentDescriptorCount",
        "transientEventDescriptorCount",
        "binarySensorDescriptorCount",
        "sensorDescriptorCount",
        "eventDomainDescriptorCount",
        "stateDescriptorCount",
        "diagnosticDescriptorCount",
        "eventDescriptorCount",
        "patOwnerDescriptorCount",
    }
)
_PUBLICATION_STATS_KEYS = frozenset(
    {
        "generatedDescriptorCount",
        "liveDescriptorCount",
        "descriptorOnlyCount",
        "retainedCurrentDescriptorCount",
        "transientEventDescriptorCount",
        "binarySensorDescriptorCount",
        "sensorDescriptorCount",
        "eventDomainDescriptorCount",
        "stateDescriptorCount",
        "diagnosticDescriptorCount",
        "eventDescriptorCount",
        "patOwnerDescriptorCount",
        "profileCount",
        "aggregateExclusionGroupCount",
        "aggregateExcludedSourceEntryCount",
        "pilotDescriptorCount",
        "fullReadPilotOverlapCount",
        "pilotOnlyDescriptorCount",
        "overallOwnerInventoryCount",
    }
)
_ENTITY_STATS_KEYS = frozenset(
    {
        "generatedEntityCount",
        "tlvEntityCount",
        "reviewedNonTlvEntityCount",
        "binarySensorCount",
        "sensorCount",
        "eventCount",
        "stateCount",
        "diagnosticCount",
        "eventExposureCount",
        "patOwnerCount",
        "aggregateExclusionGroupCount",
        "aggregateExcludedSourceEntryCount",
    }
)
_PER_MODEL_AUTHORITY_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "artifact",
        "projection",
        "feed_schema_version",
        "publication_plan_revision",
        "static_contract_projection_version",
        "source_inventory_root_sha256",
        "authorities",
        "stats",
        "root_sha256",
    }
)
_PER_MODEL_AUTHORITY_KEYS = frozenset(
    {
        "profile_id",
        "model_id",
        "platform",
        "semantics_revision",
        "model_contract_sha256",
    }
)
_PER_MODEL_AUTHORITY_STATS_KEYS = frozenset(
    {
        "authority_count",
        "thinq1_authority_count",
        "thinq2_authority_count",
    }
)
_READ_CONSUMER_PIN_KEYS = frozenset(
    {
        "projection_version",
        "static_read_contract_sha256",
        "model_contract_sha256",
    }
)
_READ_CONSUMER_PIN_SET_KEYS = frozenset(
    {"schema_version", "binding_id", "accepted", "record_sha256"}
)
_READ_CONSUMER_BINDING_STATE_V1_KEYS = frozenset(
    {
        "schema_version",
        "binding_id",
        "pat_device_id_proof_sha256",
        "adopted_projection_version",
        "adopted_static_read_contract_sha256",
        "adopted_model_contract_sha256",
        "consumer_pin_set",
        "record_sha256",
    }
)
_READ_CONSUMER_BINDING_STATE_V2_KEYS = frozenset(
    {
        *_READ_CONSUMER_BINDING_STATE_V1_KEYS,
        "predecessor_pin",
        "predecessor_record_sha256",
    }
)
_READ_CONSUMER_STATE_INVENTORY_KEYS = frozenset(
    {"schema_version", "artifact", "bindings", "root_sha256"}
)

_V1_STATIC_CONTRACT_HASH_DOMAIN = b"lg-rethink-local/read-generation-static-contract/v1\0"
_V2_STATIC_CONTRACT_HASH_DOMAIN = b"lg-rethink-local/read-generation-static-contract/v2\0"
_PER_MODEL_AUTHORITY_HASH_DOMAIN = b"lg-rethink-local/ha-per-model-read-authority/v1\0"
_READ_CONSUMER_PIN_SET_HASH_DOMAIN = b"lg-rethink-local/read-contract-consumer-pin-set/v1\0"
_READ_CONSUMER_BINDING_STATE_HASH_DOMAIN = (
    b"lg-rethink-local/read-contract-consumer-binding-state/v1\0"
)
_READ_CONSUMER_STATE_INVENTORY_HASH_DOMAIN = (
    b"lg-rethink-local/read-contract-consumer-state-inventory/v1\0"
)


class TlvReadProviderContractError(ValueError):
    """A TLV read publication failed its pinned subscriber contract."""


class TlvReadCatalogueError(RuntimeError):
    """The bundled TLV read descriptor artifact is invalid."""


def _catalogue_error(message: str = "Bundled TLV read catalogue is invalid") -> None:
    raise TlvReadCatalogueError(message)


def _catalogue_object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _catalogue_error("Bundled TLV read catalogue has duplicate keys")
        result[key] = value
    return result


def _artifact_json(raw: bytes, digest: bytes, filename: str) -> dict[str, Any]:
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > MAX_TLV_READ_ARTIFACT_BYTES
        or not isinstance(digest, bytes)
    ):
        _catalogue_error()
    expected_sidecar = (
        f"{hashlib.sha256(raw).hexdigest()}  local/model-contract/{filename}\n"
    ).encode("ascii")
    if digest != expected_sidecar:
        _catalogue_error("Bundled TLV read catalogue digest does not match")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_catalogue_object_without_duplicate_keys,
            parse_constant=lambda _value: _catalogue_error(),
        )
    except TlvReadCatalogueError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ) as err:
        raise TlvReadCatalogueError("Bundled TLV read catalogue is not JSON") from err
    if not isinstance(value, dict):
        _catalogue_error()
    return value


def _artifact_string_list(
    value: object, *, maximum: int = 32, allow_empty: bool = False
) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and all(
            isinstance(item, str) and bool(item) and len(item) <= 384 for item in value
        )
        and len(value) == len(set(value))
    )


def _valid_semantic_kind(raw: Mapping[str, Any]) -> bool:
    reviewed_decoders = {
        "reviewed-decoded-s": "semantic-decoder/aabb-reviewed-model-status/v1",
        "reviewed-decoded-e": "semantic-decoder/aabb-reviewed-interval-energy-0x3e/v1",
        "reviewed-decoded-u": "semantic-decoder/aabb-reviewed-water-usage-0x121f/v1",
        "reviewed-decoded-t": "semantic-decoder/thinq1-reviewed-nine-byte-status/v1",
    }
    reviewed_decoder = reviewed_decoders.get(raw["semanticKind"])
    if not raw["sourceTags"]:
        expected_platform = (
            "thinq1" if raw["semanticKind"] == "reviewed-decoded-t" else "thinq2"
        )
        pat_owner_keys = frozenset(
            {
                "WBEF3|burner.left_rear.power_level",
                "WBEF3|burner.left_rear.state",
                "WBEF3|burner.right_front.power_level",
                "WBEF3|burner.right_front.state",
            }
        )
        return (
            reviewed_decoder is not None
            and raw["sourceKeys"]
            == [f"{EXPECTED_REVIEWED_NON_TLV_REVISION}|{raw['key']}"]
            and raw["sourceTagHexes"] == []
            and raw["decoderContractIds"] == [reviewed_decoder]
            and raw["platform"] == expected_platform
            and raw["owner"]
            == ("PAT" if raw["key"] in pat_owner_keys else "none")
        )

    exact_kind_exposures = frozenset(
        {
            ("event", "event"),
            ("state", "state"),
            ("state", "diagnostic"),
            ("capability", "diagnostic"),
            ("query-selector", "diagnostic"),
            ("capability-marker", "diagnostic"),
            ("coordination-flag", "state"),
        }
    )
    aggregate_contracts = {
        "diagnostic.capability.mode_memory.record_count": (
            "aggregate-record-count",
            ["number"],
        ),
        "diagnostic.capability.mode_memory.records_sha256": (
            "aggregate-records-sha256",
            ["string"],
        ),
    }
    aggregate = aggregate_contracts.get(raw["semanticId"])
    if aggregate is None:
        return (
            (raw["semanticKind"], raw["exposure"]) in exact_kind_exposures
            and raw["platform"] == "thinq2"
            and raw["owner"] == "none"
        )
    semantic_kind, value_types = aggregate
    return (
        raw["semanticKind"] == semantic_kind
        and raw["valueTypes"] == value_types
        and raw["decoderContractIds"] == ["aggregate/mode-memory-triplet/v1"]
        and raw["exposure"] == "diagnostic"
        and raw.get("unit") is None
        and len(raw["sourceKeys"]) == 3
        and len(raw["sourceTags"]) == 3
        and len(raw["sourceTagHexes"]) == 3
        and raw["platform"] == "thinq2"
        and raw["owner"] == "none"
    )


def _artifact_root(
    artifact: Mapping[str, Any],
    *,
    domain: bytes,
    core_keys: tuple[str, ...],
) -> str:
    core = {key: artifact[key] for key in core_keys}
    encoded = json.dumps(
        core,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain + encoded).hexdigest()


def _load_tlv_read_catalogue(
    entity_raw: bytes,
    entity_digest: bytes,
    profile_raw: bytes,
    profile_digest: bytes,
    *,
    expected_count: int = EXPECTED_TLV_READ_DESCRIPTOR_COUNT,
) -> Mapping[str, TlvReadProfile]:
    """Validate both generated artifacts and return immutable exact-model profiles."""
    entity_artifact = _artifact_json(
        entity_raw, entity_digest, TLV_READ_ENTITY_CONTRACT_FILENAME
    )
    profile_artifact = _artifact_json(
        profile_raw, profile_digest, TLV_READ_PROFILE_FILENAME
    )
    if (
        set(entity_artifact) != _ENTITY_ARTIFACT_KEYS
        or set(profile_artifact) != _PROFILE_ARTIFACT_KEYS
    ):
        _catalogue_error()
    entity_root = _artifact_root(
        entity_artifact,
        domain=b"lg-rethink-local/full-read-entity-contract/v1\0",
        core_keys=(
            "schemaVersion",
            "authority",
            "access",
            "tlvEntityContractRevision",
            "tlvEntityContractSha256",
            "catalogRevision",
            "catalogSha256",
            "reviewedNonTlvSourceRevision",
            "reviewedNonTlvSourceSha256",
            "entities",
            "aggregateExclusions",
            "stats",
        ),
    )
    profile_root = _artifact_root(
        profile_artifact,
        domain=b"lg-rethink-local/full-read-sensor-profiles/v1\0",
        core_keys=(
            "schemaVersion",
            "authority",
            "access",
            "semanticsRevision",
            "readEntityContractRevision",
            "readEntityContractSha256",
            "catalogRevision",
            "catalogSha256",
            "reviewedNonTlvSourceRevision",
            "reviewedNonTlvSourceSha256",
            "profiles",
            "descriptorOnly",
            "aggregateExclusions",
            "stats",
        ),
    )
    if (
        entity_artifact["schemaVersion"] != 1
        or profile_artifact["schemaVersion"] != 1
        or entity_artifact["authority"] != "server"
        or profile_artifact["authority"] != "server"
        or entity_artifact["access"] != "read-only"
        or profile_artifact["access"] != "read-only"
        or not _valid_revision(entity_artifact["revision"])
        or not _valid_revision(profile_artifact["revision"])
        or not _valid_sha256(entity_artifact["rootSha256"])
        or not _valid_sha256(profile_artifact["rootSha256"])
        or entity_root != entity_artifact["rootSha256"]
        or profile_root != profile_artifact["rootSha256"]
        or entity_root != EXPECTED_TLV_READ_ENTITY_ROOT_SHA256
        or profile_root != EXPECTED_TLV_READ_PROFILE_ROOT_SHA256
        or entity_artifact["revision"]
        != f"full-read-entities-v1:{entity_root[:16]}"
        or profile_artifact["revision"]
        != f"full-read-sensor-profiles-v1:{profile_root[:16]}"
        or entity_artifact["tlvEntityContractRevision"]
        != EXPECTED_TLV_SOURCE_ENTITY_REVISION
        or entity_artifact["tlvEntityContractSha256"]
        != EXPECTED_TLV_SOURCE_ENTITY_ROOT_SHA256
        or entity_artifact["catalogRevision"] != EXPECTED_TLV_CATALOG_REVISION
        or not _valid_sha256(entity_artifact["catalogSha256"])
        or entity_artifact["catalogSha256"] != EXPECTED_TLV_CATALOG_SHA256
        or entity_artifact["reviewedNonTlvSourceRevision"]
        != EXPECTED_REVIEWED_NON_TLV_REVISION
        or entity_artifact["reviewedNonTlvSourceSha256"]
        != EXPECTED_REVIEWED_NON_TLV_SHA256
        or profile_artifact["catalogRevision"]
        != entity_artifact["catalogRevision"]
        or entity_artifact["catalogSha256"] != profile_artifact["catalogSha256"]
        or profile_artifact["reviewedNonTlvSourceRevision"]
        != entity_artifact["reviewedNonTlvSourceRevision"]
        or profile_artifact["reviewedNonTlvSourceSha256"]
        != entity_artifact["reviewedNonTlvSourceSha256"]
        or entity_artifact["revision"] != profile_artifact["readEntityContractRevision"]
        or entity_artifact["rootSha256"] != profile_artifact["readEntityContractSha256"]
        or profile_artifact["semanticsRevision"]
        != EXPECTED_TLV_READ_SEMANTICS_REVISION
    ):
        _catalogue_error("Bundled TLV read catalogue pins do not agree")

    raw_entities = entity_artifact["entities"]
    entity_stats = entity_artifact["stats"]
    expected_entity_stats = {
        "generatedEntityCount": expected_count,
        "tlvEntityCount": 290,
        "reviewedNonTlvEntityCount": 80,
        "binarySensorCount": 85,
        "sensorCount": 273,
        "eventCount": 12,
        "stateCount": 209,
        "diagnosticCount": 149,
        "eventExposureCount": 12,
        "patOwnerCount": 4,
        "aggregateExclusionGroupCount": 7,
        "aggregateExcludedSourceEntryCount": 17,
    }
    if (
        not isinstance(raw_entities, list)
        or len(raw_entities) != expected_count
        or not isinstance(entity_stats, dict)
        or set(entity_stats) != _ENTITY_STATS_KEYS
        or any(type(value) is not int or value < 0 for value in entity_stats.values())
        or entity_stats != expected_entity_stats
        or entity_artifact["aggregateExclusions"]
        != profile_artifact["aggregateExclusions"]
    ):
        _catalogue_error("Bundled TLV read entity accounting is invalid")
    entity_by_key: dict[str, dict[str, Any]] = {}
    entity_semantics: set[tuple[str, str]] = set()
    for raw in raw_entities:
        if (
            not isinstance(raw, dict)
            or not _ENTITY_DESCRIPTOR_REQUIRED_KEYS.issubset(raw)
            or not set(raw).issubset(
                _ENTITY_DESCRIPTOR_REQUIRED_KEYS | {"unit", "eventType"}
            )
            or ("eventType" in raw) is not (raw.get("exposure") == "event")
        ):
            _catalogue_error()
        key = raw["key"]
        model_id = raw["modelId"]
        semantic_id = raw["semanticId"]
        if (
            not isinstance(key, str)
            or key != f"{model_id}|{semantic_id}"
            or key in entity_by_key
            or (model_id, semantic_id) in entity_semantics
            or raw["domainAuthority"] != "server"
            or raw["access"] != "read-only"
            or raw["direction"] != "fromDevice"
            or raw["isDiagnostic"] is not (raw["exposure"] == "diagnostic")
            or not _artifact_string_list(raw["sourceKeys"], maximum=64)
            or not _artifact_string_list(
                raw["sourceTagHexes"], maximum=64, allow_empty=True
            )
            or not _artifact_string_list(raw["decoderContractIds"], maximum=64)
            or not isinstance(raw["sourceTags"], list)
            or len(raw["sourceTags"]) != len(raw["sourceTagHexes"])
            or bool(raw["sourceTags"])
            and len(raw["sourceTags"]) != len(raw["sourceKeys"])
            or any(
                type(tag) is not int or tag < 0 or tag > 65535
                for tag in raw["sourceTags"]
            )
            or raw["sourceTags"] != sorted(set(raw["sourceTags"]))
            or raw["sourceTagHexes"]
            != [f"0x{tag:04x}" for tag in raw["sourceTags"]]
        ):
            _catalogue_error("Bundled TLV read descriptor identity is invalid")
        try:
            contract = TlvReadFieldContract(
                descriptor_key=key,
                semantic_id=semantic_id,
                domain=raw["domain"],
                value_types=tuple(raw["valueTypes"])
                if isinstance(raw["valueTypes"], list)
                else (),
                exposure=raw["exposure"],
                label_ko=raw["labelKo"],
                entity_category=raw["entityCategory"],
                enabled_by_default=raw["enabledByDefault"],
                owner=raw["owner"],
                publication_mode=(
                    "transient-event"
                    if raw["exposure"] == "event"
                    else "retained-current"
                ),
                unit=raw.get("unit"),
                event_type=raw.get("eventType"),
            )
        except (TypeError, ValueError) as err:
            raise TlvReadCatalogueError(
                "Bundled TLV read descriptor matrix is invalid"
            ) from err
        if contract.model_id != model_id or not _valid_semantic_kind(raw):
            _catalogue_error()
        entity_by_key[key] = raw
        entity_semantics.add((model_id, semantic_id))

    stats = profile_artifact["stats"]
    raw_profiles = profile_artifact["profiles"]
    expected_publication_stats = {
        "generatedDescriptorCount": expected_count,
        "liveDescriptorCount": expected_count,
        "descriptorOnlyCount": 0,
        "retainedCurrentDescriptorCount": 358,
        "transientEventDescriptorCount": 12,
        "binarySensorDescriptorCount": 85,
        "sensorDescriptorCount": 273,
        "eventDomainDescriptorCount": 12,
        "stateDescriptorCount": 209,
        "diagnosticDescriptorCount": 149,
        "eventDescriptorCount": 12,
        "patOwnerDescriptorCount": 4,
        "profileCount": 16,
        "aggregateExclusionGroupCount": 7,
        "aggregateExcludedSourceEntryCount": 17,
        "pilotDescriptorCount": 184,
        "fullReadPilotOverlapCount": 96,
        "pilotOnlyDescriptorCount": 88,
        "overallOwnerInventoryCount": 458,
    }
    if (
        not isinstance(stats, dict)
        or set(stats) != _PUBLICATION_STATS_KEYS
        or any(type(value) is not int or value < 0 for value in stats.values())
        or stats != expected_publication_stats
        or not isinstance(raw_profiles, list)
        or len(raw_profiles) != 16
        or stats["profileCount"] != len(raw_profiles)
        or profile_artifact["descriptorOnly"] != []
    ):
        _catalogue_error("Bundled TLV read publication accounting is invalid")

    if len(raw_profiles) > 256:
        _catalogue_error()
    profiles: dict[str, TlvReadProfile] = {}
    seen_descriptor_keys: set[str] = set()
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, dict) or set(raw_profile) != _PROFILE_KEYS:
            _catalogue_error()
        if (
            raw_profile["authority"] != "server"
            or raw_profile["access"] != "read-only"
            or raw_profile["platform"] not in ("thinq1", "thinq2")
            or not _valid_positive_integer(raw_profile["contractRevision"])
            or raw_profile["modelId"] in profiles
        ):
            _catalogue_error()
        raw_fields = raw_profile["fields"]
        raw_stats = raw_profile["stats"]
        if (
            not isinstance(raw_fields, list)
            or not raw_fields
            or len(raw_fields) > MAX_TLV_READ_FIELDS
            or not isinstance(raw_stats, dict)
            or set(raw_stats) != _PROFILE_STATS_KEYS
            or raw_stats["descriptorCount"] != len(raw_fields)
        ):
            _catalogue_error()
        fields: list[TlvReadFieldContract] = []
        for raw in raw_fields:
            if (
                not isinstance(raw, dict)
                or not _PROFILE_FIELD_REQUIRED_KEYS.issubset(raw)
                or not set(raw).issubset(
                    _PROFILE_FIELD_REQUIRED_KEYS | {"unit", "eventType"}
                )
                or ("eventType" in raw) is not (raw.get("exposure") == "event")
            ):
                _catalogue_error()
            descriptor_key = raw["descriptorKey"]
            entity = entity_by_key.get(descriptor_key)
            if (
                entity is None
                or descriptor_key in seen_descriptor_keys
                or entity["modelId"] != raw_profile["modelId"]
                or entity["platform"] != raw_profile["platform"]
                or any(
                    raw[key] != entity[entity_key]
                    for key, entity_key in (
                        ("semanticId", "semanticId"),
                        ("domain", "domain"),
                        ("valueTypes", "valueTypes"),
                        ("exposure", "exposure"),
                        ("labelKo", "labelKo"),
                        ("owner", "owner"),
                        ("entityCategory", "entityCategory"),
                        ("enabledByDefault", "enabledByDefault"),
                    )
                )
                or raw.get("unit") != entity.get("unit")
                or raw.get("eventType") != entity.get("eventType")
                or raw["publicationMode"]
                != (
                    "transient-event"
                    if raw["exposure"] == "event"
                    else "retained-current"
                )
            ):
                _catalogue_error("Bundled TLV read profile descriptor drifted")
            try:
                fields.append(
                    TlvReadFieldContract(
                        descriptor_key=descriptor_key,
                        semantic_id=raw["semanticId"],
                        domain=raw["domain"],
                        value_types=tuple(raw["valueTypes"])
                        if isinstance(raw["valueTypes"], list)
                        else (),
                        exposure=raw["exposure"],
                        label_ko=raw["labelKo"],
                        entity_category=raw["entityCategory"],
                        enabled_by_default=raw["enabledByDefault"],
                        owner=raw["owner"],
                        publication_mode=raw["publicationMode"],
                        unit=raw.get("unit"),
                        event_type=raw.get("eventType"),
                    )
                )
            except (TypeError, ValueError) as err:
                raise TlvReadCatalogueError(
                    "Bundled TLV read profile matrix is invalid"
                ) from err
            seen_descriptor_keys.add(descriptor_key)
        actual_profile_stats = {
            "descriptorCount": len(fields),
            "retainedCurrentDescriptorCount": sum(
                item.publication_mode == "retained-current" for item in fields
            ),
            "transientEventDescriptorCount": sum(
                item.publication_mode == "transient-event" for item in fields
            ),
            "binarySensorDescriptorCount": sum(
                item.domain == "binary_sensor" for item in fields
            ),
            "sensorDescriptorCount": sum(item.domain == "sensor" for item in fields),
            "eventDomainDescriptorCount": sum(
                item.domain == "event" for item in fields
            ),
            "stateDescriptorCount": sum(item.exposure == "state" for item in fields),
            "diagnosticDescriptorCount": sum(
                item.exposure == "diagnostic" for item in fields
            ),
            "eventDescriptorCount": sum(item.exposure == "event" for item in fields),
            "patOwnerDescriptorCount": sum(item.owner == "PAT" for item in fields),
        }
        if raw_stats != actual_profile_stats:
            _catalogue_error("Bundled TLV read profile stats are invalid")
        try:
            profile = TlvReadProfile(
                profile_id=raw_profile["profileId"],
                contract_revision=raw_profile["contractRevision"],
                profile_revision=profile_artifact["revision"],
                profile_sha256=profile_artifact["rootSha256"],
                read_entity_contract_revision=entity_artifact["revision"],
                read_entity_contract_sha256=entity_artifact["rootSha256"],
                catalog_sha256=entity_artifact["catalogSha256"],
                semantics_revision=profile_artifact["semanticsRevision"],
                model_id=raw_profile["modelId"],
                platform=raw_profile["platform"],
                fields=tuple(fields),
            )
        except (TypeError, ValueError) as err:
            raise TlvReadCatalogueError("Bundled TLV read profile is invalid") from err
        profiles[profile.model_id] = profile
    if seen_descriptor_keys != set(entity_by_key):
        _catalogue_error("Bundled TLV read descriptors are not accounted exactly once")
    all_fields = tuple(
        field for profile in profiles.values() for field in profile.fields
    )
    event_fields = tuple(field for field in all_fields if field.domain == "event")
    numeric_event_keys = frozenset(
        {
            "1WPD4CMIDR__3|water.usage_delta.cold_ml",
            "1WPD4CMIDR__3|water.usage_delta.hot_ml",
            "1WPD4CMIDR__3|water.usage_delta.mineral_ml",
            "1WPD4CMIDR__3|water.usage_delta.purified_ml",
            "1WPD4CMIDR__3|water.usage_delta.soda_ml",
            "1WPD4CMIDR__3|water.usage_delta.sterilization_ml",
            "1WPD4CMIDR__3|water.usage_delta.total_ml",
            "2REFO1DBN3K_U|energy.interval.delta_wh",
            "3REK2G03VI230D_2|energy.interval.delta_wh",
            "WBEF3|energy.interval.delta_wh",
            "WMLJ32RS|energy.interval.delta_wh",
        }
    )
    pat_owner_keys = frozenset(
        {
            "WBEF3|burner.left_rear.power_level",
            "WBEF3|burner.left_rear.state",
            "WBEF3|burner.right_front.power_level",
            "WBEF3|burner.right_front.state",
        }
    )
    if (
        len(all_fields) != expected_count
        or sum(field.publication_mode == "retained-current" for field in all_fields)
        != stats["retainedCurrentDescriptorCount"]
        or sum(field.publication_mode == "transient-event" for field in all_fields)
        != stats["transientEventDescriptorCount"]
        or sum(field.exposure == "state" for field in all_fields)
        != stats["stateDescriptorCount"]
        or sum(field.exposure == "diagnostic" for field in all_fields)
        != stats["diagnosticDescriptorCount"]
        or sum(field.exposure == "event" for field in all_fields)
        != stats["eventDescriptorCount"]
        or sum(field.enabled_by_default for field in all_fields)
        != stats["stateDescriptorCount"]
        or sum(field.domain == "binary_sensor" for field in all_fields) != 85
        or sum(field.domain == "sensor" for field in all_fields) != 273
        or len(event_fields) != 12
        or {
            field.descriptor_key
            for field in event_fields
            if field.event_type == "observed"
            and field.value_types == ("number",)
        }
        != numeric_event_keys
        or {
            field.descriptor_key
            for field in event_fields
            if field.event_type == "water-tank state changed"
            and field.value_types == ("string",)
        }
        != {"DHUM_056905_WW|event.water_tank.changed"}
        or {field.descriptor_key for field in all_fields if field.owner == "PAT"}
        != pat_owner_keys
        or {profile.model_id for profile in profiles.values() if profile.platform == "thinq1"}
        != {"2REK1D04AR170"}
        or sum(
            field.semantic_id.startswith("diagnostic.capability.mode_memory.")
            for field in all_fields
        )
        != 10
    ):
        _catalogue_error("Bundled TLV read HA entity accounting is invalid")
    return MappingProxyType(profiles)


_CATALOGUE_CACHE: Mapping[str, TlvReadProfile] | None = None
_CATALOGUE_LOCK = threading.Lock()


def load_tlv_read_catalogue(path: Path | None = None) -> Mapping[str, TlvReadProfile]:
    """Pilot selected models; every other model keeps its released profile."""
    feature_database = (
        Path(__file__).resolve().parents[2] / "my_lg_features.sqlite3"
        if path is None else path
    )
    if not feature_database.is_file():
        return _load_bundled_tlv_read_catalogue()
    selected, complete = _database_rollout_models(feature_database)
    if not selected:
        return _load_bundled_tlv_read_catalogue()
    dynamic = load_tlv_read_catalogue_from_database(feature_database)
    if complete:
        return dynamic
    baseline = _load_bundled_tlv_read_catalogue()
    merged = dict(baseline)
    for model_id in selected:
        if model_id in dynamic:
            merged[model_id] = dynamic[model_id]
        else:
            merged.pop(model_id, None)
    return MappingProxyType(merged)


def _database_rollout_models(path: Path) -> tuple[frozenset[str], bool]:
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0] != 0x4C474646
                or connection.execute("PRAGMA user_version").fetchone()[0] != 2
            ):
                raise TlvReadCatalogueError("Local feature database layout is invalid")
            rows = connection.execute(
                "SELECT model_id FROM model_rollout WHERE enabled = 1"
            ).fetchall()
            total = connection.execute(
                "SELECT COUNT(*) FROM model_rollout"
            ).fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise TlvReadCatalogueError("Local feature database rollout is unavailable") from error
    selected = frozenset(row[0] for row in rows)
    return selected, total > 0 and len(selected) == total


def _load_bundled_tlv_read_catalogue() -> Mapping[str, TlvReadProfile]:
    """Legacy cache stays intact for models not yet moved to the database."""
    global _CATALOGUE_CACHE
    cached = _CATALOGUE_CACHE
    if cached is not None:
        return cached
    with _CATALOGUE_LOCK:
        cached = _CATALOGUE_CACHE
        if cached is not None:
            return cached
        directory = Path(__file__).resolve().parent
        try:
            loaded = _load_tlv_read_catalogue(
                (directory / TLV_READ_ENTITY_CONTRACT_FILENAME).read_bytes(),
                (directory / TLV_READ_ENTITY_CONTRACT_DIGEST_FILENAME).read_bytes(),
                (directory / TLV_READ_PROFILE_FILENAME).read_bytes(),
                (directory / TLV_READ_PROFILE_DIGEST_FILENAME).read_bytes(),
            )
        except OSError as err:
            raise TlvReadCatalogueError(
                "Bundled TLV read catalogue is unavailable"
            ) from err
        _CATALOGUE_CACHE = loaded
        return loaded


def load_tlv_read_catalogue_from_database(
    path: Path,
) -> Mapping[str, TlvReadProfile]:
    """One model's row edit changes only that model's visible read features.

    Receipt fields on ``TlvReadProfile`` are legacy metadata, never acceptance
    pins in field-compatible mode. No fixed descriptor count or global revision
    is used to decide whether a field can be displayed.
    """
    if not path.is_file() or path.is_symlink():
        raise TlvReadCatalogueError("Local feature database is unavailable")
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != 0x4C474646
                or connection.execute("PRAGMA user_version").fetchone()[0] != 2
            ):
                raise TlvReadCatalogueError("Local feature database layout is invalid")
            rows = connection.execute(
                "SELECT model_id, profile_id, feature_id, platform, definition_json, enabled "
                "FROM features WHERE channel = 'full-read' "
                "ORDER BY model_id, feature_id"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise TlvReadCatalogueError("Local feature database could not be read") from error
    grouped: dict[str, list[TlvReadFieldContract]] = {}
    platforms: dict[str, str] = {}
    for row in rows:
        model_id = row["model_id"]
        platform = row["platform"]
        if not row["enabled"]:
            if platform in ("thinq1", "thinq2"):
                platforms.setdefault(model_id, platform)
                grouped.setdefault(model_id, [])
            continue
        try:
            field = json.loads(row["definition_json"])
            if (
                not isinstance(field, dict)
                or row["profile_id"] != f"{model_id}:read-sensors-v1"
                or row["feature_id"] != field["semanticId"]
                or field["descriptorKey"] != f"{model_id}|{row['feature_id']}"
                or platform not in ("thinq1", "thinq2")
                or (model_id in platforms and platforms[model_id] != platform)
            ):
                raise ValueError("feature identity is invalid")
            contract = TlvReadFieldContract(
                descriptor_key=field["descriptorKey"],
                semantic_id=field["semanticId"],
                domain=field["domain"],
                value_types=tuple(field["valueTypes"]),
                exposure=field["exposure"],
                label_ko=field["labelKo"],
                entity_category=field["entityCategory"],
                enabled_by_default=field["enabledByDefault"],
                owner=field["owner"],
                publication_mode=field["publicationMode"],
                unit=field.get("unit"),
                event_type=field.get("eventType"),
            )
        except (KeyError, TypeError, ValueError) as error:
            _LOGGER.warning(
                "Local feature database skipped an invalid read definition for model %s",
                model_id,
            )
            continue
        platforms[model_id] = platform
        grouped.setdefault(model_id, []).append(contract)
    profiles: dict[str, TlvReadProfile] = {}
    for model_id, fields in grouped.items():
        # This digest helps compare a model's effective menu; it is not a gate.
        digest = hashlib.sha256(
            json.dumps(
                [field.descriptor_key for field in fields],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            profiles[model_id] = TlvReadProfile(
                profile_id=f"{model_id}:read-sensors-v1",
                contract_revision=1,
                profile_revision=f"feature-db:{digest[:16]}",
                profile_sha256=digest,
                read_entity_contract_revision="feature-db",
                read_entity_contract_sha256=digest,
                catalog_sha256=digest,
                semantics_revision=1,
                model_id=model_id,
                platform=platforms[model_id],
                fields=tuple(fields),
            )
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Local feature database skipped an invalid read menu for model %s",
                model_id,
            )
    return MappingProxyType(profiles)


def _valid_positive_integer(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_JSON_SAFE_INTEGER


def _valid_revision(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 256


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_opaque_id(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class TlvReadFieldContract:
    """One exact Home Assistant descriptor owned by the generated artifact."""

    descriptor_key: str
    semantic_id: str
    domain: Literal["binary_sensor", "sensor", "event"]
    value_types: tuple[Literal["boolean", "number", "string"], ...]
    exposure: Literal["state", "diagnostic", "event"]
    label_ko: str
    entity_category: Literal["diagnostic"] | None
    enabled_by_default: bool
    publication_mode: Literal["retained-current", "transient-event"]
    owner: Literal["none", "PAT"] = "none"
    unit: str | None = None
    event_type: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.semantic_id, str)
            or len(self.semantic_id) > 128
            or _SEMANTIC_ID.fullmatch(self.semantic_id) is None
        ):
            raise ValueError("TLV read semantic id is invalid")
        if (
            not isinstance(self.descriptor_key, str)
            or len(self.descriptor_key) > 384
            or not self.descriptor_key.endswith(f"|{self.semantic_id}")
        ):
            raise ValueError("TLV read descriptor key is invalid")
        if not isinstance(self.value_types, tuple):
            raise TypeError("TLV read value types must be an immutable tuple")

        # This is intentionally an exact matrix rather than a best-effort HA
        # domain guess. A union is authorized solely for regular sensors;
        # transient events carry one exact scalar type and a contract-owned HA
        # event type that is distinct from the observed scalar value.
        allowed_types = {
            "binary_sensor": (("boolean",),),
            "sensor": (("number",), ("string",), ("number", "string")),
            "event": (("number",), ("string",)),
        }
        if (
            self.domain not in allowed_types
            or self.value_types not in allowed_types[self.domain]
        ):
            raise ValueError("TLV read domain/value-type matrix is invalid")
        expected_mode = (
            "transient-event" if self.domain == "event" else "retained-current"
        )
        if self.publication_mode != expected_mode:
            raise ValueError("TLV read publication mode is invalid")
        if (self.domain == "event") is not (self.exposure == "event"):
            raise ValueError("TLV read event exposure is invalid")
        if self.domain != "event" and self.exposure not in ("state", "diagnostic"):
            raise ValueError("TLV read retained exposure is invalid")
        expected_event_type = None
        if self.domain == "event":
            expected_event_type = (
                "observed"
                if self.value_types == ("number",)
                else "water-tank state changed"
            )
        if self.event_type != expected_event_type:
            raise ValueError("TLV read event type is invalid")
        if (
            self.domain == "event"
            and (self.value_types == ("string",))
            is not (self.semantic_id == "event.water_tank.changed")
        ):
            raise ValueError("TLV read event semantic/type matrix is invalid")
        if self.owner not in ("none", "PAT"):
            raise ValueError("TLV read owner is invalid")
        expected_category = "diagnostic" if self.exposure == "diagnostic" else None
        if self.entity_category != expected_category:
            raise ValueError("TLV read entity category is invalid")
        if (
            type(self.enabled_by_default) is not bool
            or self.enabled_by_default is not (self.exposure == "state")
        ):
            raise ValueError("TLV read enabled-by-default policy is invalid")
        if (
            not isinstance(self.label_ko, str)
            or not self.label_ko.strip()
            or len(self.label_ko.encode("utf-8")) > 256
            or any(ord(char) < 32 for char in self.label_ko)
        ):
            raise ValueError("TLV read Korean label is invalid")
        if self.unit is not None and (
            self.domain not in ("sensor", "event")
            or self.value_types == ("string",)
            or not isinstance(self.unit, str)
            or not self.unit
            or len(self.unit) > 16
            or any(ord(char) < 32 for char in self.unit)
        ):
            raise ValueError("TLV read unit is invalid")

    @property
    def model_id(self) -> str:
        """Return the exact model prefix carried by the descriptor key."""
        return self.descriptor_key.split("|", 1)[0]


@dataclass(frozen=True)
class TlvReadProfile:
    """One exact-model complete TLV read publication profile."""

    profile_id: str
    contract_revision: int
    profile_revision: str
    profile_sha256: str
    read_entity_contract_revision: str
    read_entity_contract_sha256: str
    catalog_sha256: str
    semantics_revision: int
    model_id: str
    platform: Literal["thinq1", "thinq2"]
    fields: tuple[TlvReadFieldContract, ...]

    def __post_init__(self) -> None:
        if self.profile_id != f"{self.model_id}:read-sensors-v1":
            raise ValueError("TLV read profile id is invalid")
        if not _valid_opaque_id(self.model_id):
            raise ValueError("TLV read model id is invalid")
        if self.platform not in ("thinq1", "thinq2"):
            raise ValueError("TLV read platform is invalid")
        if not _valid_positive_integer(
            self.contract_revision
        ) or not _valid_positive_integer(self.semantics_revision):
            raise ValueError("TLV read profile revision is invalid")
        if not _valid_revision(self.profile_revision) or not _valid_revision(
            self.read_entity_contract_revision
        ):
            raise ValueError("TLV read artifact revision is invalid")
        if not all(
            _valid_sha256(value)
            for value in (
                self.profile_sha256,
                self.read_entity_contract_sha256,
                self.catalog_sha256,
            )
        ):
            raise ValueError("TLV read artifact digest is invalid")
        if (
            not isinstance(self.fields, tuple)
            or len(self.fields) > MAX_TLV_READ_FIELDS
        ):
            raise ValueError("TLV read profile fields are invalid")
        semantic_ids: set[str] = set()
        descriptor_keys: set[str] = set()
        for field in self.fields:
            if (
                not isinstance(field, TlvReadFieldContract)
                or field.model_id != self.model_id
                or field.semantic_id in semantic_ids
                or field.descriptor_key in descriptor_keys
            ):
                raise ValueError(
                    "TLV read profile contains a duplicate or foreign field"
                )
            semantic_ids.add(field.semantic_id)
            descriptor_keys.add(field.descriptor_key)

    @property
    def fields_by_semantic_id(self) -> Mapping[str, TlvReadFieldContract]:
        return MappingProxyType({field.semantic_id: field for field in self.fields})

    @property
    def fields_by_descriptor_key(self) -> Mapping[str, TlvReadFieldContract]:
        return MappingProxyType({field.descriptor_key: field for field in self.fields})


@dataclass(frozen=True)
class TlvReadPerModelAuthority:
    """One generated exact-model authority for the v2 static projection."""

    profile_id: str
    model_id: str
    platform: Literal["thinq1", "thinq2"]
    semantics_revision: int
    model_contract_sha256: str
    feed_schema_version: int
    publication_plan_revision: int
    static_contract_projection_version: int

    def __post_init__(self) -> None:
        if (
            self.profile_id != f"{self.model_id}:read-sensors-v1"
            or not _valid_opaque_id(self.model_id)
            or self.platform not in ("thinq1", "thinq2")
            or not _valid_positive_integer(self.semantics_revision)
            or not _valid_sha256(self.model_contract_sha256)
            or self.feed_schema_version != PER_MODEL_TLV_READ_SCHEMA_VERSION
            or self.publication_plan_revision
            != TLV_READ_PUBLICATION_PLAN_REVISION
            or self.static_contract_projection_version
            != READ_STATIC_CONTRACT_PROJECTION_VERSION
        ):
            raise ValueError("TLV read per-model authority is invalid")


def _reviewed_tlv_read_v2_predecessor_authority(
    successor: TlvReadPerModelAuthority | None,
) -> TlvReadPerModelAuthority | None:
    """Return one exact setup-only predecessor for a reviewed successor."""
    if successor is None:
        return None
    transition = _REVIEWED_TLV_READ_V2_SUCCESSOR_PREDECESSORS.get(
        successor.model_id
    )
    if transition is None or (
        transition["successor_semantics_revision"]
        != successor.semantics_revision
        or transition["successor_model_contract_sha256"]
        != successor.model_contract_sha256
    ):
        return None
    return TlvReadPerModelAuthority(
        profile_id=successor.profile_id,
        model_id=successor.model_id,
        platform=successor.platform,
        semantics_revision=transition["predecessor_semantics_revision"],
        model_contract_sha256=transition[
            "predecessor_model_contract_sha256"
        ],
        feed_schema_version=successor.feed_schema_version,
        publication_plan_revision=successor.publication_plan_revision,
        static_contract_projection_version=(
            successor.static_contract_projection_version
        ),
    )


@dataclass(frozen=True)
class TlvReadConsumerPin:
    """One exact static producer contract accepted for a binding."""

    projection_version: Literal[1, 2]
    static_read_contract_sha256: str
    model_contract_sha256: str | None

    def __post_init__(self) -> None:
        if (
            self.projection_version not in (1, 2)
            or not _valid_sha256(self.static_read_contract_sha256)
            or (
                self.projection_version == 1
                and self.model_contract_sha256 is not None
            )
            or (
                self.projection_version == 2
                and not _valid_sha256(self.model_contract_sha256)
            )
        ):
            raise ValueError("TLV read consumer pin is invalid")


@dataclass(frozen=True)
class TlvReadConsumerBindingAuthority:
    """Static HA authority used to derive binding-generation-specific pins."""

    binding_id: str
    pat_device_id_proof_sha256: str
    profile: TlvReadProfile
    model_authority: TlvReadPerModelAuthority | None

    def __post_init__(self) -> None:
        if (
            _BINDING_ID.fullmatch(self.binding_id) is None
            or not _valid_sha256(self.pat_device_id_proof_sha256)
            or not isinstance(self.profile, TlvReadProfile)
            or self.model_authority is not None
            and (
                self.model_authority.profile_id != self.profile.profile_id
                or self.model_authority.model_id != self.profile.model_id
                or self.model_authority.platform != self.profile.platform
                or self.model_authority.semantics_revision
                > self.profile.semantics_revision
            )
        ):
            raise ValueError("TLV read consumer binding authority is invalid")

    def pin_for_projection(
        self, projection_version: Literal[1, 2], binding_generation: int
    ) -> TlvReadConsumerPin:
        """Derive one pin using only static authority and a JIT generation."""
        if not _valid_positive_integer(binding_generation):
            raise ValueError("TLV read consumer binding generation is invalid")
        if projection_version == 1:
            value = {
                "schema_version": TLV_READ_SCHEMA_VERSION,
                "publication_plan_revision": TLV_READ_PUBLICATION_PLAN_REVISION,
                "profile_id": self.profile.profile_id,
                "profile_contract_revision": self.profile.contract_revision,
                "profile_revision": self.profile.profile_revision,
                "profile_sha256": self.profile.profile_sha256,
                "read_entity_contract_revision": self.profile.read_entity_contract_revision,
                "read_entity_contract_sha256": self.profile.read_entity_contract_sha256,
                "catalog_sha256": self.profile.catalog_sha256,
                "semantics_revision": self.profile.semantics_revision,
                "binding_id": self.binding_id,
                "model_id": self.profile.model_id,
                "platform": self.profile.platform,
                "binding_generation": binding_generation,
                "pat_device_id_proof_sha256": self.pat_device_id_proof_sha256,
            }
            return TlvReadConsumerPin(
                projection_version=1,
                static_read_contract_sha256=(
                    tlv_read_publication_static_contract_sha256(value, 1)
                ),
                model_contract_sha256=None,
            )
        authority = self.model_authority
        if projection_version != 2 or authority is None:
            raise ValueError("TLV read consumer projection authority is unavailable")
        value = {
            "schema_version": authority.feed_schema_version,
            "publication_plan_revision": authority.publication_plan_revision,
            "static_contract_projection_version": (
                authority.static_contract_projection_version
            ),
            "profile_id": authority.profile_id,
            "model_contract_sha256": authority.model_contract_sha256,
            "semantics_revision": authority.semantics_revision,
            "binding_id": self.binding_id,
            "model_id": authority.model_id,
            "platform": authority.platform,
            "binding_generation": binding_generation,
            "pat_device_id_proof_sha256": self.pat_device_id_proof_sha256,
        }
        return TlvReadConsumerPin(
            projection_version=2,
            static_read_contract_sha256=(
                tlv_read_publication_static_contract_sha256(value, 2)
            ),
            model_contract_sha256=authority.model_contract_sha256,
        )

    def reviewed_predecessor_pin_for_v2_successor(
        self, binding_generation: int
    ) -> TlvReadConsumerPin:
        """Derive the sole reviewed predecessor of this exact successor."""
        predecessor = _reviewed_tlv_read_v2_predecessor_authority(
            self.model_authority
        )
        if predecessor is None:
            raise ValueError(
                "TLV read v2 successor has no reviewed predecessor authority"
            )
        return TlvReadConsumerBindingAuthority(
            binding_id=self.binding_id,
            pat_device_id_proof_sha256=self.pat_device_id_proof_sha256,
            profile=self.profile,
            model_authority=predecessor,
        ).pin_for_projection(2, binding_generation)


@dataclass(frozen=True)
class TlvReadConsumerPinSet:
    """At most two exact producer contracts accepted for one binding."""

    binding_id: str
    accepted: tuple[TlvReadConsumerPin, ...]
    record_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or _BINDING_ID.fullmatch(self.binding_id) is None
            or not isinstance(self.accepted, tuple)
            or not 1 <= len(self.accepted) <= 2
            or any(not isinstance(pin, TlvReadConsumerPin) for pin in self.accepted)
            or len(
                {pin.static_read_contract_sha256 for pin in self.accepted}
            )
            != len(self.accepted)
            or len({pin.projection_version for pin in self.accepted})
            != len(self.accepted)
            or not _valid_sha256(self.record_sha256)
        ):
            raise ValueError("TLV read consumer pin set is invalid")


def _consumer_pin_json(pin: TlvReadConsumerPin) -> dict[str, Any]:
    return {
        "projection_version": pin.projection_version,
        "static_read_contract_sha256": pin.static_read_contract_sha256,
        "model_contract_sha256": pin.model_contract_sha256,
    }


def tlv_read_consumer_pin_set_record_sha256(
    binding_id: str, accepted: tuple[TlvReadConsumerPin, ...]
) -> str:
    """Reproduce the source runtime's stable per-binding pin-set digest."""
    core = {
        "schema_version": 1,
        "binding_id": binding_id,
        "accepted": [_consumer_pin_json(pin) for pin in accepted],
    }
    encoded = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(_READ_CONSUMER_PIN_SET_HASH_DOMAIN + encoded).hexdigest()


def build_tlv_read_consumer_pin_set(
    binding_id: str, accepted: tuple[TlvReadConsumerPin, ...]
) -> TlvReadConsumerPinSet:
    """Build one canonical pin set for a JIT-captured exact binding."""
    return TlvReadConsumerPinSet(
        binding_id=binding_id,
        accepted=accepted,
        record_sha256=tlv_read_consumer_pin_set_record_sha256(
            binding_id, accepted
        ),
    )


def parse_tlv_read_consumer_pin_set(
    value: object, binding_id: str
) -> TlvReadConsumerPinSet:
    """Validate a pin set written by the installation switch adapter."""
    if isinstance(value, TlvReadConsumerPinSet):
        raw: Mapping[str, Any] = {
            "schema_version": value.schema_version,
            "binding_id": value.binding_id,
            "accepted": [_consumer_pin_json(pin) for pin in value.accepted],
            "record_sha256": value.record_sha256,
        }
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise ValueError("TLV read consumer pin set is invalid")
    if set(raw) != _READ_CONSUMER_PIN_SET_KEYS:
        raise ValueError("TLV read consumer pin set keys are invalid")
    raw_accepted = raw["accepted"]
    if not isinstance(raw_accepted, (list, tuple)):
        raise ValueError("TLV read consumer pin set entries are invalid")
    pins: list[TlvReadConsumerPin] = []
    for item in raw_accepted:
        if not isinstance(item, Mapping) or set(item) != _READ_CONSUMER_PIN_KEYS:
            raise ValueError("TLV read consumer pin keys are invalid")
        pins.append(
            TlvReadConsumerPin(
                projection_version=item["projection_version"],
                static_read_contract_sha256=item[
                    "static_read_contract_sha256"
                ],
                model_contract_sha256=item["model_contract_sha256"],
            )
        )
    accepted = tuple(pins)
    parsed = TlvReadConsumerPinSet(
        schema_version=raw["schema_version"],
        binding_id=raw["binding_id"],
        accepted=accepted,
        record_sha256=raw["record_sha256"],
    )
    if (
        parsed.binding_id != binding_id
        or parsed.record_sha256
        != tlv_read_consumer_pin_set_record_sha256(binding_id, accepted)
    ):
        raise ValueError("TLV read consumer pin set digest or binding changed")
    return parsed


@dataclass(frozen=True)
class TlvReadConsumerBindingState:
    """Durable accepted pins and the monotonic projection latch for one binding.

    This exact rollback record deliberately contains no timestamp, counter, or
    transaction sequence. A staged v1 source can therefore be reconstructed
    byte-for-byte instead of fabricating a new historical value.
    """

    binding_id: str
    pat_device_id_proof_sha256: str
    adopted_projection_version: Literal[1, 2]
    adopted_static_read_contract_sha256: str
    adopted_model_contract_sha256: str | None
    consumer_pin_set: TlvReadConsumerPinSet
    record_sha256: str
    schema_version: int = 1
    predecessor_pin: TlvReadConsumerPin | None = None
    predecessor_record_sha256: str | None = None

    def __post_init__(self) -> None:
        matching = tuple(
            pin
            for pin in self.consumer_pin_set.accepted
            if pin.projection_version == self.adopted_projection_version
            and pin.static_read_contract_sha256
            == self.adopted_static_read_contract_sha256
            and pin.model_contract_sha256 == self.adopted_model_contract_sha256
        )
        common_invalid = (
            self.schema_version not in (1, 2)
            or _BINDING_ID.fullmatch(self.binding_id) is None
            or self.consumer_pin_set.binding_id != self.binding_id
            or not _valid_sha256(self.pat_device_id_proof_sha256)
            or self.adopted_projection_version not in (1, 2)
            or not _valid_sha256(self.adopted_static_read_contract_sha256)
            or (
                self.adopted_projection_version == 1
                and self.adopted_model_contract_sha256 is not None
            )
            or (
                self.adopted_projection_version == 2
                and not _valid_sha256(self.adopted_model_contract_sha256)
            )
            or len(matching) != 1
            or not _valid_sha256(self.record_sha256)
        )
        predecessor_invalid = False
        if self.schema_version == 1:
            predecessor_invalid = (
                self.predecessor_pin is not None
                or self.predecessor_record_sha256 is not None
            )
        elif not isinstance(self.predecessor_pin, TlvReadConsumerPin):
            predecessor_invalid = True
        else:
            adopted_pin = TlvReadConsumerPin(
                projection_version=self.adopted_projection_version,
                static_read_contract_sha256=(
                    self.adopted_static_read_contract_sha256
                ),
                model_contract_sha256=self.adopted_model_contract_sha256,
            )
            predecessor_pin_set = build_tlv_read_consumer_pin_set(
                self.binding_id, (self.predecessor_pin,)
            )
            predecessor_invalid = (
                self.adopted_projection_version != 2
                or self.predecessor_pin.projection_version != 2
                or self.predecessor_pin == adopted_pin
                or self.consumer_pin_set.accepted != (adopted_pin,)
                or not _valid_sha256(self.predecessor_record_sha256)
                or self.predecessor_record_sha256
                != tlv_read_consumer_binding_state_record_sha256(
                    binding_id=self.binding_id,
                    pat_device_id_proof_sha256=(
                        self.pat_device_id_proof_sha256
                    ),
                    adopted_pin=self.predecessor_pin,
                    consumer_pin_set=predecessor_pin_set,
                )
            )
        if common_invalid or predecessor_invalid:
            raise ValueError("TLV read consumer binding state is invalid")


@dataclass(frozen=True)
class TlvReadConsumerStateInventory:
    """One config-entry-owned collection of exact per-binding consumer states."""

    bindings: Mapping[str, TlvReadConsumerBindingState]
    root_sha256: str
    schema_version: int = 1
    artifact: str = "read-contract-consumer-state-inventory"

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.artifact != "read-contract-consumer-state-inventory"
            or not isinstance(self.bindings, Mapping)
            or len(self.bindings) > 64
            or any(
                not isinstance(state, TlvReadConsumerBindingState)
                or binding_id != state.binding_id
                for binding_id, state in self.bindings.items()
            )
            or not _valid_sha256(self.root_sha256)
        ):
            raise ValueError("TLV read consumer state inventory is invalid")


def _consumer_pin_set_json(pin_set: TlvReadConsumerPinSet) -> dict[str, Any]:
    return {
        "schema_version": pin_set.schema_version,
        "binding_id": pin_set.binding_id,
        "accepted": [_consumer_pin_json(pin) for pin in pin_set.accepted],
        "record_sha256": pin_set.record_sha256,
    }


def _consumer_binding_state_core(
    *,
    binding_id: str,
    pat_device_id_proof_sha256: str,
    adopted_pin: TlvReadConsumerPin,
    consumer_pin_set: TlvReadConsumerPinSet,
    schema_version: int = 1,
    predecessor_pin: TlvReadConsumerPin | None = None,
    predecessor_record_sha256: str | None = None,
) -> dict[str, Any]:
    core = {
        "schema_version": schema_version,
        "binding_id": binding_id,
        "pat_device_id_proof_sha256": pat_device_id_proof_sha256,
        "adopted_projection_version": adopted_pin.projection_version,
        "adopted_static_read_contract_sha256": (
            adopted_pin.static_read_contract_sha256
        ),
        "adopted_model_contract_sha256": adopted_pin.model_contract_sha256,
        "consumer_pin_set": _consumer_pin_set_json(consumer_pin_set),
    }
    if schema_version == 2:
        if predecessor_pin is None or predecessor_record_sha256 is None:
            raise ValueError("TLV read consumer predecessor is incomplete")
        core.update(
            {
                "predecessor_pin": _consumer_pin_json(predecessor_pin),
                "predecessor_record_sha256": predecessor_record_sha256,
            }
        )
    elif (
        schema_version != 1
        or predecessor_pin is not None
        or predecessor_record_sha256 is not None
    ):
        raise ValueError("TLV read consumer binding state schema is invalid")
    return core


def tlv_read_consumer_binding_state_record_sha256(
    *,
    binding_id: str,
    pat_device_id_proof_sha256: str,
    adopted_pin: TlvReadConsumerPin,
    consumer_pin_set: TlvReadConsumerPinSet,
    schema_version: int = 1,
    predecessor_pin: TlvReadConsumerPin | None = None,
    predecessor_record_sha256: str | None = None,
) -> str:
    """Hash one exact durable projection latch and its currently accepted pins."""
    core = _consumer_binding_state_core(
        binding_id=binding_id,
        pat_device_id_proof_sha256=pat_device_id_proof_sha256,
        adopted_pin=adopted_pin,
        consumer_pin_set=consumer_pin_set,
        schema_version=schema_version,
        predecessor_pin=predecessor_pin,
        predecessor_record_sha256=predecessor_record_sha256,
    )
    encoded = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(
        _READ_CONSUMER_BINDING_STATE_HASH_DOMAIN + encoded
    ).hexdigest()


def build_tlv_read_consumer_binding_state(
    *,
    binding_id: str,
    pat_device_id_proof_sha256: str,
    adopted_pin: TlvReadConsumerPin,
    consumer_pin_set: TlvReadConsumerPinSet,
    predecessor_pin: TlvReadConsumerPin | None = None,
    predecessor_record_sha256: str | None = None,
) -> TlvReadConsumerBindingState:
    """Build one canonical binding state from installer-captured exact values."""
    schema_version = (
        2
        if predecessor_pin is not None or predecessor_record_sha256 is not None
        else 1
    )
    return TlvReadConsumerBindingState(
        binding_id=binding_id,
        pat_device_id_proof_sha256=pat_device_id_proof_sha256,
        adopted_projection_version=adopted_pin.projection_version,
        adopted_static_read_contract_sha256=(
            adopted_pin.static_read_contract_sha256
        ),
        adopted_model_contract_sha256=adopted_pin.model_contract_sha256,
        consumer_pin_set=consumer_pin_set,
        schema_version=schema_version,
        predecessor_pin=predecessor_pin,
        predecessor_record_sha256=predecessor_record_sha256,
        record_sha256=tlv_read_consumer_binding_state_record_sha256(
            binding_id=binding_id,
            pat_device_id_proof_sha256=pat_device_id_proof_sha256,
            adopted_pin=adopted_pin,
            consumer_pin_set=consumer_pin_set,
            schema_version=schema_version,
            predecessor_pin=predecessor_pin,
            predecessor_record_sha256=predecessor_record_sha256,
        ),
    )


def parse_tlv_read_consumer_binding_state(
    value: object,
) -> TlvReadConsumerBindingState:
    """Validate one Store row including both nested and outer self-digests."""
    if not isinstance(value, Mapping):
        raise ValueError("TLV read consumer binding state keys are invalid")
    schema_version = value.get("schema_version")
    expected_keys = (
        _READ_CONSUMER_BINDING_STATE_V1_KEYS
        if schema_version == 1
        else _READ_CONSUMER_BINDING_STATE_V2_KEYS
        if schema_version == 2
        else frozenset()
    )
    if set(value) != expected_keys:
        raise ValueError("TLV read consumer binding state keys are invalid")
    binding_id = value["binding_id"]
    if not isinstance(binding_id, str):
        raise ValueError("TLV read consumer binding state identity is invalid")
    pin_set = parse_tlv_read_consumer_pin_set(
        value["consumer_pin_set"], binding_id
    )
    adopted_pin = TlvReadConsumerPin(
        projection_version=value["adopted_projection_version"],
        static_read_contract_sha256=value[
            "adopted_static_read_contract_sha256"
        ],
        model_contract_sha256=value["adopted_model_contract_sha256"],
    )
    predecessor_pin = None
    predecessor_record_sha256 = None
    if schema_version == 2:
        raw_predecessor_pin = value["predecessor_pin"]
        if (
            not isinstance(raw_predecessor_pin, Mapping)
            or set(raw_predecessor_pin) != _READ_CONSUMER_PIN_KEYS
        ):
            raise ValueError("TLV read consumer predecessor pin is invalid")
        predecessor_pin = TlvReadConsumerPin(
            projection_version=raw_predecessor_pin["projection_version"],
            static_read_contract_sha256=raw_predecessor_pin[
                "static_read_contract_sha256"
            ],
            model_contract_sha256=raw_predecessor_pin[
                "model_contract_sha256"
            ],
        )
        predecessor_record_sha256 = value["predecessor_record_sha256"]
    parsed = TlvReadConsumerBindingState(
        schema_version=schema_version,
        binding_id=binding_id,
        pat_device_id_proof_sha256=value["pat_device_id_proof_sha256"],
        adopted_projection_version=adopted_pin.projection_version,
        adopted_static_read_contract_sha256=(
            adopted_pin.static_read_contract_sha256
        ),
        adopted_model_contract_sha256=adopted_pin.model_contract_sha256,
        consumer_pin_set=pin_set,
        record_sha256=value["record_sha256"],
        predecessor_pin=predecessor_pin,
        predecessor_record_sha256=predecessor_record_sha256,
    )
    if parsed.record_sha256 != tlv_read_consumer_binding_state_record_sha256(
        binding_id=binding_id,
        pat_device_id_proof_sha256=parsed.pat_device_id_proof_sha256,
        adopted_pin=adopted_pin,
        consumer_pin_set=pin_set,
        schema_version=schema_version,
        predecessor_pin=predecessor_pin,
        predecessor_record_sha256=predecessor_record_sha256,
    ):
        raise ValueError("TLV read consumer binding state digest changed")
    return parsed


def _consumer_binding_state_json(
    state: TlvReadConsumerBindingState,
) -> dict[str, Any]:
    adopted_pin = TlvReadConsumerPin(
        projection_version=state.adopted_projection_version,
        static_read_contract_sha256=state.adopted_static_read_contract_sha256,
        model_contract_sha256=state.adopted_model_contract_sha256,
    )
    return {
        **_consumer_binding_state_core(
            binding_id=state.binding_id,
            pat_device_id_proof_sha256=state.pat_device_id_proof_sha256,
            adopted_pin=adopted_pin,
            consumer_pin_set=state.consumer_pin_set,
            schema_version=state.schema_version,
            predecessor_pin=state.predecessor_pin,
            predecessor_record_sha256=state.predecessor_record_sha256,
        ),
        "record_sha256": state.record_sha256,
    }


def tlv_read_consumer_binding_state_json(
    state: TlvReadConsumerBindingState | Mapping[str, Any],
) -> dict[str, Any]:
    """Serialize one fully validated state for an adapter response."""
    return _consumer_binding_state_json(
        validate_tlv_read_consumer_binding_state(state)
    )


def validate_tlv_read_consumer_binding_state(
    value: TlvReadConsumerBindingState | Mapping[str, Any],
) -> TlvReadConsumerBindingState:
    """Return one fully self-digest-checked consumer binding state."""
    return parse_tlv_read_consumer_binding_state(
        _consumer_binding_state_json(value)
        if isinstance(value, TlvReadConsumerBindingState)
        else value
    )


def tlv_read_consumer_state_inventory_json(
    states: Mapping[str, TlvReadConsumerBindingState],
) -> dict[str, Any]:
    """Serialize a canonical Store payload without any runtime-state fields."""
    bindings = [
        _consumer_binding_state_json(states[binding_id])
        for binding_id in sorted(states)
    ]
    core = {
        "schema_version": 1,
        "artifact": "read-contract-consumer-state-inventory",
        "bindings": bindings,
    }
    encoded = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        **core,
        "root_sha256": hashlib.sha256(
            _READ_CONSUMER_STATE_INVENTORY_HASH_DOMAIN + encoded
        ).hexdigest(),
    }


def parse_tlv_read_consumer_state_inventory(
    value: object,
) -> TlvReadConsumerStateInventory:
    """Validate the complete config-entry Store payload without partial drops."""
    if not isinstance(value, Mapping) or set(value) != _READ_CONSUMER_STATE_INVENTORY_KEYS:
        raise ValueError("TLV read consumer state inventory keys are invalid")
    raw_bindings = value["bindings"]
    if (
        value["schema_version"] != 1
        or value["artifact"] != "read-contract-consumer-state-inventory"
        or not isinstance(raw_bindings, list)
        or len(raw_bindings) > 64
        or not _valid_sha256(value["root_sha256"])
    ):
        raise ValueError("TLV read consumer state inventory is invalid")
    states: dict[str, TlvReadConsumerBindingState] = {}
    previous_binding_id: str | None = None
    for raw_state in raw_bindings:
        state = parse_tlv_read_consumer_binding_state(raw_state)
        if (
            state.binding_id in states
            or previous_binding_id is not None
            and state.binding_id <= previous_binding_id
        ):
            raise ValueError("TLV read consumer state inventory order is invalid")
        states[state.binding_id] = state
        previous_binding_id = state.binding_id
    canonical = tlv_read_consumer_state_inventory_json(states)
    if canonical != dict(value):
        raise ValueError("TLV read consumer state inventory digest changed")
    return TlvReadConsumerStateInventory(
        bindings=MappingProxyType(states),
        root_sha256=value["root_sha256"],
    )


def stage_tlv_read_consumer_target_pin(
    source: TlvReadConsumerBindingState,
    target_pin: TlvReadConsumerPin,
) -> TlvReadConsumerBindingState:
    """Ensure one v2 target is accepted without lowering the adopted pin."""
    if target_pin.projection_version != 2:
        raise ValueError("TLV read target consumer pin is not projection v2")
    adopted_pin = TlvReadConsumerPin(
        projection_version=source.adopted_projection_version,
        static_read_contract_sha256=source.adopted_static_read_contract_sha256,
        model_contract_sha256=source.adopted_model_contract_sha256,
    )
    accepted = source.consumer_pin_set.accepted
    if target_pin in accepted:
        return source
    if accepted != (adopted_pin,):
        raise ValueError("TLV read consumer pin stage is not an exact one-step transition")
    pin_set = build_tlv_read_consumer_pin_set(
        source.binding_id, (adopted_pin, target_pin)
    )
    return build_tlv_read_consumer_binding_state(
        binding_id=source.binding_id,
        pat_device_id_proof_sha256=source.pat_device_id_proof_sha256,
        adopted_pin=adopted_pin,
        consumer_pin_set=pin_set,
    )


def adopt_tlv_read_consumer_projection(
    source: TlvReadConsumerBindingState,
    target_pin: TlvReadConsumerPin,
) -> TlvReadConsumerBindingState:
    """Advance the durable latch to one already-staged exact projection."""
    if target_pin not in source.consumer_pin_set.accepted:
        raise ValueError("TLV read adopted projection is not staged")
    if target_pin.projection_version < source.adopted_projection_version:
        raise ValueError("TLV read adopted projection regressed")
    if (
        target_pin.projection_version == source.adopted_projection_version
        and target_pin.static_read_contract_sha256
        == source.adopted_static_read_contract_sha256
        and target_pin.model_contract_sha256
        == source.adopted_model_contract_sha256
    ):
        return source
    return build_tlv_read_consumer_binding_state(
        binding_id=source.binding_id,
        pat_device_id_proof_sha256=source.pat_device_id_proof_sha256,
        adopted_pin=target_pin,
        consumer_pin_set=source.consumer_pin_set,
    )


def retire_tlv_read_consumer_source_pin(
    source: TlvReadConsumerBindingState,
) -> TlvReadConsumerBindingState:
    """Retire every non-adopted pin only after the projection latch advanced."""
    if source.adopted_projection_version != 2:
        raise ValueError("TLV read source pin cannot retire before v2 adoption")
    adopted_pin = TlvReadConsumerPin(
        projection_version=source.adopted_projection_version,
        static_read_contract_sha256=source.adopted_static_read_contract_sha256,
        model_contract_sha256=source.adopted_model_contract_sha256,
    )
    if source.consumer_pin_set.accepted == (adopted_pin,):
        return source
    if adopted_pin not in source.consumer_pin_set.accepted:
        raise ValueError("TLV read adopted consumer pin disappeared")
    pin_set = build_tlv_read_consumer_pin_set(source.binding_id, (adopted_pin,))
    return build_tlv_read_consumer_binding_state(
        binding_id=source.binding_id,
        pat_device_id_proof_sha256=source.pat_device_id_proof_sha256,
        adopted_pin=adopted_pin,
        consumer_pin_set=pin_set,
    )


def restore_tlv_read_consumer_staged_source(
    source: TlvReadConsumerBindingState,
) -> TlvReadConsumerBindingState:
    """Restore the exact single v1 pin before the producer start boundary."""
    checked = validate_tlv_read_consumer_binding_state(source)
    if checked.adopted_projection_version != 1:
        raise ValueError("TLV read adopted v2 projection cannot roll back")
    source_pins = tuple(
        pin
        for pin in checked.consumer_pin_set.accepted
        if pin.projection_version == 1
    )
    if len(source_pins) != 1:
        raise ValueError("TLV read staged source pin is absent or ambiguous")
    source_pin = source_pins[0]
    if checked.consumer_pin_set.accepted == (source_pin,):
        return checked
    if (
        len(checked.consumer_pin_set.accepted) != 2
        or not any(
            pin.projection_version == 2
            for pin in checked.consumer_pin_set.accepted
        )
    ):
        raise ValueError("TLV read staged source transition is invalid")
    pin_set = build_tlv_read_consumer_pin_set(checked.binding_id, (source_pin,))
    return build_tlv_read_consumer_binding_state(
        binding_id=checked.binding_id,
        pat_device_id_proof_sha256=checked.pat_device_id_proof_sha256,
        adopted_pin=source_pin,
        consumer_pin_set=pin_set,
    )


def _consumer_state_adopted_pin(
    state: TlvReadConsumerBindingState,
) -> TlvReadConsumerPin:
    return TlvReadConsumerPin(
        projection_version=state.adopted_projection_version,
        static_read_contract_sha256=state.adopted_static_read_contract_sha256,
        model_contract_sha256=state.adopted_model_contract_sha256,
    )


def adopt_tlv_read_consumer_successor_v2(
    source: TlvReadConsumerBindingState,
    successor_pin: TlvReadConsumerPin,
) -> TlvReadConsumerBindingState:
    """Replace one exact singleton v2 pin while retaining exact rollback proof."""
    checked = validate_tlv_read_consumer_binding_state(source)
    adopted_pin = _consumer_state_adopted_pin(checked)
    if (
        checked.schema_version == 2
        and adopted_pin == successor_pin
        and checked.predecessor_pin is not None
    ):
        return checked
    if (
        checked.schema_version != 1
        or adopted_pin.projection_version != 2
        or successor_pin.projection_version != 2
        or successor_pin == adopted_pin
        or checked.consumer_pin_set.accepted != (adopted_pin,)
    ):
        raise ValueError(
            "TLV read v2 successor requires one exact singleton predecessor"
        )
    successor_pin_set = build_tlv_read_consumer_pin_set(
        checked.binding_id, (successor_pin,)
    )
    return build_tlv_read_consumer_binding_state(
        binding_id=checked.binding_id,
        pat_device_id_proof_sha256=checked.pat_device_id_proof_sha256,
        adopted_pin=successor_pin,
        consumer_pin_set=successor_pin_set,
        predecessor_pin=adopted_pin,
        predecessor_record_sha256=checked.record_sha256,
    )


def restore_tlv_read_consumer_predecessor_v2(
    source: TlvReadConsumerBindingState,
    predecessor_pin: TlvReadConsumerPin | None = None,
) -> TlvReadConsumerBindingState:
    """Restore the byte-identical predecessor before successor retirement."""
    checked = validate_tlv_read_consumer_binding_state(source)
    if checked.schema_version == 1:
        if (
            predecessor_pin is not None
            and _consumer_state_adopted_pin(checked) == predecessor_pin
            and checked.consumer_pin_set.accepted == (predecessor_pin,)
        ):
            return checked
        raise ValueError("TLV read v2 predecessor is not retained")
    retained = checked.predecessor_pin
    if retained is None or (
        predecessor_pin is not None and retained != predecessor_pin
    ):
        raise ValueError("TLV read v2 predecessor is absent or changed")
    pin_set = build_tlv_read_consumer_pin_set(checked.binding_id, (retained,))
    restored = build_tlv_read_consumer_binding_state(
        binding_id=checked.binding_id,
        pat_device_id_proof_sha256=checked.pat_device_id_proof_sha256,
        adopted_pin=retained,
        consumer_pin_set=pin_set,
    )
    if restored.record_sha256 != checked.predecessor_record_sha256:
        raise ValueError("TLV read v2 predecessor digest cannot be reproduced")
    return restored


def retire_tlv_read_consumer_predecessor_v2(
    source: TlvReadConsumerBindingState,
    successor_pin: TlvReadConsumerPin | None = None,
) -> TlvReadConsumerBindingState:
    """Finalize one v2 successor and make its predecessor irrecoverable."""
    checked = validate_tlv_read_consumer_binding_state(source)
    adopted_pin = _consumer_state_adopted_pin(checked)
    if checked.schema_version == 1:
        if (
            successor_pin is not None
            and adopted_pin == successor_pin
            and checked.consumer_pin_set.accepted == (successor_pin,)
        ):
            return checked
        raise ValueError("TLV read v2 predecessor is already absent")
    if successor_pin is not None and adopted_pin != successor_pin:
        raise ValueError("TLV read v2 successor changed before retirement")
    pin_set = build_tlv_read_consumer_pin_set(checked.binding_id, (adopted_pin,))
    return build_tlv_read_consumer_binding_state(
        binding_id=checked.binding_id,
        pat_device_id_proof_sha256=checked.pat_device_id_proof_sha256,
        adopted_pin=adopted_pin,
        consumer_pin_set=pin_set,
    )


def validate_tlv_read_consumer_state_replacement(
    current: TlvReadConsumerBindingState,
    candidate: TlvReadConsumerBindingState,
) -> TlvReadConsumerBindingState:
    """Apply one shared monotonic latch predicate to Store and live heads."""
    before = validate_tlv_read_consumer_binding_state(current)
    after = validate_tlv_read_consumer_binding_state(candidate)
    if (
        before.binding_id != after.binding_id
        or before.pat_device_id_proof_sha256
        != after.pat_device_id_proof_sha256
    ):
        raise ValueError("TLV read consumer state binding identity changed")
    if before.record_sha256 == after.record_sha256:
        return after
    if after.adopted_projection_version < before.adopted_projection_version:
        raise ValueError("TLV read consumer projection latch regressed")
    before_pin = _consumer_state_adopted_pin(before)
    after_pin = _consumer_state_adopted_pin(after)
    # The released v1->v2 projection migration produces another schema-1
    # record.  A schema-2 body is reserved for the reviewed same-projection
    # successor chain below; allowing it through this generic version latch
    # would let an unproven predecessor become restorable.
    if (
        after.adopted_projection_version > before.adopted_projection_version
        and after.schema_version == 1
    ):
        return after
    if before_pin == after_pin:
        return after
    forward = (
        before.schema_version == 1
        and after.schema_version == 2
        and before.adopted_projection_version == 2
        and before.consumer_pin_set.accepted == (before_pin,)
        and after.consumer_pin_set.accepted == (after_pin,)
        and after.predecessor_pin == before_pin
        and after.predecessor_record_sha256 == before.record_sha256
    )
    restore = (
        before.schema_version == 2
        and after.schema_version == 1
        and before.predecessor_pin == after_pin
        and before.predecessor_record_sha256 == after.record_sha256
        and after.consumer_pin_set.accepted == (after_pin,)
    )
    if not forward and not restore:
        raise ValueError("TLV read consumer same-projection replacement refused")
    return after


def _durable_tlv_read_v2_predecessor_pin(
    state: TlvReadConsumerBindingState,
    authority: TlvReadPerModelAuthority | None,
) -> TlvReadConsumerPin | None:
    """Return one previously trusted v2 singleton during model rollout.

    The durable consumer row is already scoped to the exact binding and proof.
    Treating its adopted singleton as the sole predecessor avoids a fleet-wide
    hand-maintained generation map while every other model/hash still fails
    closed.
    """
    v2_pins = tuple(
        pin
        for pin in state.consumer_pin_set.accepted
        if pin.projection_version == 2
    )
    if authority is None:
        return None
    adopted_pin = _consumer_state_adopted_pin(state)
    if not (
        state.schema_version == 1
        and state.adopted_projection_version == 2
        and len(v2_pins) == 1
        and state.consumer_pin_set.accepted == v2_pins
        and adopted_pin == v2_pins[0]
        and adopted_pin.model_contract_sha256
        != authority.model_contract_sha256
    ):
        return None
    return adopted_pin


def _consumer_state_v2_pins_match_authority_or_reviewed_predecessor(
    state: TlvReadConsumerBindingState,
    authority: TlvReadPerModelAuthority | None,
) -> bool:
    """Accept current v2 pins or one exact durable predecessor singleton."""
    v2_pins = tuple(
        pin
        for pin in state.consumer_pin_set.accepted
        if pin.projection_version == 2
    )
    if not v2_pins:
        return True
    if authority is None:
        return False
    if all(
        pin.model_contract_sha256 == authority.model_contract_sha256
        for pin in v2_pins
    ):
        return True
    return _durable_tlv_read_v2_predecessor_pin(state, authority) is not None


def _durable_tlv_read_v2_message_authority(
    state: TlvReadConsumerBindingState | None,
    successor: TlvReadPerModelAuthority | None,
    reported_model_contract_sha256: object,
    reported_semantics_revision: object,
) -> TlvReadPerModelAuthority | None:
    """Resolve current authority or the exact durable predecessor singleton."""
    if successor is None:
        return None
    if reported_model_contract_sha256 == successor.model_contract_sha256:
        return successor
    if (
        state is None
        or not _valid_positive_integer(reported_semantics_revision)
        or reported_semantics_revision > successor.semantics_revision
    ):
        return None
    adopted_pin = _durable_tlv_read_v2_predecessor_pin(state, successor)
    if adopted_pin is None:
        return None
    if reported_model_contract_sha256 != adopted_pin.model_contract_sha256:
        return None
    return TlvReadPerModelAuthority(
        profile_id=successor.profile_id,
        model_id=successor.model_id,
        platform=successor.platform,
        semantics_revision=reported_semantics_revision,
        model_contract_sha256=reported_model_contract_sha256,
        feed_schema_version=successor.feed_schema_version,
        publication_plan_revision=successor.publication_plan_revision,
        static_contract_projection_version=(
            successor.static_contract_projection_version
        ),
    )


def transition_tlv_read_consumer_binding_state(
    *,
    operation: Literal[
        "bootstrap-v1",
        "stage-v2",
        "restore-staged-v1",
        "adopt-v2",
        "retire-v1",
        "adopt-successor-v2",
        "restore-predecessor-v2",
        "retire-predecessor-v2",
    ],
    current: TlvReadConsumerBindingState | None,
    binding_id: str,
    pat_device_id_proof_sha256: str,
    v1_pin: TlvReadConsumerPin | None = None,
    v2_pin: TlvReadConsumerPin | None = None,
    predecessor_v2_pin: TlvReadConsumerPin | None = None,
    observed_adopted_pin: TlvReadConsumerPin | None = None,
    expected_current_record_sha256: str | None,
) -> TlvReadConsumerBindingState:
    """Derive one idempotent target and CAS it against the current Store row."""
    if operation not in TLV_READ_CONSUMER_MUTATIONS:
        raise ValueError("TLV read consumer transition operation is invalid")
    if (
        _BINDING_ID.fullmatch(binding_id) is None
        or not _valid_sha256(pat_device_id_proof_sha256)
        or v1_pin is not None
        and v1_pin.projection_version != 1
        or v2_pin is not None
        and v2_pin.projection_version != 2
        or predecessor_v2_pin is not None
        and predecessor_v2_pin.projection_version != 2
        or observed_adopted_pin is not None
        and (
            operation != "stage-v2"
            or observed_adopted_pin.projection_version != 2
        )
        or expected_current_record_sha256 is not None
        and not _valid_sha256(expected_current_record_sha256)
    ):
        raise ValueError("TLV read consumer transition identity is invalid")
    checked_current = (
        None
        if current is None
        else validate_tlv_read_consumer_binding_state(current)
    )
    if checked_current is not None and (
        checked_current.binding_id != binding_id
        or checked_current.pat_device_id_proof_sha256
        != pat_device_id_proof_sha256
    ):
        raise ValueError("TLV read consumer transition binding changed")

    if operation == "bootstrap-v1":
        if v1_pin is None:
            raise ValueError("TLV read bootstrap pin is unavailable")
        pin_set = build_tlv_read_consumer_pin_set(binding_id, (v1_pin,))
        target = build_tlv_read_consumer_binding_state(
            binding_id=binding_id,
            pat_device_id_proof_sha256=pat_device_id_proof_sha256,
            adopted_pin=v1_pin,
            consumer_pin_set=pin_set,
        )
        if (
            checked_current is not None
            and checked_current.record_sha256 != target.record_sha256
        ):
            raise ValueError("TLV read consumer state is already bootstrapped differently")
    elif checked_current is None:
        raise ValueError("TLV read consumer state is not bootstrapped")
    elif operation == "stage-v2":
        if v2_pin is None:
            raise ValueError("TLV read target pin is unavailable")
        target = stage_tlv_read_consumer_target_pin(checked_current, v2_pin)
        # The provider may already have accepted a v2 publication and advanced
        # its process-only latch while durable Store still records the overlap
        # with v1 adopted. Staging is a pin-set operation: retain that observed
        # monotonic advance instead of trying to push the live provider back
        # to v1. The exact observed pin must already be the staged target.
        if observed_adopted_pin is not None:
            target = adopt_tlv_read_consumer_projection(
                target, observed_adopted_pin
            )
    elif operation == "restore-staged-v1":
        target = restore_tlv_read_consumer_staged_source(checked_current)
    elif operation == "adopt-v2":
        staged_v2 = tuple(
            pin
            for pin in checked_current.consumer_pin_set.accepted
            if pin.projection_version == 2
        )
        if len(staged_v2) != 1:
            raise ValueError("TLV read target pin is absent or ambiguous")
        target = adopt_tlv_read_consumer_projection(
            checked_current, staged_v2[0]
        )
    elif operation == "retire-v1":
        target = retire_tlv_read_consumer_source_pin(checked_current)
    elif operation == "adopt-successor-v2":
        if v2_pin is None or predecessor_v2_pin is None:
            raise ValueError("TLV read v2 successor authority is unavailable")
        target = adopt_tlv_read_consumer_successor_v2(
            checked_current, v2_pin
        )
        if target.predecessor_pin != predecessor_v2_pin:
            raise ValueError("TLV read v2 successor predecessor changed")
    elif operation == "restore-predecessor-v2":
        if predecessor_v2_pin is None:
            raise ValueError("TLV read v2 predecessor authority is unavailable")
        target = restore_tlv_read_consumer_predecessor_v2(
            checked_current, predecessor_v2_pin
        )
    else:
        if v2_pin is None:
            raise ValueError("TLV read v2 successor authority is unavailable")
        target = retire_tlv_read_consumer_predecessor_v2(
            checked_current, v2_pin
        )

    # A crash after persistence but before the phase receipt turns the retry
    # into a no-op. Only a genuine state change consumes the caller's JIT CAS
    # token, so idempotency never conflicts with interleaving protection.
    if checked_current is not None and target.record_sha256 == checked_current.record_sha256:
        return checked_current
    observed_record_sha256 = (
        None if checked_current is None else checked_current.record_sha256
    )
    if expected_current_record_sha256 != observed_record_sha256:
        raise ValueError("TLV read consumer transition CAS changed")
    return target


def _load_tlv_read_per_model_authorities(
    raw: bytes, digest: bytes
) -> Mapping[str, TlvReadPerModelAuthority]:
    """Validate the generated compact HA projection without global pinning."""
    artifact = _artifact_json(raw, digest, TLV_READ_PER_MODEL_AUTHORITY_FILENAME)
    if set(artifact) != _PER_MODEL_AUTHORITY_ARTIFACT_KEYS:
        _catalogue_error("Bundled per-model read authority keys are invalid")
    core = {
        key: value for key, value in artifact.items() if key != "root_sha256"
    }
    try:
        encoded = json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as err:
        raise TlvReadCatalogueError(
            "Bundled per-model read authority is not canonical"
        ) from err
    root = hashlib.sha256(_PER_MODEL_AUTHORITY_HASH_DOMAIN + encoded).hexdigest()
    raw_authorities = artifact["authorities"]
    raw_stats = artifact["stats"]
    if (
        artifact["schema_version"] != 1
        or artifact["artifact"] != "ha-per-model-read-authority"
        or artifact["projection"] != "per-model-read-authority"
        or artifact["feed_schema_version"] != PER_MODEL_TLV_READ_SCHEMA_VERSION
        or artifact["publication_plan_revision"]
        != TLV_READ_PUBLICATION_PLAN_REVISION
        or artifact["static_contract_projection_version"]
        != READ_STATIC_CONTRACT_PROJECTION_VERSION
        or not _valid_sha256(artifact["source_inventory_root_sha256"])
        or artifact["root_sha256"] != root
        or not isinstance(raw_authorities, list)
        or not raw_authorities
        or len(raw_authorities) > 256
        or not isinstance(raw_stats, dict)
        or set(raw_stats) != _PER_MODEL_AUTHORITY_STATS_KEYS
    ):
        _catalogue_error("Bundled per-model read authority is invalid")
    authorities: dict[str, TlvReadPerModelAuthority] = {}
    previous_profile_id: str | None = None
    for raw_authority in raw_authorities:
        if (
            not isinstance(raw_authority, dict)
            or set(raw_authority) != _PER_MODEL_AUTHORITY_KEYS
        ):
            _catalogue_error("Bundled per-model read authority row is invalid")
        try:
            authority = TlvReadPerModelAuthority(
                profile_id=raw_authority["profile_id"],
                model_id=raw_authority["model_id"],
                platform=raw_authority["platform"],
                semantics_revision=raw_authority["semantics_revision"],
                model_contract_sha256=raw_authority["model_contract_sha256"],
                feed_schema_version=artifact["feed_schema_version"],
                publication_plan_revision=artifact[
                    "publication_plan_revision"
                ],
                static_contract_projection_version=artifact[
                    "static_contract_projection_version"
                ],
            )
        except (TypeError, ValueError) as err:
            raise TlvReadCatalogueError(
                "Bundled per-model read authority row is invalid"
            ) from err
        if (
            authority.model_id in authorities
            or previous_profile_id is not None
            and authority.profile_id <= previous_profile_id
        ):
            _catalogue_error(
                "Bundled per-model read authority order or identity is invalid"
            )
        previous_profile_id = authority.profile_id
        authorities[authority.model_id] = authority
    expected_stats = {
        "authority_count": len(authorities),
        "thinq1_authority_count": sum(
            authority.platform == "thinq1" for authority in authorities.values()
        ),
        "thinq2_authority_count": sum(
            authority.platform == "thinq2" for authority in authorities.values()
        ),
    }
    if raw_stats != expected_stats:
        _catalogue_error("Bundled per-model read authority accounting is invalid")
    return MappingProxyType(authorities)


_PER_MODEL_AUTHORITY_CACHE: Mapping[str, TlvReadPerModelAuthority] | None = None
_PER_MODEL_AUTHORITY_LOCK = threading.Lock()


def load_tlv_read_per_model_authorities() -> Mapping[
    str, TlvReadPerModelAuthority
]:
    """Load compact per-model roots generated from the source authority."""
    global _PER_MODEL_AUTHORITY_CACHE
    cached = _PER_MODEL_AUTHORITY_CACHE
    if cached is not None:
        return cached
    with _PER_MODEL_AUTHORITY_LOCK:
        cached = _PER_MODEL_AUTHORITY_CACHE
        if cached is not None:
            return cached
        directory = Path(__file__).resolve().parent
        try:
            loaded = _load_tlv_read_per_model_authorities(
                (directory / TLV_READ_PER_MODEL_AUTHORITY_FILENAME).read_bytes(),
                (
                    directory / TLV_READ_PER_MODEL_AUTHORITY_DIGEST_FILENAME
                ).read_bytes(),
            )
        except OSError as err:
            raise TlvReadCatalogueError(
                "Bundled per-model read authority is unavailable"
            ) from err
        _PER_MODEL_AUTHORITY_CACHE = loaded
        return loaded


@dataclass(frozen=True)
class TlvReadValue:
    """One validated current or transient semantic value."""

    value: bool | int | float | str
    value_type: Literal["boolean", "number", "string"]
    observed_at: datetime
    confidence: str
    exposure: Literal["state", "diagnostic", "event"]
    unit: str | None = None


@dataclass(frozen=True)
class TlvReadEvent:
    """One exactly-once transient event delivered to an HA EventEntity."""

    semantic_id: str
    descriptor_key: str
    event_type: str
    value: int | float | str
    value_type: Literal["number", "string"]
    unit: str | None
    observed_at: datetime
    confidence: str
    sequence: int


@dataclass(frozen=True)
class _CurrentCandidate:
    canonical: str
    consumer_pin: TlvReadConsumerPin
    binding_generation: int
    cohort_generation: int
    publication_session_id: str
    source_session_id: str
    sequence: int
    published_at: datetime
    fields: Mapping[str, TlvReadValue]
    invalidated_semantics: frozenset[str]
    diagnostics: Mapping[str, int]

    @property
    def outer_coordinate(self) -> tuple[int, int]:
        return self.binding_generation, self.cohort_generation

    @property
    def epoch(self) -> tuple[int, int, str, str]:
        return (
            self.binding_generation,
            self.cohort_generation,
            self.publication_session_id,
            self.source_session_id,
        )


def _contract_error(message: str) -> None:
    raise TlvReadProviderContractError(message)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _contract_error("TLV read JSON contains duplicate keys")
        result[key] = value
    return result


def _decode_payload(payload: bytes) -> dict[str, Any]:
    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > MAX_TLV_READ_PAYLOAD_BYTES
    ):
        _contract_error("TLV read payload size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=lambda _value: _contract_error(
                "TLV read JSON constant is invalid"
            ),
        )
    except TlvReadProviderContractError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ) as err:
        raise TlvReadProviderContractError("TLV read payload is invalid JSON") from err
    if not isinstance(value, dict):
        _contract_error("TLV read payload must be an object")
    return value


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _publication_projection_version(value: Mapping[str, Any]) -> Literal[1, 2]:
    if "static_contract_projection_version" not in value:
        return 1
    if (
        value.get("static_contract_projection_version")
        != READ_STATIC_CONTRACT_PROJECTION_VERSION
    ):
        _contract_error("TLV read static contract projection is unsupported")
    return 2


def tlv_read_publication_static_contract_sha256(
    value: Mapping[str, Any], projection_version: Literal[1, 2]
) -> str:
    """Derive the producer's static contract from an exact envelope shape."""
    if projection_version == 1:
        # Preserve the released JavaScript insertion-order contract exactly.
        static_contract = {
            "schema_version": value.get("schema_version"),
            "publication_plan_revision": value.get("publication_plan_revision"),
            "profile_id": value.get("profile_id"),
            "profile_contract_revision": value.get("profile_contract_revision"),
            "profile_revision": value.get("profile_revision"),
            "profile_sha256": value.get("profile_sha256"),
            "read_entity_contract_revision": value.get(
                "read_entity_contract_revision"
            ),
            "read_entity_contract_sha256": value.get(
                "read_entity_contract_sha256"
            ),
            "catalog_sha256": value.get("catalog_sha256"),
            "semantics_revision": value.get("semantics_revision"),
            "binding_id": value.get("binding_id"),
            "model_id": value.get("model_id"),
            "platform": value.get("platform"),
            "binding_generation": value.get("binding_generation"),
            "pat_device_id_proof_sha256": value.get(
                "pat_device_id_proof_sha256"
            ),
            "authority": "server",
            "access": "read-only",
        }
        encoded = json.dumps(
            static_contract,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(_V1_STATIC_CONTRACT_HASH_DOMAIN + encoded).hexdigest()
    if projection_version != 2:
        raise ValueError("TLV read static contract projection is unsupported")
    static_contract = {
        "schema_version": value.get("schema_version"),
        "publication_plan_revision": value.get("publication_plan_revision"),
        "static_contract_projection_version": value.get(
            "static_contract_projection_version"
        ),
        "profile_id": value.get("profile_id"),
        "model_contract_sha256": value.get("model_contract_sha256"),
        "semantics_revision": value.get("semantics_revision"),
        "binding_id": value.get("binding_id"),
        "model_id": value.get("model_id"),
        "platform": value.get("platform"),
        "binding_generation": value.get("binding_generation"),
        "pat_device_id_proof_sha256": value.get("pat_device_id_proof_sha256"),
    }
    encoded = json.dumps(
        static_contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(_V2_STATIC_CONTRACT_HASH_DOMAIN + encoded).hexdigest()


def _js_string_length(value: str) -> int:
    """Return JavaScript String.length (UTF-16 code units), not code points."""
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def _timestamp(value: object, now: datetime, name: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 40:
        _contract_error(f"TLV read {name} timestamp is invalid")
    match = _ISO_TIMESTAMP.fullmatch(value)
    if match is None:
        _contract_error(f"TLV read {name} timestamp is invalid")
    (
        _year,
        _month,
        _day,
        _hour,
        _minute,
        _second,
        _fraction,
        zone,
        zone_hour,
        zone_minute,
    ) = match.groups()
    if zone != "Z" and (
        int(zone_hour) > 14
        or int(zone_minute) > 59
        or (int(zone_hour) == 14 and int(zone_minute) != 0)
    ):
        _contract_error(f"TLV read {name} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise TlvReadProviderContractError(
            f"TLV read {name} timestamp is invalid"
        ) from err
    if parsed.tzinfo is None:
        _contract_error(f"TLV read {name} timestamp lacks a timezone")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now + MAX_FUTURE_SKEW:
        _contract_error(f"TLV read {name} timestamp is in the future")
    return parsed


def _actual_value_type(value: object) -> str | None:
    if type(value) is bool:
        return "boolean"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            finite = math.isfinite(float(value))
        except OverflowError:
            finite = False
        if not finite:
            return None
        return "number"
    if isinstance(value, str) and _js_string_length(value) <= 128:
        return "string"
    return None


def _parse_field(
    raw: object,
    contract: TlvReadFieldContract,
    now: datetime,
    published_at: datetime,
) -> TlvReadValue:
    if (
        not isinstance(raw, dict)
        or not _FIELD_REQUIRED_KEYS.issubset(raw)
        or not set(raw).issubset(_FIELD_ALLOWED_KEYS)
    ):
        _contract_error("TLV read field keys are invalid")
    value_type = raw["value_type"]
    if (
        value_type not in contract.value_types
        or _actual_value_type(raw["value"]) != value_type
    ):
        _contract_error("TLV read field value type is not authorized")
    if (
        raw["exposure"] != contract.exposure
        or raw.get("unit") != contract.unit
        or ("unit" in raw) is not (contract.unit is not None)
    ):
        _contract_error("TLV read field metadata is not authorized")
    confidence = raw["confidence"]
    if (
        not isinstance(confidence, str)
        or not confidence
        or _js_string_length(confidence) > 128
        or any(ord(char) < 32 for char in confidence)
    ):
        _contract_error("TLV read field confidence is invalid")
    observed_at = _timestamp(raw["observed_at"], now, "field observation")
    if observed_at > published_at:
        _contract_error("TLV read field observation is after publication")
    return TlvReadValue(
        value=raw["value"],
        value_type=value_type,
        observed_at=observed_at,
        confidence=confidence,
        exposure=raw["exposure"],
        unit=raw.get("unit"),
    )


def _primary_authority(primary: object) -> tuple[int, str] | None:
    """Return the live presence generation and publication service identity."""
    authority = getattr(primary, "read_publication_authority", None)
    if (
        not isinstance(authority, tuple)
        or len(authority) != 2
        or not _valid_positive_integer(authority[0])
        or not isinstance(authority[1], str)
        or _PUBLICATION_SESSION_ID.fullmatch(authority[1]) is None
    ):
        return None
    return authority


def _primary_state_coordinate(
    primary: object, authority: tuple[int, str] | None
) -> tuple[int, int, str] | None:
    """Return a current semantic-state lower bound for the live authority."""
    generation = getattr(primary, "binding_generation", None)
    cohort = getattr(primary, "cohort_generation", None)
    session = getattr(primary, "session_id", None)
    if (
        not _valid_positive_integer(generation)
        or not _valid_positive_integer(cohort)
        or not isinstance(session, str)
        or _PUBLICATION_SESSION_ID.fullmatch(session) is None
    ):
        return None
    if authority is not None and (generation, session) != authority:
        return None
    return generation, cohort, session


def _primary_live(primary: object) -> bool:
    """Require the current authenticated presence and runtime transport fence."""
    return _primary_authority(primary) is not None


class TlvReadShadowProvider:
    """Validate one model's read-only feed under the selected binding policy."""

    def __init__(
        self,
        binding_id: str,
        pat_device_id: str,
        profile: TlvReadProfile,
        primary_provider: object,
        *,
        model_authority: TlvReadPerModelAuthority | None = None,
        consumer_state: TlvReadConsumerBindingState | Mapping[str, Any] | None = None,
        allow_legacy_v1_fallback: bool = False,
        read_contract_policy: Literal["pinned", "field-compatible"] = "pinned",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(binding_id, str) or _BINDING_ID.fullmatch(binding_id) is None:
            raise ValueError("TLV read binding id is invalid")
        if not _valid_opaque_id(pat_device_id):
            raise ValueError("TLV read PAT device id is invalid")
        if not isinstance(profile, TlvReadProfile):
            raise TypeError("TLV read profile is invalid")
        expected_proof = getattr(primary_provider, "expected_proof", None)
        if not _valid_sha256(expected_proof):
            raise ValueError("TLV read provider requires a V3 identity proof")
        if (
            getattr(primary_provider, "binding_id", None) != binding_id
            or getattr(primary_provider, "model_id", None) != profile.model_id
            or getattr(primary_provider, "platform", None) != profile.platform
        ):
            raise ValueError("TLV read provider primary identity does not match")
        add_listener = getattr(primary_provider, "async_add_listener", None)
        if not callable(add_listener):
            raise TypeError("TLV read primary provider is not observable")
        if not hasattr(primary_provider, "read_publication_authority"):
            raise TypeError("TLV read primary provider has no publication authority")
        if model_authority is not None and (
            not isinstance(model_authority, TlvReadPerModelAuthority)
            or model_authority.profile_id != profile.profile_id
            or model_authority.model_id != profile.model_id
            or model_authority.platform != profile.platform
            # The v1/full profile follows the released global semantics
            # revision.  A v2 authority follows only the exact model's
            # decoder closure, so it may intentionally lag that global
            # revision but may never claim an unreleased future revision.
            or model_authority.semantics_revision > profile.semantics_revision
        ):
            raise ValueError("TLV read per-model authority does not match the profile")
        if type(allow_legacy_v1_fallback) is not bool:
            raise TypeError("TLV read legacy fallback policy is invalid")
        if read_contract_policy not in ("pinned", "field-compatible"):
            raise ValueError("TLV read contract policy is invalid")
        parsed_consumer_state = (
            None
            if consumer_state is None
            else validate_tlv_read_consumer_binding_state(consumer_state)
        )
        if parsed_consumer_state is not None:
            if (
                parsed_consumer_state.binding_id != binding_id
                or parsed_consumer_state.pat_device_id_proof_sha256
                != expected_proof
            ):
                raise ValueError(
                    "TLV read consumer state does not match the exact binding"
                )
            if (
                read_contract_policy == "pinned"
                and not _consumer_state_v2_pins_match_authority_or_reviewed_predecessor(
                    parsed_consumer_state, model_authority
                )
            ):
                raise ValueError(
                    "TLV read v2 consumer pin does not match per-model authority"
                )

        self.binding_id = binding_id
        self.profile = profile
        self._read_contract_policy = read_contract_policy
        self._model_authority = model_authority
        self._consumer_state = parsed_consumer_state
        self._consumer_pin_set = (
            None
            if parsed_consumer_state is None
            else parsed_consumer_state.consumer_pin_set
        )
        self._adopted_projection_version = (
            0
            if parsed_consumer_state is None
            else parsed_consumer_state.adopted_projection_version
        )
        self._allow_legacy_v1_fallback = allow_legacy_v1_fallback
        self._primary = primary_provider
        self._expected_proof = expected_proof
        self._consumer_binding_authority = TlvReadConsumerBindingAuthority(
            binding_id=binding_id,
            pat_device_id_proof_sha256=expected_proof,
            profile=profile,
            model_authority=model_authority,
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.current_topic = f"{TLV_READ_TOPIC_PREFIX}/current/{binding_id}"
        self.event_topic = f"{TLV_READ_TOPIC_PREFIX}/event/{binding_id}"
        self.topics = (self.current_topic, self.event_topic)

        self._transport_ready = False
        self._current_transport_current = False
        self._fields: Mapping[str, TlvReadValue] = MappingProxyType({})
        self._current_invalidated_semantics: frozenset[str] = frozenset()
        self._current_diagnostics: Mapping[str, int] = MappingProxyType({})
        self._binding_generation: int | None = None
        self._cohort_generation: int | None = None
        self._publication_session_id: str | None = None
        self._source_session_id: str | None = None
        self._current_sequence = 0
        self._current_canonical: str | None = None
        self._current_published_at: datetime | None = None
        self._current_consumer_pin: TlvReadConsumerPin | None = None
        self._outer_high_water: tuple[int, int] | None = None
        self._pending_current: _CurrentCandidate | None = None
        # An empty retained-current publication retires only the cursor that it
        # replaced. It does not retire the V3 publication identity or the raw
        # source epoch: a physical reconnect can legitimately continue the same
        # source session at a higher sequence, or start a new source session,
        # while the service identity/cohort remain unchanged.
        self._tombstoned_current_cursors: dict[
            tuple[int, int, str, str], tuple[int, str]
        ] = {}
        # Current and event publications share the producer's source cursor.
        # Keep its high-water independent from retained HA field storage while
        # still requiring each event to match an accepted current exactly.
        self._publication_sources: dict[tuple[int, int, str], str] = {}
        self._source_sequence_high_water: dict[tuple[int, int, str, str], int] = {}
        self._event_high_water: dict[
            tuple[int, int, str, str, str], tuple[int, str]
        ] = {}
        self._listeners: list[Callable[[], None]] = []
        self._event_listeners: list[Callable[[TlvReadEvent], None]] = []
        self._rejected_messages = 0
        self._skipped_read_fields = 0
        self._closed = False
        self._remove_primary_listener = add_listener(self._primary_updated)

    @property
    def fields(self) -> Mapping[str, TlvReadValue]:
        return self._fields

    def update_profile(self, profile: TlvReadProfile, *, notify: bool = True) -> None:
        """Refresh visible descriptors using the already accepted current.

        Subscription, presence, cursor, pending messages and energy consumers
        stay intact. Cached observations are reused only with matching types
        and units; an added descriptor can read a previously hidden raw field.
        No old event is emitted again.
        """
        if (profile.profile_id, profile.model_id, profile.platform) != (
            self.profile.profile_id, self.profile.model_id, self.profile.platform
        ):
            raise ValueError("A feature edit cannot change the connected appliance")
        old = self.profile.fields_by_semantic_id
        fields: dict[str, TlvReadValue] = {}
        raw_fields = (
            {} if self._current_canonical is None
            else json.loads(self._current_canonical)["fields"]
        )
        for contract in profile.fields:
            previous = old.get(contract.semantic_id)
            field = self._fields.get(contract.semantic_id)
            if field is not None and previous is not None and (
                previous.value_types, previous.unit, previous.exposure,
                previous.publication_mode
            ) == (
                contract.value_types, contract.unit, contract.exposure,
                contract.publication_mode
            ):
                fields[contract.semantic_id] = field
            elif (contract.publication_mode == "retained-current"
                  and contract.semantic_id in raw_fields
                  and self._current_published_at is not None):
                try:
                    fields[contract.semantic_id] = _parse_field(
                        raw_fields[contract.semantic_id], contract,
                        self._now().astimezone(timezone.utc), self._current_published_at,
                    )
                except TlvReadProviderContractError:
                    pass
        self.profile = profile
        self._fields = MappingProxyType(fields)
        if notify:
            self._notify_listeners()

    @property
    def current_diagnostics(self) -> Mapping[str, int]:
        """Return counters from the latest accepted retained-current envelope."""
        return self._current_diagnostics

    @property
    def current_sequence(self) -> int:
        return self._current_sequence

    @property
    def current_published_at(self) -> datetime | None:
        """Return the accepted retained-current publication time.

        This is deliberately the producer publication clock, not a field's
        ``observed_at``.  A carried field keeps its original observation time
        across sealed generations. Consumers may use this boundary to reject a
        stale carried field, but must use the field's own ``observed_at`` as an
        integration clock.
        """
        return self._current_published_at

    @property
    def transport_ready(self) -> bool:
        return self._transport_ready

    @property
    def rejected_messages(self) -> int:
        return self._rejected_messages

    @property
    def skipped_read_fields(self) -> int:
        return self._skipped_read_fields

    @property
    def adopted_projection_version(self) -> int:
        """Return the process-local monotonic projection latch."""
        return self._adopted_projection_version

    @property
    def consumer_state(self) -> TlvReadConsumerBindingState | None:
        return self._consumer_state

    @property
    def consumer_binding_authority(self) -> TlvReadConsumerBindingAuthority:
        return self._consumer_binding_authority

    def current_contract_observation(self) -> Mapping[str, object] | None:
        """Return exact accepted-current identity, never an availability gate.

        Absence means only that the installer must keep the overlap and wait;
        it never removes an offline binding from installation. Requiring the
        current primary coordinate prevents an old retained publication from
        proving that the newly started producer converged.
        """
        if self._read_contract_policy == "field-compatible":
            # A field-compatible message is not evidence that a durable pin
            # migration converged. Keep the old installer fail-closed.
            return None
        pin = self._current_consumer_pin
        if (
            not self._current_transport_current
            or pin is None
            or not self._current_matches_primary()
        ):
            return None
        return MappingProxyType(
            {
                "schema_version": 1,
                "binding_id": self.binding_id,
                "model_id": self.profile.model_id,
                "platform": self.profile.platform,
                "projection_version": pin.projection_version,
                "static_read_contract_sha256": (
                    pin.static_read_contract_sha256
                ),
                "model_contract_sha256": pin.model_contract_sha256,
            }
        )

    def consumer_pin_for_projection(
        self,
        projection_version: Literal[1, 2],
        binding_generation: int,
    ) -> TlvReadConsumerPin:
        """Derive one exact pin without requiring a live appliance publication."""
        if not _valid_positive_integer(binding_generation):
            raise ValueError("TLV read consumer binding generation is invalid")
        known_generation = getattr(self._primary, "binding_generation", None)
        if (
            known_generation is not None
            and known_generation != binding_generation
        ):
            raise ValueError("TLV read consumer binding generation changed")
        return self._consumer_binding_authority.pin_for_projection(
            projection_version, binding_generation
        )

    def replace_consumer_state(
        self, state: TlvReadConsumerBindingState | Mapping[str, Any]
    ) -> None:
        """Install one already-durable exact state without lowering the latch."""
        parsed = self.validate_consumer_state_replacement(state)
        self._consumer_state = parsed
        self._consumer_pin_set = parsed.consumer_pin_set
        self._adopted_projection_version = parsed.adopted_projection_version

    def validate_consumer_state_replacement(
        self, state: TlvReadConsumerBindingState | Mapping[str, Any]
    ) -> TlvReadConsumerBindingState:
        """Validate an adapter write without changing the live provider."""
        parsed = validate_tlv_read_consumer_binding_state(state)
        if (
            parsed.binding_id != self.binding_id
            or parsed.pat_device_id_proof_sha256 != self._expected_proof
        ):
            raise ValueError("TLV read consumer state binding identity changed")
        if self._consumer_state is not None:
            parsed = validate_tlv_read_consumer_state_replacement(
                self._consumer_state, parsed
            )
        elif parsed.adopted_projection_version < self._adopted_projection_version:
            raise ValueError("TLV read consumer projection latch regressed")
        if not _consumer_state_v2_pins_match_authority_or_reviewed_predecessor(
            parsed, self._model_authority
        ):
            raise ValueError("TLV read consumer state model authority changed")
        return parsed

    def _adopt_consumer_pin(self, pin: TlvReadConsumerPin) -> None:
        if self._read_contract_policy == "field-compatible":
            return
        if pin.projection_version < self._adopted_projection_version:
            _contract_error("TLV read publication projection regressed")
        if pin.projection_version == self._adopted_projection_version:
            state = self._consumer_state
            if state is not None and (
                pin.static_read_contract_sha256
                != state.adopted_static_read_contract_sha256
                or pin.model_contract_sha256
                != state.adopted_model_contract_sha256
            ):
                authority = self._model_authority
                if (
                    pin.projection_version != 2
                    or authority is None
                    or pin.model_contract_sha256
                    != authority.model_contract_sha256
                    or _durable_tlv_read_v2_predecessor_pin(state, authority)
                    is None
                ):
                    _contract_error("TLV read adopted static contract changed")
                # Latch the successor in this process.  The durable row remains
                # the restart-safe predecessor anchor until the adapter elects
                # to persist the already validated transition.
                adopted = adopt_tlv_read_consumer_successor_v2(state, pin)
                self._consumer_state = adopted
                self._consumer_pin_set = adopted.consumer_pin_set
            return
        state = self._consumer_state
        if state is None:
            if self._allow_legacy_v1_fallback and pin.projection_version == 1:
                self._adopted_projection_version = 1
                return
            _contract_error("TLV read projection adoption has no durable state")
        adopted = adopt_tlv_read_consumer_projection(state, pin)
        self._consumer_state = adopted
        self._consumer_pin_set = adopted.consumer_pin_set
        self._adopted_projection_version = adopted.adopted_projection_version

    def field_value(self, semantic_id: str) -> bool | int | float | str | None:
        field = self._fields.get(semantic_id)
        return None if field is None else field.value

    def _pilot_display_field(self, semantic_id: str) -> TlvReadValue | None:
        """Reuse only a matching, live Local pilot value for a read-only entity.

        This is a presentation fallback, never a full-read publication or an
        input to cumulative-energy integration and control routing.
        """
        if (
            not self._transport_ready
            or not _primary_live(self._primary)
            or semantic_id in self._current_invalidated_semantics
        ):
            return None
        read_contract = self.profile.fields_by_semantic_id.get(semantic_id)
        if (
            read_contract is None
            or read_contract.owner != "none"
            or read_contract.domain not in ("sensor", "binary_sensor")
            or read_contract.publication_mode != "retained-current"
        ):
            return None
        primary_profile = getattr(self._primary, "profile", None)
        pilot_contracts = getattr(primary_profile, "fields", None)
        pilot_fields = getattr(self._primary, "shadow_fields", None)
        pilot_available = getattr(self._primary, "semantic_field_available", None)
        if (
            not isinstance(pilot_contracts, Mapping)
            or not isinstance(pilot_fields, Mapping)
            or not callable(pilot_available)
        ):
            return None
        pilot_contract = pilot_contracts.get(semantic_id)
        pilot_field = pilot_fields.get(semantic_id)
        if (
            pilot_contract is None
            or pilot_field is None
            or pilot_contract.value_type not in read_contract.value_types
            or pilot_contract.unit != read_contract.unit
            or pilot_contract.exposure != read_contract.exposure
            or pilot_field.value_type != pilot_contract.value_type
            or pilot_field.unit != pilot_contract.unit
            or pilot_field.exposure != pilot_contract.exposure
            or not pilot_available(semantic_id)
        ):
            return None
        return TlvReadValue(
            value=pilot_field.value,
            value_type=pilot_field.value_type,
            observed_at=pilot_field.observed_at,
            confidence=pilot_field.confidence,
            exposure=pilot_field.exposure,
            unit=pilot_field.unit,
        )

    def display_field(self, semantic_id: str) -> TlvReadValue | None:
        """Select a live full-read value, else a byte-compatible Local pilot."""
        if self.field_available(semantic_id):
            return self._fields[semantic_id]
        return self._pilot_display_field(semantic_id)

    def display_field_available(self, semantic_id: str) -> bool:
        return self.display_field(semantic_id) is not None

    def display_field_source(self, semantic_id: str) -> str | None:
        if self.field_available(semantic_id):
            return "full-read"
        return "pilot-read" if self._pilot_display_field(semantic_id) is not None else None

    def _current_matches_primary(self) -> bool:
        authority = _primary_authority(self._primary)
        return authority == (
            self._binding_generation,
            self._publication_session_id,
        )

    def _read_value_matches_live_device(self) -> bool:
        """A read-only last value needs the same binding, not a state cohort."""
        authority = _primary_authority(self._primary)
        return authority is not None and authority[0] == self._binding_generation

    def field_available(self, semantic_id: str) -> bool:
        return (
            semantic_id in self._fields
            and self._transport_ready
            and self._current_transport_current
            and _primary_live(self._primary)
            and (
                self._read_value_matches_live_device()
                if self._read_contract_policy == "field-compatible"
                else self._current_matches_primary()
            )
        )

    @property
    def event_available(self) -> bool:
        """Return whether transient events are fenced to the live current cursor."""
        return (
            self._transport_ready
            and self._current_transport_current
            and _primary_live(self._primary)
            and self._current_matches_primary()
        )

    @property
    def diagnostics_available(self) -> bool:
        """Return whether current diagnostic counters share a live authority."""
        return bool(self._current_diagnostics) and self.event_available

    def async_add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("TLV read listener is invalid")
        self._listeners.append(callback)

        def remove() -> None:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

        return remove

    def async_add_event_listener(
        self, callback: Callable[[TlvReadEvent], None]
    ) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("TLV read event listener is invalid")
        self._event_listeners.append(callback)

        def remove() -> None:
            try:
                self._event_listeners.remove(callback)
            except ValueError:
                pass

        return remove

    def _notify_listeners(self) -> None:
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:  # One HA entity cannot stop the feed.
                _LOGGER.exception("TLV read provider listener failed")

    def _notify_event(self, event: TlvReadEvent) -> None:
        for callback in tuple(self._event_listeners):
            try:
                callback(event)
            except Exception:  # One HA entity cannot stop the feed.
                _LOGGER.exception("TLV read provider event listener failed")

    def set_transport_ready(self, ready: bool) -> None:
        if type(ready) is not bool:
            raise TypeError("TLV read transport readiness must be boolean")
        changed = ready != self._transport_ready or (
            not ready and self._current_transport_current
        )
        self._transport_ready = ready
        if not ready:
            self._current_transport_current = False
        if changed:
            self._notify_listeners()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.set_transport_ready(False)
        self._remove_primary_listener()
        self._listeners.clear()
        self._event_listeners.clear()

    def _validate_read_coordinate(
        self, value: Mapping[str, Any], now: datetime
    ) -> None:
        for key in ("binding_generation", "cohort_generation", "sequence"):
            if not _valid_positive_integer(value.get(key)):
                _contract_error(f"TLV read {key} is invalid")
        if value["cohort_generation"] > MAX_COHORT_GENERATION:
            _contract_error("TLV read cohort_generation is invalid")
        if (
            not isinstance(value.get("publication_session_id"), str)
            or _PUBLICATION_SESSION_ID.fullmatch(value["publication_session_id"])
            is None
        ):
            _contract_error("TLV read publication_session_id is invalid")
        if (
            not isinstance(value.get("source_session_id"), str)
            or _SOURCE_SESSION_ID.fullmatch(value["source_session_id"]) is None
        ):
            _contract_error("TLV read source_session_id is invalid")
        _timestamp(value.get("published_at"), now, "publication")

    def _field_compatible_read_pin(
        self, value: Mapping[str, Any], projection_version: Literal[1, 2]
    ) -> TlvReadConsumerPin:
        """Keep receipt fields diagnostic while checking exact device identity."""
        expected = {
            "profile_id": self.profile.profile_id,
            "binding_id": self.binding_id,
            "model_id": self.profile.model_id,
            "platform": self.profile.platform,
            "pat_device_id_proof_sha256": self._expected_proof,
        }
        if projection_version == 1:
            expected["schema_version"] = TLV_READ_SCHEMA_VERSION
            if not _valid_positive_integer(value.get("profile_contract_revision")):
                _contract_error("TLV read profile revision is invalid")
            for key in (
                "profile_revision",
                "read_entity_contract_revision",
            ):
                if not isinstance(value.get(key), str) or not value[key]:
                    _contract_error(f"TLV read {key} is invalid")
            for key in (
                "profile_sha256",
                "read_entity_contract_sha256",
                "catalog_sha256",
            ):
                if not _valid_sha256(value.get(key)):
                    _contract_error(f"TLV read {key} is invalid")
            model_contract_sha256 = None
        else:
            expected["schema_version"] = PER_MODEL_TLV_READ_SCHEMA_VERSION
            expected["static_contract_projection_version"] = (
                READ_STATIC_CONTRACT_PROJECTION_VERSION
            )
            model_contract_sha256 = value.get("model_contract_sha256")
            if not _valid_sha256(model_contract_sha256):
                _contract_error("TLV read model contract receipt is invalid")
        if not _valid_positive_integer(value.get("publication_plan_revision")):
            _contract_error("TLV read publication plan revision is invalid")
        if not _valid_positive_integer(value.get("semantics_revision")):
            _contract_error("TLV read semantics revision is invalid")
        if any(
            value.get(key) != expected_value
            for key, expected_value in expected.items()
        ):
            _contract_error("TLV read publication identity does not match")
        return TlvReadConsumerPin(
            projection_version=projection_version,
            static_read_contract_sha256=tlv_read_publication_static_contract_sha256(
                value, projection_version
            ),
            model_contract_sha256=model_contract_sha256,
        )

    def _validate_pins(
        self,
        value: Mapping[str, Any],
        now: datetime,
        projection_version: Literal[1, 2],
    ) -> TlvReadConsumerPin:
        if self._read_contract_policy == "field-compatible":
            matched_pin = self._field_compatible_read_pin(value, projection_version)
            self._validate_read_coordinate(value, now)
            return matched_pin
        if projection_version < self._adopted_projection_version:
            _contract_error("TLV read publication projection regressed")
        matched_pin: TlvReadConsumerPin
        if (
            projection_version == 1
            and self._consumer_pin_set is None
            and self._allow_legacy_v1_fallback
        ):
            # Preserve the released v0.11.4 compatibility window until the JIT
            # installer supplies an explicit per-binding state. Production
            # setup never enables this fallback; it exists only for exact
            # released-v1 fixture verification.
            expected = {
                "schema_version": TLV_READ_SCHEMA_VERSION,
                "publication_plan_revision": TLV_READ_PUBLICATION_PLAN_REVISION,
                "profile_id": self.profile.profile_id,
                "profile_contract_revision": self.profile.contract_revision,
                "profile_revision": self.profile.profile_revision,
                "profile_sha256": self.profile.profile_sha256,
                "read_entity_contract_revision": self.profile.read_entity_contract_revision,
                "read_entity_contract_sha256": self.profile.read_entity_contract_sha256,
                "catalog_sha256": self.profile.catalog_sha256,
                "semantics_revision": self.profile.semantics_revision,
                "binding_id": self.binding_id,
                "model_id": self.profile.model_id,
                "platform": self.profile.platform,
                "pat_device_id_proof_sha256": self._expected_proof,
            }
            supported = [expected]
            current_bundled_profile = (
                self.profile.profile_revision
                == f"full-read-sensor-profiles-v1:{EXPECTED_TLV_READ_PROFILE_ROOT_SHA256[:16]}"
                and self.profile.profile_sha256
                == EXPECTED_TLV_READ_PROFILE_ROOT_SHA256
                and self.profile.read_entity_contract_revision
                == f"full-read-entities-v1:{EXPECTED_TLV_READ_ENTITY_ROOT_SHA256[:16]}"
                and self.profile.read_entity_contract_sha256
                == EXPECTED_TLV_READ_ENTITY_ROOT_SHA256
                and self.profile.catalog_sha256 == EXPECTED_TLV_CATALOG_SHA256
                and self.profile.semantics_revision
                == EXPECTED_TLV_READ_SEMANTICS_REVISION
            )
            if current_bundled_profile:
                supported.extend(
                    {**expected, **retained_generation}
                    for retained_generation in _RETAINED_TLV_READ_PUBLICATION_PIN_GENERATIONS
                )
            if not any(
                all(
                    value.get(key) == expected_value
                    for key, expected_value in pins.items()
                )
                for pins in supported
            ):
                _contract_error("TLV read publication pins do not match the binding")
            matched_pin = TlvReadConsumerPin(
                projection_version=1,
                static_read_contract_sha256=(
                    tlv_read_publication_static_contract_sha256(value, 1)
                ),
                model_contract_sha256=None,
            )
        else:
            pin_set = self._consumer_pin_set
            if pin_set is None:
                _contract_error(
                    "TLV read projection is not staged for this binding"
                )
            if projection_version == 1:
                expected_identity = {
                    "schema_version": TLV_READ_SCHEMA_VERSION,
                    "publication_plan_revision": TLV_READ_PUBLICATION_PLAN_REVISION,
                    "profile_id": self.profile.profile_id,
                    "binding_id": self.binding_id,
                    "model_id": self.profile.model_id,
                    "platform": self.profile.platform,
                    "pat_device_id_proof_sha256": self._expected_proof,
                }
                expected_model_contract = None
            else:
                authority = _durable_tlv_read_v2_message_authority(
                    self._consumer_state,
                    self._model_authority,
                    value.get("model_contract_sha256"),
                    value.get("semantics_revision"),
                )
                if authority is None:
                    _contract_error(
                        "TLV read per-model authority is unavailable"
                    )
                expected_identity = {
                    "schema_version": authority.feed_schema_version,
                    "publication_plan_revision": authority.publication_plan_revision,
                    "static_contract_projection_version": authority.static_contract_projection_version,
                    "profile_id": authority.profile_id,
                    "model_contract_sha256": authority.model_contract_sha256,
                    "semantics_revision": authority.semantics_revision,
                    "binding_id": self.binding_id,
                    "model_id": authority.model_id,
                    "platform": authority.platform,
                    "pat_device_id_proof_sha256": self._expected_proof,
                }
                expected_model_contract = authority.model_contract_sha256
            if not all(
                value.get(key) == expected_value
                for key, expected_value in expected_identity.items()
            ):
                _contract_error("TLV read publication identity does not match")
            static_sha256 = tlv_read_publication_static_contract_sha256(
                value, projection_version
            )
            matching_pins = tuple(
                pin
                for pin in pin_set.accepted
                if pin.projection_version == projection_version
                and pin.static_read_contract_sha256 == static_sha256
                and pin.model_contract_sha256 == expected_model_contract
            )
            if (
                not matching_pins
                and projection_version == 2
                and authority is self._model_authority
                and self._consumer_state is not None
                and _durable_tlv_read_v2_predecessor_pin(
                    self._consumer_state, self._model_authority
                )
                is not None
            ):
                # The current exact model authority may replace the sole
                # durable predecessor without a fleet-wide admin transition.
                matching_pins = (
                    TlvReadConsumerPin(
                        projection_version=2,
                        static_read_contract_sha256=static_sha256,
                        model_contract_sha256=expected_model_contract,
                    ),
                )
            if len(matching_pins) != 1:
                _contract_error(
                    "TLV read static contract is not staged for this binding"
                )
            matched_pin = matching_pins[0]
        self._validate_read_coordinate(value, now)
        return matched_pin

    def _parse_current(self, payload: bytes) -> _CurrentCandidate:
        value = _decode_payload(payload)
        projection_version = _publication_projection_version(value)
        keys = set(value)
        required_keys = _CURRENT_REQUIRED_KEYS_BY_PROJECTION[projection_version]
        if not required_keys.issubset(keys) or not keys.issubset(
            required_keys | _CURRENT_OPTIONAL_KEYS
        ):
            _contract_error("TLV read current keys are invalid")
        now = self._now().astimezone(timezone.utc)
        consumer_pin = self._validate_pins(value, now, projection_version)
        published_at = _timestamp(value["published_at"], now, "publication")
        raw_fields = value["fields"]
        if not isinstance(raw_fields, dict) or len(raw_fields) > MAX_TLV_READ_FIELDS:
            _contract_error("TLV read current fields are invalid")
        contracts = self.profile.fields_by_semantic_id
        fields: dict[str, TlvReadValue] = {}
        skipped_fields = 0
        for semantic_id, raw_field in raw_fields.items():
            contract = contracts.get(semantic_id)
            if contract is None and self._read_contract_policy == "field-compatible":
                skipped_fields += 1
                continue
            if contract is None or contract.publication_mode != "retained-current":
                _contract_error("TLV read current contains an unauthorized semantic")
            if self._read_contract_policy == "field-compatible":
                if (
                    not isinstance(raw_field, dict)
                    or not _FIELD_REQUIRED_KEYS.issubset(raw_field)
                    or not set(raw_field).issubset(_FIELD_ALLOWED_KEYS)
                ):
                    _contract_error("TLV read field keys are invalid")
                if (
                    raw_field["value_type"] not in contract.value_types
                    or _actual_value_type(raw_field["value"])
                    != raw_field["value_type"]
                    or raw_field["exposure"] != contract.exposure
                    or raw_field.get("unit") != contract.unit
                    or ("unit" in raw_field) is not (contract.unit is not None)
                ):
                    skipped_fields += 1
                    continue
            fields[semantic_id] = _parse_field(
                raw_field, contract, now, published_at
            )

        invalidated = value.get("invalidated_fields", {})
        if (
            not isinstance(invalidated, dict)
            or ("invalidated_fields" in value and not invalidated)
            or len(invalidated) > MAX_TLV_READ_FIELDS
            or len(raw_fields) + len(invalidated) > MAX_TLV_READ_FIELDS
            or not raw_fields
            and not invalidated
        ):
            _contract_error("TLV read invalidations are invalid")
        if set(invalidated).intersection(raw_fields):
            _contract_error("TLV read fields and invalidations overlap")
        accepted_invalidations: set[str] = set()
        for semantic_id, raw_invalidation in invalidated.items():
            contract = contracts.get(semantic_id)
            if contract is None and self._read_contract_policy == "field-compatible":
                skipped_fields += 1
                continue
            if (
                contract is None
                or contract.publication_mode != "retained-current"
                or not isinstance(raw_invalidation, dict)
                or set(raw_invalidation) != _INVALIDATION_KEYS
            ):
                _contract_error("TLV read invalidation is not authorized")
            invalidated_at = _timestamp(
                raw_invalidation["observed_at"], now, "invalidation"
            )
            if invalidated_at > published_at:
                _contract_error("TLV read invalidation is after publication")
            confidence = raw_invalidation["confidence"]
            if (
                not isinstance(confidence, str)
                or not confidence
                or _js_string_length(confidence) > 128
            ):
                _contract_error("TLV read invalidation confidence is invalid")
            accepted_invalidations.add(semantic_id)

        if (
            self._read_contract_policy == "field-compatible"
            and not fields
            and not accepted_invalidations
        ):
            _contract_error("TLV read current has no compatible fields")

        diagnostics = value["diagnostics"]
        if not isinstance(diagnostics, dict) or set(diagnostics) != _DIAGNOSTIC_KEYS:
            _contract_error("TLV read diagnostics are invalid")
        if any(
            type(item) is not int or item < 0 or item > MAX_JSON_SAFE_INTEGER
            for item in diagnostics.values()
        ):
            _contract_error("TLV read diagnostic counter is invalid")

        if skipped_fields:
            self._skipped_read_fields += skipped_fields
            _LOGGER.debug(
                "TLV read skipped %d unrecognized or incompatible fields for binding %s",
                skipped_fields,
                self.binding_id,
            )

        return _CurrentCandidate(
            canonical=_canonical(value),
            consumer_pin=consumer_pin,
            binding_generation=value["binding_generation"],
            cohort_generation=value["cohort_generation"],
            publication_session_id=value["publication_session_id"],
            source_session_id=value["source_session_id"],
            sequence=value["sequence"],
            published_at=published_at,
            fields=MappingProxyType(fields),
            invalidated_semantics=frozenset(accepted_invalidations),
            diagnostics=MappingProxyType(dict(diagnostics)),
        )

    def _candidate_primary_relation(self, candidate: _CurrentCandidate) -> int:
        """Return -1 for a prior-session stale cursor, else 0.

        Per-model publishers deliberately preserve their one retained-current
        slot across a process restart so an offline appliance keeps its last
        exact value.  The publication session is therefore a liveness signal,
        not part of the durable consumer pin.  Commit an exactly pinned prior-
        session current, while ``field_available`` continues to require the
        current live presence session independently. A state-only cohort may
        advance without a new read observation in that same physical session.
        """
        authority = _primary_authority(self._primary)
        if authority is not None and candidate.binding_generation != authority[0]:
            _contract_error("TLV read publication authority is foreign")
        if self._read_contract_policy == "field-compatible":
            return 0
        primary_state = _primary_state_coordinate(self._primary, authority)
        if primary_state is not None:
            if (
                candidate.binding_generation == primary_state[0]
                and candidate.cohort_generation < primary_state[1]
                and authority is not None
                and candidate.publication_session_id != authority[1]
            ):
                return -1
        return 0

    @staticmethod
    def _compare_candidates(left: _CurrentCandidate, right: _CurrentCandidate) -> int:
        if left.outer_coordinate != right.outer_coordinate:
            return -1 if left.outer_coordinate < right.outer_coordinate else 1
        if left.publication_session_id != right.publication_session_id:
            _contract_error("TLV read pending publication session collided")
        if left.source_session_id != right.source_session_id:
            _contract_error("TLV read pending source session collided")
        if left.sequence != right.sequence:
            return -1 if left.sequence < right.sequence else 1
        if left.canonical != right.canonical:
            _contract_error("TLV read pending cursor collided")
        return 0

    def _stage_current(self, candidate: _CurrentCandidate) -> bool:
        tombstoned = self._tombstoned_current_cursors.get(candidate.epoch)
        if tombstoned is not None:
            tombstoned_sequence, tombstoned_canonical = tombstoned
            if candidate.sequence < tombstoned_sequence:
                _contract_error("TLV read tombstoned current sequence regressed")
            if candidate.sequence == tombstoned_sequence:
                if candidate.canonical != tombstoned_canonical:
                    _contract_error("TLV read tombstoned current cursor collided")
                _contract_error("TLV read tombstoned current was replayed")
        source_high_water = self._source_sequence_high_water.get(candidate.epoch)
        if source_high_water is not None and candidate.sequence < source_high_water:
            _contract_error("TLV read source sequence regressed")
        relation = self._candidate_primary_relation(candidate)
        if relation < 0:
            _contract_error("TLV read current is older than the primary identity")
        if relation > 0:
            if self._pending_current is not None:
                comparison = self._compare_candidates(candidate, self._pending_current)
                if comparison < 0:
                    _contract_error("TLV read pending cursor regressed")
                if comparison == 0:
                    return False
            self._pending_current = candidate
            return False
        self._pending_current = None
        return self._commit_current(
            candidate,
            anchor_outer_high_water=_primary_authority(self._primary) is not None,
        )

    def _commit_current(
        self,
        candidate: _CurrentCandidate,
        *,
        anchor_outer_high_water: bool = True,
    ) -> bool:
        """Commit one exact current, anchoring replay state only to live authority.

        A retained current can arrive before presence and is still useful as the
        last exact value for an offline appliance.  It must not, however, advance
        the process-local outer high-water: scoped fresh recovery preserves the
        retained slot while legitimately restarting cohorts under the same
        binding generation.  The first current backed by live authority anchors
        that replay fence.
        """
        publication_coordinate = (
            candidate.binding_generation,
            candidate.cohort_generation,
            candidate.publication_session_id,
        )
        tombstoned = self._tombstoned_current_cursors.get(candidate.epoch)
        if tombstoned is not None:
            tombstoned_sequence, tombstoned_canonical = tombstoned
            if candidate.sequence < tombstoned_sequence:
                _contract_error("TLV read tombstoned current sequence regressed")
            if candidate.sequence == tombstoned_sequence:
                if candidate.canonical != tombstoned_canonical:
                    _contract_error("TLV read tombstoned current cursor collided")
                _contract_error("TLV read tombstoned current was replayed")
        publication_source = self._publication_sources.get(publication_coordinate)
        if (
            publication_source is not None
            and publication_source != candidate.source_session_id
        ):
            _contract_error("TLV read source session collided inside one cohort")
        source_high_water = self._source_sequence_high_water.get(candidate.epoch)
        if source_high_water is not None and candidate.sequence < source_high_water:
            _contract_error("TLV read source sequence regressed")
        if (
            self._outer_high_water is not None
            and candidate.outer_coordinate < self._outer_high_water
        ):
            _contract_error("TLV read current generation regressed")
        outer_advanced = (
            self._outer_high_water is None
            or candidate.outer_coordinate > self._outer_high_water
        )
        if outer_advanced and anchor_outer_high_water:
            self._prune_history_before(candidate.outer_coordinate)
        if (
            publication_coordinate not in self._publication_sources
            and len(self._publication_sources) >= MAX_CURSOR_HISTORY
        ):
            _contract_error("TLV read publication source history is exhausted")
        if (
            candidate.epoch not in self._source_sequence_high_water
            and len(self._source_sequence_high_water) >= MAX_CURSOR_HISTORY
        ):
            _contract_error("TLV read source sequence history is exhausted")

        if not outer_advanced and self._publication_session_id is not None:
            if candidate.publication_session_id != self._publication_session_id:
                _contract_error("TLV read publication session collided")
            if candidate.source_session_id == self._source_session_id:
                if candidate.sequence < self._current_sequence:
                    _contract_error("TLV read current sequence regressed")
                if candidate.sequence == self._current_sequence:
                    if candidate.canonical != self._current_canonical:
                        _contract_error("TLV read current cursor collided")
                    changed = not self._current_transport_current
                    self._current_transport_current = True
                    if changed:
                        self._notify_listeners()
                    return changed
            else:
                _contract_error("TLV read source session collided inside one cohort")
        self._binding_generation = candidate.binding_generation
        self._cohort_generation = candidate.cohort_generation
        if anchor_outer_high_water:
            self._outer_high_water = candidate.outer_coordinate
        self._publication_session_id = candidate.publication_session_id
        self._source_session_id = candidate.source_session_id
        self._current_sequence = candidate.sequence
        self._current_canonical = candidate.canonical
        self._current_published_at = candidate.published_at
        self._fields = candidate.fields
        self._current_invalidated_semantics = candidate.invalidated_semantics
        self._current_diagnostics = candidate.diagnostics
        self._current_transport_current = True
        self._publication_sources[publication_coordinate] = candidate.source_session_id
        self._source_sequence_high_water[candidate.epoch] = max(
            candidate.sequence,
            self._source_sequence_high_water.get(candidate.epoch, 0),
        )
        self._adopt_consumer_pin(candidate.consumer_pin)
        self._current_consumer_pin = candidate.consumer_pin
        self._trim_high_waters()
        self._notify_listeners()
        return True

    def _prune_history_before(self, outer: tuple[int, int]) -> None:
        """Drop superseded cohorts already fenced by the strict outer high-water."""
        for history in (
            self._tombstoned_current_cursors,
            self._publication_sources,
            self._source_sequence_high_water,
            self._event_high_water,
        ):
            for key in tuple(history):
                if key[:2] < outer:
                    del history[key]

    def _trim_high_waters(self) -> None:
        if len(self._tombstoned_current_cursors) > MAX_CURSOR_HISTORY:
            _contract_error("TLV read current cursor history is exhausted")
        if len(self._publication_sources) > MAX_CURSOR_HISTORY:
            _contract_error("TLV read publication source history is exhausted")
        if len(self._source_sequence_high_water) > MAX_CURSOR_HISTORY:
            _contract_error("TLV read source sequence history is exhausted")
        if len(self._event_high_water) > MAX_CURSOR_HISTORY:
            _contract_error("TLV read event cursor history is exhausted")

    def _primary_updated(self) -> None:
        candidate = self._pending_current
        if candidate is not None:
            try:
                relation = self._candidate_primary_relation(candidate)
            except TlvReadProviderContractError:
                self._pending_current = None
                self._rejected_messages += 1
                _LOGGER.warning("Pending TLV read publication authority was rejected")
                self._notify_listeners()
                return
            if relation < 0:
                self._pending_current = None
                self._rejected_messages += 1
                _LOGGER.warning("Pending TLV read current became stale")
            elif relation == 0:
                self._pending_current = None
                try:
                    self._commit_current(candidate)
                except TlvReadProviderContractError:
                    self._rejected_messages += 1
                    _LOGGER.warning("Pending TLV read current was rejected")
                return
        # Event availability is also pinned to primary identity/presence, so
        # every primary update remains observable by the HA EventEntity.
        self._notify_listeners()

    def _ingest_event(self, payload: bytes, *, qos: int, retained: bool) -> bool:
        if type(qos) is not int or qos != 1 or type(retained) is not bool or retained:
            _contract_error("TLV read event transport flags are invalid")
        if not self._transport_ready:
            _contract_error("TLV read event arrived before transport readiness")
        if (
            not isinstance(payload, bytes)
            or len(payload) > MAX_TLV_READ_EVENT_PAYLOAD_BYTES
        ):
            _contract_error("TLV read event payload size is invalid")
        value = _decode_payload(payload)
        projection_version = _publication_projection_version(value)
        if set(value) != _EVENT_KEYS_BY_PROJECTION[projection_version]:
            _contract_error("TLV read event keys are invalid")
        now = self._now().astimezone(timezone.utc)
        self._validate_pins(value, now, projection_version)
        authority = _primary_authority(self._primary)
        if authority != (
            value["binding_generation"],
            value["publication_session_id"],
        ):
            _contract_error("TLV read event does not match the live primary identity")
        primary_state = _primary_state_coordinate(self._primary, authority)
        if primary_state is not None and value["cohort_generation"] < primary_state[1]:
            _contract_error("TLV read event is older than the primary state")
        descriptor_key = value["descriptor_key"]
        semantic_id = value["semantic_id"]
        if not isinstance(descriptor_key, str) or not isinstance(semantic_id, str):
            _contract_error("TLV read event descriptor identity is invalid")
        contract = self.profile.fields_by_descriptor_key.get(descriptor_key)
        if (
            contract is None
            or contract.semantic_id != semantic_id
            or contract.publication_mode != "transient-event"
            or contract.event_type is None
            or value["event_type"] != contract.event_type
        ):
            _contract_error("TLV read event descriptor is not authorized")
        published_at = _timestamp(value["published_at"], now, "publication")
        field = _parse_field(value["field"], contract, now, published_at)
        if field.value_type not in ("number", "string") or isinstance(
            field.value, bool
        ):
            _contract_error("TLV read event value is invalid")
        if (
            semantic_id == "event.water_tank.changed"
            and field.value != "water-tank state changed"
        ):
            _contract_error("TLV read water-tank event value is not authorized")
        if (
            not self._current_transport_current
            or value["binding_generation"] != self._binding_generation
            or value["cohort_generation"] != self._cohort_generation
            or value["publication_session_id"] != self._publication_session_id
            or value["source_session_id"] != self._source_session_id
            or value["sequence"] != self._current_sequence
        ):
            _contract_error("TLV read event does not match the accepted current cursor")
        publication_coordinate = (
            value["binding_generation"],
            value["cohort_generation"],
            value["publication_session_id"],
        )
        source_epoch = publication_coordinate + (value["source_session_id"],)
        publication_source = self._publication_sources.get(publication_coordinate)
        if (
            publication_source is not None
            and publication_source != value["source_session_id"]
        ):
            _contract_error("TLV read event source session collided inside one cohort")
        source_high_water = self._source_sequence_high_water.get(source_epoch)
        if source_high_water is not None and value["sequence"] < source_high_water:
            _contract_error("TLV read event source sequence regressed")
        cursor_key = (
            value["binding_generation"],
            value["cohort_generation"],
            value["publication_session_id"],
            value["source_session_id"],
            descriptor_key,
        )
        canonical = _canonical(value)
        previous = self._event_high_water.get(cursor_key)
        if previous is not None:
            previous_sequence, previous_canonical = previous
            if value["sequence"] < previous_sequence:
                _contract_error("TLV read event sequence regressed")
            if value["sequence"] == previous_sequence:
                if canonical != previous_canonical:
                    _contract_error("TLV read event cursor collided")
                return False
        if (
            cursor_key not in self._event_high_water
            and len(self._event_high_water) >= MAX_CURSOR_HISTORY
        ):
            _contract_error("TLV read event cursor history is exhausted")
        if (
            publication_coordinate not in self._publication_sources
            and len(self._publication_sources) >= MAX_CURSOR_HISTORY
        ):
            _contract_error("TLV read publication source history is exhausted")
        if (
            source_epoch not in self._source_sequence_high_water
            and len(self._source_sequence_high_water) >= MAX_CURSOR_HISTORY
        ):
            _contract_error("TLV read source sequence history is exhausted")
        self._event_high_water[cursor_key] = (value["sequence"], canonical)
        self._publication_sources[publication_coordinate] = value["source_session_id"]
        self._source_sequence_high_water[source_epoch] = max(
            value["sequence"], source_high_water or 0
        )
        self._trim_high_waters()
        self._notify_event(
            TlvReadEvent(
                semantic_id=semantic_id,
                descriptor_key=descriptor_key,
                event_type=contract.event_type,
                value=field.value,
                value_type=field.value_type,
                unit=field.unit,
                observed_at=field.observed_at,
                confidence=field.confidence,
                sequence=value["sequence"],
            )
        )
        return True

    def ingest(
        self,
        topic: str,
        payload: bytes,
        *,
        qos: int,
        retained: bool,
    ) -> bool:
        """Validate and commit one exact publication on the HA event loop."""
        try:
            if topic == self.current_topic:
                # MQTT brokers clear RETAIN on live delivery and set it only for
                # subscription replay. Producer-side journaling owns the
                # retain=true contract; this consumer can safely require only
                # the exact topic, QoS 1, pins, and payload shape.
                if type(qos) is not int or qos != 1 or type(retained) is not bool:
                    _contract_error("TLV read current transport flags are invalid")
                if not self._transport_ready:
                    _contract_error(
                        "TLV read current arrived before transport readiness"
                    )
                if payload == b"":
                    pending = self._pending_current
                    changed = (
                        bool(self._fields)
                        or self._current_transport_current
                        or pending is not None
                    )
                    tombstoned: dict[tuple[int, int, str, str], tuple[int, str]] = {}
                    if pending is not None:
                        tombstoned[pending.epoch] = (
                            pending.sequence,
                            pending.canonical,
                        )
                    if (
                        self._binding_generation is not None
                        and self._cohort_generation is not None
                        and self._publication_session_id is not None
                        and self._source_session_id is not None
                    ):
                        epoch = (
                            self._binding_generation,
                            self._cohort_generation,
                            self._publication_session_id,
                            self._source_session_id,
                        )
                        assert self._current_canonical is not None
                        tombstoned[epoch] = (
                            self._current_sequence,
                            self._current_canonical,
                        )
                    new_tombstones = set(tombstoned).difference(
                        self._tombstoned_current_cursors
                    )
                    if (
                        len(self._tombstoned_current_cursors) + len(new_tombstones)
                        > MAX_CURSOR_HISTORY
                    ):
                        _contract_error("TLV read current cursor history is exhausted")
                    self._pending_current = None
                    self._tombstoned_current_cursors.update(tombstoned)
                    for epoch in tombstoned:
                        self._publication_sources.pop(epoch[:3], None)
                    if not tombstoned:
                        # A duplicate lifecycle tombstone also clears any source
                        # fence left without an accepted retained current.
                        authority = _primary_authority(self._primary)
                        if authority is not None:
                            for coordinate in tuple(self._publication_sources):
                                if (coordinate[0], coordinate[2]) == authority:
                                    self._publication_sources.pop(coordinate, None)
                    self._fields = MappingProxyType({})
                    self._current_invalidated_semantics = frozenset()
                    self._current_diagnostics = MappingProxyType({})
                    self._binding_generation = None
                    self._cohort_generation = None
                    self._publication_session_id = None
                    self._source_session_id = None
                    self._current_sequence = 0
                    self._current_canonical = None
                    self._current_published_at = None
                    self._current_consumer_pin = None
                    self._current_transport_current = False
                    if changed:
                        self._notify_listeners()
                    return changed
                return self._stage_current(self._parse_current(payload))
            if topic == self.event_topic:
                return self._ingest_event(payload, qos=qos, retained=retained)
            _contract_error("TLV read topic is not authorized")
        except TlvReadProviderContractError:
            self._rejected_messages += 1
            raise


class TlvReadConsumerStatePushError(RuntimeError):
    """Report that Store persistence succeeded but the live push did not."""

    durable_state_persisted = True

    def __init__(self, state: TlvReadConsumerBindingState) -> None:
        self.state = state
        self.binding_id = state.binding_id
        self.record_sha256 = state.record_sha256
        super().__init__(
            "TLV read consumer state persisted but live provider push failed"
        )


async def async_apply_tlv_read_consumer_binding_state(
    *,
    states: dict[str, TlvReadConsumerBindingState],
    store: object,
    lock: object,
    providers: Mapping[str, TlvReadShadowProvider],
    state: TlvReadConsumerBindingState | Mapping[str, Any],
) -> TlvReadConsumerBindingState:
    """Immediately persist one adapter-owned state and update a live provider.

    ``states`` is the durable Store head, not the provider's process-local
    head. It advances immediately after a successful save even when the later
    live push fails. Keeping that object stable also ensures a second caller
    waiting on ``lock`` observes the first caller's persisted result. A live
    provider is optional so an offline or not-yet-connected appliance is never
    filtered from installation.
    """
    parsed = validate_tlv_read_consumer_binding_state(state)
    async with lock:  # type: ignore[attr-defined]
        matching_providers = tuple(
            provider
            for provider in providers.values()
            if provider.binding_id == parsed.binding_id
        )
        if len(matching_providers) > 1:
            raise RuntimeError("TLV read consumer binding has duplicate providers")
        provider = matching_providers[0] if matching_providers else None
        if provider is not None:
            parsed = provider.validate_consumer_state_replacement(parsed)
        current = states.get(parsed.binding_id)
        if current is not None:
            parsed = validate_tlv_read_consumer_state_replacement(
                current, parsed
            )
        candidate = dict(states)
        candidate[parsed.binding_id] = parsed
        # This Store has exactly one writer: the reviewed installation
        # transaction. Never replace this immediate save with a delayed save;
        # an older delayed stage could land after retirement and re-authorize
        # the source projection.
        save = getattr(store, "async_save", None)
        if not callable(save):
            raise TypeError("TLV read consumer state Store is invalid")
        await save(tlv_read_consumer_state_inventory_json(candidate))
        states.clear()
        states.update(candidate)
        if provider is not None:
            try:
                provider.replace_consumer_state(parsed)
            except Exception as err:
                raise TlvReadConsumerStatePushError(parsed) from err
        return parsed

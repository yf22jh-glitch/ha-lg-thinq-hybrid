"""Contract tests for the generated Rethink Local profile catalogue mirror."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.test_local_provider import local

REPOSITORY = Path(__file__).resolve().parents[1]
BUNDLED_DIRECTORY = REPOSITORY / "custom_components" / "my_lg"
BUNDLED_CATALOGUE = BUNDLED_DIRECTORY / local.LOCAL_PROFILE_CATALOGUE_FILENAME
BUNDLED_DIGEST = BUNDLED_DIRECTORY / local.LOCAL_PROFILE_CATALOGUE_DIGEST_FILENAME
SIBLING_DIRECTORY = REPOSITORY.parent / "lg_rethink_local" / "local" / "semantic"


def digest_sidecar(catalogue: bytes) -> bytes:
    digest = hashlib.sha256(catalogue).hexdigest()
    return f"{digest}  local/semantic/pilot-profiles.v1.json\n".encode("ascii")


def load_isolated_provider(directory: Path):
    """Import local_provider from a directory with controlled artifacts."""
    module_path = directory / "local_provider.py"
    shutil.copyfile(BUNDLED_DIRECTORY / "local_provider.py", module_path)
    name = f"my_lg_local_provider_isolated_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class LocalProfileCatalogueTests(unittest.TestCase):
    def test_missing_or_corrupt_optional_catalogue_never_blocks_import(self) -> None:
        for case in ("missing", "corrupt"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_dir:
                directory = Path(raw_dir)
                if case == "corrupt":
                    catalogue = b"{}\n"
                    (directory / local.LOCAL_PROFILE_CATALOGUE_FILENAME).write_bytes(
                        catalogue
                    )
                    (
                        directory / local.LOCAL_PROFILE_CATALOGUE_DIGEST_FILENAME
                    ).write_bytes(digest_sidecar(catalogue))

                isolated = load_isolated_provider(directory)

                self.assertEqual(
                    isolated.local_shadow_configurations(
                        {
                            isolated.OPT_LOCAL_PROVIDER_MODE: isolated.LOCAL_PROVIDER_MODE_DISABLED
                        }
                    ),
                    (),
                )
                with self.assertRaises(isolated.LocalProviderConfigurationError):
                    isolated.local_shadow_configurations(
                        {
                            isolated.OPT_LOCAL_PROVIDER_MODE: isolated.LOCAL_PROVIDER_MODE_SHADOW,
                            isolated.OPT_LOCAL_PAT_DEVICE_ID: "pat-device-001",
                            isolated.OPT_LOCAL_BINDING_ID: "pilot_dhum_provider_001",
                            isolated.OPT_LOCAL_MQTT_PASSWORD: "private-test-password",
                        }
                    )

    def test_lazy_catalogue_load_is_thread_safe_and_repairable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            isolated = load_isolated_provider(directory)

            with self.assertRaises(isolated.LocalProviderConfigurationError):
                isolated.load_local_semantic_profile_catalogue()

            shutil.copyfile(BUNDLED_CATALOGUE, directory / BUNDLED_CATALOGUE.name)
            shutil.copyfile(BUNDLED_DIGEST, directory / BUNDLED_DIGEST.name)
            original_load = isolated._load_bundled_local_semantic_profiles
            call_count = 0
            counter_lock = threading.Lock()

            def counted_load():
                nonlocal call_count
                with counter_lock:
                    call_count += 1
                time.sleep(0.01)
                return original_load()

            isolated._load_bundled_local_semantic_profiles = counted_load
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(
                    executor.map(
                        lambda _index: isolated.load_local_semantic_profile_catalogue(),
                        range(16),
                    )
                )

            self.assertEqual(call_count, 1)
            self.assertTrue(all(result is results[0] for result in results))

    def test_bundled_catalogue_constructs_every_exact_profile_and_field(self) -> None:
        catalogue_bytes = BUNDLED_CATALOGUE.read_bytes()
        digest_bytes = BUNDLED_DIGEST.read_bytes()
        raw = json.loads(catalogue_bytes)

        revision, profiles, digest = local._load_local_semantic_profile_catalogue(
            catalogue_bytes, digest_bytes
        )

        self.assertEqual(revision, raw["semantics_revision"])
        self.assertEqual(digest, hashlib.sha256(catalogue_bytes).hexdigest())
        self.assertEqual(
            list(profiles),
            [profile["profile_id"] for profile in raw["profiles"]],
        )
        self.assertEqual(len(profiles), 15)
        self.assertEqual(sum(len(profile.fields) for profile in profiles.values()), 161)
        for raw_profile in raw["profiles"]:
            profile = profiles[raw_profile["profile_id"]]
            self.assertEqual(
                profile.contract_revision, raw_profile["contract_revision"]
            )
            self.assertEqual(
                profile.supported_semantics_revisions,
                tuple(raw_profile["supported_semantics_revisions"]),
            )
            self.assertEqual(profile.model_id, raw_profile["model_id"])
            self.assertEqual(profile.platform, raw_profile["platform"])
            self.assertEqual(
                list(profile.fields),
                [field["semantic_id"] for field in raw_profile["fields"]],
            )
            for raw_field in raw_profile["fields"]:
                field = profile.fields[raw_field["semantic_id"]]
                self.assertEqual(field.value_type, raw_field["value_type"])
                self.assertEqual(field.exposure, raw_field["exposure"])
                self.assertEqual(field.unit, raw_field.get("unit"))
                self.assertEqual(field.confidence, tuple(raw_field["confidence"]))

    def test_catalogue_validation_fails_closed(self) -> None:
        source = json.loads(BUNDLED_CATALOGUE.read_bytes())
        cases: list[tuple[str, object]] = []

        wrong_schema = json.loads(json.dumps(source))
        wrong_schema["schema_version"] = 2
        cases.append(("schema", wrong_schema))

        duplicate_profile = json.loads(json.dumps(source))
        duplicate_profile["profiles"].append(duplicate_profile["profiles"][0])
        cases.append(("duplicate profile", duplicate_profile))

        unsupported_revision = json.loads(json.dumps(source))
        unsupported_revision["profiles"][0]["supported_semantics_revisions"] = [
            source["semantics_revision"] - 1
        ]
        cases.append(("unsupported revision", unsupported_revision))

        unknown_exposure = json.loads(json.dumps(source))
        unknown_exposure["profiles"][0]["fields"][0]["exposure"] = "diagnostic"
        cases.append(("exposure", unknown_exposure))

        for name, value in cases:
            with self.subTest(name=name):
                encoded = (json.dumps(value) + "\n").encode()
                with self.assertRaises(RuntimeError):
                    local._load_local_semantic_profile_catalogue(
                        encoded, digest_sidecar(encoded)
                    )

        with self.assertRaises(RuntimeError):
            local._load_local_semantic_profile_catalogue(
                BUNDLED_CATALOGUE.read_bytes(), b"0" * 89
            )

        duplicate_key = BUNDLED_CATALOGUE.read_bytes().replace(
            b'"schema_version": 1,',
            b'"schema_version": 1, "schema_version": 1,',
            1,
        )
        with self.assertRaises(RuntimeError):
            local._load_local_semantic_profile_catalogue(
                duplicate_key, digest_sidecar(duplicate_key)
            )

    def test_bundled_artifacts_are_byte_identical_to_rethink_when_present(
        self,
    ) -> None:
        sibling_catalogue = SIBLING_DIRECTORY / BUNDLED_CATALOGUE.name
        sibling_digest = SIBLING_DIRECTORY / BUNDLED_DIGEST.name
        if not sibling_catalogue.is_file() or not sibling_digest.is_file():
            self.skipTest("Rethink sibling repository is not present")

        self.assertEqual(BUNDLED_CATALOGUE.read_bytes(), sibling_catalogue.read_bytes())
        self.assertEqual(BUNDLED_DIGEST.read_bytes(), sibling_digest.read_bytes())


if __name__ == "__main__":
    unittest.main()

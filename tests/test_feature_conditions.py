"""Conditions are device/function data, independent of contracts or HA boot."""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parents[1] / "custom_components/my_lg"
PACKAGE = "feature_condition_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(DIRECTORY)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.feature_conditions", DIRECTORY / "feature_conditions.py")
conditions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conditions)


class FeatureConditionsTest(unittest.TestCase):
    def test_no_policy_is_legacy_and_declared_conditions_never_guess_missing_state(self):
        policy = {"all": [{"semanticId": "operation.power_requested", "values": [True]}], "reasonKo": "전원 필요"}
        self.assertEqual(conditions.evaluate_condition(None, {}), (True, None))
        for value in [None, False, 1, "true"]:
            self.assertEqual(conditions.evaluate_condition(policy, {"operation.power_requested": value}), (False, "전원 필요"))
        self.assertEqual(conditions.evaluate_condition(policy, {"operation.power_requested": True}), (True, None))

    def test_per_value_conditions_filter_options_but_not_unrelated_commands(self):
        policy = {"all": [{"semanticId": "operation.power_requested", "values": [True]}], "byValue": {
            "humidify": {"all": [{"semanticId": "operation.mode", "values": ["humidify", "humidify+clean"]}], "reasonKo": "원격 가습 전환 불가"}}}
        state = {"operation.power_requested": True, "operation.mode": "air clean"}
        self.assertEqual(conditions.evaluate_condition(policy, state, "humidify"), (False, "원격 가습 전환 불가"))
        self.assertEqual(conditions.evaluate_condition(policy, state, "air clean"), (True, None))
        state["operation.mode"] = "humidify+clean"
        self.assertEqual(conditions.evaluate_condition(policy, state, "humidify"), (True, None))
        for malformed in ["code", {"when": "ON"}, {"all": "ON"}, {"byValue": {"true": None}}, {"all": [{"semanticId": "power", "values": []}]}]:
            self.assertFalse(conditions.evaluate_condition(malformed, {})[0])

    def test_optional_table_edits_are_detected_including_direct_sql_and_wal(self):
        from feature_condition_tests import feature_database as db
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "features.sqlite3"
            db.create_database(path, [])
            with sqlite3.connect(path) as connection:
                connection.execute("INSERT INTO model_rollout VALUES('HUM_056905_WW',1)")
            self.assertEqual(conditions.load_control_conditions(path), {})
            before = db.feature_database_token(path)
            with sqlite3.connect(path) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE control_conditions(model_id TEXT,capability_id TEXT,condition_json TEXT,PRIMARY KEY(model_id,capability_id))")
                connection.execute("INSERT INTO control_conditions VALUES('HUM_056905_WW','operation.mode',?)", (json.dumps({"all": []}),))
            self.assertNotEqual(db.feature_database_token(path), before)
            self.assertEqual(conditions.load_control_conditions(path)[("HUM_056905_WW", "operation.mode")], {"all": []})
            before = db.feature_database_token(path)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE control_conditions SET condition_json='broken'")
            self.assertNotEqual(db.feature_database_token(path), before)
            policy = conditions.load_control_conditions(path)[("HUM_056905_WW", "operation.mode")]
            self.assertFalse(conditions.evaluate_condition(policy, {})[0])

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('link_feature_handler_test', ROOT / 'scripts/link_feature_handler.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LinkFeatureHandlerTest(unittest.TestCase):
    def test_link_keeps_disabled_flags_and_registers_the_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'features.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.execute('PRAGMA application_id=1279739462')
                connection.execute('PRAGMA user_version=2')
                connection.execute('CREATE TABLE features (channel,model_id,profile_id,feature_id,definition_json,producer_registered,enabled)')
                connection.execute('CREATE TABLE feature_changes (channel,model_id,profile_id,feature_id,action)')
                for channel in ('full-read', 'control-entity'):
                    connection.execute('INSERT INTO features VALUES (?,?,?,?,?,0,0)', (channel,'MODEL','','feature.enabled','{}'))
            self.assertEqual(module.link(path, 'MODEL', 'feature.enabled', 'model.mjs'), 2)
            with sqlite3.connect(path) as connection:
                rows = connection.execute('SELECT channel,definition_json,producer_registered,enabled FROM features ORDER BY channel').fetchall()
            self.assertEqual([row[3] for row in rows], [0, 0])
            self.assertEqual([row[2] for row in rows], [0, 1])
            self.assertTrue(all(json.loads(row[1])['runtimeHandler'] == 'model.mjs' for row in rows))
            with self.assertRaises(ValueError):
                module.link(path, 'MODEL', 'feature.enabled', '../outside.mjs')

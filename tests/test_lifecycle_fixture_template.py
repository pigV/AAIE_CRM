"""Fixture caching cannot share a writable service database between tests."""
from pathlib import Path
import sqlite3,tempfile,unittest
from unittest.mock import Mock
class LifecycleFixtureTemplateTests(unittest.TestCase):
 def test_each_copy_is_independent_and_seed_built_once(self):
  from tests.lifecycle_fixture_template import FixtureTemplate
  with tempfile.TemporaryDirectory() as name:
   root=Path(name)
   def build(path):
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE probe(value TEXT)');c.execute("INSERT INTO probe VALUES('pristine')")
   factory=Mock(side_effect=build)
   with FixtureTemplate(factory) as fixture:
    a,b=root/'one.db',root/'two.db';fixture.copy_to(a)
    with sqlite3.connect(a) as c:c.execute("UPDATE probe SET value='changed'")
    fixture.copy_to(b)
    with sqlite3.connect(b) as c:self.assertEqual(c.execute('SELECT value FROM probe').fetchone()[0],'pristine')
    factory.assert_called_once()
 def test_existing_target_is_never_overwritten(self):
  from tests.lifecycle_fixture_template import FixtureTemplate
  with tempfile.TemporaryDirectory() as name:
   path=Path(name)/'existing.db';path.write_bytes(b'keep');factory=Mock()
   with FixtureTemplate(factory) as fixture:
    with self.assertRaises(FileExistsError):fixture.copy_to(path)
   self.assertEqual(path.read_bytes(),b'keep');factory.assert_not_called()

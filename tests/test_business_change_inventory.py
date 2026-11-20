import unittest

from tools.business_change_inventory import inspect_source, build_inventory


class InventoryTests(unittest.TestCase):
    def test_reads_are_not_classified_as_writes(self):
        rows = inspect_source('def read(conn):\n conn.execute("SELECT * FROM students")\n')
        self.assertEqual([], rows[0]['writes'])

    def test_fstring_update_records_table_and_transaction_scope(self):
        rows = inspect_source('def edit(conn):\n with db.transaction() as conn:\n  conn.execute(f"UPDATE students SET {column}=?", args)\n')
        self.assertEqual(['students'], rows[0]['tables'])
        self.assertIn('db.transaction', rows[0]['transactions'])

    def test_nested_function_write_is_not_attributed_to_parent(self):
        rows = inspect_source('def outer():\n def inner(conn):\n  conn.execute("DELETE FROM offers")\n return inner\n')
        self.assertEqual([], rows[0]['writes'])
        self.assertEqual(['offers'], rows[1]['tables'])

    def test_dynamic_sql_is_explicitly_unresolved(self):
        rows = inspect_source('def edit(conn, sql):\n conn.execute(sql)\n')
        self.assertEqual('dynamic_sql', rows[0]['writes'][0]['kind'])

    def test_route_through_helper_inherits_mutation_dependency(self):
        sources = {'app.py': '''class Handler:
 def routes(self):
  return [(r"^/students/(\\d+)/edit$", self.edit)]
 def edit(self):
  save_student()
def save_student():
 db.execute("UPDATE students SET name=?", data)
'''}
        result = build_inventory(sources)
        row = next(x for x in result['functions'] if x['name']=='Handler.edit')
        self.assertEqual(['^/students/(\\d+)/edit$'], row['routes'])
        self.assertTrue(row['may_mutate'])
        self.assertIn('app.py:save_student', row['dependencies'])

    def test_module_level_and_sql_script_are_not_silently_missed(self):
        result = inspect_source('conn.execute("UPDATE students SET name=?")\ndef init(conn):\n conn.executescript("CREATE TABLE example(id); INSERT INTO example VALUES(1);")\n')
        self.assertEqual('<module>',result[0]['name'])
        self.assertTrue(result[0]['writes'])
        self.assertIn('example',result[1]['tables'])

    def test_syntax_error_stops_inventory_instead_of_reporting_complete(self):
        with self.assertRaises(SyntaxError):
            build_inventory({'broken.py':'def broken('})

    def test_upsert_clause_does_not_invent_table_named_set(self):
        rows = inspect_source('def save(conn):\n conn.execute("INSERT INTO offers(id) VALUES(?) ON CONFLICT(id) DO UPDATE SET id=excluded.id")\n')
        self.assertEqual(['offers'],rows[0]['tables'])

    def test_dynamic_mixin_dispatch_is_visible_for_manual_review(self):
        rows=inspect_source("def route(g):\n g['save_record']()\n")
        self.assertEqual(1,len(rows[0]['unresolved_calls']))

    def test_literal_string_replace_is_not_a_file_mutation(self):
        rows=inspect_source('def label():\n return "Visa Case".replace(" ", "_")\n')
        self.assertEqual([],rows[0]['writes'])


if __name__ == '__main__':
    unittest.main()

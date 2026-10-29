import ast
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
from unittest import TestCase, mock
from tests.test_transfer_source_routes import handler_method


def method_source(name):
    source = __import__('pathlib').Path('app.py').read_text(encoding='utf-8')
    marker = f'    def {name}('
    start = source.index(marker)
    next_method = source.find('\n    def ', start + len(marker))
    return source[start:] if next_method < 0 else source[start:next_method]


def direct_handler_method(name, namespace):
    """Extract a CRMHandler-local route without resolving HTTP server bases."""
    tree = ast.parse(__import__('pathlib').Path('app.py').read_text(encoding='utf-8'))
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'CRMHandler')
    method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'app.py', 'exec'), namespace)
    return namespace[name]


def module_function(name, namespace):
    """Extract one module-level function without importing the CRM runtime."""
    tree = ast.parse(__import__('pathlib').Path('app.py').read_text(encoding='utf-8'))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), namespace)
    return namespace[name]

class TransferContractRouteGateTests(TestCase):
    def test_staff_transfer_context_hides_all_contract_creation_tools(self):
        function = module_function(
            "restrict_contract_creation_for_transfer_context",
            {
                "normalize_contract_type": lambda value: value,
                "can_manage_transfer_contract": lambda user: user["role_name"] == "Manager",
            },
        )
        contracts = [
            {"contract_type": "LocalStudentApplicationContract"},
            {"contract_type": "TransferApplicationContract"},
        ]

        active_contexts = {
            22: SimpleNamespace(
                cycle_status="active", lifecycle_type="transfer", cycle_id=7
            ),
        }
        contracts[0]["id"] = 11
        contracts[0]["application_cycle_id"] = None
        contracts[1].update({"id": 22, "application_cycle_id": 7})

        self.assertTrue(function(contracts, active_contexts, {"role_name": "Staff"}))
        self.assertFalse(function(contracts, active_contexts, {"role_name": "Manager"}))
        self.assertFalse(
            function(
                [{"id": 11, "application_cycle_id": None,
                  "contract_type": "LocalStudentApplicationContract"}],
                {},
                {"role_name": "Staff"},
            )
        )
        self.assertFalse(
            function(
                [{"id": 22, "application_cycle_id": 7,
                  "contract_type": "TransferApplicationContract"}],
                {22: SimpleNamespace(
                    cycle_status="closed", lifecycle_type="transfer", cycle_id=7
                )},
                {"role_name": "Staff"},
            )
        )
        self.assertTrue(
            function(
                [{"id": 22, "application_cycle_id": 7,
                  "contract_type": "TransferApplicationContract"}],
                {},
                {"role_name": "Staff"},
            )
        )
        for invalid_context in (
            SimpleNamespace(cycle_status="closed", lifecycle_type="initial", cycle_id=7),
            SimpleNamespace(cycle_status="closed", lifecycle_type="progression", cycle_id=7),
            SimpleNamespace(cycle_status="closed", lifecycle_type="transfer", cycle_id=8),
        ):
            with self.subTest(invalid_context=invalid_context):
                self.assertTrue(
                    function(
                        [{"id": 22, "application_cycle_id": 7,
                          "contract_type": "TransferApplicationContract"}],
                        {22: invalid_context},
                        {"role_name": "Staff"},
                    )
                )

    def test_server_guard_releases_only_exact_same_student_closed_transfer_cycle(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY, student_id TEXT, cycle_type TEXT, status TEXT
            );
            CREATE TABLE contracts(
              id INTEGER PRIMARY KEY, student_id TEXT, contract_type TEXT,
              application_cycle_id INTEGER, deleted_at TEXT, voided_at TEXT
            );
            """
        )
        database = SimpleNamespace(
            one=lambda statement, parameters: connection.execute(
                statement, parameters
            ).fetchone()
        )
        function = module_function(
            "restrict_contract_creation_for_student_transfer_context",
            {
                "can_manage_transfer_contract": lambda user: False,
                "db": database,
            },
        )
        contract_sql = (
            "INSERT INTO contracts(id,student_id,contract_type,application_cycle_id) "
            "VALUES(22,'S1','TransferApplicationContract',7)"
        )
        scenarios = (
            ((7, "S1", "transfer", "closed"), False),
            ((7, "S1", "initial", "closed"), True),
            ((7, "S1", "progression", "closed"), True),
            ((7, "OTHER", "transfer", "closed"), True),
            ((7, "S1", "transfer", "active"), True),
        )
        try:
            for cycle, expected in scenarios:
                with self.subTest(cycle=cycle):
                    connection.execute("DELETE FROM contracts")
                    connection.execute("DELETE FROM application_cycles")
                    connection.execute(
                        "INSERT INTO application_cycles(id,student_id,cycle_type,status) "
                        "VALUES(?,?,?,?)",
                        cycle,
                    )
                    connection.execute(contract_sql)
                    self.assertIs(
                        expected,
                        function("S1", {"role_name": "Staff"}),
                    )
            connection.execute("DELETE FROM application_cycles")
            self.assertTrue(function("S1", {"role_name": "Staff"}))
        finally:
            connection.close()

    def test_staff_cannot_disguise_transfer_context_generation_as_ordinary_contract(self):
        database = mock.Mock()
        database.one.side_effect = [
            {"student_id": "S1", "nas_status": "ready"},
            {"id": 77},
        ]
        unsafe_connect = mock.Mock(side_effect=AssertionError("must deny before business reads"))
        database.connect = unsafe_connect
        denied = mock.Mock(return_value=("forbidden", 403))
        method = handler_method(
            "contract_generate",
            {
                "can_generate_contract": lambda user: True,
                "can_manage_transfer_contract": lambda user: False,
                "restrict_contract_creation_for_student_transfer_context": lambda *args: True,
                "normalize_contract_type": lambda value: value,
                "db": database,
            },
        )
        handler = SimpleNamespace(
            user={"id": 7, "role_name": "Staff"},
            form={"contract_type": "StudyAbroadContract"},
            error_page=denied,
        )

        self.assertEqual(("forbidden", 403), method(handler, "POST", "S1"))
        denied.assert_called_once_with("仅 Admin / Manager 可以管理转学合同", 403)
        unsafe_connect.assert_not_called()

    def test_staff_cannot_upload_generic_contract_inside_transfer_context(self):
        database = mock.Mock()
        database.one.return_value = {"id": 77}
        denied = mock.Mock(return_value=("forbidden", 403))
        method = handler_method(
            "contract_forms_upload",
            {
                "can_review_module_action": lambda *args: True,
                "can_manage_transfer_contract": lambda user: False,
                "restrict_contract_creation_for_student_transfer_context": lambda *args: True,
                "normalize_contract_type": lambda value: value,
                "db": database,
            },
        )
        handler = SimpleNamespace(
            user={"id": 7, "role_name": "Staff"},
            form={},
            error_page=denied,
        )

        self.assertEqual(("forbidden", 403), method(handler, "POST", "S1"))
        denied.assert_called_once_with("仅 Admin / Manager 可以管理转学合同", 403)

    def test_student_contract_panel_omits_fee_and_generation_form_for_staff_transfer(self):
        source = method_source("contract_panel")
        self.assertIn(
            "restricted_transfer_context = restrict_contract_creation_for_transfer_context(",
            source,
        )
        self.assertIn("contracts, lifecycle_contexts, self.user", source)
        self.assertIn("if restricted_transfer_context:", source)
        self.assertIn("contract_creation_tools =", source)
        self.assertIn("{contract_creation_tools}", source)

    def test_transfer_contract_policy_uses_exact_role_plus_module_authority(self):
        allowed = {
            ("Admin", True): True,
            ("Manager", True): True,
            ("Manager", False): False,
            ("Staff", True): False,
            ("Consultant", True): False,
        }
        for (role, module_access), expected in allowed.items():
            with self.subTest(role=role, module_access=module_access):
                access = mock.Mock(return_value=module_access)
                function = module_function(
                    "can_manage_transfer_contract",
                    {"can_generate_contract": access},
                )
                user = {
                    "role_name": role,
                    "permissions_json": '{"contracts.manage": true}',
                    "module_permissions": ["student_contracts"],
                }
                self.assertIs(expected, function(user))

    def test_staff_transfer_contract_student_view_does_not_sync_draft(self):
        unsafe_sync = mock.Mock(side_effect=AssertionError("unsafe draft mutation"))
        function = module_function(
            "contract_effective_draft_for_view",
            {
                "normalize_contract_type": lambda value: value,
                "can_manage_transfer_contract": mock.Mock(return_value=False),
                "ensure_contract_effective_draft_file": unsafe_sync,
                "Path": __import__('pathlib').Path,
            },
        )
        contract = {
            "contract_type": "TransferApplicationContract",
            "contract_file_path": "/existing/draft.docx",
        }

        self.assertEqual(
            __import__('pathlib').Path("/existing/draft.docx"),
            function(contract, {"role_name": "Staff"}),
        )
        unsafe_sync.assert_not_called()

    def test_staff_transfer_contract_center_view_does_not_generate_uid(self):
        unsafe_uid = mock.Mock(side_effect=AssertionError("unsafe UID mutation"))
        function = module_function(
            "contract_uid_for_view",
            {
                "normalize_contract_type": lambda value: value,
                "can_manage_transfer_contract": mock.Mock(return_value=False),
                "ensure_contract_uid": unsafe_uid,
            },
        )
        contract = {
            "contract_type": "TransferApplicationContract",
            "contract_uid": "",
        }

        self.assertEqual("", function(contract, {"role_name": "Staff"}))
        unsafe_uid.assert_not_called()

    def test_staff_transfer_document_output_does_not_sync_draft(self):
        unsafe_sync = mock.Mock(side_effect=AssertionError("unsafe draft mutation"))
        existing_document = {
            "document_type": "StudyAbroadContract",
            "student_id": "S-1",
            "file_path": "/existing/draft.docx",
        }
        contract = {
            "contract_type": "TransferApplicationContract",
            "contract_file_path": "/existing/draft.docx",
        }
        database = mock.Mock()
        database.one.return_value = existing_document
        function = module_function(
            "effective_contract_document_for_output",
            {
                "contract_for_document_path": lambda path: contract,
                "normalize_contract_type": lambda value: value,
                "can_manage_transfer_contract": mock.Mock(return_value=False),
                "ensure_contract_effective_draft_file": unsafe_sync,
                "db": database,
            },
        )

        with self.assertRaises(PermissionError):
            function(existing_document, {"role_name": "Staff"})

        unsafe_sync.assert_not_called()

    def test_staff_transfer_contract_center_download_denied_before_file_access(self):
        file_access = mock.Mock(side_effect=AssertionError("unsafe file access"))
        database = mock.Mock()
        database.one.return_value = {
            "id": 100,
            "contract_type": "TransferApplicationContract",
        }
        error_page = mock.Mock(return_value="denied")
        method = direct_handler_method(
            "contract_center_download",
            {
                "can_view_contract_center": mock.Mock(return_value=True),
                "can_manage_transfer_contract": mock.Mock(return_value=False),
                "normalize_contract_type": lambda value: value,
                "db": database,
            },
        )
        handler = type("Handler", (), {
            "user": {"role_name": "Staff"},
            "error_page": error_page,
            "contract_center_file_path": file_access,
        })()

        self.assertEqual("denied", method(handler, "GET", 100, "draft"))
        error_page.assert_called_once_with("仅 Admin / Manager 可以查看转学合同文件", 403)
        file_access.assert_not_called()

    def test_staff_transfer_document_routes_return_403_before_output(self):
        for method_name in ("document_download", "document_preview"):
            with self.subTest(method_name=method_name):
                database = mock.Mock()
                database.one.side_effect = [
                    {
                        "id": 7,
                        "student_id": "S-1",
                        "document_type": "StudyAbroadContract",
                        "file_path": "/existing/draft.docx",
                    },
                    {"student_id": "S-1"},
                ]
                error_page = mock.Mock(return_value="denied")
                effective = mock.Mock(side_effect=PermissionError("transfer denied"))
                namespace = {
                    "db": database,
                    "SAFE_PRODUCTION_ACTIVE": True,
                    "effective_contract_document_for_output": effective,
                }
                method = direct_handler_method(method_name, namespace)
                handler = type("Handler", (), {
                    "user": {"role_name": "Staff"},
                    "query": {},
                    "require": lambda self, permission: True,
                    "error_page": error_page,
                })()

                self.assertEqual("denied", method(handler, "GET", 7))
                error_page.assert_called_once_with(
                    "仅 Admin / Manager 可以查看转学合同文件", 403
                )
                effective.assert_called_once()

    def test_contract_center_hides_transfer_download_links_without_exact_authority(self):
        source = __import__('pathlib').Path('app.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "contract_center_table"
        )
        method_source = ast.get_source_segment(source, method)
        self.assertIn(
            'if transfer_management_allowed and contract["contract_file_path"] else ""',
            method_source,
        )
        self.assertIn(
            'if transfer_management_allowed and contract["signed_file_path"] else ""',
            method_source,
        )
        self.assertIn(
            'if transfer_management_allowed and contract["final_signed_file_path"] else ""',
            method_source,
        )

    def test_contract_center_omits_transfer_rows_without_exact_authority(self):
        source = __import__('pathlib').Path('app.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "contract_center_table"
        )
        method_source = ast.get_source_segment(source, method)
        self.assertIn("if not transfer_management_allowed:\n                continue", method_source)

    def test_student_contract_panel_gates_transfer_file_and_signature_reads(self):
        source = __import__('pathlib').Path('app.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "contract_panel"
        )
        method_source = ast.get_source_segment(source, method)
        self.assertIn("Transfer 合同由 Admin / Manager 处理", method_source)
        self.assertIn(
            'contract_file_actions(doc, "合同草稿", latest_version) if doc else',
            method_source,
        )
        self.assertLess(
            method_source.index("if not transfer_management_allowed:"),
            method_source.index("draft_path = None"),
        )

    def test_staff_transfer_signature_image_denied_before_signature_file_read(self):
        database = mock.Mock()
        database.one.return_value = {
            "contract_type": "TransferApplicationContract",
        }
        error_page = mock.Mock(return_value="denied")
        method = direct_handler_method(
            "contract_signature_image",
            {
                "can_view_contract_features": mock.Mock(return_value=True),
                "can_manage_transfer_contract": mock.Mock(return_value=False),
                "normalize_contract_type": lambda value: value,
                "db": database,
            },
        )
        handler = type("Handler", (), {
            "user": {"role_name": "Staff"},
            "error_page": error_page,
        })()

        self.assertEqual("denied", method(handler, "GET", 9))
        error_page.assert_called_once_with("仅 Admin / Manager 可以查看转学合同文件", 403)
        self.assertEqual(1, database.one.call_count)

    def test_admin_manager_transfer_views_retain_authorized_sync(self):
        for role in ("Admin", "Manager"):
            with self.subTest(role=role):
                draft_sync = mock.Mock(return_value="draft")
                uid_sync = mock.Mock(return_value="uid")
                namespace = {
                    "normalize_contract_type": lambda value: value,
                    "can_manage_transfer_contract": mock.Mock(return_value=True),
                    "ensure_contract_effective_draft_file": draft_sync,
                    "ensure_contract_uid": uid_sync,
                    "Path": __import__('pathlib').Path,
                }
                draft = module_function("contract_effective_draft_for_view", namespace)
                uid = module_function("contract_uid_for_view", namespace)
                contract = {
                    "contract_type": "TransferApplicationContract",
                    "contract_file_path": "",
                    "contract_uid": "",
                }

                self.assertEqual("draft", draft(contract, {"role_name": role}))
                self.assertEqual("uid", uid(contract, {"role_name": role}))
                draft_sync.assert_called_once()
                uid_sync.assert_called_once()

    def test_generate_transfer_reserves_source_draft_before_storage(self):
        sentinel=object()
        namespace={'can_generate_contract':lambda user:True,
                   'can_manage_transfer_contract':lambda user:True,
                   'restrict_contract_creation_for_student_transfer_context':lambda *a:False,
                   'normalize_contract_type':lambda value:value,
                   'student_higher_education_experience':lambda *a:{'contract_applicable':True},
                   'db':SimpleNamespace(one=lambda *a:{'student_id':'S1','nas_status':'not_ready'},connect=lambda:object(),_release_connection=lambda c:None)}
        method=handler_method('contract_generate',namespace)
        handler=SimpleNamespace(user={'id':1},form={'contract_type':'TransferApplicationContract'},
            transfer_contract_prepare=lambda sid:sentinel)
        self.assertIs(sentinel,method(handler,'POST','S1'))

    def test_signing_link_requires_source_before_pdf_access(self):
        class Hold(ValueError): pass
        def gate(contract): raise Hold('SOURCE VERIFICATION HOLD')
        namespace={'db':SimpleNamespace(one=lambda *a:{'id':1}), 'require_transfer_contract_signable':gate}
        method=handler_method('contract_signing_create_link',namespace)
        with self.assertRaises(Hold): method(SimpleNamespace(),1)

    def test_verified_transfer_replaces_reserved_draft_with_published_v2(self):
        class Cursor:
            def fetchall(self):
                return [(77, 'UID-77', None)]
            def fetchone(self): return ('UID-77',)
        connection = SimpleNamespace(execute=lambda *a: Cursor())
        @contextmanager
        def transaction():
            yield connection
        authority = {
            'id': 9, 'transfer_contract_id': 77, 'student_id': 'S1',
            'source_classification': 'aaie_existing', 'rule_version': 'transfer-source-v1',
        }
        bound = {'contract_type': 'TransferApplicationContract',
                 'contract_instance': {'consulting_and_visa_service_fee':'1200.00'}}
        replace = mock.Mock(return_value=SimpleNamespace(contract_id=77, document_id=5,
                                                          file_path='/isolated/contract.docx', created=True))
        binding = mock.Mock(return_value=(bound, object()))
        package = SimpleNamespace(version_id=12)
        namespace = {
            'db': SimpleNamespace(transaction=transaction,
                                  one=lambda *a: {'student_id':'S1','nas_status':'ready','form_data_json':'{}'}),
            'sqlite3': sqlite3,
            'transfer_source_authority': SimpleNamespace(identify_for_submission=lambda *a, **kw: authority, ensure_for_submission=lambda *a, **kw: authority,
                                                         SourceAuthorityError=ValueError),
            'transfer_contract_authority': SimpleNamespace(
                commercials=lambda *a, **kw: {'authority':authority, 'base_fee':'1200.00'},
                trusted_binding=lambda value: value,
            ),
            'current_published_contract_package': lambda kind: package,
            'authority_contract_bindings': binding,
            'replace_draft_atomic': replace,
            'json': __import__('json'), 'Path': __import__('pathlib').Path,
            'date': __import__('datetime').date, 'datetime': __import__('datetime').datetime,
            'now_iso': lambda: '2026-09-16T00:00:00+10:00',
            'new_token': lambda: 'server-request-77',
            'next_version_path': lambda folder, name: (__import__('pathlib').Path('/isolated/contract.docx'), 1),
            'forms_folder_for_student': lambda student: __import__('pathlib').Path('/isolated'),
            'quote': lambda value: value,
            'emit_contract_business_event': mock.Mock(),
            'PublishedTemplateAuthorityError': ValueError,
            'ContractGenerationPersistenceError': RuntimeError,
        }
        method = handler_method('transfer_contract_prepare', namespace)
        handler = SimpleNamespace(
            form={'service_fee_currency':'国内银行'},
            user={'id':1},
            contract_creation_signing_link_redirect=lambda student_id, contract_id: ('signed', student_id, contract_id),
            flash_redirect=lambda *args: args,
        )
        self.assertEqual(('signed','S1',77), method(handler,'S1'))
        self.assertEqual(77, replace.call_args.kwargs['contract_id'])
        self.assertEqual(bound, replace.call_args.kwargs['bound'])
        self.assertEqual(authority, binding.call_args.kwargs['trusted_transfer_source'])
        self.assertIs(package, binding.call_args.args[3])
        self.assertEqual('server-request-77', binding.call_args.args[1]['generation_request_id'])

    def test_unverified_transfer_stays_hold_without_render_or_storage(self):
        class Hold(ValueError): pass
        class Cursor:
            def fetchall(self): return [(77, 'UID-77', None)]
            def fetchone(self): return ('UID-77',)
        connection = SimpleNamespace(execute=lambda *a: Cursor())
        @contextmanager
        def transaction(): yield connection
        namespace = {
            'db': SimpleNamespace(transaction=transaction),
            'sqlite3': sqlite3,
            'transfer_source_authority': SimpleNamespace(identify_for_submission=mock.Mock(side_effect=Hold('SOURCE VERIFICATION HOLD')),
                                                         SourceAuthorityError=Hold),
            'transfer_contract_authority': SimpleNamespace(
                commercials=mock.Mock(side_effect=Hold('SOURCE VERIFICATION HOLD')),
            ),
            'quote': lambda value: value,
        }
        method = handler_method('transfer_contract_prepare', namespace)
        handler = SimpleNamespace(
            form={'service_fee_currency':'国内银行'}, user={'id':1},
            flash_redirect=lambda *args: args,
        )
        result = method(handler,'S1')
        self.assertIn('/students/S1?tab=contracts#contracts-section', result[0])
        self.assertIn('SOURCE VERIFICATION HOLD', result[1])

    def test_legacy_edit_route_delegates_unbound_transfer_draft_to_authority_flow(self):
        sentinel = object()
        contract = {
            'id': 77, 'student_id': 'S1', 'contract_status': 'Draft',
            'contract_type': 'TransferApplicationContract',
            'application_cycle_id': None, 'standalone_data_json': '{}',
        }
        namespace = {
            'can_generate_contract': lambda user: True,
            'can_manage_transfer_contract': lambda user: True,
            'db': SimpleNamespace(one=lambda *args: contract),
            'snapshot_payload': lambda value: {},
            'is_published_v2_snapshot': lambda value: False,
            'normalize_contract_type': lambda value: value,
        }
        method = handler_method('contract_edit_regenerate', namespace)
        handler = SimpleNamespace(
            user={'id': 1}, form={'consulting_and_visa_service_fee': '2400'},
            transfer_contract_prepare=mock.Mock(return_value=sentinel),
        )

        self.assertIs(sentinel, method(handler, 'POST', 'S1', 77))
        handler.transfer_contract_prepare.assert_called_once_with('S1')
        self.assertEqual('TransferApplicationContract', handler.form['contract_type'])
        self.assertEqual('mother', handler.form['parent_signer_source'])

    def test_pending_transfer_edit_uses_resolved_source_fee_instead_of_2400_default(self):
        panel = method_source('contract_panel')
        self.assertIn('transfer_fee_authority_by_contract', panel)
        self.assertIn('edit_fee_value = pending_fee.get("base_fee", "")', panel)
        self.assertIn('edit_final_fee_value', panel)

    def test_revision_post_delegates_unbound_transfer_to_authority_flow(self):
        sentinel = object()
        contract = {
            'id': 77, 'student_id': 'S1',
            'contract_type': 'TransferApplicationContract',
        }
        namespace = {
            'can_revise_contract': lambda user: True,
            'can_manage_transfer_contract': lambda user: True,
            'db': SimpleNamespace(one=lambda *args: contract, audit=mock.Mock()),
            'normalize_contract_type': lambda value: value,
            'is_unbound_transfer_draft': lambda value: True,
        }
        method = handler_method('contract_revision_assistant', namespace)
        handler = SimpleNamespace(
            user={'id': 1}, form={'consulting_and_visa_service_fee': '2400'},
            transfer_contract_prepare=mock.Mock(return_value=sentinel),
        )

        self.assertIs(sentinel, method(handler, 'POST', 'S1', 77))
        handler.transfer_contract_prepare.assert_called_once_with('S1')
        self.assertEqual('TransferApplicationContract', handler.form['contract_type'])
        self.assertEqual('mother', handler.form['parent_signer_source'])

    def test_revision_page_reads_transfer_fee_from_source_authority(self):
        page = method_source('render_contract_revision_page')
        self.assertIn('pending_transfer = is_unbound_transfer_draft(contract)', page)
        self.assertIn('transfer_contract_authority.commercials(', page)
        self.assertIn('revision_base_fee = format_money(', page)
        self.assertIn('revision_final_fee = format_money(', page)
        self.assertIn('data-contract-fee-authority="source"', page)

    def test_revision_page_uses_contract_type_service_scope_authority(self):
        page = method_source('render_contract_revision_page')
        self.assertIn('current_published_contract_package(', page)
        self.assertIn('published_service_scope_options(revision_scope_package)', page)
        self.assertIn('revision_scope_ready = False', page)
        self.assertIn('revision_submit_disabled', page)
        self.assertNotIn('for value, label in study_contract_service_scope_items()', page)

    def test_published_scope_projection_honours_disabled_label_and_default(self):
        from contract_generation_authority import published_service_scope_options

        package = SimpleNamespace(
            contract_type='TransferApplicationContract',
            snapshot={
                'template_type': 'TransferApplicationContract',
                'services': [
                    {'service_id': 'school_transfer_planning_and_application',
                     'label': 'Published transfer planning', 'enabled': True,
                     'default_selected': False},
                    {'service_id': 'school_information_and_selection_advice',
                     'label': 'Published school advice', 'enabled': True,
                     'default_selected': True},
                    {'service_id': 'document_checklist_guidance_and_document_preparation',
                     'label': 'Disabled document help', 'enabled': False,
                     'default_selected': True},
                ],
            },
        )

        options, defaults = published_service_scope_options(package)

        self.assertEqual(
            options,
            (
                ('school_transfer_planning_and_application', 'Published transfer planning'),
                ('school_information_and_selection_advice', 'Published school advice'),
            ),
        )
        self.assertEqual(defaults, ('school_information_and_selection_advice',))

    def test_transfer_generation_ignores_injected_generic_contract_access_for_staff_and_consultant(self):
        """Transfer generation is role-owned, even if stale generic grants say yes."""
        for role in ("Staff", "Consultant"):
            with self.subTest(role=role):
                generic_access = mock.Mock(return_value=True)
                transfer_access = mock.Mock(return_value=False)
                forbidden = mock.Mock(return_value=("forbidden", 403))
                unsafe_prepare = mock.Mock(return_value="UNSAFE")
                namespace = {
                    "can_generate_contract": generic_access,
                    "can_manage_transfer_contract": transfer_access,
                    "normalize_contract_type": lambda value: value,
                }
                method = handler_method("contract_generate", namespace)
                handler = SimpleNamespace(
                    user={"id": 7, "role_name": role,
                          "permissions_json": '{"contracts.manage": true}',
                          "module_permissions": ["student_contracts"]},
                    form={"contract_type": "TransferApplicationContract"},
                    error_page=forbidden,
                    transfer_contract_prepare=unsafe_prepare,
                )

                self.assertEqual(("forbidden", 403), method(handler, "POST", "S1"))
                transfer_access.assert_called_once_with(handler.user)
                unsafe_prepare.assert_not_called()

    def test_transfer_generation_allows_manager_when_transfer_role_gate_passes(self):
        sentinel = object()
        namespace = {
            "can_generate_contract": mock.Mock(return_value=True),
            "can_manage_transfer_contract": mock.Mock(return_value=True),
            "restrict_contract_creation_for_student_transfer_context": lambda *args: False,
            "normalize_contract_type": lambda value: value,
            "db": SimpleNamespace(
                one=lambda *args: {"student_id": "S1", "nas_status": "ready"},
                connect=lambda: object(), _release_connection=lambda connection: None,
            ),
            "student_higher_education_experience": lambda *args: {"contract_applicable": True},
        }
        method = handler_method("contract_generate", namespace)
        handler = SimpleNamespace(
            user={"id": 1, "role_name": "Manager"},
            form={"contract_type": "TransferApplicationContract"},
            transfer_contract_prepare=mock.Mock(return_value=sentinel),
        )

        self.assertIs(sentinel, method(handler, "POST", "S1"))
        handler.transfer_contract_prepare.assert_called_once_with("S1")

    def test_non_transfer_generation_retains_generic_contract_authorization(self):
        source = method_source("contract_generate")
        transfer_gate = source.index("can_manage_transfer_contract")
        self.assertIn('requested_contract_type == "TransferApplicationContract"', source[:transfer_gate + 80])
        self.assertIn("if student[\"nas_status\"] != \"ready\"", source[transfer_gate:])

    def test_unbound_transfer_edit_denies_staff_with_injected_generic_contract_access(self):
        contract = {
            "id": 77, "student_id": "S1", "contract_status": "Draft",
            "contract_type": "TransferApplicationContract", "application_cycle_id": None,
            "standalone_data_json": "{}",
        }
        generic_access = mock.Mock(return_value=True)
        transfer_access = mock.Mock(return_value=False)
        namespace = {
            "can_generate_contract": generic_access,
            "can_manage_transfer_contract": transfer_access,
            "db": SimpleNamespace(one=lambda *args: contract),
            "snapshot_payload": lambda value: {},
            "is_published_v2_snapshot": lambda value: False,
            "normalize_contract_type": lambda value: value,
        }
        method = handler_method("contract_edit_regenerate", namespace)
        handler = SimpleNamespace(
            user={"id": 7, "role_name": "Staff", "permissions_json": '{"contracts.manage": true}'},
            form={}, error_page=mock.Mock(return_value=("forbidden", 403)),
            transfer_contract_prepare=mock.Mock(return_value="UNSAFE"),
        )

        self.assertEqual(("forbidden", 403), method(handler, "POST", "S1", 77))
        transfer_access.assert_called_once_with(handler.user)
        handler.transfer_contract_prepare.assert_not_called()

    def test_transfer_revision_entry_denies_consultant_with_injected_generic_contract_access(self):
        contract = {"id": 77, "student_id": "S1", "contract_type": "TransferApplicationContract"}
        generic_access = mock.Mock(return_value=True)
        transfer_access = mock.Mock(return_value=False)
        namespace = {
            "can_revise_contract": generic_access,
            "can_manage_transfer_contract": transfer_access,
            "db": SimpleNamespace(one=lambda *args: contract, audit=mock.Mock()),
            "normalize_contract_type": lambda value: value,
            "is_unbound_transfer_draft": lambda value: True,
        }
        method = handler_method("contract_revision_assistant", namespace)
        handler = SimpleNamespace(
            user={"id": 8, "role_name": "Consultant", "permissions_json": '{"contracts.manage": true}'},
            form={}, error_page=mock.Mock(return_value=("forbidden", 403)),
            transfer_contract_prepare=mock.Mock(return_value="UNSAFE"),
        )

        self.assertEqual(("forbidden", 403), method(handler, "POST", "S1", 77))
        transfer_access.assert_called_once_with(handler.user)
        handler.transfer_contract_prepare.assert_not_called()

    def test_transfer_signing_link_action_denies_staff_with_injected_generic_contract_access(self):
        contract = {"id": 77, "student_id": "S1", "contract_type": "TransferApplicationContract"}
        namespace = {
            "can_generate_contract": mock.Mock(return_value=True),
            "can_manage_transfer_contract": mock.Mock(return_value=False),
            "normalize_contract_type": lambda value: value,
            "db": SimpleNamespace(one=lambda *args: contract),
        }
        method = handler_method("contract_signing_link_action", namespace)
        handler = SimpleNamespace(
            user={"id": 7, "role_name": "Staff", "permissions_json": '{"contracts.manage": true}'},
            error_page=mock.Mock(return_value=("forbidden", 403)),
            contract_signing_create_link=mock.Mock(return_value={"signing_url": "UNSAFE"}),
        )

        self.assertEqual(("forbidden", 403), method(handler, "POST", "S1", 77, "generate"))
        handler.contract_signing_create_link.assert_not_called()

    def test_transfer_signed_upload_and_status_update_deny_staff_before_file_or_state_mutation(self):
        transfer_contract = {"id": 77, "student_id": "S1", "contract_type": "TransferApplicationContract"}
        for handler_name in ("contract_center_signed_upload", "contract_center_status_update"):
            with self.subTest(handler_name=handler_name):
                transfer_access = mock.Mock(return_value=False)
                signable_gate = mock.Mock(side_effect=AssertionError("unsafe Transfer contract action"))
                namespace = {
                    "can_create_contract_center": mock.Mock(return_value=True),
                    "can_manage_transfer_contract": transfer_access,
                    "normalize_contract_type": lambda value: value,
                    "db": SimpleNamespace(one=lambda *args: transfer_contract),
                    "require_transfer_contract_signable": signable_gate,
                }
                method = handler_method(handler_name, namespace)
                handler = SimpleNamespace(
                    user={"id": 7, "role_name": "Staff", "permissions_json": '{"contracts.manage": true}'},
                    form={"contract_status": "FullyExecuted"},
                    error_page=mock.Mock(return_value=("forbidden", 403)),
                )

                self.assertEqual(("forbidden", 403), method(handler, "POST", 77))
                transfer_access.assert_called_once_with(handler.user)
                signable_gate.assert_not_called()

    def test_transfer_stamp_denies_staff_even_when_generic_stamp_permission_is_injected(self):
        transfer_contract = {"id": 77, "student_id": "S1", "contract_type": "TransferApplicationContract"}
        transfer_access = mock.Mock(return_value=False)
        signable_gate = mock.Mock(side_effect=AssertionError("unsafe Transfer stamp"))
        namespace = {
            "can_apply_aaie_stamp": mock.Mock(return_value=True),
            "can_manage_transfer_contract": transfer_access,
            "normalize_contract_type": lambda value: value,
            "db": SimpleNamespace(one=lambda *args: transfer_contract),
            "require_transfer_contract_signable": signable_gate,
        }
        method = direct_handler_method("contract_center_apply_aaie_stamp", namespace)
        handler = SimpleNamespace(
            user={"id": 7, "role_name": "Staff", "permissions_json": '{"contracts.manage": true}'},
            require=mock.Mock(return_value=True),
            error_page=mock.Mock(return_value=("forbidden", 403)),
        )

        self.assertEqual(("forbidden", 403), method(handler, "POST", 77))
        transfer_access.assert_called_once_with(handler.user)
        signable_gate.assert_not_called()

    def test_parent_public_signing_routes_remain_public_token_flows(self):
        self.assertNotIn("can_manage_transfer_contract", method_source("contract_signing_portal"))
        self.assertNotIn("can_manage_transfer_contract", method_source("contract_signing_submit"))

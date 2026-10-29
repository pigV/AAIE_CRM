import sqlite3
import unittest

from visa_document_type_service import (
    InvalidVisaDocumentType,
    VISA_DOCUMENT_TYPE_DEFINITIONS,
    VisaDocumentNotFound,
    visa_document_type_definition,
    update_existing_visa_document_type,
)


class PathChangingConnection(sqlite3.Connection):
    """Move the selected row immediately before its document-type update."""

    def execute(self, sql, parameters=(), /):
        normalized_sql = " ".join(str(sql).split()).lower()
        if (
            getattr(self, "move_before_document_type_update", False)
            and normalized_sql.startswith(
                "update student_documents set document_type="
            )
        ):
            self.move_before_document_type_update = False
            sqlite3.Connection.execute(
                self,
                """UPDATE student_documents SET file_path=?
                   WHERE student_id=? AND file_path=?""",
                (
                    "/nas/student-a/reassigned.pdf",
                    "STUDENT-A",
                    "/nas/student-a/visa.pdf",
                ),
            )
        return sqlite3.Connection.execute(self, sql, parameters)


class VisaDocumentTypeCatalogTests(unittest.TestCase):
    def test_catalog_round_trips_form_and_stored_values(self):
        for definition in VISA_DOCUMENT_TYPE_DEFINITIONS:
            with self.subTest(value=definition.form_value):
                self.assertEqual(
                    visa_document_type_definition(definition.form_value, strict=True),
                    definition,
                )
                self.assertEqual(
                    visa_document_type_definition(definition.document_type),
                    definition,
                )

    def test_legacy_visafile_reads_as_other_but_new_other_is_standard(self):
        definition = visa_document_type_definition("VisaFile")
        self.assertEqual(definition.form_value, "other")
        self.assertEqual(definition.document_type, "VisaOther")

    def test_strict_save_rejects_unknown_missing_and_blank_values(self):
        for value in (None, "", "unknown_type", "VisaFileTypo"):
            with self.subTest(value=value):
                with self.assertRaises(InvalidVisaDocumentType):
                    visa_document_type_definition(value, strict=True)


class VisaDocumentTypePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE student_documents(
               id INTEGER PRIMARY KEY,
               student_id TEXT NOT NULL,
               file_path TEXT NOT NULL,
               document_type TEXT NOT NULL,
               uploaded_at TEXT NOT NULL
            )"""
        )

    def tearDown(self):
        self.conn.close()

    def _insert_document(
        self,
        *,
        student_id="STUDENT-A",
        file_path="/nas/student-a/visa.pdf",
        document_type="VisaGrantLetter",
        uploaded_at="2026-08-05T10:00:00+10:00",
    ):
        cursor = self.conn.execute(
            """INSERT INTO student_documents(
                   student_id,file_path,document_type,uploaded_at
               ) VALUES(?,?,?,?)""",
            (student_id, file_path, document_type, uploaded_at),
        )
        return cursor.lastrowid

    def _document_rows(self):
        return tuple(
            tuple(row)
            for row in self.conn.execute(
                """SELECT id,student_id,file_path,document_type,uploaded_at
                   FROM student_documents ORDER BY id"""
            )
        )

    def test_updates_exact_existing_document_and_returns_persistence_result(self):
        self._insert_document()

        result = update_existing_visa_document_type(
            self.conn,
            student_id="STUDENT-A",
            file_path="/nas/student-a/visa.pdf",
            requested_value="s56_request",
        )

        self.assertEqual(result.old_document_type, "VisaGrantLetter")
        self.assertEqual(result.new_document_type, "VisaS56Request")
        self.assertTrue(result.changed)
        self.assertEqual(
            self.conn.execute(
                "SELECT document_type FROM student_documents WHERE id=?",
                (result.document_id,),
            ).fetchone()[0],
            "VisaS56Request",
        )

    def test_same_type_save_returns_changed_false(self):
        document_id = self._insert_document(document_type="VisaS56Request")

        result = update_existing_visa_document_type(
            self.conn,
            student_id="STUDENT-A",
            file_path="/nas/student-a/visa.pdf",
            requested_value="s56_request",
        )

        self.assertEqual(result.document_id, document_id)
        self.assertEqual(result.old_document_type, "VisaS56Request")
        self.assertEqual(result.new_document_type, "VisaS56Request")
        self.assertFalse(result.changed)

    def test_exact_student_and_path_do_not_update_other_student(self):
        target_id = self._insert_document()
        same_path_other_student_id = self._insert_document(student_id="STUDENT-B")
        other_path_same_student_id = self._insert_document(
            file_path="/isolated/student-a/visa.pdf"
        )

        result = update_existing_visa_document_type(
            self.conn,
            student_id="STUDENT-A",
            file_path="/nas/student-a/visa.pdf",
            requested_value="s56_request",
        )

        self.assertEqual(result.document_id, target_id)
        rows_by_id = {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT id,document_type FROM student_documents"
            )
        }
        self.assertEqual(rows_by_id[target_id], "VisaS56Request")
        self.assertEqual(rows_by_id[same_path_other_student_id], "VisaGrantLetter")
        self.assertEqual(rows_by_id[other_path_same_student_id], "VisaGrantLetter")

    def test_duplicate_path_updates_newest_uploaded_row_only(self):
        older_id = self._insert_document(uploaded_at="2026-08-04T10:00:00+10:00")
        newest_id = self._insert_document(uploaded_at="2026-08-05T10:00:00+10:00")

        result = update_existing_visa_document_type(
            self.conn,
            student_id="STUDENT-A",
            file_path="/nas/student-a/visa.pdf",
            requested_value="s56_request",
        )

        self.assertEqual(result.document_id, newest_id)
        rows_by_id = {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT id,document_type FROM student_documents"
            )
        }
        self.assertEqual(rows_by_id[older_id], "VisaGrantLetter")
        self.assertEqual(rows_by_id[newest_id], "VisaS56Request")

    def test_missing_exact_document_raises_without_writes(self):
        self._insert_document(file_path="/isolated/student-a/visa.pdf")
        before = self._document_rows()

        with self.assertRaises(VisaDocumentNotFound):
            update_existing_visa_document_type(
                self.conn,
                student_id="STUDENT-A",
                file_path="/nas/student-a/visa.pdf",
                requested_value="s56_request",
            )

        self.assertEqual(self._document_rows(), before)

    def test_invalid_requested_value_raises_without_writes(self):
        self._insert_document()
        before = self._document_rows()

        with self.assertRaises(InvalidVisaDocumentType):
            update_existing_visa_document_type(
                self.conn,
                student_id="STUDENT-A",
                file_path="/nas/student-a/visa.pdf",
                requested_value="forged_type",
            )

        self.assertEqual(self._document_rows(), before)

    def test_path_change_between_lookup_and_update_raises_without_type_write(self):
        conn = sqlite3.connect(":memory:", factory=PathChangingConnection)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """CREATE TABLE student_documents(
                   id INTEGER PRIMARY KEY,
                   student_id TEXT NOT NULL,
                   file_path TEXT NOT NULL,
                   document_type TEXT NOT NULL,
                   uploaded_at TEXT NOT NULL
                )"""
            )
            document_id = conn.execute(
                """INSERT INTO student_documents(
                       student_id,file_path,document_type,uploaded_at
                   ) VALUES(?,?,?,?)""",
                (
                    "STUDENT-A",
                    "/nas/student-a/visa.pdf",
                    "VisaGrantLetter",
                    "2026-08-05T10:00:00+10:00",
                ),
            ).lastrowid
            conn.move_before_document_type_update = True

            with self.assertRaises(VisaDocumentNotFound):
                update_existing_visa_document_type(
                    conn,
                    student_id="STUDENT-A",
                    file_path="/nas/student-a/visa.pdf",
                    requested_value="s56_request",
                )

            row = conn.execute(
                """SELECT file_path,document_type FROM student_documents
                   WHERE id=?""",
                (document_id,),
            ).fetchone()
            self.assertEqual(row["file_path"], "/nas/student-a/reassigned.pdf")
            self.assertEqual(row["document_type"], "VisaGrantLetter")
        finally:
            conn.close()

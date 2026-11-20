import unittest
from dataclasses import FrozenInstanceError

from business_changes.contracts import Change, Subject
from business_changes.registry import Handler, Registry


class ChangeRegistryTests(unittest.TestCase):
    def test_change_is_immutable_and_deduplicates_explicit_affected_subjects(self):
        source=Subject('document','10');old=Subject('application_cycle','7');new=Subject('application_cycle','8')
        change=Change('document.reassigned',source,3,(old,new,old))
        self.assertEqual((old,new),change.affected_subjects)
        with self.assertRaises(FrozenInstanceError):change.source_version=4

    def test_no_implicit_other_student_or_cycle_is_added(self):
        registry=Registry([Handler('workflow','1',frozenset({'document.reviewed'}),frozenset({'application_cycle'}))])
        change=Change('document.reviewed',Subject('document','10'),1,(Subject('application_cycle','7'),))
        plan=registry.plan(change)
        self.assertEqual([Subject('application_cycle','7')],[x.subject for x in plan])

    def test_source_itself_can_be_a_target_without_double_processing(self):
        source=Subject('invoice','10')
        registry=Registry([Handler('invoice_view','1',frozenset({'invoice.updated'}),frozenset({'invoice'}))])
        self.assertEqual(1,len(registry.plan(Change('invoice.updated',source,1,(source,)))))

    def test_unrelated_event_produces_no_work(self):
        registry=Registry([Handler('workflow','1',frozenset({'document.reviewed'}),frozenset({'application_cycle'}))])
        self.assertEqual((),registry.plan(Change('invoice.updated',Subject('invoice','10'),1,())))

    def test_dependencies_are_ordered_before_dependents(self):
        handlers=[Handler('dashboard','1',frozenset({'document.reviewed'}),frozenset({'application_cycle'}),frozenset({'workflow'})),Handler('workflow','1',frozenset({'document.reviewed'}),frozenset({'application_cycle'}))]
        plan=Registry(handlers).plan(Change('document.reviewed',Subject('document','10'),1,(Subject('application_cycle','7'),)))
        self.assertEqual(['workflow','dashboard'],[x.handler_name for x in plan])

    def test_cycle_in_handlers_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'cycle'):
            Registry([Handler('a','1',frozenset({'x'}),frozenset({'student'}),frozenset({'b'})),Handler('b','1',frozenset({'x'}),frozenset({'student'}),frozenset({'a'}))])

    def test_duplicate_handler_and_unknown_dependency_are_rejected(self):
        h=Handler('a','1',frozenset({'x'}),frozenset({'student'}))
        with self.assertRaisesRegex(ValueError,'duplicate'):Registry([h,h])
        with self.assertRaisesRegex(ValueError,'unknown'):Registry([Handler('b','1',h.event_types,h.subject_kinds,frozenset({'missing'}))])

    def test_invalid_source_version_and_subject_are_rejected(self):
        for version in (0,-1,True,'1'):
            with self.subTest(version=version),self.assertRaises(ValueError):Change('document.reviewed',Subject('document','10'),version,())
        for kind,identifier in [('','1'),('Student','1'),('student',''),('student',None)]:
            with self.subTest(kind=kind),self.assertRaises(ValueError):Subject(kind,identifier)


if __name__=='__main__':unittest.main()

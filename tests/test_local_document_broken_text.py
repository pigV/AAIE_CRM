"""Parser configuration must replace broken text page by page, offline."""
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase, mock

from local_document_lab.parse import parse_document


class BrokenTextParserTests(TestCase):
    def test_native_broken_text_replacement_is_enabled_without_global_full_page_ocr(self):
        options = SimpleNamespace(replace_broken_text_layer=False, mode='default')
        pipeline = SimpleNamespace()
        converted = object()
        converter = mock.Mock()
        converter.convert.return_value = SimpleNamespace(document=converted)
        modules = {}
        def module(name, **members):
            value = ModuleType(name)
            value.__dict__.update(members)
            modules[name] = value
        module('docling.document_converter', DocumentConverter=mock.Mock(return_value=converter),
               PdfFormatOption=lambda **kw: SimpleNamespace(**kw))
        module('docling.datamodel.base_models', InputFormat=SimpleNamespace(PDF='pdf'))
        module('docling.datamodel.pipeline_options',
               PdfPipelineOptions=mock.Mock(return_value=pipeline),
               OcrMacOptions=lambda **kw: SimpleNamespace(**{**vars(options), **kw}))
        module('docling.datamodel.accelerator_options', AcceleratorOptions=lambda **kw: SimpleNamespace(**kw),
               AcceleratorDevice=SimpleNamespace(CPU='cpu'))
        with mock.patch.dict('sys.modules', modules), mock.patch('local_document_lab.import_cache.install'):
            result = parse_document(Path('/synthetic/document.pdf'), Path('/synthetic/models'), page_end=6)
        self.assertIs(result, converted)
        self.assertTrue(pipeline.ocr_options.replace_broken_text_layer)
        self.assertEqual(pipeline.ocr_options.mode, 'default')
        self.assertEqual(pipeline.ocr_options.lang, ['en-US'])
        converter.convert.assert_called_once_with(Path('/synthetic/document.pdf'), page_range=(1, 6))
        modules['docling.datamodel.pipeline_options'].PdfPipelineOptions.assert_called_once_with(
            artifacts_path=Path('/synthetic/models'), enable_remote_services=False)

class BrokenTextPlanTests(TestCase):
    def test_raw_control_font_is_detected_before_backend_numeric_conversion(self):
        from local_document_lab import parse
        corrupt=('\x04\x05 ABcd \x06 XYzt \x07 ' * 80)
        self.assertEqual(parse.broken_text_pages(['Normal school details.',corrupt,'']), [2])

    def test_normal_scripts_dates_and_unmapped_bullets_do_not_force_full_page_ocr(self):
        from local_document_lab import parse
        normal='School: École 学校 مدرسة\nStart Date: 27/01/2027\n'
        bullets='\ue000 Normal readable bullet sentence with dates 2027.\n'*40
        self.assertEqual(parse.broken_text_pages([normal,bullets,'']), [])

    def test_garbled_late_page_stays_in_layout_prefix(self):
        from local_document_lab import parse
        legal='These are the ordinary terms and conditions with no dated facts. '*30
        corrupt=('\x04 ABcd \x05 XYzt \x06 ' * 100)
        self.assertEqual(parse.layout_page_end(['Cover','Intro','Course']+[legal]*5+[corrupt]),9)

    def test_only_bad_pages_get_forced_ocr_and_all_original_pages_remain(self):
        from local_document_lab import parse
        self.assertEqual(parse.layout_segments(9,[1,3,4,5,9]),[(1,1,True),(2,2,False),(3,5,True),(6,8,False),(9,9,True)])
        self.assertEqual(parse.layout_segments(3,[]),[(1,3,False)])

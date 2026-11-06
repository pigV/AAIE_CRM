"""Parse only local PDF paths, retaining layout and source coordinates."""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import re
from collections import Counter
from local_document_lab.evidence import dates_in
import time
import unicodedata


def parser_signature():
    root=Path(__file__).resolve().parents[1]
    digest=hashlib.sha256(Path(__file__).read_bytes())
    for path in (root/'STAGING_DATA/environment.lock',root/'STAGING_DATA/models/docling-artifacts.json'):
        if path.exists():digest.update(path.read_bytes())
    return digest.hexdigest()



def broken_text_pages(page_texts):
    """Inspect original PDF text before a backend turns unmapped glyphs into digits."""
    def junk(char):
        category = unicodedata.category(char)
        return char == '\ufffd' or category in ('Cs', 'Co') or (category == 'Cc' and not char.isspace())
    bad = []
    for number, text in enumerate(page_texts, 1):
        lines = []
        for line in text.splitlines():
            line = line.lstrip()
            # An isolated icon/bullet does not make a readable paragraph corrupt.
            if line and junk(line[0]) and (len(line) == 1 or line[1].isspace()):
                line = line[1:]
            lines.append(line)
        chars = [char for line in lines for char in line if not char.isspace()]
        if len(chars) >= 20 and sum(junk(char) for char in chars) >= .03 * len(chars):
            bad.append(number)
    return bad


def layout_segments(page_end, broken_pages):
    """Cover the layout prefix exactly once, forcing OCR only for suspect pages."""
    if type(page_end) is not int or page_end < 0:
        raise ValueError('Invalid layout page range')
    bad = set(broken_pages)
    if any(type(page) is not int or not 1 <= page <= page_end for page in bad):
        raise ValueError('Broken page outside layout range')
    segments = []
    for page in range(1, page_end + 1):
        forced = page in bad
        if segments and segments[-1][2] == forced:
            start, _, state = segments[-1]
            segments[-1] = (start, page, state)
        else:
            segments.append((page, page, forced))
    return segments


def layout_page_end(page_texts):
    """Keep layout/OCR for facts and unreadable pages; preserve all tail text separately."""
    if not page_texts:return 0
    per_page=[dates_in(' '.join(text.split())) for text in page_texts]
    counts=Counter(date for values in per_page for date in values)
    repeated={date for date,n in counts.items() if n>=3 and n>len(page_texts)/2}
    end=max([min(3,len(page_texts)), *broken_text_pages(page_texts)])
    heading=re.compile(r'^\s*(?:(?:[A-Z]|\d+)[.)]?\s+)?(?:course details|program summary|enrolment details|welfare details|provider details|program name(?: and location)?)\s*:?[ ]*$',re.I|re.M)
    labels=re.compile(r'^\s*(?:school|school name|english language centre|course start date|course end date|start date|finish date)\s*:',re.I|re.M)
    for index,text in enumerate(page_texts[end:],end):
        words=re.findall(r'[A-Za-z]{2,}',text)
        unreadable=len(text.strip())<80 or len(words)<8
        if unreadable or heading.search(text) or labels.search(text) or per_page[index]-repeated:
            end=index+1
    return end


def parse_document(path, artifacts, page_end=None, *, page_start=1, force_ocr=False):
    from local_document_lab.import_cache import install
    install()
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, OcrMacOptions
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    options = PdfPipelineOptions(artifacts_path=artifacts,enable_remote_services=False)
    options.ocr_options = OcrMacOptions(lang=['en-US'], replace_broken_text_layer=True)
    if force_ocr:
        from docling.datamodel.pipeline_options import OcrMode
        options.ocr_options.mode = OcrMode.FULL_PAGE
    options.do_table_structure = True
    options.accelerator_options = AcceleratorOptions(num_threads=2,device=AcceleratorDevice.CPU)
    converter = DocumentConverter(format_options={InputFormat.PDF:PdfFormatOption(pipeline_options=options)})
    return converter.convert(Path(path), **({"page_range":(page_start,page_end)} if page_end else {})).document


def packet_from_document(document, key, kind):
    blocks = []
    for index,(item,level) in enumerate(document.iterate_items()):
        text = getattr(item,'text','')
        label = str(getattr(item,'label',''))
        if 'table' in label.casefold() and hasattr(item,'export_to_markdown'):
            text = item.export_to_markdown(doc=document)
        if not text or not text.strip():
            continue
        provenance=getattr(item,'prov',[])
        pages=sorted({prov.page_no for prov in provenance})
        prov=provenance[0] if len(pages)==1 else None
        bbox=prov.bbox.model_dump(mode='json') if prov and prov.bbox else None
        source = {'id':f'b{index}', 'kind':label, 'text':text,
                  'page':prov.page_no if prov else None, 'bbox':bbox, 'page_candidates':pages}
        blocks.append(source)
        if 'table' in label.casefold() and getattr(item,'data',None):
            for cell_index,cell in enumerate(item.data.table_cells):
                if cell.text.strip():
                    blocks.append({'id':f'b{index}-c{cell_index}','kind':'table_cell','text':cell.text,
                        'page':source['page'],'page_candidates':pages,
                        'bbox':(cell.bbox.model_dump(mode='json') if cell.bbox else bbox) if prov else None,
                        'table_id':source['id'],'row':cell.start_row_offset_idx,'column':cell.start_col_offset_idx})
    return {'id':key,'kind':kind,'blocks':blocks,'page_count':len(document.pages)}


def main():
    parser=argparse.ArgumentParser()
    for arg in ('input','id','kind','artifacts','output'):parser.add_argument('--'+arg,required=True)
    args=parser.parse_args()
    source=Path(args.input)
    if not source.is_file() or source.suffix.lower()!='.pdf':raise ValueError('Local PDF required')
    if source.stat().st_size>30*1024**2:raise ValueError('Prototype PDF size limit: 30 MiB')
    import pypdfium2 as pdfium
    page_texts=[];sizes=[]
    with pdfium.PdfDocument(source) as pdf:
        if len(pdf)>30:raise ValueError('Prototype PDF page limit: 30')
        for index in range(len(pdf)):
            page=pdf[index];textpage=page.get_textpage()
            page_texts.append(textpage.get_text_range());sizes.append(page.get_size())
            textpage.close();page.close()
    page_end=layout_page_end(page_texts)
    started=time.monotonic()
    broken_pages=broken_text_pages(page_texts)
    segments=layout_segments(page_end,broken_pages)
    packet={'id':args.id,'kind':args.kind,'blocks':[]}
    for first,last,forced in segments:
        document=parse_document(source,Path(args.artifacts),last,page_start=first,force_ocr=forced)
        part=packet_from_document(document,args.id,args.kind)
        prefix=f'p{first}-' if len(segments)>1 else ''
        for block in part['blocks']:
            pages=block.get('page_candidates') or ([block['page']] if block.get('page') else [])
            if any(not first <= page <= last for page in pages):
                raise ValueError('Parser changed original page identity')
            block['id']=prefix+block['id']
            if block.get('table_id'):
                block['table_id']=prefix+block['table_id']
            packet['blocks'].append(block)
    # No original text is discarded. Trailing text-only pages retain real page provenance.
    for index in range(page_end,len(page_texts)):
        width,height=sizes[index]
        packet['blocks'].append({'id':f'pdf-page-{index+1}','kind':'pdf_text_page','text':page_texts[index],
             'page':index+1,'bbox':{'l':0,'t':0,'r':width,'b':height,'coord_origin':'TOPLEFT'},
             'bbox_precision':'page','page_candidates':[index+1]})
    packet.update(forced_ocr_pages=broken_pages,layout_segments=segments,page_count=len(page_texts),layout_pages=list(range(1,page_end+1)),
                  text_layer_only_pages=list(range(page_end+1,len(page_texts)+1)),
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),parser_signature=parser_signature(),
                  parse_seconds=time.monotonic()-started,rss_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    Path(args.output).write_text(json.dumps(packet,ensure_ascii=False,indent=2))
    print(json.dumps({k:packet[k] for k in ('id','page_count','parse_seconds','rss_peak_bytes')}),flush=True)

if __name__=='__main__':main()

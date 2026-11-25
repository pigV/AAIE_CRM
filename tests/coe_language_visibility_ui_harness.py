"""Browser harness using real CoE renderer, scripts and styles; no CRM writes."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from revenue.school_courses import render_course_fields
FIELDS=render_course_fields(0,[dict(id=1,school_name='TEST Public School',sector_key='public',state='VIC'),dict(id=2,school_name='TEST Private Language School',sector_key='private',state='VIC')],offers=[dict(id=1,school_name='TEST Accepted Offer')],show_submit=False)
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  path=urlsplit(self.path)
  if path.path.startswith('/assets/'):
   p=ROOT/path.path.lstrip('/')
   if p.name not in {'style.css','ui-premium-v2.css','student-intake-tuition.js','course-school-search.js'}:return self.send_error(404)
   body=p.read_bytes();kind='text/css' if p.suffix=='.css' else 'text/javascript'
  elif path.path=='/':
   premium=parse_qs(path.query).get('theme',['premium'])[0]=='premium'
   body=('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CoE 隔离显隐验收</title><link rel="stylesheet" href="/assets/style.css">'''+('<link rel="stylesheet" href="/assets/ui-premium-v2.css">' if premium else '')+'''</head><body'''+(' class="ui-premium-v2"' if premium else '')+'''><main style="max-width:740px;margin:20px auto;padding:16px"><h1>CoE 隔离显隐验收</h1><p>只读浏览器测试样本，不写CRM。</p><section class="upload-review upload-review-page"><form><article data-upload-review-workbench><label>Document Type<select data-document-type-select><option value="COE">CoE</option></select></label>'''+FIELDS+'''</article><button type="button" onclick="document.querySelector('#validity').textContent=this.form.checkValidity()?'表单校验通过':'请完成必填字段'">检查表单</button><output id="validity"></output></form></section></main><script src="/assets/student-intake-tuition.js" defer></script></body></html>''').encode();kind='text/html; charset=utf-8'
  else:return self.send_error(404)
  self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
if __name__=='__main__':ThreadingHTTPServer(('127.0.0.1',19502),Handler).serve_forever()

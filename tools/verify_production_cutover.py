"""Read-oriented cutover acceptance; uses the existing account without exposing it."""
import argparse
from contextlib import closing
import http.cookiejar
import json
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from runtime_configuration import RuntimeConfiguration
from tools.runtime_stack import status


def verify(config,closed=False):
    report={'stack':status(config),'closed_checks':closed,'http':[]}
    assert report['stack']['ready']
    jar=http.cookiejar.CookieJar()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar))
    base='http://127.0.0.1:'+str(config.admin_port)
    def request(path, data=None, expected=200, parent=False):
        url=('http://127.0.0.1:'+str(config.parent_port) if parent else base)+path
        try:
            response=opener.open(urllib.request.Request(url,data=data),timeout=45)
        except urllib.error.HTTPError as exc:
            response=exc
        with response:
            body=response.read()
            report['http'].append(dict(path=path,parent=parent,status=response.status,bytes=len(body)))
            assert response.status==expected,(path,response.status)
            if path not in ('/login','/') and expected==200:
                assert '/login' not in response.url,(path,'unexpected_login_redirect')
            return body
    request('/health');request('/health',parent=True)
    secret=Path('/Users/faqiang/Downloads/AAIE_CRM_Test_5985902_20261030/STAGING_DATA/TEST_LOGIN.txt').read_text()
    username=re.search(r'用户名：([^\n]+)',secret).group(1)
    password=re.search(r'密码：([^\n]+)',secret).group(1)
    request('/login',urllib.parse.urlencode({'username':username,'password':password}).encode())
    assert any(c.name==config.namespace+'aaie_session' for c in jar),'Login did not issue session'
    request('/students')
    with closing(sqlite3.connect(config.db.as_uri()+'?mode=ro',uri=True)) as db:
        student=db.execute('SELECT student_id FROM students WHERE deleted_at IS NULL ORDER BY student_id LIMIT 1').fetchone()[0]
        request('/students/'+str(student))
        if closed:
            request('/students',b'cutover_probe=not_a_business_submission',expected=503)
            report['business_write_blocked']=True
        else:
            request('/contracts');request('/school-plan-reports');request('/agent-reach')
            docs=db.execute("SELECT id,file_path FROM student_documents WHERE is_active=1 AND file_path LIKE ? LIMIT 5",(str(config.root)+'/%',)).fetchall()
            downloaded=0
            for doc_id,path in docs:
                if Path(path).is_file():
                    assert request(f'/documents/{doc_id}/download')==Path(path).read_bytes()
                    downloaded+=1
            assert downloaded,'No real documents checked'
            reports=db.execute("SELECT id,file_path FROM school_plan_reports WHERE file_path LIKE ? LIMIT 2",(str(config.root)+'/%',)).fetchall()
            for report_id,path in reports:
                assert request(f'/school-plan-reports/{report_id}/download')==Path(path).read_bytes()
            assert reports,'No report checked'
            contracts=db.execute("SELECT id,contract_file_path FROM contracts WHERE contract_file_path LIKE ? LIMIT 1",(str(config.root)+'/%',)).fetchall()
            for contract_id,path in contracts:
                assert request(f'/contracts/{contract_id}/download/draft')==Path(path).read_bytes()
            assert contracts,'No contract checked'
    request('/',parent=True,expected=404)
    request('/parent-form/cutover-invalid-link',parent=True,expected=503 if closed else 410)
    dest=config.root/'reports'/('cutover-closed-verification.json' if closed else 'cutover-business-verification.json')
    dest.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--closed',action='store_true')
    args=parser.parse_args()
    verify(RuntimeConfiguration.load(args.config,source_root=SOURCE),args.closed)

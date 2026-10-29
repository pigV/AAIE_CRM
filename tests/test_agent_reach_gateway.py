"""Finite Foundation gateway contract. All transport is mocked, no live socket."""
import importlib
import json
import socket
import unittest
from unittest.mock import patch

try:
    gateway = importlib.import_module('agent_reach_gateway')
except ModuleNotFoundError:
    gateway = None


class GatewayModuleGate(unittest.TestCase):
    def test_gateway_implementation_exists(self):
        self.assertIsNotNone(gateway, 'Finite Foundation gateway is not implemented')


class Response:
    def __init__(self, payload=None, *, status=200, body=None, content_type='application/json', encoding=None):
        self.status=status
        self.body=body if body is not None else json.dumps(payload).encode()
        self.headers={'Content-Type':content_type,'Content-Length':str(len(self.body))}
        if encoding:self.headers['Content-Encoding']=encoding
        self.closed=False
    def getheader(self,name,default=None):return self.headers.get(name,default)
    def read(self,amount):return self.body[:amount]
    def close(self):self.closed=True


class Connection:
    def __init__(self,response,error=None):self.response=response;self.error=error;self.requests=[];self.closed=False
    def request(self,method,path,body=None,headers=None):
        self.requests.append((method,path,body,headers))
        if self.error:raise self.error
    def getresponse(self):return self.response
    def close(self):self.closed=True


def run_payload(**updates):
    value={'run_id':'20260908-120000-fixture','source':'opencli','keyword':'测试',
        'status':'completed','started_at':'2026-09-08T12:00:00+10:00','finished_at':'2026-09-08T12:00:01+10:00',
        'raw_count':3,'cleaned_count':3,'unique_count':2}
    value.update(updates);return value


def trend_run(**updates):
    value=run_payload(source='trendradar',requested_platforms=['weibo','zhihu'],successful_platforms=['weibo','zhihu'],failed_platforms=[])
    value.pop('keyword');value.update(updates);return value


@unittest.skipIf(gateway is None,'RED: gateway module not implemented')
class FoundationGatewayTests(unittest.TestCase):
    def setUp(self):
        # Unexpected networking is blocked independently of HTTPConnection mocks.
        guard=patch('socket.create_connection',side_effect=AssertionError('live network forbidden'))
        self.blocked_socket=guard.start();self.addCleanup(guard.stop)
        self.addCleanup(lambda:self.blocked_socket.assert_not_called())

    def invoke(self,operation,payload=None,*,response=None,error=None):
        conn=Connection(response or Response({'items':[]}),error)
        with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=conn) as factory:
            result=gateway.FoundationGateway().request(operation,payload)
        self.assertEqual(factory.call_args.args,('127.0.0.1',8790))
        self.assertEqual(len(conn.requests),1);self.assertTrue(conn.closed)
        return result,conn

    def assert_local_rejection(self,operation,payload=None):
        with patch('agent_reach_gateway.http.client.HTTPConnection') as factory:
            with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request(operation,payload)
        factory.assert_not_called()

    def test_search_submit_fixed_path_one_request(self):
        data,conn=self.invoke('search_submit',{'keyword':' 测试 '},response=Response(run_payload(),status=201))
        method,path,body,headers=conn.requests[0]
        self.assertEqual((method,path),('POST','/api/runs'))
        self.assertEqual(json.loads(body),{'keyword':'测试'})
        self.assertEqual(data['status_label'],'已完成')
        self.assertEqual(data['unique_count'],2)
        self.assertEqual(set(headers),{'Accept','Content-Type'})

    def test_trends_submit_exact_two_platforms(self):
        data,conn=self.invoke('trends_submit',response=Response(trend_run(),status=201))
        self.assertEqual(json.loads(conn.requests[0][2]),{'platforms':['weibo','zhihu']})
        self.assertEqual(data['successful_platforms'],['weibo','zhihu'])

    def test_trends_partial_is_clear(self):
        data,_=self.invoke('trends_submit',response=Response(trend_run(status='partial',successful_platforms=['zhihu'],failed_platforms=['weibo']),status=207))
        self.assertEqual(data['status_label'],'部分完成')

    def test_four_history_operations_fixed_path(self):
        for operation,path in [('search_runs','/api/runs'),('search_results','/api/results'),('trends_runs','/api/trend-runs'),('trends_results','/api/trends')]:
            with self.subTest(operation=operation):
                data,conn=self.invoke(operation)
                self.assertEqual(data,{'items':[]})
                self.assertEqual(conn.requests[0][:2],('GET',path))
                self.assertIsNone(conn.requests[0][2])

    def test_unknown_and_deep_operations_rejected_before_connection(self):
        for operation in ('detail','comments','query_self','session_check','login','health','sources','http://localhost/admin',None,{},[]):
            with self.subTest(kind=type(operation).__name__):self.assert_local_rejection(operation)

    def test_search_payload_rejects_missing_extra_keys_and_wrong_type(self):
        for value in (None,{},[],{'keyword':1},{'keyword':True},{'keyword':''},{'keyword':'x'*121},{'keyword':'x','url':'x'},{'keyword':'x','cookies':'x'},{'keyword':'x','operation':'detail'}):
            self.assert_local_rejection('search_submit',value)

    def test_search_rejects_url_and_control_characters(self):
        for keyword in ('https://example.invalid/path','http://127.0.0.1:1234','//host/path','file:///private/x','x\r\ny','x\x00y'):
            self.assert_local_rejection('search_submit',{'keyword':keyword})

    def test_trends_rejects_platform_expansion_and_transport_options(self):
        for payload in ({'platforms':['weibo']},{'platforms':['zhihu','weibo']},{'platforms':['weibo','zhihu','baidu']},{'timeout':200},{'url':'x'},{'smoke_policy':'x'}):
            self.assert_local_rejection('trends_submit',payload)

    def test_read_operations_reject_payload_or_url_override(self):
        self.assert_local_rejection('search_runs',{'url':'x'})
        self.assert_local_rejection('trends_results',{'platform':'other'})

    def test_redirect_never_followed(self):
        for status in (301,302,303,307,308):
            conn=Connection(Response({},status=status));conn.response.headers['Location']='https://example.invalid/private'
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=conn):
                with self.assertRaises(gateway.GatewayError) as cm:gateway.FoundationGateway().request('search_runs')
            self.assertEqual(len(conn.requests),1);self.assertTrue(conn.closed)
            self.assertEqual(cm.exception.code,'backend_response_invalid')
            self.assertNotIn('example',cm.exception.message)

    def test_transport_error_has_no_retry_or_raw_exception(self):
        conn=Connection(Response({}),error=TimeoutError('synthetic_cookie=private'))
        with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=conn):
            with self.assertRaises(gateway.GatewayError) as cm:gateway.FoundationGateway().request('search_submit',{'keyword':'测试'})
        self.assertEqual(len(conn.requests),1);self.assertTrue(conn.closed)
        self.assertNotIn('private',str(cm.exception));self.assertIsNone(cm.exception.__context__)

    def test_backend_errors_drop_body_and_headers(self):
        for status in (400,401,403,409,429,500,502,503):
            conn=Connection(Response({'error':'private_internal','message':'synthetic_token=private'},status=status))
            conn.response.headers['Set-Cookie']='synthetic_private'
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=conn):
                with self.assertRaises(gateway.GatewayError) as cm:gateway.FoundationGateway().request('search_runs')
            self.assertNotIn('private',str(cm.exception));self.assertEqual(len(conn.requests),1)

    def test_invalid_json_or_shape_fails_safely(self):
        for body in (b'not-json',b'[]',b'{"items":{}}',b'{"items":[1]}',b'{"items":[],"items":[]}'):
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response(body=body))):
                with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('search_runs')

    def test_body_size_and_encoding_limits(self):
        for response in (Response(body=b' '* (2*1024*1024+1)),Response({},content_type='text/html'),Response({},encoding='gzip')):
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(response)):
                with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('search_runs')

    def test_run_dto_drops_internal_fields(self):
        payload=run_payload(error_message='private_internal',error_code='private_code',raw_path='/Volumes/private',cookies='private',raw={'token':'private'})
        dto,_=self.invoke('search_runs',response=Response({'items':[payload]}))
        serialized=json.dumps(dto)
        self.assertNotIn('private',serialized)
        self.assertNotIn('error',serialized)
        self.assertNotIn('source',dto['items'][0])

    def test_numeric_counts_are_bounded_integer_not_bool(self):
        for value in (True,-1,10**20,1.2,'4',None):
            response=Response({'items':[run_payload(raw_count=value)]})
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(response)):
                with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('search_runs')

    def test_unknown_status_and_wrong_source_rejected(self):
        for payload in (run_payload(status='private_state'),run_payload(source='mediacrawler')):
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response({'items':[payload]}))):
                with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('search_runs')

    def test_search_results_are_business_only(self):
        row={'source':'opencli','stable_identity':'note:abc123','canonical_url':'https://example.invalid/private','title':'测试结果','author':'作者','likes':2,'published_at':'2026-09-08','note_id':'abc123'}
        dto,_=self.invoke('search_results',response=Response({'items':[row]}))
        self.assertEqual(dto['items'][0],{'title':'测试结果','author':'作者','likes':2,'published_at':'2026-09-08','note_id':'abc123','source_label':'小红书','source':'opencli','stable_identity':'note:abc123','keywords':[],'last_seen':None,'latest_search_rank':None,'latest_search_run_at':None,'latest_search_run_id':None,'keyword_search_ranks':[]})
        self.assertNotIn('private',json.dumps(dto))

    def test_trends_results_projection(self):
        row={'source':'trendradar','platform':'weibo','title':'热榜','source_native_id':'123','latest_rank':1,'occurrence_count':2,'first_seen':'2026-09-08T12:00:00+10:00','last_seen':'2026-09-08T12:01:00+10:00','canonical_url':'private','identity_basis':'private'}
        dto,_=self.invoke('trends_results',response=Response({'items':[row]}))
        self.assertEqual(dto['items'][0]['platform_label'],'微博')
        self.assertNotIn('private',json.dumps(dto))

    def test_projected_fields_cannot_smuggle_credential_or_runtime_path(self):
        for keyword in ('Cookie: synthetic_private','Authorization=synthetic_private','/Volumes/private','xsec_token=synthetic_private'):
            payload=run_payload(keyword=keyword)
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response({'items':[payload]}))):
                with self.assertRaises(gateway.GatewayError) as cm:gateway.FoundationGateway().request('search_runs')
            self.assertNotIn('private',cm.exception.message)

    def test_proxy_environment_does_not_change_fixed_backend(self):
        with patch.dict('os.environ',{'HTTP_PROXY':'http://invalid.local','HTTPS_PROXY':'http://invalid.local'}):
            self.invoke('search_runs')

    def test_trends_results_reject_unapproved_platform(self):
        row={'source':'trendradar','platform':'baidu','title':'synthetic','occurrence_count':1}
        with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response({'items':[row]}))):
            with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('trends_results')

    def test_platform_status_sets_cannot_conflict(self):
        row=trend_run(successful_platforms=['weibo'],failed_platforms=['weibo'])
        with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response({'items':[row]}))):
            with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('trends_runs')

    def test_trends_runs_accept_supported_nonempty_requested_subsets(self):
        cases = (
            trend_run(requested_platforms=['weibo'], successful_platforms=['weibo']),
            trend_run(requested_platforms=['zhihu'], successful_platforms=['zhihu']),
            trend_run(),
        )
        for row in cases:
            with self.subTest(requested=row['requested_platforms']):
                dto,_=self.invoke('trends_runs',response=Response({'items':[row]}))
                self.assertEqual(dto['items'][0]['requested_platforms'],row['requested_platforms'])

    def test_trends_runs_reject_invalid_or_incomplete_platform_status_sets(self):
        cases = (
            trend_run(requested_platforms=[], successful_platforms=[]),
            trend_run(requested_platforms=['baidu'], successful_platforms=['baidu']),
            trend_run(requested_platforms=['weibo','weibo'], successful_platforms=['weibo']),
            trend_run(requested_platforms=['weibo'], successful_platforms=['weibo'], failed_platforms=['weibo']),
            trend_run(requested_platforms=['weibo'], successful_platforms=['zhihu']),
            trend_run(requested_platforms=['weibo'], successful_platforms=[], failed_platforms=[]),
        )
        for row in cases:
            with self.subTest(requested=row['requested_platforms'],successful=row['successful_platforms'],failed=row['failed_platforms']):
                with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response({'items':[row]}))):
                    with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('trends_runs')

    def test_malformed_timestamp_fails_without_echo(self):
        row=run_payload(started_at='synthetic_invalid_timestamp')
        with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response({'items':[row]}))):
            with self.assertRaises(gateway.GatewayError) as cm:gateway.FoundationGateway().request('search_runs')
        self.assertNotIn('synthetic',cm.exception.message)

    def test_oversized_list_and_non_finite_numeric_json_rejected(self):
        for response in (Response({'items':[run_payload()]*1001}),Response(body=b'{"items":[],"ignored":NaN}')):
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(response)):
                with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('search_runs')

    def test_truncated_response_and_missing_length_oversize_rejected(self):
        truncated=Response({'items':[]});truncated.headers['Content-Length']='1000'
        oversized=Response(body=b' '*(2*1024*1024+1));oversized.headers.pop('Content-Length')
        for response in (truncated,oversized):
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(response)):
                with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('search_runs')

    def test_submit_does_not_treat_failed_status_as_success(self):
        with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response(run_payload(status='failed'),status=201))):
            with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('search_submit',{'keyword':'测试'})

    def test_lone_unicode_surrogate_is_a_safe_validation_error(self):
        error=None
        with patch('agent_reach_gateway.http.client.HTTPConnection') as factory:
            try:gateway.FoundationGateway().request('search_submit',{'keyword':'\ud800'})
            except Exception as exc:error=exc
        self.assertIsInstance(error,gateway.GatewayError)
        factory.assert_not_called()

    def test_backend_surrogate_cannot_escape_projection(self):
        row=run_payload(keyword='\ud800')
        with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response({'items':[row]}))):
            with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('search_runs')

    def test_search_publication_preserves_relative_partial_and_numeric_display(self):
        for value in ('昨天','09-08',1725807600,1725807600000,1725807600.5,None):
            with self.subTest(kind=type(value).__name__):
                row={'source':'opencli','title':'测试','author':None,'likes':None,'note_id':None,'published_at':value}
                dto,_=self.invoke('search_results',response=Response({'items':[row]}))
                self.assertEqual(dto['items'][0]['published_at'],None if value is None else str(value))

    def test_search_publication_does_not_expose_unsafe_scalar_values(self):
        for value in ('https://example.invalid/private','Cookie=synthetic_private','/Volumes/private','昨天\nprivate',True,{'token':'private'}):
            row={'source':'opencli','title':'测试','published_at':value}
            with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=Connection(Response({'items':[row]}))):
                with self.assertRaises(gateway.GatewayError) as cm:gateway.FoundationGateway().request('search_results')
            self.assertNotIn('private',cm.exception.message)

    def test_trends_native_identity_is_unmodified_backend_data_not_display_dto(self):
        for native in ('123','中文原生身份'*40):
            row={'source':'trendradar','platform':'weibo','title':'热榜','source_native_id':native,'latest_rank':1,'occurrence_count':2,'first_seen':None,'last_seen':None}
            dto,_=self.invoke('trends_results',response=Response({'items':[row]}))
            self.assertNotIn('source_native_id',dto['items'][0])
            self.assertEqual(row['source_native_id'],native)

    def test_constructor_failure_is_safe(self):
        with patch('agent_reach_gateway.http.client.HTTPConnection',side_effect=OSError('synthetic_private')):
            with self.assertRaises(gateway.GatewayError) as cm:gateway.FoundationGateway().request('search_runs')
        self.assertNotIn('private',cm.exception.message)

if __name__=='__main__':unittest.main()

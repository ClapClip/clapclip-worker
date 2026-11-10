import contextlib
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mediaworker.downloader import egress, safety
from mediaworker.downloader.worker import Config, ProcessRunner
from mediaworker.queues import queue_failure_report


class AdmissionMappingTests(unittest.TestCase):
    def test_real_url_admission_codes_survive_public_safe_mapping(self):
        for value, expected in ((None, 'INVALID_URL'), ('https://unsupported.example/video', 'UNSUPPORTED_SITE')):
            with self.subTest(expected=expected):
                with self.assertRaises(safety.ServiceError) as caught:
                    safety.validate_url(value)
                report = queue_failure_report('download', caught.exception, {'operation': 'info'})
                self.assertEqual((report.message, report.phase, report.diagnostic_code), (expected, 'info', expected))
                self.assertFalse(report.include_traceback)

    def test_only_reviewed_admission_codes_are_promoted_and_no_message_leaks(self):
        error = safety.ServiceError('INVALID_URL', 'private signed data must not escape')
        report = queue_failure_report('download', error, {'operation': 'info'})
        self.assertNotIn('private', report.message)
        unknown = safety.ServiceError('UNREVIEWED_CODE', 'private signed data')
        self.assertEqual(queue_failure_report('download', unknown, {'operation': 'info'}).diagnostic_code, 'UNKNOWN_FAILURE')
        self.assertEqual(queue_failure_report('cutout', error, {}).message, str(error))


class SafetyReasonTests(unittest.TestCase):
    def test_dns_empty_policy_and_connect_failures_keep_original_public_codes(self):
        def dns(*_, **__):raise socket.gaierror('untrusted resolver data')
        cases = [('DNS_FAILED', 'MEDIA_UNAVAILABLE', lambda:safety.public_addresses('public.example',443,dns)),
                 ('DNS_EMPTY', 'BLOCKED_DESTINATION', lambda:safety.public_addresses('public.example',443,lambda *a,**k:[])),
                 ('PORT_REJECTED', 'BLOCKED_DESTINATION', lambda:safety.public_addresses('public.example',22)),
                 ('HOST_REJECTED', 'BLOCKED_DESTINATION', lambda:safety.public_addresses('public%scope.example',443)),
                 ('ADDRESS_REJECTED', 'BLOCKED_DESTINATION', lambda:safety.public_addresses('public.example',443,
                    lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]))]
        for reason, code, call in cases:
            with self.subTest(reason=reason), self.assertRaises(safety.ServiceError) as caught:call()
            self.assertEqual((caught.exception.egress_reason,caught.exception.code,caught.exception.status),(reason,code,422))
        fake=Mock();fake.connect.side_effect=OSError('private address text')
        with patch.object(safety.socket,'socket',return_value=fake), self.assertRaises(safety.ServiceError) as caught:
            safety.connect_public('public.example',443,lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
        self.assertEqual((caught.exception.egress_reason,caught.exception.code),('CONNECT_FAILED','MEDIA_UNAVAILABLE'))
        fake.close.assert_called_once()


class RealProxyDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.proxy=egress.EgressProxy(2)
        self.addCleanup(self.proxy.close)
        self.peers=[]
        self.addCleanup(lambda:[p.close() for p in self.peers])

    def request(self, target='CONNECT public.example:443 HTTP/1.0', *, auth=True, headers=()):
        with socket.create_connection(('127.0.0.1',self.proxy.port),timeout=3) as client:
            text=target+'\r\n'
            if auth:text+='Proxy-Authorization: '+self.proxy.server.authorization+'\r\n'
            text+=''.join(h+'\r\n' for h in headers)+'\r\n'
            client.sendall(text.encode('ascii'))
            with client.makefile('rb') as response:return response.readline().decode('ascii').split()[1]

    def success(self,*_):
        remote,peer=socket.socketpair();self.peers.append(peer)
        peer.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok');peer.shutdown(socket.SHUT_WR)
        return remote

    def test_real_proxy_auth_target_and_body_rejections_are_distinct_without_policy_change(self):
        dial=Mock(side_effect=AssertionError('must reject before dial'));self.proxy.server.dial=dial
        cases=[('CONNECT public.example:443 HTTP/1.0',False,(),'407','AUTH_FAILED'),
               ('CONNECT public.example:443/path HTTP/1.0',True,(),'403','CONNECT_TARGET_INVALID'),
               ('GET https://public.example/private HTTP/1.0',True,(),'403','HTTP_TARGET_INVALID'),
               ('POST http://public.example/private HTTP/1.0',True,('Content-Length: 1048577',),'403','REQUEST_BODY_INVALID'),
               ('HEAD http://public.example/private HTTP/1.0',True,('Transfer-Encoding: chunked',),'403','REQUEST_BODY_INVALID')]
        with self.assertLogs(egress.log,level='INFO') as logs:
            for target,auth,headers,status,reason in cases:
                with self.proxy.diagnostics('info') as receipt:
                    self.assertEqual(self.request(target,auth=auth,headers=headers),status)
                    self.assertEqual(receipt.snapshot()[0]['reason'],reason)
        dial.assert_not_called()
        output='\n'.join(logs.output)
        self.assertNotIn('public.example',output);self.assertNotIn('private',output)
        self.assertNotIn(self.proxy.server.authorization,output)

    def test_same_403_retains_distinct_structured_dns_tcp_and_policy_observations(self):
        for reason,code in [('DNS_FAILED','MEDIA_UNAVAILABLE'),('CONNECT_FAILED','MEDIA_UNAVAILABLE'),('ADDRESS_REJECTED','BLOCKED_DESTINATION')]:
            error=safety._egress_error(code,'private raw exception',reason)
            self.proxy.server.dial=Mock(side_effect=error)
            with self.proxy.diagnostics('info') as receipt:
                self.assertEqual(self.request(),'403')
                self.assertEqual(receipt.snapshot(),({'stage':safety.EGRESS_REASON_STAGES[reason],'reason':reason,'count':1},))

    def test_benign_rejection_success_and_next_failure_do_not_rewrite_terminal_cause(self):
        runner=ProcessRunner(Config(),self.proxy);receipts=[]
        def first(*_):
            receipts.append(self.proxy.server.diagnostic_receipt())
            self.proxy.server.dial=Mock(side_effect=safety._egress_error('MEDIA_UNAVAILABLE','private','DNS_FAILED'))
            self.assertEqual(self.request(),'403')
            self.proxy.server.dial=self.success
            self.assertEqual(self.request('GET http://public.example/video HTTP/1.0'),'200')
            return '{}'
        error=safety.ServiceError('MEDIA_UNAVAILABLE','safe failure',422);error.diagnostic_code='EXTRACTOR_FAILED'
        def second(*_):
            receipts.append(self.proxy.server.diagnostic_receipt())
            self.assertEqual(self.request('GET http://public.example/video HTTP/1.0'),'200')
            raise error
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(runner,'_run',side_effect=first):
                self.assertEqual(runner.run([],Path(temporary),threading.Event(),1,failure_phase='info'),'{}')
            with patch.object(runner,'_run',side_effect=second),self.assertRaises(safety.ServiceError) as caught:
                runner.run([],Path(temporary),threading.Event(),1,failure_phase='info')
        self.assertIs(caught.exception,error);self.assertEqual(error.diagnostic_code,'EXTRACTOR_FAILED')
        self.assertEqual(len(receipts[0].snapshot()),1);self.assertEqual(receipts[1].snapshot(),())
        self.assertIsNone(self.proxy.server.diagnostic_receipt())

    def test_late_request_rejection_stays_with_accepted_old_scope(self):
        started=threading.Event();release=threading.Event();statuses=[]
        def dial(*_):
            started.set()
            if not release.wait(3):raise RuntimeError('test release missing')
            raise safety._egress_error('MEDIA_UNAVAILABLE','private','DNS_FAILED')
        self.proxy.server.dial=dial
        with self.proxy.diagnostics('info') as old:
            thread=threading.Thread(target=lambda:statuses.append(self.request()));thread.start()
            self.assertTrue(started.wait(2))
        try:
            with self.proxy.diagnostics('download_source') as new:
                release.set();thread.join(3)
                self.assertFalse(thread.is_alive());self.assertEqual(statuses,['403'])
                self.assertEqual(new.snapshot(),())
                self.assertEqual(old.snapshot()[0]['reason'],'DNS_FAILED')
        finally:release.set();thread.join(3)

    def test_same_run_observed_rejection_is_not_promoted_to_unrelated_failure_cause(self):
        runner=ProcessRunner(Config(),self.proxy);captured=[]
        error=safety.ServiceError('MEDIA_UNAVAILABLE','safe message',422);error.diagnostic_code='EXTRACTOR_FAILED'
        def process(*_):
            captured.append(self.proxy.server.diagnostic_receipt())
            self.proxy.server.dial=Mock(side_effect=safety._egress_error('MEDIA_UNAVAILABLE','private','DNS_FAILED'))
            self.assertEqual(self.request(),'403')
            self.proxy.server.dial=self.success
            self.assertEqual(self.request('GET http://public.example/video HTTP/1.0'),'200')
            raise error
        with tempfile.TemporaryDirectory() as temporary,patch.object(runner,'_run',side_effect=process):
            with self.assertRaises(safety.ServiceError) as caught:
                runner.run([],Path(temporary),threading.Event(),1,failure_phase='info')
        self.assertIs(caught.exception,error);self.assertEqual(error.diagnostic_code,'EXTRACTOR_FAILED')
        self.assertEqual(captured[0].snapshot()[0]['reason'],'DNS_FAILED')

    def test_request_accepted_before_phase_change_keeps_scope_even_if_headers_arrive_late(self):
        accepted=threading.Event();original=self.proxy.server.process_request
        def dispatch(*args):
            original(*args);accepted.set()
        self.proxy.server.dial=Mock(side_effect=safety._egress_error('MEDIA_UNAVAILABLE','private','DNS_FAILED'))
        with patch.object(self.proxy.server,'process_request',side_effect=dispatch):
            with self.proxy.diagnostics('info') as old:
                client=socket.create_connection(('127.0.0.1',self.proxy.port),timeout=3)
                client.sendall(('CONNECT public.example:443 HTTP/1.0\r\nProxy-Authorization: '+self.proxy.server.authorization+'\r\n').encode())
                self.assertTrue(accepted.wait(2))
            try:
                with self.proxy.diagnostics('download_source') as new:
                    client.sendall(b'\r\n')
                    with client.makefile('rb') as response:self.assertIn(b'403',response.readline())
                    self.assertEqual(new.snapshot(),())
                    self.assertEqual(old.snapshot()[0]['reason'],'DNS_FAILED')
            finally:client.close()

    def test_bounded_fixed_enums_and_logger_failure_do_not_change_rejection(self):
        self.proxy.server.dial=Mock(side_effect=safety._egress_error('MEDIA_UNAVAILABLE','private','DNS_FAILED'))
        with patch.object(egress.log,'info',side_effect=OSError('unavailable logger')):
            with self.proxy.diagnostics('info') as receipt:
                self.assertEqual(self.request(),'403')
                for _ in range(1100):receipt.record('DNS_FAILED')
                receipt.record('untrusted host text');receipt.record([])
                self.assertEqual(receipt.snapshot(),({'stage':'dns','reason':'DNS_FAILED','count':1000},))

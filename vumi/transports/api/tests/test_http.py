
from twisted.trial.unittest import TestCase
from twisted.web.resource import Resource
from twisted.internet.defer import inlineCallbacks, DeferredQueue
from twisted.web.server import Site
from twisted.internet import reactor
from twisted.internet.base import DelayedCall

from vumi.utils import http_request
from vumi.transports.api.http import HttpTransport
from vumi.message import TransportUserMessage
from vumi.tests.utils import get_stubbed_worker


class TestTransport(TestCase):

    @inlineCallbacks
    def setUp(self):
        DelayedCall.debug = True
        self.ok_transport_calls = DeferredQueue()
        config = {
            'transport_name': 'test_http_transport',
            'web_path': "foo",
            'web_port': 0,
            }
        self.worker = get_stubbed_worker(HttpTransport, config)
        self.broker = self.worker._amqp_client.broker
        yield self.worker.startWorker()
        addr = self.worker.web_resource.getHost()
        self.worker_url = "http://%s:%s/" % (addr.host, addr.port)

    @inlineCallbacks
    def tearDown(self):
        yield self.worker.stopWorker()

    def handle_request(self, request):
        self.ok_transport_calls.put(request)
        return ''

    @inlineCallbacks
    def test_health(self):
        result = yield http_request(self.worker_url + "health", "",
                                    method='GET')
        self.assertEqual(result, "OK")

    @inlineCallbacks
    def test_inbound(self):
        args = '/?to_addr=555&from_addr=123&content=hello'
        d = http_request(self.worker_url + "foo" + args, '', method='POST')
        msg, = yield self.broker.wait_messages("vumi",
            "test_http_transport.inbound", 1)
        payload = msg.payload
        self.assertEqual(payload['transport_name'], "test_http_transport")
        self.assertEqual(payload['to_addr'], "555")
        self.assertEqual(payload['from_addr'], "123")
        self.assertEqual(payload['content'], "hello")
        expected_response = '{"message_id": "%s"}' % payload['message_id']
        response = yield d
        self.assertEqual(response, expected_response)

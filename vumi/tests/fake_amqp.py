# -*- test-case-name: vumi.tests.test_fake_amqp -*-

from uuid import uuid4
import re

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue, Deferred
from txamqp.client import TwistedDelegate

from vumi.service import WorkerAMQClient


def gen_id(prefix=''):
    return ''.join([prefix, uuid4().get_hex()])


def gen_longlong():
    return uuid4().int & 0xffffffffffffffff


class Thing(object):
    """
    A generic thing to reply with.
    """
    def __init__(self, kind, **kw):
        self._kind = kind
        self._kwfields = kw.keys()
        for k, v in kw.items():
            setattr(self, k, v)

    def __str__(self):
        return "<Thing:: %s %s>" % (self._kind,
                                    ['[%s: %s]' % (f, getattr(self, f))
                                     for f in self._kwfields])


class Message(object):
    """
    A message is more complicated than a Thing.
    """
    def __init__(self, method, fields=(), content=None):
        self.method = method
        self._fields = fields
        self.content = content

    def __getattr__(self, key):
        for k, v in self._fields:
            if k == key:
                return v
        raise AttributeError(key)


def mkMethod(name, index=None):
    if index is None:
        index = {
            'deliver': 60,
            'get-ok': 71,
            }.get(name, -1)
    return Thing("Method", name=name, id=index)


def mkContent(body, children=None, properties=None):
    return Thing("Content", body=body, children=children,
                 properties=properties)


def mk_deliver(body, exchange, routing_key, ctag, dtag):
    return Message(mkMethod('deliver'), [
            ('consumer_tag', ctag),
            ('delivery_tag', dtag),
            ('redelivered', False),
            ('exchange', exchange),
            ('routing_key', routing_key),
            ], mkContent(body))


def mk_get_ok(body, exchange, routing_key, dtag):
    return Message(mkMethod('deliver'), [
            ('delivery_tag', dtag),
            ('redelivered', False),
            ('exchange', exchange),
            ('routing_key', routing_key),
            ], mkContent(body))


class FakeAMQPBroker(object):
    def __init__(self):
        self.queues = {}
        self.exchanges = {}
        self.channels = []
        self.dispatched = {}

    def _get_queue(self, queue):
        assert queue in self.queues
        return self.queues[queue]

    def _get_exchange(self, exchange):
        assert exchange in self.exchanges
        return self.exchanges[exchange]

    def channel_open(self, channel):
        assert channel not in self.channels
        self.channels.append(channel)
        return Message(mkMethod("open-ok", 11))

    def exchange_declare(self, exchange, exchange_type):
        exchange_class = None
        if exchange_type == 'direct':
            exchange_class = FakeAMQPExchangeDirect
        elif exchange_type == 'topic':
            exchange_class = FakeAMQPExchangeTopic
        assert exchange_class is not None
        self.exchanges.setdefault(exchange, exchange_class(exchange))
        assert exchange_type == self.exchanges[exchange].exchange_type
        return Message(mkMethod("declare-ok", 11))

    def queue_declare(self, queue):
        if not queue:
            queue = gen_id('queue.')
        self.queues.setdefault(queue, FakeAMQPQueue(queue))
        queue_obj = self._get_queue(queue)
        return Message(mkMethod("declare-ok", 11), [
                ('queue', queue),
                ('message_count', queue_obj.message_count()),
                ('consumer_count', queue_obj.consumer_count()),
                ])

    def queue_bind(self, queue, exchange, routing_key):
        self._get_exchange(exchange).queue_bind(routing_key,
                                            self._get_queue(queue))
        return Message(mkMethod("bind-ok", 21))

    def basic_consume(self, queue, tag):
        self._get_queue(queue).add_consumer(tag)
        self.kick_delivery()
        return Message(mkMethod("consume-ok", 21), [("consumer_tag", tag)])

    def basic_cancel(self, tag, queue):
        if queue in self.queues:
            self.queues[queue].remove_consumer(tag)
        return Message(mkMethod("cancel-ok", 31), [("consumer_tag", tag)])

    def basic_publish(self, exchange, routing_key, content):
        exc = self.dispatched.setdefault(exchange, {})
        exc.setdefault(routing_key, []).append(content)
        if exchange not in self.exchanges:
            # This is to test, so we don't care about missing queues
            return None
        self._get_exchange(exchange).basic_publish(routing_key, content)
        self.kick_delivery()
        return None

    def basic_get(self, queue):
        return self._get_queue(queue).get_message()

    def basic_ack(self, queue, delivery_tag):
        self._get_queue(queue).ack(delivery_tag)
        self.kick_delivery()
        return None

    def deliver_to_channels(self, d):
        if any([self.try_deliver_to_channel(channel)
                for channel in self.channels]):
            self._kick_delivery(d)
        else:
            d.callback(None)

    def try_deliver_to_channel(self, channel):
        if not channel.deliverable():
            return False
        for ctag, queue in channel.consumers.items():
            dtag, msg = self._get_queue(queue).get_message()
            if dtag is not None:
                dmsg = mk_deliver(msg['content'], msg['exchange'],
                                  msg['routing_key'], ctag, dtag)
                channel.deliver_message(dmsg, queue)
                return True
            return False

    def kick_delivery(self):
        """
        Schedule a message delivery run.

        Returns a deferred that will fire when there are no more
        deliverable messages. This is useful for manually triggering a
        delivery run from inside a test.
        """
        d = Deferred()
        self._kick_delivery(d)
        return d

    def _kick_delivery(self, d):
        reactor.callLater(0, self.deliver_to_channels, d)

    def get_dispatched(self, exchange, rkey):
        return self.dispatched.get(exchange, {}).get(rkey, [])


class FakeAMQPChannel(object):
    def __init__(self, channel_id, broker, delegate):
        self.channel_id = channel_id
        self.broker = broker
        self.qos_prefetch_count = 0
        self.consumers = {}
        self.delegate = delegate
        self.unacked = []

    def channel_open(self):
        return self.broker.channel_open(self)

    def basic_qos(self, _prefetch_size, prefetch_count, _global):
        self.qos_prefetch_count = prefetch_count

    def exchange_declare(self, exchange, type, durable=None):
        return self.broker.exchange_declare(exchange, type)

    def queue_declare(self, queue, durable=None):
        return self.broker.queue_declare(queue)

    def queue_bind(self, queue, exchange, routing_key):
        return self.broker.queue_bind(queue, exchange, routing_key)

    def basic_consume(self, queue, tag=None):
        if not tag:
            tag = gen_id('consumer.')
        assert tag not in self.consumers
        self.consumers[tag] = queue
        return self.broker.basic_consume(queue, tag)

    def basic_cancel(self, tag):
        queue = self.consumers.pop(tag, None)
        if queue:
            self.broker.basic_cancel(tag, queue)
        return Message(mkMethod("cancel-ok", 31))

    def basic_publish(self, exchange, routing_key, content):
        return self.broker.basic_publish(exchange, routing_key, content)

    def basic_ack(self, delivery_tag, multiple):
        assert delivery_tag in [d for d, _q in self.unacked]
        for dtag, queue in self.unacked[:]:
            if multiple or (dtag == delivery_tag):
                self.unacked.remove((dtag, queue))
                resp = self.broker.basic_ack(queue, dtag)
                if (dtag == delivery_tag):
                    return resp

    def deliverable(self):
        if self.qos_prefetch_count < 1:
            return True
        return len(self.unacked) < self.qos_prefetch_count

    def deliver_message(self, msg, queue):
        self.unacked.append((msg.delivery_tag, queue))
        self.delegate.basic_deliver(self, msg)

    def basic_get(self, queue):
        dtag, msg = self.broker.basic_get(queue)
        if msg:
            self.unacked.append((dtag, queue))
            return mk_get_ok(msg['content'], msg['exchange'],
                             msg['routing_key'], dtag)
        return Message(mkMethod("get-empty", 72))


class FakeAMQPExchange(object):
    def __init__(self, name):
        self.name = name
        self.binds = {}

    def queue_bind(self, routing_key, queue):
        binds = self.binds.setdefault(routing_key, set())
        binds.add(queue)

    def basic_publish(self, routing_key, content):
        raise NotImplementedError()


class FakeAMQPExchangeDirect(FakeAMQPExchange):
    exchange_type = 'direct'

    def basic_publish(self, routing_key, content):
        for queue in self.binds.get(routing_key, set()):
            queue.put(self.name, routing_key, content)


class FakeAMQPExchangeTopic(FakeAMQPExchange):
    exchange_type = 'topic'

    def _bind_regex(self, bind):
        for k, v in [('.', r'\.'),
                     ('*', r'[^.]+'),
                     ('\.#\.', r'\.([^.]+\.)*'),
                     ('#\.', r'([^.]+\.)*'),
                     ('\.#', r'(\.[^.]+)*')]:
            bind = '^%s$' % bind.replace(k, v)
        return re.compile(bind)

    def match_rkey(self, bind, rkey):
        return (self._bind_regex(bind).match(rkey) is not None)

    def basic_publish(self, routing_key, content):
        for bind, queues in self.binds.items():
            if self.match_rkey(bind, routing_key):
                for queue in queues:
                    queue.put(self.name, routing_key, content)


class FakeAMQPQueue(object):
    def __init__(self, name):
        self.name = name
        self.messages = []
        self.consumers = set()
        self.unacked_messages = {}

    def __eq__(self, other):
        return self.name == other.name

    def __hash__(self):
        return hash(self.name)

    def add_consumer(self, consumer_tag):
        if consumer_tag not in self.consumers:
            self.consumers.add(consumer_tag)

    def remove_consumer(self, consumer_tag):
        if consumer_tag in self.consumers:
            self.consumers.remove(consumer_tag)

    def message_count(self):
        return len(self.messages)

    def consumer_count(self):
        return len(self.consumers)

    def put(self, exchange, routing_key, content):
        self.messages.append({
                'exchange': exchange,
                'routing_key': routing_key,
                'content': content.body,
                })

    def ack(self, delivery_tag):
        self.unacked_messages.pop(delivery_tag)

    def get_message(self):
        try:
            msg = self.messages.pop(0)
        except IndexError:
            return (None, None)
        dtag = gen_longlong()
        self.unacked_messages[dtag] = msg
        return (dtag, msg)


class FakeAMQClient(WorkerAMQClient):
    def __init__(self, spec, vumi_options=None, broker=None):
        WorkerAMQClient.__init__(self, TwistedDelegate(), '', spec)
        if vumi_options is not None:
            self.vumi_options = vumi_options
        if broker is None:
            broker = FakeAMQPBroker()
        self.broker = broker

    @inlineCallbacks
    def channel(self, id):
        yield self.channelLock.acquire()
        try:
            try:
                ch = self.channels[id]
            except KeyError:
                ch = FakeAMQPChannel(id, self.broker, self.delegate)
                self.channels[id] = ch
        finally:
            self.channelLock.release()
        returnValue(ch)

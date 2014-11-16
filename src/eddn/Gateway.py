"""
Contains the necessary ZeroMQ socket and a helper function to publish
market data to the Announcer daemons.
"""
import gevent
import hashlib
import logging
import simplejson
import urlparse
import zlib
import zmq.green as zmq
from eddn import __version__ as EDDN_VERSION
from eddn.conf import Settings

from gevent import monkey
monkey.patch_all()
from bottle import run, request, response, get, post

logger = logging.getLogger(__name__)

# This socket is used to push market data out to the Announcers over ZeroMQ.
context = zmq.Context()
sender = context.socket(zmq.PUB)
# Get the list of transports to bind from settings. This allows us to PUB
# messages to multiple announcers over a variety of socket types
# (UNIX sockets and/or TCP sockets).
for binding in Settings.GATEWAY_SENDER_BINDINGS:
    sender.bind(binding)


def push_message(string_message):
    """
    Spawned as a greenlet to push messages (strings) through ZeroMQ.
    This is a dumb method that just pushes strings; it assumes you've already validated
    and serialised as you want to.
    """

    # Push a zlib compressed JSON representation of the message to
    # announcers.
    compressed_msg = zlib.compress(string_message)
    sender.send(compressed_msg)


def get_remote_address():
    """
    Determines the address of the uploading client. First checks the for
    proxy-forwarded headers, then falls back to request.remote_addr.
    :rtype: str
    """
    return request.headers.get('X-Forwarded-For', request.remote_addr)


def get_decompressed_message():
    """
    For upload formats that support it, detect gzip Content-Encoding headers
    and de-compress on the fly.
    :rtype: str
    :returns: The de-compressed request body.
    """
    content_encoding = request.headers.get('Content-Encoding', '')

    if content_encoding in ['gzip', 'deflate']:
        # Compressed request. We have to decompress the body, then figure out
        # if it's form-encoded.
        try:
            # Auto header checking.
            message_body = zlib.decompress(request.body.read(), 15 + 32)
        except zlib.error:
            # Negative wbits suppresses adler32 checksumming.
            message_body = zlib.decompress(request.body.read(), -15)

        # At this point, we're not sure whether we're dealing with a straight
        # un-encoded POST body, or a form-encoded POST. Attempt to parse the
        # body. If it's not form-encoded, this will return an empty dict.
        form_enc_parsed = urlparse.parse_qs(message_body)
        if form_enc_parsed:
            # This is a form-encoded POST. The value of the data attrib will
            # be the body we're looking for.
            try:
                message_body = form_enc_parsed['data'][0]
            except (KeyError, IndexError):
                raise MalformedUploadError(
                    "No 'data' POST key/value found. Check your POST key "
                    "name for spelling, and make sure you're passing a value."
                )
    else:
        # Uncompressed request. Bottle handles all of the parsing of the
        # POST key/vals, or un-encoded body.
        data_key = request.forms.get('data')
        if data_key:
            # This is a form-encoded POST. Support the silly people.
            message_body = data_key
        else:
            # This is a non form-encoded POST body.
            message_body = request.body.read()

    return message_body


def parse_and_error_handle(data):
    try:
        parsed_message = simplejson.loads(data)
    except (
        MalformedUploadError, TypeError, ValueError
    ) as exc:
        # Something bad happened. We know this will return at least a
        # semi-useful error message, so do so.
        response.status = 400
        logger.error("Error to %s: %s" % (get_remote_address(), exc.message))
        return exc.message

    ip_hash_salt = Settings.GATEWAY_IP_KEY_SALT
    if ip_hash_salt:
        # If an IP hash is set, salt+hash the uploader's IP address and set
        # it as the EMDR upload key value.
        ip_hash = hashlib.sha1(ip_hash_salt + get_remote_address()).hexdigest()
        parsed_message.upload_keys.append({'name': 'EMDR', 'key': ip_hash})

    # Sends the parsed MarketOrderList or MarketHistoryList to the Announcers
    # as compressed JSON.
    gevent.spawn(push_message, simplejson.dumps(parsed_message))
    logger.info("Accepted %s upload from %s" % (
        parsed_message, get_remote_address()
    ))
    return 'OK'


@post('/upload/')
def upload():
    try:
        # Body may or may not be compressed.
        message_body = get_decompressed_message()
    except zlib.error as exc:
        # Some languages and libs do a crap job zlib compressing stuff. Provide
        # at least some kind of feedback for them to try to get pointed in
        # the correct direction.
        response.status = 400
        logger.error("gzip error with %s: %s" % (get_remote_address(), exc.message))
        return exc.message
    except MalformedUploadError as exc:
        # They probably sent an encoded POST, but got the key/val wrong.
        response.status = 400
        logger.error("Error to %s: %s" % (get_remote_address(), exc.message))
        return exc.message

    return parse_and_error_handle(message_body)


@get('/health_check/')
def health_check():
    """
    This should only be used by the gateway monitoring script. It is used
    to detect whether the gateway is still alive, and whether it should remain
    in the DNS rotation.
    """
    return EDDN_VERSION


class MalformedUploadError(Exception):
    """
    Raise this when an upload is structurally incorrect. This isn't so much
    to do with something like a bogus region ID, this is more like "You are
    missing a POST key/val, or a body".
    """
    pass


if __name__ == '__main__':
    run(host='localhost', port=8080, server='gevent')

from dataclasses import dataclass
from enum import Enum
from typing import Any, Union, Dict
from yarl import URL

from feedsearch_crawler.crawler.queueable import Queueable


@dataclass
class CallbackResult(Queueable):
    """Dataclass for holding callback results and recording recursion"""

    result: Any
    callback_recursion: int


class Stats(Enum):
    # Number of HTTP Requests added and sent.
    REQUESTS_ADDED = "requests_added"
    # Number of HTTP Requests that were successful (HTTP Status code 200-299).
    REQUESTS_SUCCESSFUL = "requests_successful"
    # Number of HTTP Requests that were unsuccessful (HTTP Status code not in 200s).
    REQUESTS_FAILED = "requests_failed"
    # Total size in bytes of all HTTP Requests.
    CONTENT_LENGTH_TOTAL = "content_length_total"
    # Harmonic mean of total HTTP Response content length in bytes.
    CONTENT_LENGTH_AVG = "content_length_avg"
    # Highest HTTP Response content length in bytes.
    CONTENT_LENGTH_MAX = "content_length_max"
    # Lowest HTTP Response content length in bytes.
    CONTENT_LENGTH_MIN = "content_length_min"
    # Median HTTP Response content length in bytes.
    CONTENT_LENGTH_MEDIAN = "content_length_med"
    # Number of Items processed.
    ITEMS_PROCESSED = "items_processed"
    # Number of URls seen and added to duplicate filter.
    URLS_SEEN = "urls_seen"
    # Harmonic mean of HTTP Request duration in Milliseconds.
    REQUESTS_DURATION_AVG = "requests_duration_avg"
    # Highest HTTP request duration in Milliseconds.
    REQUESTS_DURATION_MAX = "requests_duration_max"
    # Lowest HTTP request duration in Milliseconds.
    REQUESTS_DURATION_MIN = "requests_duration_min"
    # Total HTTP request duration in Milliseconds.
    REQUESTS_DURATION_TOTAL = "requests_duration_total"
    # Median HTTP request duration in Milliseconds.
    REQUESTS_DURATION_MEDIAN = "requests_duration_med"
    # Total duration of crawl in Milliseconds.
    TOTAL_DURATION = "total_duration"
    # Response status codes.
    STATUS_CODES = "status_codes"
    # Highest queue wait time in Milliseconds.
    QUEUE_WAIT_MAX = "queue_wait_max"
    # Lowest queue wait time in Milliseconds.
    QUEUE_WAIT_MIN = "queue_wait_min"
    # Harmonic mean of queue wait time in Milliseconds.
    QUEUE_WAIT_AVG = "queue_wait_avg"
    # Median queue wait time in Milliseconds.
    QUEUE_WAIT_MEDIAN = "queue_wait_med"
    # Highest queue size.
    QUEUE_SIZE_MAX = "queue_size_max"
    # Harmonic mean of queue size.
    QUEUE_SIZE_AVG = "queue_size_avg"
    # Median queue size.
    QUEUE_SIZE_MEDIAN = "queue_size_med"
    # Total objects put on queue.
    QUEUED_TOTAL = "queued_total"

    def __repr__(self):
        return self._value_


def coerce_url(url: Union[URL, str], https: bool = False) -> URL:
    """
    Coerce URL to valid format

    :param url: URL
    :param https: Force https if no scheme in url
    :return: str
    """
    if isinstance(url, str):
        url = URL(url)

    scheme = "https" if https else "http"

    if not url.is_absolute():
        url_string = str(url)
        split = url_string.split("/", 1)
        url = URL.build(scheme=scheme, host=split[0])
        if len(split) > 1:
            url = url.with_path(split[1])

    return url


def to_bytes(text, encoding: str = "utf-8", errors: str = "strict"):
    """Return the binary representation of `text`. If `text`
    is already a bytes object, return it as-is."""
    if not text:
        return b""
    if isinstance(text, bytes):
        return text
    return text.encode(encoding, errors)


def to_string(item: Any, encoding: str = "utf-8", errors: str = "strict") -> str:
    """
    Return the string representation of 'item'.
    """
    if item is None:
        return ""
    if isinstance(item, bytes):
        return item.decode(encoding, errors)
    return str(item)


def case_insensitive_key(key: str, dictionary: Dict) -> bool:
    """
    Check if a case-insensitive key is in a dictionary.
    """
    k = key.lower()
    for key in dictionary.keys():
        if key.lower() == k:
            return True


def ignore_aiohttp_ssl_eror(loop, aiohttpversion="3.5.4"):
    """Ignore aiohttp #3535 issue with SSL data after close
     There appears to be an issue on Python 3.7 and aiohttp SSL that throws a
    ssl.SSLError fatal error (ssl.SSLError: [SSL: KRB5_S_INIT] application data
    after close notify (_ssl.c:2609)) after we are already done with the
    connection. See GitHub issue aio-libs/aiohttp#3535
     Given a loop, this sets up a exception handler that ignores this specific
    exception, but passes everything else on to the previous exception handler
    this one replaces.
     If the current aiohttp version is not exactly equal to aiohttpversion
    nothing is done, assuming that the next version will have this bug fixed.
    This can be disabled by setting this parameter to None
     """
    import ssl
    import aiohttp
    import asyncio

    try:
        import uvloop

        protocol_class = uvloop.loop.SSLProtocol
    except ImportError:
        protocol_class = asyncio.sslproto.SSLProtocol
        pass

    if aiohttpversion is not None and aiohttp.__version__ != aiohttpversion:
        return

    orig_handler = loop.get_exception_handler()

    # noinspection PyUnresolvedReferences
    def ignore_ssl_error(loop, context):
        errors = ["SSL error", "Fatal error"]
        if any(x in context.get("message") for x in errors):
            # validate we have the right exception, transport and protocol
            exception = context.get("exception")
            protocol = context.get("protocol")
            if (
                isinstance(exception, ssl.SSLError)
                and exception.reason == "KRB5_S_INIT"
                and isinstance(protocol, protocol_class)
            ):
                if loop.get_debug():
                    asyncio.log.logger.debug("Ignoring aiohttp SSL KRB5_S_INIT error")
                return
        if orig_handler is not None:
            orig_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(ignore_ssl_error)

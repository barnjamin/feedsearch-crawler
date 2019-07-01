import asyncio
import copy
import inspect
import logging
import time
from abc import ABC, abstractmethod
from statistics import harmonic_mean, median
from types import AsyncGeneratorType
from typing import List, Any
from typing import Union

import aiohttp
from aiohttp import ClientTimeout
from yarl import URL

from feedsearch_crawler.crawler.duplicatefilter import DuplicateFilter
from feedsearch_crawler.crawler.item import Item
from feedsearch_crawler.crawler.lib import (
    coerce_url,
    ignore_aiohttp_ssl_eror,
    Stats,
    CallbackResult,
)
from feedsearch_crawler.crawler.queueable import Queueable
from feedsearch_crawler.crawler.request import Request
from feedsearch_crawler.crawler.response import Response

try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    uvloop = None
    pass


class Crawler(ABC):

    # Class Name of the Duplicate Filter.
    # May be overridden to use different Duplicate Filter.
    # Not an instantiation of the class.
    duplicate_filter_class = DuplicateFilter

    # Callback to be run after all workers are finished.
    post_crawl_callback = None

    # Max number of concurrent http requests.
    concurrency: int = 10
    # Max size of incoming http response content.
    max_content_length = 1024 * 1024 * 10
    # Max crawl depth. i.e. The max length of the response history.
    max_depth: int = 4
    # Max callback recursion depth, to prevent accidental infinite recursion from AsyncGenerators.
    max_callback_recursion: int = 10
    # Time in seconds to delay each HTTP request.
    delay: float = 0

    # List of worker tasks.
    _workers = []

    # ClientSession for requests. Created on Crawl start.
    _session: aiohttp.ClientSession
    # Task queue for Requests. Created on Crawl start.
    _request_queue: asyncio.Queue
    # Semaphore for controlling HTTP Request concurrency.
    _semaphore: asyncio.Semaphore

    def __init__(
        self,
        start_urls: List[str] = None,
        concurrency: int = 10,
        total_timeout: Union[float, ClientTimeout] = 30,
        request_timeout: Union[float, ClientTimeout] = 3,
        user_agent: str = "",
        max_content_length: int = 1024 * 1024 * 10,
        max_depth: int = 10,
        headers: dict = None,
        allowed_schemes: List[str] = None,
        delay: float = 0,
        *args,
        **kwargs,
    ):
        """
        Base class for a WebCrawler implementation.

        :param allowed_schemes: List of strings of allowed Request URI schemes. e.g. ["http", "https"]
        :param start_urls: List of initial URLs to crawl.
        :param concurrency: Max number of workers and of concurrent HTTP requests.
        :param total_timeout: Total aiohttp ClientSession timeout in seconds.
            Crawl will end if this timeout is triggered.
        :param request_timeout: Total timeout in seconds for each individual HTTP request.
        :param user_agent: Default User-Agent for HTTP requests.
        :param max_content_length: Max size in bytes of incoming http response content.
        :param max_depth: Max crawl depth. i.e. The max length of the response history.
        :param headers: Default HTTP headers to be included in each request.
        :param delay: Time in seconds to delay each HTTP request.
        :param args: Additional positional arguments for subclasses.
        :param kwargs: Additional keyword arguments for subclasses.
        """
        self.start_urls = start_urls or []
        self.concurrency = concurrency

        if not isinstance(total_timeout, ClientTimeout):
            total_timeout = aiohttp.ClientTimeout(total=total_timeout)
        if not isinstance(request_timeout, ClientTimeout):
            request_timeout = aiohttp.ClientTimeout(total=request_timeout)

        self.total_timeout: ClientTimeout = total_timeout
        self.request_timeout: ClientTimeout = request_timeout

        self.max_content_length = max_content_length
        self.max_depth = max_depth

        self.user_agent = user_agent or (
            "Mozilla/5.0 (compatible; Feedsearch-Crawler; +https://pypi.org/project/feedsearch-crawler)"
        )

        self.headers = {"User-Agent": self.user_agent, "Upgrade-Insecure-Requests": "1"}

        if headers:
            self.headers = {**self.headers, **headers}

        self.allowed_schemes = allowed_schemes
        self.delay = delay

        self.logger = logging.getLogger("feedsearch_crawler")

        # Default set for parsed items.
        self.items: set = set()

        # URL Duplicate Filter instance.
        self._duplicate_filter = self.duplicate_filter_class()

        # List of durations in Milliseconds of all Requests.
        self._stats_request_durations = []
        # List of Content Length in bytes of all Responses.
        self._stats_response_content_lengths = []
        # List of time in Milliseconds that each item spend on the queue.
        self._stats_queue_wait_times = []
        # List of the size of the queue each time an item was popped off the queue.
        self._stats_queue_sizes = []

        # Initialise Crawl Statistics.
        self.stats: dict = {
            Stats.REQUESTS_ADDED: 0,
            Stats.REQUESTS_SUCCESSFUL: 0,
            Stats.REQUESTS_FAILED: 0,
            Stats.CONTENT_LENGTH_TOTAL: 0,
            Stats.CONTENT_LENGTH_AVG: 0,
            Stats.CONTENT_LENGTH_MIN: 0,
            Stats.CONTENT_LENGTH_MAX: 0,
            Stats.CONTENT_LENGTH_MEDIAN: 0,
            Stats.ITEMS_PROCESSED: 0,
            Stats.URLS_SEEN: 0,
            Stats.REQUESTS_DURATION_AVG: 0,
            Stats.REQUESTS_DURATION_MAX: 0,
            Stats.REQUESTS_DURATION_MIN: 0,
            Stats.REQUESTS_DURATION_TOTAL: 0,
            Stats.REQUESTS_DURATION_MEDIAN: 0,
            Stats.TOTAL_DURATION: 0,
            Stats.STATUS_CODES: {},
            Stats.QUEUE_WAIT_MAX: 0,
            Stats.QUEUE_WAIT_MIN: 0,
            Stats.QUEUE_WAIT_AVG: 0,
            Stats.QUEUE_WAIT_MEDIAN: 0,
            Stats.QUEUE_SIZE_MAX: 0,
            Stats.QUEUE_SIZE_AVG: 0,
            Stats.QUEUE_SIZE_MEDIAN: 0,
            Stats.QUEUED_TOTAL: 0,
        }

    async def _handle_request(self, request: Request) -> None:
        """
        Handle fetching of Requests and processing of Request callbacks.

        :param request: Request
        :return: None
        """
        try:
            if request.has_run:
                self.logger.warning("%s has already run", request)
                return

            start = time.perf_counter()

            # Fetch the request and run its callback
            results, response = await request.fetch_callback(self._semaphore)

            dur = int((time.perf_counter() - start) * 1000)
            self._stats_request_durations.append(dur)
            self.logger.debug(
                "Fetched: url=%s dur=%dms status=%s prev=%s",
                response.url,
                dur,
                response.status_code,
                response.originator_url,
            )

            if response.ok:
                self.stats[Stats.REQUESTS_SUCCESSFUL] += 1
            else:
                self.stats[Stats.REQUESTS_FAILED] += 1

            if response.status_code in self.stats[Stats.STATUS_CODES]:
                self.stats[Stats.STATUS_CODES][response.status_code] += 1
            else:
                self.stats[Stats.STATUS_CODES][response.status_code] = 1

            self._stats_response_content_lengths.append(response.content_length)

            # Mark the Response URL as seen in the duplicate filter, as it may be different from the Request URL
            # due to redirects.
            await self._duplicate_filter.url_seen(response.url, response.method)

            # Add callback results to the queue for processing.
            if results:
                self._request_queue.put_nowait(CallbackResult(results, 0))

        except asyncio.CancelledError:
            self.logger.debug("Cancelled: %s", request)
        except Exception as e:
            self.logger.exception("Exception during %s: %s", request, e)
        finally:
            return

    async def _process_request_callback_result(
        self, result: Any, callback_recursion: int = 0
    ) -> None:
        """
        Process the Request callback result depending on the result type.
        Request callbacks may contain nested iterators.

        :param result: Callback Result. May be an CallbackResult class, AsyncGenerator, Coroutine, Request, or Item.
        :param callback_recursion: Incremented counter to limit this method's recursion.
        :return: None
        """
        if callback_recursion >= self.max_callback_recursion:
            self.logger.warning(
                "Max callback recursion of %d reached", self.max_callback_recursion
            )
            return

        try:
            # If a CallbackResult class is passed, process the result values from within the class.
            if isinstance(result, CallbackResult):
                await self._process_request_callback_result(
                    result.result, result.callback_recursion
                )
            # For async generators, put each value back on the queue for processing.
            # This will happen recursively until the end of the recursion chain or max_callback_recursion is reached.
            elif inspect.isasyncgen(result):
                async for value in result:
                    if value:
                        self._put_queue(CallbackResult(value, callback_recursion + 1))
            # For coroutines, await the result then put the value back on the queue for further processing.
            elif inspect.iscoroutine(result):
                value = await result
                self._put_queue(CallbackResult(value, callback_recursion + 1))
            # Requests are put onto the queue to be fetched.
            elif isinstance(result, Request):
                await self._process_request(result)

            # Items are handled by the implementing Class.
            elif isinstance(result, Item):
                await self.process_item(result)
                self.stats[Stats.ITEMS_PROCESSED] += 1
        except Exception as e:
            self.logger.exception(e)

    async def _process_request(self, request: Request) -> None:
        """
        Process a Request onto the Request Queue.

        :param request: HTTP Request
        :return: None
        """
        self.stats[Stats.REQUESTS_ADDED] += 1
        self.logger.debug("Queue Add: %s", request)
        # Add the Request to the queue for processing.
        self._put_queue(request)

    async def follow(
        self,
        url: Union[str, URL],
        callback=None,
        response: Response = None,
        method: str = "GET",
        **kwargs,
    ) -> Union[Request, None]:
        """
        Follow a URL by creating an HTTP Request.

        If the URL is not absolute then it is joined with the previous Response URL.
        The previous Response history is copied to the Request.

        Before a Request is followed, first check that the Request URL has not already been seen,
        that the max URL depth has not been reached, and that the URI scheme is allowed.

        These checks are performed before the Request is created so that we don't yield multiple requests
        to the same URL to the queue for further processing. We want to stop duplicates and invalid
        requests as early as possible.

        :param url: URL to follow.
        :param callback: Callback method to run if the Request is successful.
        :param response: Previous Response that contained the Request URL.
        :param kwargs: Optional Request keyword arguments. See Request for details.
        :param method: HTTP method for Request.
        :return: Request
        """
        if isinstance(url, str):
            url = URL(url)

        history = []
        if response:
            # Join the URL to the Response URL if it doesn't contain a domain.
            if not url.is_absolute():
                url = response.url.origin().join(url)
            # Copy the Response history so that it isn't a pointer.
            history = copy.deepcopy(response.history)

        # Check if URL is not already seen, and add it to the duplicate filter seen list.
        if await self._duplicate_filter.url_seen(url, method):
            return

        # The URL scheme must be in the list of allowed schemes.
        if self.allowed_schemes and url.scheme not in self.allowed_schemes:
            self.logger.debug("URI Scheme '%s' not allowed: %s", url.scheme, url)
            return

        # Restrict the depth of the Request chain to the maximum depth.
        if self.max_depth and len(history) >= self.max_depth:
            self.logger.debug("Max Depth of '%d' reached: %s", self.max_depth, response)
            return

        request = Request(
            url=url,
            request_session=self._session,
            history=history,
            callback=callback,
            xml_parser=self.parse_xml,
            max_content_length=self.max_content_length,
            timeout=self.request_timeout,
            method=method,
            delay=self.delay,
            **kwargs,
        )

        return request

    @abstractmethod
    async def process_item(self, item: Item) -> None:
        """
        Processed a parsed Item in some way. e.g. Add it to the Item set, or database, or send a signal.

        :param item: A parsed Item.
        """
        self.items.add(item)

    @abstractmethod
    async def parse_xml(self, response_text: str) -> Any:
        """
        Parse Response text as XML.
        Used to allow implementations to provide their own XML parser.

        :param response_text: Response text as string.
        """
        raise NotImplementedError("Not Implemented")

    @abstractmethod
    async def parse(self, request: Request, response: Response) -> AsyncGeneratorType:
        """
        Parse an HTTP Response. Must yield Items, Requests, AsyncGenerators, or Coroutines.

        :param request: HTTP Request that created the Response.
        :param response: HTTP Response.
        """
        raise NotImplementedError("Not Implemented")

    def _put_queue(self, queueable: Queueable) -> None:
        """
        Put an object that inherits from Queueable on the Request Queue.

        :param queueable: An object that inherits from Queueable.
        """
        queueable.set_queue_put_time()
        self._request_queue.put_nowait(queueable)
        self.stats[Stats.QUEUED_TOTAL] += 1

    async def _work(self, task_num):
        """
        Worker function for handling request queue items.
        """
        try:
            while True:
                self._stats_queue_sizes.append(self._request_queue.qsize())
                item: Queueable = await self._request_queue.get()
                if item.get_queue_wait_time():
                    self._stats_queue_wait_times.append(item.get_queue_wait_time())

                if self._session.closed:
                    self.logger.debug("Session is closed. Cannot run %s", item)
                    continue

                try:
                    # Fetch Request and handle callbacks
                    if isinstance(item, Request):
                        await self._handle_request(item)
                    # Process Callback results
                    elif isinstance(item, CallbackResult):
                        await self._process_request_callback_result(
                            item.result, item.callback_recursion
                        )
                except Exception as e:
                    self.logger.error("Error handling item: %s : %s", item, e)
                finally:
                    self._request_queue.task_done()
        except asyncio.CancelledError:
            self.logger.debug("Cancelled Worker: %s", task_num)

    async def _run_callback(self, callback, *args, **kwargs) -> None:
        """
        Runs a callback function.

        :param callback: Function to run. May be async.
        :param args: Positional arguments to pass to the function.
        :param kwargs: Keyword arguments to pass to the function.
        :return: None
        """
        if not callback:
            return
        if inspect.iscoroutinefunction(callback):
            await callback(*args, **kwargs)
        elif inspect.isfunction(callback):
            callback(*args, **kwargs)
        else:
            self.logger.warning("Callback %s must be a coroutine or function", callback)

    def create_start_urls(self, url: Union[str, URL]) -> List[URL]:
        """
        Create the start URLs for the crawl from an initial URL. May be overridden.

        :param url: Initial URL
        """
        if isinstance(url, str):
            url = URL(url)

        if url.scheme.lower() not in ["http", "https"]:
            url = url.with_scheme("http")

        return [url]

    async def crawl(self, url: Union[URL, str] = ""):
        """
        Start the web crawler.

        :param url: An optional URL to start the crawl. If not provided then start_urls are used.
        """

        # Fix for ssl errors
        ignore_aiohttp_ssl_eror(asyncio.get_running_loop())

        if url:
            self.start_urls = self.create_start_urls(url)

        if not self.start_urls:
            raise ValueError("crawler.start_urls are required")

        start = time.perf_counter()
        # Create the Request Queue and ClientSession within the asyncio loop.
        self._request_queue = asyncio.Queue()

        # Create the Semaphore for controlling HTTP Request concurrency within the asyncio loop.
        self._semaphore = asyncio.Semaphore(self.concurrency)

        self._session = aiohttp.ClientSession(
            timeout=self.total_timeout, headers=self.headers
        )

        # Create a Request for each start URL and add it to the Request Queue.
        for url in self.start_urls:
            await self._process_request(await self.follow(coerce_url(url), self.parse))

        # Create workers to process the Request Queue.
        # Create twice as many workers as potential concurrent requests, to handle request callbacks without delay.
        self._workers = [
            asyncio.create_task(self._work(i)) for i in range(self.concurrency * 2)
        ]

        try:
            # Run workers within the ClientSession.
            async with self._session:
                await asyncio.wait_for(
                    self._request_queue.join(), timeout=self.total_timeout.total
                )
        except asyncio.TimeoutError:
            self.logger.debug("Timed out after %s seconds", self.total_timeout.total)
        finally:
            # Make sure all workers are cancelled.
            for w in self._workers:
                w.cancel()

        # Run the post crawl callback if it exists.
        await self._run_callback(self.post_crawl_callback)

        # The ClientSession is closed only after all work is completed.
        await self._session.close()

        duration = int((time.perf_counter() - start) * 1000)

        # Record statistics
        self.stats[Stats.TOTAL_DURATION] = duration
        self.stats[Stats.REQUESTS_DURATION_TOTAL] = int(
            sum(self._stats_request_durations)
        )
        self.stats[Stats.REQUESTS_DURATION_AVG] = int(
            harmonic_mean(self._stats_request_durations)
        )
        self.stats[Stats.REQUESTS_DURATION_MAX] = int(
            max(self._stats_request_durations)
        )
        self.stats[Stats.REQUESTS_DURATION_MIN] = int(
            min(self._stats_request_durations)
        )
        self.stats[Stats.REQUESTS_DURATION_MEDIAN] = int(
            median(self._stats_request_durations)
        )
        self.stats[Stats.CONTENT_LENGTH_TOTAL] = int(
            sum(self._stats_response_content_lengths)
        )
        self.stats[Stats.CONTENT_LENGTH_AVG] = int(
            harmonic_mean(self._stats_response_content_lengths)
        )
        self.stats[Stats.CONTENT_LENGTH_MAX] = int(
            max(self._stats_response_content_lengths)
        )
        self.stats[Stats.CONTENT_LENGTH_MIN] = int(
            min(self._stats_response_content_lengths)
        )
        self.stats[Stats.CONTENT_LENGTH_MEDIAN] = int(
            median(self._stats_response_content_lengths)
        )
        self.stats[Stats.URLS_SEEN] = len(self._duplicate_filter.fingerprints)
        self.stats[Stats.QUEUE_WAIT_AVG] = harmonic_mean(self._stats_queue_wait_times)
        self.stats[Stats.QUEUE_WAIT_MIN] = min(self._stats_queue_wait_times)
        self.stats[Stats.QUEUE_WAIT_MAX] = max(self._stats_queue_wait_times)
        self.stats[Stats.QUEUE_WAIT_MEDIAN] = median(self._stats_queue_wait_times)
        self.stats[Stats.QUEUE_SIZE_MAX] = max(self._stats_queue_sizes)
        self.stats[Stats.QUEUE_SIZE_AVG] = int(harmonic_mean(self._stats_queue_sizes))
        self.stats[Stats.QUEUE_SIZE_MEDIAN] = int(median(self._stats_queue_sizes))

        self.logger.info(
            "Crawl finished: requests=%s time=%dms",
            self.stats[Stats.REQUESTS_ADDED],
            duration,
        )
        self.logger.debug("Stats: %s", self.stats)

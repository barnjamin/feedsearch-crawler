import asyncio
import copy
import json
import logging
import uuid
from asyncio import Semaphore
from typing import List, Tuple, Any, Union

import aiohttp
from aiohttp import ClientSession, ClientTimeout
from yarl import URL

from feedsearch.crawler.response import Response


class Request:
    METHOD = ["GET", "POST"]

    def __init__(
        self,
        url: URL,
        request_session: ClientSession,
        encoding: str = None,
        method: str = "GET",
        headers: dict = None,
        timeout: Union[float, ClientTimeout] = 5.0,
        history: List = None,
        callback=None,
        xml_parser=None,
        failure_callback=None,
        max_size: int = 1024 * 1024 * 10,
        **kwargs,
    ):
        self.url = url
        self.method = method.upper()
        if self.method not in self.METHOD:
            raise ValueError(f"{self.method} is not supported")
        if not isinstance(request_session, ClientSession):
            raise ValueError(f"request_session must be of type ClientSession")
        self.request_session = request_session
        self.headers = headers
        if not isinstance(timeout, ClientTimeout):
            timeout = aiohttp.ClientTimeout(total=self.timeout)
        self.timeout = timeout
        self.history = history or []
        self.encoding = encoding
        self._callback = callback
        self._failure_callback = failure_callback
        self.id = uuid.uuid4()
        self._xml_parser = xml_parser
        self.max_size = max_size

        for key, value in kwargs:
            if hasattr(self, key):
                setattr(self, key, value)

        self.logger = logging.getLogger(__name__)

    async def fetch_callback(self, semaphore: Semaphore) -> Tuple[Any, Response]:
        async with semaphore:
            response = await self._fetch()

        callback_result = None

        if response.ok and self._callback:
            callback_result = self._callback(self, response)
        elif not response.ok and self._failure_callback:
            callback_result = self._failure_callback(self, response)

        return callback_result, response

    # noinspection PyProtectedMember
    async def _fetch(self) -> Response:
        history = copy.deepcopy(self.history)

        try:
            request = self.request_session.get(
                self.url, headers=self.headers, timeout=self.timeout
            )

            async with request as resp:
                history.append(resp.url)

                content_length: int = int(resp.headers.get("Content-Length", "0"))
                if content_length > self.max_size:
                    return self._failed_response(413)

                content_read, actual_content_length = await self._read_response(resp)
                if not content_read:
                    return self._failed_response(413)

                if not self.encoding:
                    self.encoding = resp.get_encoding()

                try:
                    resp_json = await self._json(resp, encoding=self.encoding)
                except ValueError:
                    resp_json = None

                resp_text = None
                if not resp_json:
                    try:
                        resp_text = await resp.text(encoding=self.encoding)
                    except UnicodeDecodeError:
                        resp_text = None

                response = Response(
                    url=resp.url,
                    method=resp.method,
                    encoding=self.encoding,
                    status_code=resp.status,
                    history=history,
                    text=resp_text,
                    data=resp._body,
                    json=resp_json,
                    headers=resp.headers,
                    xml_parser=self._parse_xml,
                    cookies=resp.cookies,
                    redirect_history=resp.history,
                    content_length=actual_content_length,
                )

                resp.raise_for_status()

        except asyncio.TimeoutError:
            self.logger.debug("Failed fetch: url=%s reason=timeout", self.url)
            history.append(self.url)
            response = self._failed_response(408, history)
        except aiohttp.ClientResponseError as e:
            self.logger.debug("Failed fetch: url=%s reason=%s", self.url, e.message)
            if not response:
                response = self._failed_response(e.status, history)
        finally:
            return response

    async def _read_response(self, resp) -> Tuple[bool, int]:
        body: bytes = b""
        while True:
            chunk = await resp.content.read(1024)
            if not chunk:
                break
            body += chunk
            if len(body) > self.max_size:
                return False, 0
        resp._body = body
        return True, len(body)

    # noinspection PyProtectedMember
    async def _json(self, resp, encoding: str = None) -> str:
        if resp._body is None:
            await self._read_response(resp)

        stripped = resp._body.strip()  # type: ignore
        if not stripped:
            return ""

        if encoding is None:
            encoding = resp.get_encoding()

        return json.loads(stripped.decode(encoding))

    def _failed_response(
        self, status: int, history: List[URL] = None, headers=None
    ) -> Response:
        return Response(
            url=self.url,
            method=self.method,
            encoding=self.encoding,
            history=history or [],
            status_code=status,
            headers=headers or {},
        )

    async def _parse_xml(self, response_text: str) -> Any:
        try:
            return await self._xml_parser(response_text)
        except Exception as e:
            self.logger.error("Error parsing response xml: %s", e)
            return None

    def __repr__(self):
        return f"{self.__class__.__name__}({str(self.url)})"
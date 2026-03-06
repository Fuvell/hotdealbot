# base_crawler.py

import os
import datetime
import logging
import urllib.request
import urllib.error
from abc import ABCMeta, abstractmethod
from typing import Any, TypedDict, Union

class BaseArticle(TypedDict):
    article_id: int
    title: str
    category: str
    site_name: str
    board_name: str
    writer_name: str
    crawler_name: str
    url: str
    is_end: bool
    extra: dict[str, Any]

class ArticleCollection(dict[int, BaseArticle]):
    def __init__(self, data: dict[int, BaseArticle] | None = None):
        super().__init__()
        if data is None:
            data = {}
        for k, v in data.items():
            self[k] = v

    def __setitem__(self, __key: Union[int, str], __value: BaseArticle) -> None:
        return super().__setitem__(int(__key), __value)

    def __getitem__(self, __key: int) -> BaseArticle:
        return super().__getitem__(__key)

class BaseCrawler(metaclass=ABCMeta):
    """
    A synchronous crawler using urllib.request, disabling compression with 'Accept-Encoding: identity'.
    """

    def __init__(self, name: str, url_list: list[str]) -> None:
        self.url_list = url_list
        self.name = name
        self.logger = logging.getLogger(f"crawler.{self.__class__.__name__}")
        self._prev_status = 200  # track repeated error codes

        # Disables compression by forcing identity, plus a realistic User-Agent
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/114.0.0.0 Safari/537.36"
            ),
            "Accept-Encoding": "identity",  # no Brotli/gzip
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://arca.live/",  # optional
        }

    def get(self) -> "ArticleCollection":
        data = ArticleCollection()
        for url in self.url_list:
            html = self.request(url)
            if html:
                parsed = self.parsing(html)
                data.update(parsed)
        return data

    def request(self, url: str) -> str | None:
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status_code = resp.getcode()
                if status_code != 200:
                    if status_code != self._prev_status:
                        self.logger.error(f"Client response error: {status_code} ({url})")
                        self.dump_http_response(resp)
                    else:
                        self.logger.info(f"Client response error [skip]: {status_code} ({url})")
                    self._prev_status = status_code
                    return None
                else:
                    self._prev_status = status_code

                raw_data = resp.read()
                encoding = "utf-8"
                html = raw_data.decode(encoding, errors="replace")
                return html

        except urllib.error.HTTPError as e:
            self.logger.error(f"HTTPError: {e.code} {e.reason} ({url})")
            return None
        except urllib.error.URLError as e:
            self.logger.error(f"URLError: {e.reason} ({url})")
            return None
        except Exception as e:
            self.logger.error(f"Exception: {e} ({url})")
            return None

    @abstractmethod
    def parsing(self, html: str) -> dict[int, BaseArticle]:
        """Parse HTML => {article_id: BaseArticle}."""
        pass

    def dump_http_response(self, resp: urllib.request.addinfourl):
        """Save raw response to 'error/' folder for debugging."""
        current_datetime = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if not os.path.exists("error"):
            os.makedirs("error")

        filename = os.path.join("error", f"{current_datetime}_{self.name}.html")
        try:
            content = resp.read()
            with open(filename, "wb") as f:
                f.write(content)
            self.logger.debug(f"Dumped response binary to {filename}")
        except Exception:
            self.logger.debug("Could not dump HTTP response")

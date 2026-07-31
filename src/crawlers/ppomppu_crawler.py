from __future__ import annotations

import re
import time
from typing import Dict, List
from urllib.parse import urljoin

import requests

try:
    from .base_crawler import BaseArticle, BaseCrawler, make_soup
except ImportError:
    from base_crawler import BaseArticle, BaseCrawler, make_soup

try:
    from ..deal_key import encode_deal_key
except ImportError:
    from deal_key import encode_deal_key


PPOMPPU_BOARD_ID = "ppomppu"
PPOMPPU_SITE_CODE = "ppomppu"
PPOMPPU_BASE_URL = "https://www.ppomppu.co.kr"
PPOMPPU_LIST_URL = "https://www.ppomppu.co.kr/zboard/zboard.php?id=ppomppu"
PPOMPPU_BLOCK_STATUS_CODES = {403, 404, 429}
PPOMPPU_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
PPOMPPU_LOGO_URL = "https://www.google.com/s2/favicons?domain=ppomppu.co.kr&sz=64"


def _normalize_category_token(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "")).casefold()


def _strip_leading_bracket_tags(title: str) -> str:
    return re.sub(r"^(?:\[[^\]]+\]\s*)+", "", str(title or "").strip()).strip()


def _extract_price_from_title(title: str) -> str:
    title_text = str(title or "")

    chunks: list[str] = []
    for m in re.finditer(r"\(([^()]*)\)|\[([^\[\]]*)\]", title_text):
        candidate = m.group(1) or m.group(2)
        if candidate:
            chunks.append(candidate)

    for chunk in chunks:
        text = str(chunk).strip()
        if not text:
            continue
        if any(token in text for token in ("원", "$", "€", "¥", "무료", "공짜", "무배")):
            left = text.split("/", 1)[0].strip()
            return left or text

    match = re.search(r"\d[\d,]*(?:\.\d+)?\s*원", title_text)
    if match:
        return match.group(0).strip()

    return ""


def _extract_trailing_category(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""
    match = re.search(r"\[([^\[\]]+)\]\s*$", text)
    if match is None:
        return ""
    return match.group(1).strip()


_PPOMPPU_CATEGORY_MAP_RAW = {
    "식품/건강": "식품",
    "식품": "식품",
    "디지털": "전자제품",
    "컴퓨터": "PC",
    "컴퓨터/주변기기": "PC",
    "가전": "가전",
    "가전/가구": "가전",
    "생활/주방": "생활용품",
    "생활": "생활용품",
    "육아": "생활용품",
    "의류/잡화": "의류",
    "패션/의류": "의류",
    "화장품": "화장품",
    "뷰티": "화장품",
    "상품권/쿠폰": "상품권",
    "쿠폰": "상품권",
    "기프티콘": "상품권",
    "포인트": "상품권",
    "게임/SW": "게임",
    "SW/게임": "게임",
    "기타": "기타",
}

PPOMPPU_CATEGORY_MAP = {
    _normalize_category_token(raw): canonical
    for raw, canonical in _PPOMPPU_CATEGORY_MAP_RAW.items()
}


def normalize_ppomppu_category(raw_category: str, title: str) -> str:
    token = _normalize_category_token(raw_category)
    if token:
        mapped = PPOMPPU_CATEGORY_MAP.get(token)
        if mapped:
            return mapped

    merged = _normalize_category_token(f"{raw_category} {title}")
    if not merged:
        return "기타"

    if any(k in merged for k in ("식품", "먹거리", "음료", "커피", "건강")):
        return "식품"
    if any(k in merged for k in ("생활", "리빙", "세제", "휴지", "주방", "욕실")):
        return "생활용품"
    if any(k in merged for k in ("게임", "steam", "스팀", "닌텐도", "xbox", "ps5", "ps4")):
        return "게임"
    if any(k in merged for k in ("pc", "cpu", "그래픽", "ssd", "램", "메모리", "하드웨어")):
        return "PC"
    if any(k in merged for k in ("전자", "휴대폰", "폰", "스마트폰", "태블릿", "이어폰")):
        return "전자제품"
    if any(k in merged for k in ("가전", "tv", "냉장고", "세탁기", "청소기")):
        return "가전"
    if any(k in merged for k in ("패션", "의류", "신발", "운동화", "맨투맨", "후드", "바지")):
        return "의류"
    if any(k in merged for k in ("화장품", "뷰티", "스킨", "로션", "선크림", "향수")):
        return "화장품"
    if any(k in merged for k in ("상품권", "쿠폰", "기프티콘", "포인트", "적립")):
        return "상품권"
    return "기타"


class PpomppuCrawler(BaseCrawler):
    def __init__(self, name: str, url_list: list[str]) -> None:
        super().__init__(name=name, url_list=url_list)
        self._session = requests.Session()
        self._warmed_up = False
        self._configure_headers()

    def _configure_headers(self) -> None:
        self.headers.update(
            {
                "User-Agent": PPOMPPU_DESKTOP_USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Connection": "keep-alive",
                "Referer": PPOMPPU_BASE_URL + "/",
            }
        )
        self.headers.pop("Accept-Encoding", None)

    def _warm_up_session(self) -> None:
        if self._warmed_up:
            return
        try:
            self._session.get(
                PPOMPPU_BASE_URL + "/",
                headers=self.headers,
                timeout=10,
                allow_redirects=True,
            )
        except requests.RequestException:
            pass
        self._warmed_up = True

    def _request_once(self, url: str) -> requests.Response | None:
        try:
            return self._session.get(
                url,
                headers=self.headers,
                timeout=15,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            self.logger.error(f"Request error: {e} ({url})")
            return None

    def request(self, url: str) -> str | None:
        self._warm_up_session()

        for attempt in (1, 2):
            response = self._request_once(url)
            if response is None:
                return None

            status_code = int(response.status_code)
            if status_code == 200:
                self._prev_status = status_code
                response.encoding = response.apparent_encoding or response.encoding or "euc-kr"
                return response.text

            if status_code in PPOMPPU_BLOCK_STATUS_CODES and attempt == 1:
                self.logger.info(f"Blocked response: {status_code} ({url}); retrying once.")
                self._warmed_up = False
                self._warm_up_session()
                time.sleep(0.25)
                continue

            if status_code != self._prev_status:
                self.logger.error(f"Client response error: {status_code} ({url})")
            else:
                self.logger.info(f"Client response error [skip]: {status_code} ({url})")
            self._prev_status = status_code
            return None

        return None

    def parsing(self, html: str) -> Dict[int, BaseArticle]:
        soup = make_soup(html)
        rows = soup.select("tr.baseList")
        if not rows:
            self.logger.error("Can't find article list rows, skip parsing")
            return {}

        data: Dict[int, BaseArticle] = {}
        for row in rows:
            no_cell = row.select_one("td.baseList-numb")
            if no_cell is None:
                continue

            no_text = no_cell.get_text(" ", strip=True)
            if not no_text.isdigit():
                continue
            article_id = int(no_text)

            title_link = row.select_one("a.baseList-title[href*='view.php?id=ppomppu']")
            if title_link is None:
                title_link = row.select_one("a[href*='view.php?id=ppomppu']")
            if title_link is None:
                continue

            href = str(title_link.get("href", "") or "").strip()
            if not href:
                continue

            raw_title = title_link.get_text(" ", strip=True)
            title = _strip_leading_bracket_tags(raw_title)
            if not title:
                continue

            title_cell = row.select_one("td.title")
            title_cell_text = title_cell.get_text(" ", strip=True) if title_cell else raw_title
            raw_category = _extract_trailing_category(title_cell_text)

            image_url = None
            image_tag = row.select_one("a.baseList-thumb img")
            if image_tag is not None:
                src = image_tag.get("src") or image_tag.get("data-original") or image_tag.get(
                    "data-src"
                )
                if src:
                    image_url = urljoin(PPOMPPU_BASE_URL, str(src))

            data[article_id] = {
                "article_id": article_id,
                "board_id": PPOMPPU_BOARD_ID,
                "title": title,
                "category": normalize_ppomppu_category(raw_category, title),
                "site_name": "뽐뿌",
                "site_color": "aad7ff",
                "board_name": "뽐뿌게시판",
                "writer_name": (
                    row.select_one("td:nth-child(3)").get_text(" ", strip=True)
                    if row.select_one("td:nth-child(3)")
                    else ""
                ),
                "crawler_name": self.name,
                "logo": PPOMPPU_LOGO_URL,
                "url": urljoin(PPOMPPU_BASE_URL + "/zboard/", href),
                "is_end": False,
                "extra": {
                    "price": _extract_price_from_title(raw_title) or _extract_price_from_title(title),
                    "image_url": image_url,
                },
            }
        return data


_CRAWLER: PpomppuCrawler | None = None


def _get_crawler() -> PpomppuCrawler:
    global _CRAWLER
    if _CRAWLER is None:
        _CRAWLER = PpomppuCrawler(
            name="Ppomppu",
            url_list=[PPOMPPU_LIST_URL],
        )
    return _CRAWLER


def fetch_hot_deals_ppomppu() -> List[dict]:
    crawler = _get_crawler()
    articles = crawler.get()

    deals_list: List[dict] = []
    for article_id, article in articles.items():
        extra = article.get("extra", {})
        if not isinstance(extra, dict):
            extra = {}

        title = str(article.get("title", "") or "").strip()
        if not title:
            continue

        try:
            deal_id = encode_deal_key("pp", PPOMPPU_BOARD_ID, article_id)
        except ValueError as e:
            crawler.logger.warning(f"Skipping article with invalid key parts: {e}")
            continue

        deals_list.append(
            {
                "id": deal_id,
                "site_code": PPOMPPU_SITE_CODE,
                "title": title,
                "category": str(article.get("category", "기타") or "기타"),
                "price": str(extra.get("price", "") or "").strip(),
                "url": str(article.get("url", "") or "").strip(),
                "logo": str(article.get("logo", "") or "").strip(),
                "site_color": str(article.get("site_color", "aad7ff") or "aad7ff"),
                "site_name": str(article.get("site_name", "뽐뿌") or "뽐뿌"),
                "image_url": extra.get("image_url"),
            }
        )
    return deals_list

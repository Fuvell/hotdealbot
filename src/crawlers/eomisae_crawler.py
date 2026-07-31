from __future__ import annotations

import re
from typing import Dict, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:
    from .base_crawler import BaseArticle, BaseCrawler, make_soup
except ImportError:
    from base_crawler import BaseArticle, BaseCrawler, make_soup

DETAIL_CACHE_MAX_ENTRIES = 500

try:
    from ..deal_key import encode_deal_key
except ImportError:
    from deal_key import encode_deal_key


EOMISAE_BOARD_ID = "fs"
EOMISAE_SITE_CODE = "eomisae"
EOMISAE_BASE_URL = "https://eomisae.co.kr"
EOMISAE_LIST_URL = "https://eomisae.co.kr/fs"


def _normalize_category_token(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "")).casefold()


def _normalize_eomisae_url(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    return urljoin(EOMISAE_BASE_URL, text)


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
        if any(token in text for token in ("원", "$", "€", "¥", "발", "무배", "무료", "공짜")):
            return text

    return _extract_price_from_text(title_text)


def _extract_price_from_text(text: str) -> str:
    if not text:
        return ""

    normalized = str(text).replace("\u00a0", " ")
    patterns = (
        r"\$\s?\d[\d,]*(?:\.\d+)?",
        r"\d[\d,]*(?:\.\d+)?\s*만원",
        r"\d[\d,]*(?:\.\d+)?\s*원",
        r"\d[\d,]*(?:\.\d+)?\s*발",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return ""


def _is_content_image_url(url: str) -> bool:
    lower = str(url or "").lower()
    if not lower:
        return False
    if not lower.startswith(("http://", "https://")):
        return False
    if "/files/attach/images/" in lower:
        return True
    blocked_tokens = (
        "/modules/",
        "/layouts/",
        "/addons/",
        "/common/",
        "/sejin7940_rank/icons/",
        "icons_eomisae",
    )
    return not any(token in lower for token in blocked_tokens)


_EOMISAE_CATEGORY_MAP_RAW = {
    "패션해외": "의류",
    "패션국내": "의류",
    "패션": "의류",
    "의류": "의류",
    "뷰티": "화장품",
    "화장품": "화장품",
    "전자": "전자제품",
    "디지털": "전자제품",
    "컴퓨터": "PC",
    "pc": "PC",
    "가전": "가전",
    "생활": "생활용품",
    "리빙": "생활용품",
    "식품": "식품",
    "쿠폰": "상품권",
    "상품권": "상품권",
    "포인트": "상품권",
    "기프티콘": "상품권",
}

EOMISAE_CATEGORY_MAP = {
    _normalize_category_token(raw): canonical
    for raw, canonical in _EOMISAE_CATEGORY_MAP_RAW.items()
}


def normalize_eomisae_category(raw_category: str, title: str) -> str:
    token = _normalize_category_token(raw_category)
    if token:
        mapped = EOMISAE_CATEGORY_MAP.get(token)
        if mapped:
            return mapped

    merged = _normalize_category_token(f"{raw_category} {title}")
    if not merged:
        return "기타"

    if any(k in merged for k in ("패션", "의류", "신발", "운동화", "맨투맨", "후드", "바지")):
        return "의류"
    if any(k in merged for k in ("화장품", "뷰티", "스킨", "로션", "향수", "선크림")):
        return "화장품"
    if any(k in merged for k in ("pc", "cpu", "그래픽", "ssd", "메모리", "하드웨어")):
        return "PC"
    if any(k in merged for k in ("전자", "휴대폰", "스마트폰", "태블릿", "이어폰")):
        return "전자제품"
    if any(k in merged for k in ("가전", "tv", "냉장고", "세탁기", "청소기")):
        return "가전"
    if any(k in merged for k in ("식품", "먹거리", "음료", "커피", "건강식품")):
        return "식품"
    if any(k in merged for k in ("생활", "리빙", "주방", "욕실", "세제")):
        return "생활용품"
    if any(k in merged for k in ("게임", "steam", "스팀", "닌텐도", "xbox", "ps5", "ps4")):
        return "게임"
    if any(k in merged for k in ("상품권", "쿠폰", "기프티콘", "포인트", "적립")):
        return "상품권"
    return "기타"


class EomisaeCrawler(BaseCrawler):
    def __init__(self, name: str, url_list: list[str]) -> None:
        super().__init__(name=name, url_list=url_list)
        self.headers["Referer"] = EOMISAE_BASE_URL + "/"
        self._detail_cache: dict[str, tuple[str | None, str]] = {}

    def extract_image_and_price_from_detail(self, detail_url: str) -> tuple[str | None, str]:
        cached = self._detail_cache.get(detail_url)
        if cached is not None:
            return cached
        if len(self._detail_cache) >= DETAIL_CACHE_MAX_ENTRIES:
            self._detail_cache.clear()

        html = self.request(detail_url)
        if not html:
            result = (None, "")
            self._detail_cache[detail_url] = result
            return result

        soup = make_soup(html)
        content = (
            soup.select_one("div.xe_content")
            or soup.select_one("div.rd_body")
            or soup.select_one("article")
        )

        image_url = None
        search_root = content if content is not None else soup
        for image_tag in search_root.select("img"):
            src = (
                image_tag.get("src")
                or image_tag.get("data-src")
                or image_tag.get("data-original")
            )
            if not src:
                continue
            absolute_url = urljoin(EOMISAE_BASE_URL, str(src))
            if _is_content_image_url(absolute_url):
                image_url = absolute_url
                break

        content_text = content.get_text(" ", strip=True) if content is not None else ""
        price = _extract_price_from_text(content_text)

        result = (image_url, price)
        self._detail_cache[detail_url] = result
        return result

    def _parse_card_layout(self, soup: BeautifulSoup) -> Dict[int, BaseArticle]:
        cards = soup.select("div.bd_card div.card_el")
        if not cards:
            return {}

        data: Dict[int, BaseArticle] = {}
        for card in cards:
            title_link = card.select_one("h3 a.pjax[href]")
            if title_link is None:
                title_link = card.select_one("a.pjax[href*='/fs/']")
            if title_link is None:
                continue

            href = str(title_link.get("href", "") or "").strip()
            id_match = re.search(r"/fs/(\d+)", href)
            if id_match is None:
                continue
            article_id = int(id_match.group(1))

            title = title_link.get_text(" ", strip=True)
            if not title:
                continue

            raw_category = ""
            category_tag = card.select_one("span.cate")
            if category_tag is not None:
                raw_category = category_tag.get_text(" ", strip=True).rstrip(",").strip()

            writer_name = ""
            writer_tag = card.select_one(".info span div")
            if writer_tag is not None:
                writer_name = writer_tag.get_text(" ", strip=True)

            image_url: str | None = None
            image_tag = card.select_one("img.tmb")
            if image_tag is not None:
                image_url_text = _normalize_eomisae_url(
                    str(image_tag.get("src") or image_tag.get("data-src") or "")
                )
                if image_url_text:
                    image_url = image_url_text

            article_url = _normalize_eomisae_url(href)
            price = _extract_price_from_title(title)
            # NOTE: no detail-page fetch here — the service enriches only NEW
            # deals (after dedup) via enrich_deal_eomisae() below.

            data[article_id] = {
                "article_id": article_id,
                "board_id": EOMISAE_BOARD_ID,
                "title": title,
                "category": normalize_eomisae_category(raw_category, title),
                "site_name": "\uc5b4\ubbf8\uc0c8",
                "site_color": "da0022",
                "board_name": "\ud56b\ub51c\uc815\ubcf4",
                "writer_name": writer_name,
                "crawler_name": self.name,
                "logo": "https://play-lh.googleusercontent.com/Aq8BfRkCKY_D79xhkyqPXzbc4Z2RXMNbQEpNK_074h5-m_Mr0jH-NYQhf2QLTRYWWIPP",
                "url": article_url,
                "is_end": False,
                "extra": {
                    "price": price,
                    "image_url": image_url,
                },
            }
        return data

    def _parse_table_layout(self, soup: BeautifulSoup) -> Dict[int, BaseArticle]:
        table = soup.select_one("table._listA")
        if table is None:
            return {}

        rows = table.select("tr")
        data: Dict[int, BaseArticle] = {}

        for row in rows:
            no_cell = row.select_one("td.no")
            title_cell = row.select_one("td.title")
            if no_cell is None or title_cell is None:
                continue

            no_text = no_cell.get_text(" ", strip=True)
            if "공지" in no_text:
                continue
            if "AD" in no_text.upper() and not any(ch.isdigit() for ch in no_text):
                continue

            title_link = None
            for anchor in title_cell.select("a[href]"):
                href = str(anchor.get("href", "") or "").strip()
                if re.match(r"^/fs/\d+$", href):
                    title_link = anchor
                    break
            if title_link is None:
                continue

            href = str(title_link.get("href", "") or "").strip()
            id_match = re.search(r"/fs/(\d+)$", href)
            if id_match is None:
                continue
            article_id = int(id_match.group(1))

            title = title_link.get_text(" ", strip=True)
            if not title:
                continue

            raw_parts = list(title_cell.stripped_strings)
            raw_category = ""
            if raw_parts:
                first = str(raw_parts[0]).strip()
                if first and first != title and not first.isdigit():
                    raw_category = first

            article_url = urljoin(EOMISAE_BASE_URL, href)
            image_url = None
            price = _extract_price_from_title(title)

            data[article_id] = {
                "article_id": article_id,
                "board_id": EOMISAE_BOARD_ID,
                "title": title,
                "category": normalize_eomisae_category(raw_category, title),
                "site_name": "어미새",
                "site_color": "da0022",
                "board_name": "인기정보",
                "writer_name": (
                    row.select_one("td:nth-child(3)").get_text(" ", strip=True)
                    if row.select_one("td:nth-child(3)")
                    else ""
                ),
                "crawler_name": self.name,
                "logo": "https://play-lh.googleusercontent.com/Aq8BfRkCKY_D79xhkyqPXzbc4Z2RXMNbQEpNK_074h5-m_Mr0jH-NYQhf2QLTRYWWIPP",
                "url": article_url,
                "is_end": False,
                "extra": {
                    "price": price,
                    "image_url": image_url,
                },
            }
        return data

    def parsing(self, html: str) -> Dict[int, BaseArticle]:
        soup = make_soup(html)

        card_data = self._parse_card_layout(soup)
        if card_data:
            return card_data

        table_data = self._parse_table_layout(soup)
        if table_data:
            return table_data

        self.logger.error("Can't find article list in known layouts, skip parsing")
        return {}


_CRAWLER: EomisaeCrawler | None = None


def _get_crawler() -> EomisaeCrawler:
    global _CRAWLER
    if _CRAWLER is None:
        _CRAWLER = EomisaeCrawler(
            name="Eomisae",
            url_list=[EOMISAE_LIST_URL],
        )
    return _CRAWLER


def enrich_deal_eomisae(deal: dict) -> None:
    """Fill in detail-page image/price for a NEW deal (called after dedup)."""
    needs_image = not deal.get("image_url")
    needs_price = not str(deal.get("price", "") or "").strip()
    if not needs_image and not needs_price:
        return

    url = str(deal.get("url", "") or "").strip()
    if not url:
        return

    detail_image_url, detail_price = _get_crawler().extract_image_and_price_from_detail(url)
    if needs_image and detail_image_url:
        deal["image_url"] = detail_image_url
    if needs_price and detail_price:
        deal["price"] = detail_price


def fetch_hot_deals_eomisae() -> List[dict]:
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
            deal_id = encode_deal_key("eo", EOMISAE_BOARD_ID, article_id)
        except ValueError as e:
            crawler.logger.warning(f"Skipping article with invalid key parts: {e}")
            continue

        deals_list.append(
            {
                "id": deal_id,
                "site_code": EOMISAE_SITE_CODE,
                "title": title,
                "category": str(article.get("category", "기타") or "기타"),
                "price": str(extra.get("price", "") or "").strip(),
                "url": str(article.get("url", "") or "").strip(),
                "logo": str(article.get("logo", "") or "").strip(),
                "site_color": str(article.get("site_color", "da0022") or "da0022"),
                "site_name": str(article.get("site_name", "어미새") or "어미새"),
                "image_url": extra.get("image_url"),
            }
        )
    return deals_list

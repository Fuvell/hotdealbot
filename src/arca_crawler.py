# arca_crawler.py

import re
from bs4 import BeautifulSoup
from typing import Dict, List

from base_crawler import BaseCrawler, BaseArticle, ArticleCollection

class ArcaLiveCrawler(BaseCrawler):
    def parsing(self, html: str) -> Dict[int, BaseArticle]:
        soup = BeautifulSoup(html, "html.parser")

        # 채널 이름
        if (_board_name := soup.select_one(".board-title .title")) is None \
           or (board_name := _board_name.attrs.get("data-channel-name")) is None:
            self.logger.error("Can't find board name, skip parsing")
            return {}

        # 게시글 목록
        if (table := soup.select_one(".list-table")) is None:
            self.logger.error("Can't find article list, skip parsing")
            return {}

        rows = table.select(".vrow.hybrid")
        data: Dict[int, BaseArticle] = {}

        for row in rows:
            _title_tag = row.select_one(".title")
            if not _title_tag:
                self.logger.warning("No title tag")
                continue
            # multiple text nodes
            _title_strings = _title_tag.find_all(string=True, recursive=False)
            if not _title_strings:
                self.logger.warning("No title strings")
                continue
            title = "".join(_title_strings).strip()

            # Extract image URL
            image_url = None
            preview_image = row.select_one(".vrow-preview img")
            if preview_image and preview_image.attrs.get("src"):
                image_url = preview_image.attrs.get("src")
                if image_url.startswith("//"):
                    image_url = "https:" + image_url


            # parse ID from href
            _url = _title_tag.attrs.get("href")
            if not _url:
                self.logger.warning("No href in title tag")
                continue
            re_id = re.match(r"/b/([\w\d]+)/(\d+)\??.*", _url)
            if not re_id:
                self.logger.warning("Cannot parse article id from url")
                continue

            _board_id = re_id.group(1)
            _id = int(re_id.group(2))

            _category_tag = row.select_one(".badge")
            _store_name_tag = row.select_one(".deal-store")
            _writer_tag = row.select_one(".user-info span:first-child")
            _recommend_tag = row.select_one(".col-rate")
            _view_tag = row.select_one(".col-view")
            _price_tag = row.select_one(".deal-price")
            _delivery_tag = row.select_one(".deal-delivery")

            if not all((_category_tag, _store_name_tag, _writer_tag, _recommend_tag, _view_tag, _price_tag, _delivery_tag)):
                self.logger.warning("Missing required tags, skip row.")
                continue

            is_end = bool(row.select_one(".deal-close"))

            data[_id] = {
                "article_id": _id,
                "title": title,
                "category": _category_tag.text.strip(),
                "site_name": "아카라이브",
                "site_color": "ffffff",
                "board_name": board_name,
                "writer_name": _writer_tag.text.strip(),
                "crawler_name": self.name,
                "logo": "https://ac-p2.namu.la/20210404/e3e3eb4e00a1cef4fc9259f603f477fa60c25710676b8d3353a6b9c628962a68.png?expires=1735986941&key=i8bB2GfIMvL8I3Hd6yA2pA",
                "url": f"https://arca.live/b/{_board_id}/{_id}",
                "is_end": is_end,
                "extra": {
                    "recommend": _recommend_tag.text.strip(),
                    "view": _view_tag.text.strip(),
                    "price": _price_tag.text.strip(),
                    "delivery": _delivery_tag.text.strip(),
                    "image_url": image_url
                },
            }
        return data


def fetch_hot_deals_arca() -> List[dict]:
    """
    Synchronous function:
    Fetches arca.live/b/hotdeal with 'Accept-Encoding: identity'
    so we get uncompressed HTML.
    Returns a list of dict { id, title, price, url, ... }
    """
    crawler = ArcaLiveCrawler(
        name="ArcaLive",
        url_list=["https://arca.live/b/hotdeal"]
    )
    articles = crawler.get()
    deals_list = []
    for article_id, article in articles.items():
        deals_list.append({
            "id": f"al-{article_id}",
            "title": article["title"],
            "category": article["category"],
            "price": article["extra"].get("price", ""),
            "url": article["url"],
            "logo": article["logo"],
            "site_color": article["site_color"],
            "site_name": article["site_name"],
            "image_url": article["extra"].get("image_url"),
            # anything else you want
        })
    return deals_list

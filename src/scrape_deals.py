import os
import re
import requests
from typing import Dict, List
from bs4 import BeautifulSoup, Tag

from base_crawler import BaseCrawler, BaseArticle, ArticleCollection

class QuasarzoneCrawler(BaseCrawler):
    def parsing(self, html: str) -> Dict[int, BaseArticle]:
        soup = BeautifulSoup(html, "html.parser")

        if (_board_name := soup.select_one(".l-title h2")) is None:
            self.logger.error("Can't find board name, skip parsing")
            return {}

        board_name = _board_name.text.strip()

        if (table := soup.select_one(".market-info-type-list > table > tbody")) is None:
            self.logger.error("Can't find article list, skip parsing")
            return {}

        rows = table.select("tr")
        data: Dict[int, BaseArticle] = {}

        for row in rows:
            if (_url_tag := row.select_one(".subject-link")) is None or (_url := _url_tag.attrs.get("href")) is None:
                self.logger.warning("Cannot find article url tag")
                continue

            if (_re_url := re.search(r"/bbs/([\w\d_]+)/views/(\d+)", _url)) is None:
                self.logger.warning("Cannot find board id and article id")
                continue

            # Locked article?
            if row.select_one(".fa-lock"):
                self.logger.debug("Locked article, skip.")
                continue

            # Title
            if (_title_tag := row.select_one(".ellipsis-with-reply-cnt")) is None or not _title_tag.text:
                self.logger.warning("Cannot find article title tag")
                continue
            raw_title = _title_tag.text.strip()
            title = re.sub(r"^\[.*?\]\s*", "", raw_title)

            # Image
            image_url = None
            for _image_tag in row.select("img"):
                 _src = _image_tag.attrs.get("src") or _image_tag.attrs.get("data-src")
                 if _src and "tangerine.png" not in _src:
                    image_url = _src
                    break

            # Writer
            if (_nick_tag := row.select_one(".nick")) is None or not _nick_tag.attrs.get("data-nick"):
                self.logger.warning("Cannot find article writer tag")
                continue

            # Recommend / view
            if (_recommend_tag := row.select_one("td .num")) is None or not _recommend_tag.text:
                self.logger.warning("Cannot get recommend value tag")
                continue
            if (_view_tag := row.select_one(".count")) is None or not _view_tag.text:
                self.logger.warning("Cannot get view count tag")
                continue

            # Additional info
            if (_info_tag := row.select_one(".market-info-sub p:first-child")) is None:
                self.logger.warning("Cannot get sub info tag")
                continue

            # Check if ended
            is_end = False
            if (_is_end_tag := row.select_one(".label")) and _is_end_tag.text.strip() == "종료":
                is_end = True

            _board_id = _re_url.group(1)
            _id = int(_re_url.group(2))

            # Parse sub info
            _info = self.info_tag_parser(_info_tag)
            _category = _info.pop("category", "")
            if not _category:
                self.logger.warning("Cannot get category")

            data[_id] = {
                "article_id": _id,
                "title": title,
                "category": _category,
                "site_name": "퀘이사존",
                "site_color": "ff9900",
                "board_name": board_name,
                "writer_name": _nick_tag.text.strip(),
                "crawler_name": self.name,
                "logo": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSbxUBUWksXWUh0hKjndR29gmjbxdF2yVxFQg&s",
                "url": f"https://quasarzone.com/bbs/{_board_id}/views/{_id}",
                "is_end": is_end,
                "extra": {
                    "recommend": _recommend_tag.text.strip(),
                    "view": _view_tag.text.strip(),
                    "image_url": image_url,  # Add the image URL here
                    **_info
                }
            }
        return data

    def info_tag_parser(self, el: Tag) -> dict:
        data = {}
        for e in el.find_all("span", recursive=False):
            if not isinstance(e, Tag):
                continue
            if "category" in e.attrs.get("class", []):
                data["category"] = e.text.strip()
            elif e.find(text=True, recursive=False) and "가격" in e.text:
                price_el = e.find("span")
                data["price"] = price_el.text.strip() if price_el else ""
            elif e.find(text=True, recursive=False) and "직배" in e.text:
                data["direct_delivery"] = "가능" in e.text
            elif "배송비" in e.text.strip():
                data["delivery"] = e.text.replace("배송비", "").strip()
        return data


def fetch_hot_deals() -> List[dict]:
    """
    Synchronously fetch the Quasarzone 핫딜 page and return a list of deals.
    """
    crawler = QuasarzoneCrawler(
        name="Quasarzone",
        url_list=["https://quasarzone.com/bbs/qb_saleinfo"]
    )

    articles_dict = crawler.get()  # Synchronous

    deals_list = []
    for article_id, article in articles_dict.items():
        deals_list.append({
            "id": f"qz-{article_id}",
            "title": article["title"],
            "category": article["category"],
            "price": article["extra"].get("price", ""),
            "url": article["url"],
            "logo": article["logo"],
            "site_color": article["site_color"],
            "site_name": article["site_name"],
            "image_url": article["extra"].get("image_url"),  # Include the image URL
        })

    return deals_list
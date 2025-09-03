# -*- coding: utf-8 -*-

# Copyright 2025 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractors for https://fansly.com/"""

from .common import Extractor, Message
from .. import text
import time

BASE_PATTERN = r"(?:https?://)?(?:www\.)?fansly\.com"


class FanslyExtractor(Extractor):
    """Base class for fansly extractors"""
    category = "fansly"
    root = "https://fansly.com"
    directory_fmt = ("{category}", "{account[username]} ({account[id]})")
    filename_fmt = "{id}_{num}_{file[id]}.{extension}"
    archive_fmt = "{file[id]}"

    def _init(self):
        self.api = FanslyAPI(self)

    def items(self):
        for post in self.posts():
            files = self._extract_files(post)
            post["count"] = len(files)
            post["date"] = text.parse_timestamp(post["createdAt"])

            yield Message.Directory, post
            for post["num"], file in enumerate(files, 1):
                post.update(file)
                url = file["url"]
                yield Message.Url, url, text.nameext_from_url(url, post)

    def _extract_files(self, post):
        files = []

        for attachment in post.pop("attachments"):
            media = attachment["media"]
            file = {
                **media,
                "date": text.parse_timestamp(media["createdAt"]),
                "date_updated": text.parse_timestamp(media["updatedAt"]),
            }

            width = 0
            for variant in media["variants"]:
                if variant["width"] > width:
                    width = variant["width"]
                    variant_max = variant
                if variant["type"] == 303:
                    break
            else:
                # image
                file["type"] = "image"
                files.append({
                    "file": file,
                    "url" : variant_max["locations"][0]["location"],
                })
                continue

            # video
            location = variant["locations"][0]
            meta = location["metadata"]

            file["type"] = "video"
            files.append({
                "file": file,
                "url": f"ytdl:{location['location']}",
                "_fallback": (media["locations"][0]["location"],),
                "_ytdl_manifest": "dash",
                "_ytdl_manifest_cookies": (
                    ("CloudFront-Key-Pair-Id", meta["Key-Pair-Id"]),
                    ("CloudFront-Signature"  , meta["Signature"]),
                    ("CloudFront-Policy"     , meta["Policy"]),
                ),
            })

        return files


class FanslyPostExtractor(FanslyExtractor):
    subcategory = "post"
    pattern = rf"{BASE_PATTERN}/post/(\d+)"
    example = "https://fansly.com/post/1234567890"

    def posts(self):
        return self.api.post(self.groups[0])


class FanslyHomeExtractor(FanslyExtractor):
    subcategory = "home"
    pattern = rf"{BASE_PATTERN}/home(?:/(?:subscribed()|list/(\d+)))?"
    example = "https://fansly.com/home"

    def posts(self):
        subscribed, list_id = self.groups
        if subscribed is not None:
            mode = "1"
        elif list_id is not None:
            mode = None
        else:
            mode = "0"
        return self.api.timeline_home(mode, list_id)


class FanslyListExtractor(FanslyExtractor):
    subcategory = "list"
    pattern = rf"{BASE_PATTERN}/lists/(\d+)"
    example = "https://fansly.com/lists/1234567890"

    def items(self):
        base = f"{self.root}/"
        for account in self.api.lists_itemsnew(self.groups[0]):
            account["_extractor"] = FanslyCreatorPostsExtractor
            url = f"{base}{account['username']}"
            yield Message.Queue, url, account


class FanslyListsExtractor(FanslyExtractor):
    subcategory = "lists"
    pattern = rf"{BASE_PATTERN}/lists"
    example = "https://fansly.com/lists"

    def items(self):
        base = f"{self.root}/lists/"
        for list in self.api.lists_account():
            list["_extractor"] = FanslyListExtractor
            url = f"{base}{list['id']}#{list['label']}"
            yield Message.Queue, url, list


class FanslyCreatorPostsExtractor(FanslyExtractor):
    subcategory = "creator-posts"
    pattern = rf"{BASE_PATTERN}/([^/?#]+)/posts"
    example = "https://fansly.com/CREATOR/posts"

    def posts(self):
        creator = self.groups[0]
        if creator.startswith("id:"):
            pass
        else:
            account = self.api.account(self.groups[0])
            wall_id = account["walls"][0]["id"]
        return self.api.timeline_new(account["id"], wall_id)


class FanslyAPI():
    ROOT = "https://apiv3.fansly.com"

    def __init__(self, extractor):
        self.extractor = extractor

        token = extractor.config("token")
        if not token:
            self.extractor.log.warning("No 'token' provided")

        self.headers = {
            "fansly-client-ts": None,
            "Origin"          : extractor.root,
            "authorization"   : token,
        }

    def account(self, username):
        endpoint = "/v1/account"
        params = {"usernames": username}
        return self._call(endpoint, params)["response"][0]

    def account_by_id(self, account_id):
        endpoint = "/v1/account"
        params = {"ids": account_id}
        return self._call(endpoint, params)["response"][0]

    def accounts_by_id(self, account_ids):
        endpoint = "/v1/account"
        params = {"ids": ",".join(map(str, account_ids))}
        return self._call(endpoint, params)["response"]

    def lists_account(self):
        endpoint = "/v1/lists/account"
        params = {"itemId": ""}
        return self._call(endpoint, params)["response"]

    def lists_itemsnew(self, list_id, sort="3"):
        endpoint = "/v1/lists/itemsnew"
        params = {
            "listId"  : list_id,
            "limit"   : 50,
            "after"   : None,
            "sortMode": sort,
        }
        return self._pagination_items(endpoint, params)

    def post(self, post_id):
        endpoint = "/v1/post"
        params = {"ids": post_id}
        return self._update_posts(self._call(endpoint, params))

    def timeline_home(self, mode="0", list_id=None):
        endpoint = "/v1/timeline/home"
        params = {"before": "0", "after": "0"}
        if list_id is None:
            params["mode"] = mode
        else:
            params["listId"] = list_id
        return self._pagination(endpoint, params)

    def timeline_new(self, account_id, wall_id):
        endpoint = f"/v1/timelinenew/{account_id}"
        params = {
            "before"       : "0",
            "after"        : "0",
            "wallId"       : wall_id,
            "contentSearch": "",
        }
        return self._pagination(endpoint, params)

    def _update_posts(self, data):
        response = data["response"]
        accounts = {
            account["id"]: account
            for account in response["accounts"]
        }
        media = {
            media["id"]: media
            for media in response["accountMedia"]
        }
        bundles = {
            bundle["id"]: bundle
            for bundle in response["accountMediaBundles"]
        }

        posts = response["posts"]
        for post in posts:
            post["account"] = accounts[post.pop("accountId")]

            att = []
            for attachment in post["attachments"]:
                cid = attachment["contentId"]
                if cid in media:
                    att.append(media[cid])
                elif cid in bundles:
                    content = bundles[cid]["bundleContent"]
                    content.sort(key=lambda c: c["pos"])
                    att.extend(
                        media[c["accountMediaId"]]
                        for c in content
                    )
            post["attachments"] = att
        return posts

    def _update_items(self, items):
        ids = [item["id"] for item in items]
        accounts = {
            account["id"]: account
            for account in self.accounts_by_id(ids)
        }
        return [accounts[id] for id in ids]

    def _call(self, endpoint, params):
        url = f"{self.ROOT}/api{endpoint}"
        params["ngsw-bypass"] = "true"
        headers = self.headers.copy()
        headers["fansly-client-ts"] = str(int(time.time() * 1000))

        return self.extractor.request_json(url, params=params, headers=headers)

    def _pagination(self, endpoint, params):
        while True:
            data = self._call(endpoint, params)

            posts = data["response"]
            if not posts:
                return
            yield from self._update_posts(data)

            params["before"] = min(p["id"] for p in posts)

    def _pagination_items(self, endpoint, params):
        while True:
            data = self._call(endpoint, params)

            items = data["response"]
            if not items:
                return
            yield from self._update_items(items)

            params["after"] = items[-1]["sortId"]

#!/usr/bin/python3
# vim:fileencoding=utf-8:sw=4:et
"""
Network operations.
"""

import sys
import os
import io
import logging

import re
import time
import json
import requests
import threading
import collections

NATIVE = sys.getfilesystemencoding()


PageInfo = collections.namedtuple("PageInfo", "video_list, page")

class Extractor:
    """Fetch video/member information from network"""
    def __init__(self):
        self.thread_local = threading.local()

    def setup_headers(self):
        self.thread_local.session.headers = {
            "User-Agent":
                "Mozilla/5.0 (X11; Linux x86_64; rv:98.1) Gecko/20100101 Firefox/98.1",
        }
        return self.thread_local.session.headers

    @property
    def session(self):
        """Thread local requests.Session"""
        try:
            sess = self.thread_local.session
        except (AttributeError,):
            tname = threading.current_thread().name
            log.debug(f"Create new session for thread {tname}")
            self.thread_local.session = requests.Session()
            self.setup_headers()
            sess = self.thread_local.session

        return sess

    def get(self, *args, **kwargs):
        """Thread safe session.get()"""
        session = self.session
        if log.getEffectiveLevel() <= logging.DEBUG:
            tname = threading.current_thread().name
            thead_local_id = hex(id(self.thread_local))
            log.debug(f"{tname}(TL-{thead_local_id} Sess-{hex(id(session))}): get {args}")
        resp = session.get(*args, **kwargs)
        return resp

    def post(self, *args, **kwargs):
        """Thread safe session.post()"""
        tname = threading.current_thread().name
        session = self.session
        log.debug(f"{tname}(TL-{hex(id(self.thread_local))} Sess-{hex(id(session))}): post {args}")
        resp = session.post(*args, **kwargs)
        return resp

    def get_video_page(self, member_id, page_num):
        raise NotImplementedError("get_video_page")

    def get_all_video_pages(self, member_id):
        raise NotImplementedError("get_all_video_pages")

    def get_new_videos(self, mid, last_update):
        raise NotImplementedError("get_new_videos")

    def get_video_playlist_pages(self, video_id):
        raise NotImplementedError("get_video_playlist_pages")

class ExtractorBilibili(Extractor):
    """Fetch video/member information from bilibili"""
    def __init__(self):
        Extractor.__init__(self)

    def setup_headers(self):
        headers = Extractor.setup_headers(self)
        headers |= {
            "Referer": "https://space.bilibili.com/",
        }

    def get_video_page(self, mid, page_num):
        """Download a video page for a member"""
        url = "https://api.bilibili.com/x/space/arc/search"
        log.debug(f"Getting page: {page_num} ...")
        query = {
                "mid": mid,
                "ps": "30",
                "tid": "0",
                # "pn": "1",
                "keyword": "",
                "order": "pubdate",
                "jsonp": "jsonp"
                }
        query["pn"] = str(page_num)
        resp = self.get(url, params=query)

        data = resp.json()
        # print(data)
        pinfo = PageInfo(data["data"]["list"]["vlist"], data["data"]["page"])
        return pinfo

    def get_all_video_pages(self, mid):
        """Download all the video page for a member"""
        data_1 = self.get_video_page(mid, 1)
        total = data_1.page["count"]
        page_size = data_1.page["ps"]
        for i in range(2, total // page_size + 2):
            data_i = self.get_video_page(mid, i)
            data_1.video_list.extend(data_i.video_list)
            time.sleep(1.0)

        return data_1

    def get_new_videos(self, mid, last_update):
        """Download new video list for a member"""
        new_videos = []
        n = 1
        try:
            while True:
                data_n = self.get_video_page(mid, n)
                if len(data_n.video_list) < 1:
                    raise StopIteration

                for vinfo in data_n.video_list:
                    if vinfo["created"] > last_update:
                        new_videos.append(vinfo)
                    else:
                        raise StopIteration
                time.sleep(1)
        except StopIteration:
            pass

        return new_videos

    def get_video_playlist_pages(self, bvid):
        """Get list of video pages for a video by bvid"""
        pages = {}
        url = f"https://www.bilibili.com/video/{bvid}"
        resp = self.get(url)
        page_pat = re.compile(r'window\.__INITIAL_STATE__=.+?"pages":(\[\{.+?\}\]),.+?<\/script>')
        mobj = page_pat.search(resp.text)
        if mobj:
            pages_str = mobj.group(1)
            pages = json.loads(pages_str)
            log.debug(f"get_video_playlist_pages: {pages}")
        return pages


def setup_log(log_level=None):
    global log
    rlog = logging.getLogger()
    if __name__ == "__main__" and not rlog.hasHandlers():
        # setup root logger
        ch = logging.StreamHandler()
        formatter = logging.Formatter("%(levelname)s:%(module)s:%(lineno)d:: %(message)s")
        ch.setFormatter(formatter)
        rlog.addHandler(ch)

    log = logging.getLogger(__name__)

    if log_level is not None:
        log.setLevel(log_level)
        rlog.setLevel(log_level)


setup_log()


def main():
    def set_stdio_encoding(enc=NATIVE):
        import codecs; stdio = ["stdin", "stdout", "stderr"]
        for x in stdio:
            obj = getattr(sys, x)
            if not obj.encoding: setattr(sys, x, codecs.getwriter(enc)(obj))
    set_stdio_encoding()

    log_level = logging.INFO
    setup_log(log_level)

    member_id = sys.argv[1]
    svideo = ExtractorBilibili()
    __import__('pprint').pprint(svideo.get(member_id).json()); return
    # print(svideo.get_video_playlist_pages(member_id)); return
    #resp = svideo.get(member_id); print(resp.content); return
    # page_info = svideo.get_video_page(member_id, 1)
    # page_info = svideo.get_all_video_pages()
    # for v in page_info.video_list: print(v["title"])
    __import__('pprint').pprint(page_info.video_list[0])
    print(page_info.page)
    print(len(page_info.video_list))


if __name__ == '__main__':
    main()


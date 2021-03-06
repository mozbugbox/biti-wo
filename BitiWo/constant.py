#!/usr/bin/python3
# vim:fileencoding=utf-8:sw=4:et

__all__ = ["APPNAMEFULL", "APPNAME", "CACHE_DIR", "DATA_DIR", "DB_PATH", "PLAYER_BIN", "PKG_DIR", "VERSION", ]

import os
import pathlib

from gi.repository import GLib

VERSION = "0.1"
APPNAMEFULL = "BiTi Wo"
APPNAME = APPNAMEFULL.lower().replace(" ", "-")
PKG_DIR = pathlib.Path(__file__).resolve().parent

PLAYER_BIN = " ".join([
    "/usr/bin/mpv",
    "--http-header-fields='referer: https://www.bilibili.com/'",  # https://github.com/mpv-player/mpv/issues/9978
    "--no-terminal",
    "--ytdl-format='bestvideo[height<=?720]+bestaudio'",
])

CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), APPNAME)
DATA_DIR = os.path.join(GLib.get_user_data_dir(), APPNAME)
DB_PATH = os.path.join(DATA_DIR, f"{APPNAME}.sqlite")


#!/usr/bin/python3
# vim:fileencoding=utf-8:sw=4:et
"""
Misc utils
"""

import sys
import os
import io
import logging

try:
    import immatcher
    IMMatcher = immatcher.create_matcher()
except ImportError:
    IMMatcher = None

NATIVE = sys.getfilesystemencoding()

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


def match_func(key, value):
    """Match string key with value using Input Method Matcher"""
    matched = True
    keys = key.lower().split()
    value = value.lower()
    for k in keys:
        if IMMatcher is None:
            if k not in value:
                matched = False
        else:
            if not IMMatcher.contains(value, k):
                matched = False
    return matched

def model_search_func(model, cid, key, miter, *args):
    """Search function for Gtk.TreeStore, return False if matched"""
    value = model.get_value(miter, cid).lower()
    matched = match_func(key, value)
    return not matched

def main():
    def set_stdio_encoding(enc=NATIVE):
        import codecs; stdio = ["stdin", "stdout", "stderr"]
        for x in stdio:
            obj = getattr(sys, x)
            if not obj.encoding: setattr(sys, x, codecs.getwriter(enc)(obj))
    set_stdio_encoding()

    log_level = logging.INFO
    setup_log(log_level)

if __name__ == '__main__':
    main()


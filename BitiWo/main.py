#!/usr/bin/python3
# vim:fileencoding=utf-8:sw=4:et

# * Sqlite3 is not thread safe

import sys
import os
import io
import logging

import gc
import re
import time
import shlex
import hashlib
import datetime
import threading
import collections
import concurrent.futures

from .constant import *  # Import package constants
from . import utils
from . import dbase
from . import extractorbilibili

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, GObject, Gio, Gdk, Gtk, GdkPixbuf, Pango


NATIVE = sys.getfilesystemencoding()
(
        MEMBER_COL_MID,
        MEMBER_COL_NAME,
        MEMBER_COL_LASTUPDATE,
        MEMBER_COL_NEWITEM,
        MEMBER_COL_VISIBLE,
) = range(5)

(
        VIDEO_PAGE_COL_PAGE,
        VIDEO_PAGE_COL_TITLE,
        VIDEO_PAGE_COL_LENGTH,
        VIDEO_PAGE_COL_BVID,
) = range(4)

IMAGE_WIDTH = 256

def main_thread_run(func, *args):
    """Run function in the Gtk main thread"""
    timeout = 12
    return GLib.timeout_add(timeout, func, *args)

def debug_future_exception(future):
    """Debug exception raise in concurrent.Future"""
    try:
        exc = future.exception()
        if exc is not None:
            log.error(exc)
    except Exception as e:
        log.debug(e)

@Gtk.Template(filename=f"{PKG_DIR}/video-pages-popover.ui")
class VideoPagesPopover(Gtk.Popover):
    """Popover to show pages for a single video"""
    __gtype_name__ = "VideoPagesPopover"

    treeview_video_pages = Gtk.Template.Child()

    def __init__(self, controller):
        super(Gtk.Popover, self).__init__()
        self.init_template()
        self.controller = controller

    @Gtk.Template.Callback()
    def treeview_video_pages_row_activated_cb(self, tree, path, column):
        model = tree.get_model()
        bvid = model[path][VIDEO_PAGE_COL_BVID]
        page = model[path][VIDEO_PAGE_COL_PAGE]
        url = f"https://www.bilibili.com/video/{bvid}?p={page}"
        self.controller.play_url(url)
        self.controller.do_action("mark-video-visited", GLib.Variant("s", bvid))

@Gtk.Template(filename=f"{PKG_DIR}/video-listrow.ui")
class VideoRow(Gtk.ListBoxRow):
    """A row of video GtkListBox"""
    __gtype_name__ = "VideoRow"

    image_cover = Gtk.Template.Child()
    label_title = Gtk.Template.Child()
    label_description = Gtk.Template.Child()
    label_length = Gtk.Template.Child()
    label_played = Gtk.Template.Child()
    label_date = Gtk.Template.Child()

    def __init__(self, controller, vinfo):
        super(Gtk.Bin, self).__init__()
        self.init_template()
        self.controller = controller
        self.video_info = vinfo

        self.cache_dir = self.controller.get_cache_dir(vinfo["mid"])

        date = datetime.datetime.fromtimestamp(vinfo["created"])
        now = datetime.datetime.now()
        if date.year == now.year:
            if date.month == now.month and date.day == now.day:
                date_str = date.strftime("Today %I:%M %p")
            elif date.month == now.month and date.day + 1 == now.day:
                date_str = date.strftime("Yestoday %I:%M %p")
            else:
                date_str = date.strftime("%m-%d")
        else:
            date_str = date.strftime("%Y-%m-%d")

        duration = vinfo["length"]
        du_parts = duration.strip().split(":")
        if len(du_parts) == 2:
            m = int(du_parts[0])
            s = du_parts[1]

            h = int(m) // 60
            m = int(m) % 60
            duration = f"{h}:{m:02}:{s}"
        duration = duration.lstrip("0:")

        self.set_title_attributes()
        self.label_title.set_text(vinfo["title"])
        self.label_title.set_tooltip_text(vinfo["title"])
        self.label_description.set_text(vinfo["description"])
        self.label_length.set_text(duration)
        if isinstance(vinfo["view_count"], int):
            self.label_played.set_text(f"{vinfo['view_count']:,d}")
        else:
            self.label_played.set_text(f"{vinfo['view_count']}")
        self.label_date.set_text(date_str)

        self.image_cover.set_tooltip_text(vinfo["name"])

        self.set_action_target_value(GLib.Variant("s", vinfo["bvid"]))
        self.set_action_name("win.play-video")

    @property
    def video_info(self):
        return self._video_info

    @video_info.setter
    def video_info(self, x):
        """Convert sqlite3.Row to dict"""
        self._video_info = dict(x)

    @property
    def image_path(self):
        url = self.video_info["picture_url"]
        sha3 = hashlib.sha3_224()
        sha3.update(url.encode("utf-8"))
        fname_hash = sha3.hexdigest()
        path = os.path.join(self.cache_dir, fname_hash + ".jpg")
        return path

    @property
    def cover_downloaded(self):
        """Test if we have disk cache for cover image"""
        return os.path.exists(self.image_path)

    def set_title_attributes(self):
        attrlist = Pango.AttrList()
        font_desc = Pango.FontDescription()
        font_desc.set_size(18 * Pango.SCALE)
        if self.video_info["visited"] == 0:
            font_desc.set_weight(Pango.Weight.BOLD)
        attrlist.insert(Pango.AttrFontDesc.new(font_desc))
        self.label_title.set_attributes(attrlist)

    def load_image_pixbuf(self, url):
        """Load image pixbuf from a url with disk cache"""
        from_net = False
        path = self.image_path

        if os.path.exists(path):
            log.debug(f"load_image_pixbuf from disk: {path}")
            pix = GdkPixbuf.Pixbuf.new_from_file(path)
        else:
            resp = self.controller.extractor.get(url)
            loader = GdkPixbuf.PixbufLoader()
            loader.write(resp.content)
            loader.close()

            pix = loader.get_pixbuf()
            h = pix.get_height()
            w = pix.get_width()
            pix = pix.scale_simple(IMAGE_WIDTH, h / w * IMAGE_WIDTH,
                    GdkPixbuf.InterpType.BILINEAR)
            pix.savev(path, "jpeg", None, None)
            from_net = True
        return from_net, pix

    def load_image(self):
        url = self.video_info["picture_url"]
        log.debug(f"Load_image: {url=}")
        from_net, pix = self.load_image_pixbuf(url)
        def set_image_cover():
            if self.get_parent() is not None:
                self.image_cover.set_from_pixbuf(pix)
            else:
                log.debug(f"{self.video_info['title']} unparented.")
        main_thread_run(set_image_cover)

        if from_net:
            time.sleep(0.1)

    def reload_cover_image(self):
        """Reload a disk cached cover image from network"""
        image_path = self.image_path
        if os.path.exists(image_path):
            os.remove(image_path)
        future = self.controller.executor_image_network_loader.submit(self.load_image)
        future.add_done_callback(debug_future_exception)
        self.controller.load_image_futures.add(future)
        return future

class LoadMoreVideoRow(Gtk.ListBoxRow):
    """A row to show a "Load More" item in video ListBox"""""
    __gtype_name__ = "LoadMoreVideoRow"
    def __init__(self):
        Gtk.ListBoxRow.__init__(self)
        self.set_action_name("win.load-more-videos")
        label = Gtk.Label()
        label.set_text("Load More...")
        self.add(label)
        self.show_all()

AccelAction = collections.namedtuple("AccelAction", "action, accels")
class AccelManager:
    """Manage shortcut Accelerators"""
    def __init__(self, controller):
        self.controller = controller
        self.app = controller.app
        self.single_key_actions = None

    def get_actions(self, null_param_only=True):
        """collect actions for the GtkApplication
        null_param_only: only should actions with NULL param

        @return: a list of [[action-name, [keynames,...]], ...]
        """
        app = self.app
        action_maps = {app: app.list_actions()}
        action_maps |= {w: w.list_actions() for w in app.get_windows()}
        if null_param_only:
            for k, action_list in action_maps.items():
                action_maps[k] = [a for a in action_list
                        if k.get_action_parameter_type(a) is None]

        # action_desc = app.list_action_descriptions()
        # print(action_desc)
        result = []
        def do_aname(aname):
            accels = []
            accels = app.get_accels_for_action(aname)
            item = AccelAction(aname, accels)
            result.append(item)

        for k in action_maps.keys():
            for a in action_maps[k]:
                name = f"{'app' if k== app else 'win'}.{a}"
                do_aname(name)

        return result

    def toggle_single_key_accels(self, single_key):
        """Turn on or off single key accles for actions"""
        if single_key and self.single_key_actions is None:
            single_key_actions = {}
            actions_new = []

            actions = self.get_actions()
            for aa in actions:
                new_accels = []
                for accel in aa.accels:
                    keyval, modifier = Gtk.accelerator_parse(accel)
                    modifier = modifier & Gdk.ModifierType.MODIFIER_MASK
                    if modifier == 0 or modifier == Gdk.ModifierType.SHIFT_MASK:
                        single_key_actions[aa.action] = aa
                    else:
                        new_accels.append(accel)

                if aa.action in single_key_actions:
                    aa = aa._replace(accels=new_accels)
                    self.app.set_accels_for_action(aa.action, [])
                    actions_new.append(aa)

            self.single_key_actions = list(single_key_actions.values())

        elif not single_key and self.single_key_actions is not None:
            actions_new = self.single_key_actions
            self.single_key_actions = None
        else:
            return

        for aa in actions_new:
            self.app.set_accels_for_action(aa.action, aa.accels)

class CacheCleaner:
    """Clean up disk cache managed with time"""
    def __init__(self, controller):
        self.controller = controller
        self.max_residue_time = 7 * 24 * 3600  # Left the cache for 1 week
        self.timestamp_last_sweep = 0
        self.timestamp_last_orphaned_check = 0
        self.removed_member_times = {}

        self.controller.connect("member-removed", self.on_member_removed)

    @property
    def db(self):
        return self.controller.db

    def on_member_removed(self, mid):
        self.db.add_removed_member(mid)
        self.sweep_member_caches()

    def sweep_member_caches(self):
        """Check disk cache for unlinkable cache files"""
        if (self.timestamp_last_sweep == 0 and
                (last_sweep := self.db.get_config("timestamp_last_sweep")) is not None):
            self.timestamp_last_sweep = float(last_sweep)
            self.timestamp_last_orphaned_check = float(
                    self.db.get_config("timestamp_last_orphaned_check"))

        if not self.sweep_removed_member():
            self.sweep_orphaned_cache()

    def sweep_removed_member(self):
        ret = False
        removed_member_list = self.db.get_all_removed_member()
        now = time.time()
        remove_mid_list = []
        for row in removed_member_list:
            if now - row["time_stamp"] > self.max_residue_time:
                remove_mid_list.append(row["mid"])

        if len(remove_mid_list) > 0:
            ret = True
            t = threading.Thread(target=self.do_sweep_mid_list,
                    args=(remove_mid_list,))
            t.start()
            self.timestamp_last_sweep = now
            self.db.set_config("timestamp_last_sweep", now)
        return ret

    def do_sweep_mid_list(self, mid_list):
        """Actually remove cache files on disk"""
        import shutil
        for mid in self.mid_list:
            mid_path = self.controller.get_cache_dir(mid, False)
            if os.path.exists(mid_path):
                print(f"rmtree: {mid_path}")
                # shutil.rmtree(mid_path)

            main_thread_run(self.db.delete_removed_member, mid)

    def sweep_orphaned_cache(self):
        """Found file leftover in cache that does not exist in video database"""
        ret = False  # Nothing removed

        now = time.time()
        if now - self.timestamp_last_orphaned_check > self.max_residue_time:
            self.timestamp_last_orphaned_check = now
            self.db.set_config("timestamp_last_orphaned_check", now)
            return ret

        import Pathlib
        member_set = {x["mid"] for x in self.db.get_member_list()}

        prefix = "mid-"
        cache_base = Pathlib.Path(CACHE_DIR)
        glob_set = {x.name[len(prefix):] for x in cache_base.glob(f"{prefix}*")}

        orphaned_mid_set = glob_set - member_set
        if len(orphaned_mid_set) > 0:
            ret = True
            t = threading.Thread(target=self.do_sweep_mid_list,
                    args=(orphaned_mid_set,))
            t.start()
        return ret

class Controller(GObject.GObject):
    """Controller in MVC framework"""
    __gtype_name__ = "Controller"
    __gsignals__ = {
            "member-removed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }
    def __init__(self, app, db):
        GObject.GObject.__init__(self)

        self.load_image_futures = set()  # Image Loading futures that can be cancelled
        self.load_member_futures = set()  # Member Loading futures that can be cancelled
        self.load_misc_futures = set()  # Member Loading futures that can be cancelled
        self.app = app
        self.db = db

        self.loading_member = False
        self.update_all_source_id = -1
        self.load_video_source_id = -1

        self.update_member_waiting_list = {}
        self.extractor = extractorbilibili.ExtractorBilibili()
        self.executor_image_loader = concurrent.futures.ThreadPoolExecutor(
                thread_name_prefix="ImageLoader")
        self.executor_image_network_loader = concurrent.futures.ThreadPoolExecutor(max_workers=8,
                thread_name_prefix="ImageNetworkLoader")
        self.executor_member_loader = concurrent.futures.ThreadPoolExecutor(
                thread_name_prefix="MemberLoader")
        self.executor_misc_loader = concurrent.futures.ThreadPoolExecutor(
                thread_name_prefix="MiscLoader")

        self.player_bin = None
        self.member_url_pat = re.compile(r"^\s*(\d{6,})|space\.bilibili\.com/(\d{6,})(/|\?.*|$)")

        self.listbox = self.builder_object("listbox_videos")

        self.accel_manager = None
        self.setup_actions()

    def setup_actions(self):
        actions = [
                ["add-new-member", self.on_add_new_member, "s"],
                ["add-member-dialog", self.on_add_member_dialog],
                ["add-member-from-clipboard", self.on_add_member_from_clipboard],
                ["remove-member", self.on_remove_member, "t"],
                ["remove-selected-member", self.on_remove_selected_member],
                ["yank-member-url", self.on_yank_member_url],
                ["yank-video-url", self.on_yank_video_url],
                ["play-selected", self.on_play_selected],
                ["play-clipboard-url", self.on_play_clipboard_url],
                ["play-video", self.on_play_video, "s"],
                ["update-member-videos", self.on_update_member_videos, "t"],
                ["update-selected-member-videos", self.on_update_selected_member_videos],
                ["update-all-member-videos", self.on_update_all_member_videos],
                ["mark-video-visited", self.on_mark_video_visited, "s"],
                ["mark-selected-video-visited", self.on_mark_selected_video_visited],
                ["catch-up-selected-member", self.on_catch_up_selected_member],
                ["next-video", self.on_next_video],
                ["previous-video", self.on_previous_video],
                ["next-member", self.on_next_member],
                ["previous-member", self.on_previous_member],
                ["next-updated-member", self.on_next_updated_member],
                ["show-video-playlist-pages", self.on_show_video_playlist_pages],
                ["load-more-videos", self.on_load_more_videos],
                ["reload-video-cover-image", self.on_reload_video_cover_image],
                ["setup-player-bin", self.on_setup_player_bin],
                ["popup-menu", self.on_popup_menu],
            ]
        Gio.ActionMap.add_action_entries(self.app.window, actions)

        accels = [
                ("win.add-member-dialog", ["plus", "equal"]),
                ("win.add-member-from-clipboard", ["<control>p", "<control>v"]),
                ("win.remove-selected-member", ["minus", "Delete"]),
                ("win.yank-member-url", ["<shift>y", "<control>c"]),
                ("win.yank-video-url", ["y"]),
                ("win.play-clipboard-url", ["<shift>p"]),
                ("win.update-selected-member-videos", ["r"]),
                ("win.update-all-member-videos", ["<shift>r"]),
                ("win.catch-up-selected-member", ["<shift>c"]),
                ("win.next-video", ["j"]),
                ("win.previous-video", ["k"]),
                ("win.next-member", ["f"]),
                ("win.previous-member", ["b"]),
                ("win.next-updated-member", ["d"]),
                ("win.show-video-playlist-pages", ["l"]),
                ("win.load-more-videos", ["m"]),
                ("win.reload-video-cover-image", ["<control>l"]),
                ("win.popup-menu", ["<alt>F"]),
        ]
        for accel in accels:
            self.app.set_accels_for_action(accel[0], accel[1]);

        self.accel_manager = AccelManager(self)

    def builder_object(self, name):
        """lookup widget object from Gtk.Builder"""
        return self.app.builder_object(name)

    def pop_invalid_message(self, msg):
        """Show a error message dialog"""
        dlg = Gtk.MessageDialog(parent=self.app.window,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text=msg)
        dlg.run()
        dlg.destroy()

    def pop_confirm_message(self, msg):
        """Show a dialog ask for confirmation"""
        dlg = Gtk.MessageDialog(parent=self.app.window,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.YES_NO,
                text=msg)
        resp = dlg.run()
        dlg.destroy()
        return resp

    def lookup_action(self, action_name):
        """Lookup connected GAction by name"""
        action = self.app.lookup_action(action_name) or self.app.window.lookup_action(action_name)
        return action

    def do_action(self, action_name, param=None):
        """Activate an action"""
        action = self.lookup_action(action_name)
        if action:
            action.activate(param)
        else:
            self.pop_invalid_message(f"Invalid action name: {action_name}.")

    def setup_member_row_data(self, mid, minfo):
        """Build liststore_member row data from db query result"""
        last_update = self.db.get_last_update(mid)
        new_count = self.db.get_unvisited_count(mid)
        row_data = (mid, minfo["name"], last_update, new_count, new_count != 0)
        return row_data

    def load_members(self):
        """Load member list from db to treeview_member"""
        self.loading_member = True
        member_info_list = self.db.get_member_list()
        model = self.builder_object("liststore_member")
        model.clear()
        member_order = self.db.get_config("member_order")

        mid_list = []
        member_dict = {x["mid"]: x for x in member_info_list}
        if member_order:
            mid_list += [mid for mid in member_order if mid in member_dict]
        filled_members = set(mid_list)
        mid_list += [mid for mid in member_dict.keys() if mid not in filled_members]

        def append_members():
            step = 32
            for i, mid in enumerate(mid_list):
                minfo = member_dict[mid]
                model.append(self.setup_member_row_data(mid, minfo))
                if (i + 1) % step == 0:
                    yield True
            yield False
        appender = append_members()
        if next(appender):
            main_thread_run(next, appender)

        self.loading_member = False

    def get_video_row_by_bvid(self, bvid):
        """return ListBoxRow for a given video bvid"""
        for row in self.listbox:
            if row.video_info["bvid"] == bvid:
                return row

    @property
    def selected_video_row(self):
        """Return a selected video row """
        rows = self.listbox.get_selected_rows()
        return rows[0] if len(rows) > 0 else None

    @property
    def selected_video_bvid(self):
        """Return the bvid of a selected video row"""
        srow = self.selected_video_row
        return srow.video_info["bvid"] if srow is not None else None

    def get_member_row_by_mid(self, mid):
        """Return a treeview_member TreeModelRow by mid"""
        tree = self.builder_object("treeview_member")
        for row in tree.get_model():
            if row[MEMBER_COL_MID] == mid:
                return row

    def get_member_row_at_cursor(self):
        """Return treeview_member TreeModelRow at cursor"""
        tree = self.builder_object("treeview_member")
        cursor = tree.get_cursor()
        if cursor.path is None:
            return None
        model = tree.get_model()
        row = model[cursor.path]
        return row

    @property
    def member_id_at_cursor(self):
        mid = -1
        mrow = self.get_member_row_at_cursor()
        if mrow:
            mid = mrow[MEMBER_COL_MID]
        return mid

    def load_videos_to_listbox(self, mid, video_list, clear=True, reverse_insert=False):
        """Load video list from database into listbox"""
        # mid == None when filling search result
        if mid is not None and mid != self.member_id_at_cursor:
            return

        # stop old futures
        for future in self.load_image_futures:
            future.cancel()

        if self.load_video_source_id > 0:
            GLib.source_remove(self.load_video_source_id)
            self.load_video_source_id = -1

        self.load_image_futures = set()

        if clear:
            for row in self.listbox:
                row.destroy()

            swin = self.builder_object("scrolledwindow_videos")
            swin.props.vadjustment.set_value(0)
            gc.collect()

        last_row = self.listbox.get_row_at_index(len(self.listbox) - 1)
        if isinstance(last_row, LoadMoreVideoRow):
            self.listbox.remove(last_row)

        def insert_generator():
            """Progressive loader"""
            step = 32
            if reverse_insert:
                insert_list = reversed(video_list)
            else:
                insert_list = video_list

            for i, vinfo in enumerate(insert_list):
                row = VideoRow(self, vinfo)
                if reverse_insert:
                    self.listbox.prepend(row)
                else:
                    self.listbox.insert(row, -1)

                # load image in background
                # Use different executor to prevent network thread block disk thread
                if row.cover_downloaded:
                    future = self.executor_image_loader.submit(row.load_image)
                else:
                    future = self.executor_image_network_loader.submit(row.load_image)
                future.add_done_callback(debug_future_exception)
                self.load_image_futures.add(future)

                if (i + 1) % step == 0:
                    yield True

            if mid is not None and self.db.get_video_count(mid) > len(self.listbox):
                self.listbox.insert(LoadMoreVideoRow(), -1)

            self.load_video_source_id = -1
            yield False

        insertor = insert_generator()
        if next(insertor):
            self.load_video_source_id = main_thread_run(next, insertor)

    def on_load_more_videos(self, action, param, udata):
        """Load more video from db for current member"""
        while True:
            last_row = self.listbox.get_row_at_index(len(self.listbox) - 1)
            if isinstance(last_row, LoadMoreVideoRow):
                if last_row in self.listbox.get_selected_rows():
                    self.listbox.emit("move-cursor", Gtk.MovementStep.DISPLAY_LINES, -1)
                self.listbox.remove(last_row)
            else:
                break

        before = last_row.video_info["created"]
        mid = last_row.video_info["mid"]
        bulk_size = 100
        video_list = self.db.get_member_videos(mid, count=bulk_size, before=before)
        if len(video_list) > 0:
            self.load_videos_to_listbox(mid, video_list, clear=False, reverse_insert=False)

    def on_add_member_dialog(self, action, param, udata):
        """Show a dialog to add new member"""
        dlg = self.app.builder.get_object("dialog_add_member")
        resp = dlg.run()
        dlg.hide()

        entry = self.app.builder.get_object("entry_add_member")
        if resp == Gtk.ResponseType.OK:
            text = entry.get_text()
            self.do_action("add-new-member", GLib.Variant("s", text))

        entry.set_text("")

    def on_add_member_from_clipboard(self, action, param, udata):
        """Add new member by text from clipboard"""
        clip = Gtk.Clipboard.get_default(self.app.window.get_display())
        text = clip.wait_for_text()
        if text:
            self.do_action("add-new-member", GLib.Variant("s", text))

    def on_add_new_member(self, action, param, udata):
        """Give a text, add a new member from it"""
        mid = -1

        text = param.unpack()
        mobj = self.member_url_pat.search(text.strip())
        if mobj is not None:
            mid_txt = mobj.group(1) or mobj.group(2)
            mid = int(mid_txt)
        else:
            self.pop_invalid_message(f"Invalid member id: {text}.")

        log.debug(f"add_new_member: {mid=}")

        if mid > 0:
            if (uinfo := self.db.get_member_info(mid)) is not None:
                self.pop_invalid_message(f"Member {uinfo['name']}[ID: {mid}] already exists.")
            else:
                self.app.status_show_message("new-member", f"Fetching user ID {mid}...")
                future = self.executor_member_loader.submit(self.fetch_new_member, mid)
                future.add_done_callback(debug_future_exception)
                self.load_member_futures.add(future)

    def on_reload_video_cover_image(self, action, param, udata):
        """Reload a cached cover image"""
        row = self.selected_video_row
        if row is not None:
            row.reload_cover_image()

    def fetch_new_member(self, mid):
        """Get new member information from network and add it"""
        page_info = self.extractor.get_all_video_pages(mid)
        if len(page_info) > 0:
            def add_a_member():
                log.debug("add_a_member")
                member_name = page_info.video_list[0]["author"]
                self.db.add_member(mid, member_name)
                video_data_list = self.member_video_data_to_db_format(page_info.video_list)
                self.db.add_member_videos(video_data_list)

                minfo = self.db.get_member_info(mid)
                tree = self.builder_object("treeview_member")
                model = tree.get_model()
                row = self.get_member_row_at_cursor()
                if row:
                    new_row = model.insert_after(row.iter)
                else:
                    new_row = model.append()
                model[new_row] = self.setup_member_row_data(mid, minfo)
                self.app.status_show_message("new-member",
                        f"Fetch user {member_name}[ID:{mid}] done.")

            main_thread_run(add_a_member)

    def member_video_data_to_db_format(self, video_info_list):
        """Process video info from member video page into form suitable for db insertion"""
        # aid, bvid, mid, created, title, description, length,
        # picture_url, view_count, comment, visited,
        video_data_list = [(
            x["aid"],
            x["bvid"],
            x["mid"],
            x["created"],
            x["title"],
            x["description"],
            x["length"],
            x["pic"],
            x["play"],
            x["comment"],
            0
        ) for x in video_info_list]
        return video_data_list

    def on_remove_member(self, action, param, udata):
        """Remove a member by mid"""
        resp = self.pop_confirm_message(f"Really want to remove member?")
        if resp != Gtk.ResponseType.YES:
            return
        # print("Continue remove member"); return
        mid = param.unpack()
        minfo = self.db.get_member_info(mid)
        self.db.delete_member_videos(mid)
        self.db.delete_member(mid)
        mrow = self.get_member_row_at_cursor()
        if mrow and mrow[MEMBER_COL_MID] == mid:
            next_row = mrow.next
            if not next_row:
                next_row = mrow.previous
            log.debug(f"remove_member: {next_row=}")
            if next_row:
                tree = self.builder_object("treeview_member")
                tree.set_cursor(next_row.path, None)
            else:
                for row in self.listbox:
                    self.listbox.remove(row)
            mrow.model.remove(mrow.iter)
        self.app.status_show_message("remove-member",
                f"Member {minfo['name']}[ID:{minfo['mid']}] removed")

        self.emit("member-removed", mid)

    def on_remove_selected_member(self, action, param, udata):
        """Remove a member under treeview cursor"""
        mid = self.member_id_at_cursor
        if mid > 0:
            self.do_action("remove-member", GLib.Variant("t", mid))

    def on_yank_member_url(self, action, param, udata):
        """Yank/Copy member url to clipboard"""
        mid = self.member_id_at_cursor
        if mid > 0:
            text = f"https://space.bilibili.com/{mid}/video"
            clip = Gtk.Clipboard.get_default(self.app.window.get_display())
            clip.set_text(text, -1)
            self.app.status_show_message("clipboard", f"Yanked {text} to clipboard")

    def on_yank_video_url(self, action, param, udata):
        """Yank/Copy video url to clipboard"""
        if (bvid := self.selected_video_bvid) is not None:
            text = f"https://www.bilibili.com/video/{bvid}"
            clip = Gtk.Clipboard.get_default(self.app.window.get_display())
            clip.set_text(text, -1)
            self.app.status_show_message("clipboard", f"Yanked {text} to clipboard")

    def on_play_clipboard_url(self, action, param, udata):
        clip = Gtk.Clipboard.get_default(self.app.window.get_display())
        text = clip.wait_for_text().strip()
        if 'bilibili.com' in text:
            self.do_action("play-video", GLib.Variant("s", text))

    def on_play_selected(self, action, param, udata):
        """Play a selected video"""
        self.listbox.emit("activate-cursor-row")
        return

    def play_url(self, url):
        """Play a url in media player"""
        if not self.player_bin:
            player_bin = self.db.get_config("player_bin")
            if not player_bin:
                self.do_action("setup-player-bin")
                player_bin = self.db.get_config("player_bin")
            log.debug(f"{player_bin=}")

            if player_bin:
                self.player_bin = shlex.split(player_bin)
            else:
                self.pop_invalid_message("No proper media player to play the video URL.")
                return

        self.app.status_show_message("play-url", f"Playing {url}")
        import subprocess
        cmd = self.player_bin + [url]
        print(shlex.join(cmd))
        pid = subprocess.Popen(cmd).pid
        return pid

    def on_play_video(self, action, param, udata):
        """Play a video by bvid and make it visited"""
        addr = param.unpack()
        if not addr.startswith("http"):
            url = f"https://www.bilibili.com/video/{addr}"
        else:
            url = addr
        self.play_url(url)
        self.do_action("mark-video-visited", GLib.Variant("s", addr))

    def on_mark_video_visited(self, action, param, udata):
        """Mark a video as visited by bvid"""
        bvid = param.unpack()

        vinfo = self.db.get_video_info(bvid)
        if not vinfo:
            return

        if vinfo["visited"] == 0:
            self.db.set_video_visited(bvid, 1)
            vinfo = self.db.get_video_info(bvid)

        # update title font
        row = self.get_video_row_by_bvid(bvid)
        if row:
            row.video_info = vinfo
            row.set_title_attributes()

        mid = vinfo["mid"]
        member_row = self.get_member_row_by_mid(mid)
        newitem = self.db.get_unvisited_count(mid)
        member_row[MEMBER_COL_NEWITEM] = newitem
        member_row[MEMBER_COL_VISIBLE] = newitem > 0

    def on_mark_selected_video_visited(self, action, param, udata):
        """Mark video of selected video row as visited"""
        if (bvid := self.selected_video_bvid) is not None:
            self.do_action("mark-video-visited", GLib.Variant("s", bvid))

    def on_catch_up_selected_member(self, action, param, udata):
        """Mark all the video of selected member as visited"""
        row = self.get_member_row_at_cursor()
        mid = row[MEMBER_COL_MID]
        self.db.set_member_videos_visited(mid, 1)
        for vrow in self.listbox:
            vrow.video_info["visited"] = 1
            vrow.set_title_attributes()
        row[MEMBER_COL_NEWITEM] = 0
        row[MEMBER_COL_VISIBLE] = False

    def on_update_selected_member_videos(self, action, param, udata):
        """Fetch new video for the selected member"""
        mid = self.member_id_at_cursor
        if mid > 0:
            self.do_action("update-member-videos", GLib.Variant("t", mid))

    def on_update_member_videos(self, action, param, udata):
        """Fetch new video for a given member"""
        mid = param.unpack()
        future = self.update_member_videos(mid)

    def update_member_videos(self, mid):
        """Fetch new video for a given member"""
        minfo = self.db.get_member_info(mid)
        log.debug(f"Updating {minfo['name']}[{mid}]")
        self.app.status_show_message("update-member", f"Updating member {minfo['name']} ...")
        if mid in self.update_member_waiting_list:
            self.update_member_waiting_list.pop(mid)

        last_update = self.db.get_last_update(mid)
        def do_it():
            new_video_info_list = self.extractor.get_new_videos(mid, last_update)
            log.debug(f"{new_video_info_list=}")
            main_thread_run(self.add_new_member_videos, mid, new_video_info_list, last_update)
        future = self.executor_member_loader.submit(do_it)
        future.add_done_callback(debug_future_exception)
        self.load_member_futures.add(future)
        return future

    def add_new_member_videos(self, mid, new_video_info_list, last_update):
        """Add new video infos from network to db and video listbox"""
        minfo = self.db.get_member_info(mid)
        if len(new_video_info_list) <= 0:
            self.app.status_show_message("update-member", f"Update member {minfo['name']} done.")
            return

        video_data_list = self.member_video_data_to_db_format(new_video_info_list)
        self.db.add_member_videos(video_data_list)
        video_list = self.db.get_newer_video_by_mid(mid, last_update)
        if len(video_list) > 0:
            if self.member_id_at_cursor == mid:
                self.load_videos_to_listbox(mid, video_list, False, True)

            mrow = self.get_member_row_by_mid(mid)
            mpath = mrow.path
            model = self.builder_object("liststore_member")

            row_data = self.setup_member_row_data(mid, minfo)
            model[mpath][MEMBER_COL_NEWITEM] = row_data[MEMBER_COL_NEWITEM]
            model[mpath][MEMBER_COL_LASTUPDATE] = row_data[MEMBER_COL_LASTUPDATE]
            model[mpath][MEMBER_COL_VISIBLE] = row_data[MEMBER_COL_VISIBLE]

        self.app.status_show_message("update-member", f"Update member {minfo['name']} done.")

    def update_all_member_videos(self):
        """Fetch new videos for all the members"""
        members = self.db.get_member_list()
        self.update_member_waiting_list |= {x["mid"]: x["name"] for x in members}
        while len(self.update_member_waiting_list) > 0:
            # FIFO for dict
            mid = next(iter(self.update_member_waiting_list))
            name = self.update_member_waiting_list.pop(mid)
            future = self.update_member_videos(mid)
            while not future.done():
                yield True

        GLib.source_remove(self.update_all_source_id)
        self.update_all_source_id = -1

        yield False

    def on_update_all_member_videos(self, action, param, udata):
        """Fetch new videos for all the members"""
        update_interval = 1
        updator = self.update_all_member_videos()
        if self.update_all_source_id > 0:
            GLib.source_remove(self.update_all_source_id)
            self.update_all_source_id = -1

        if next(updator):
            self.update_all_source_id = GLib.timeout_add_seconds(update_interval, next, updator)

    def step_video(self, step):
        """Move video listbox cursor"""
        row = self.selected_video_row
        if row is not None:
            self.listbox.emit("move-cursor", Gtk.MovementStep.DISPLAY_LINES, step)
        else:
            row = self.listbox.get_row_at_index(0)
            if row:
                row.grab_focus()
                self.listbox.select_row(row)
        self.listbox.grab_focus()

    def on_next_video(self, action, param, udata):
        """Move cursor to next video in listbox"""
        self.step_video(1)

    def on_previous_video(self, action, param, udata):
        """Move cursor to previous video in listbox"""
        self.step_video(-1)

    def step_member(self, step):
        """Move member treeview cursor"""
        tree = self.builder_object("treeview_member")
        mid = self.member_id_at_cursor
        if mid < 0:
            tree.set_cursor(Gtk.TreePath.new_first(), None, False)
        else:
            tree.grab_focus()
            tree.emit("move-cursor", Gtk.MovementStep.DISPLAY_LINES, step)

    def save_member_order(self):
        """Save member order into db"""
        member_order = []

        tree = self.builder_object("treeview_member")
        for row in tree.get_model():
            mid = row[MEMBER_COL_MID]
            member_order.append(mid)

        self.db.set_config("member_order", member_order)

    def on_next_member(self, action, param, udata):
        """Move cursor to next member in treeview"""
        self.step_member(1)

    def on_previous_member(self, action, param, udata):
        """Move cursor to previous member in treeview"""
        self.step_member(-1)

    def on_next_updated_member(self, action, param, udata):
        """Move cursor to next member with unvisited videos"""
        mrow = self.get_member_row_at_cursor()
        if not mrow:
            self.do_action("next-member")
            return
        treeview = self.builder_object("treeview_member")
        model = treeview.get_model()
        while (mrow := mrow.next):
            if model[mrow.path][MEMBER_COL_NEWITEM] > 0:
                break
        if mrow:
            treeview.set_cursor(mrow.path, None, False)

    def pop_video_pages(self, bvid, video_page_info_list):
        """Show list of pages for a given video"""
        if len(video_page_info_list) <= 0:
            return

        vrow = self.get_video_row_by_bvid(bvid)
        popover = VideoPagesPopover(self)
        model = popover.treeview_video_pages.get_model()
        for page in video_page_info_list:
            du = page["duration"]
            h = du // 3600
            reminder = du % 3600
            m = reminder // 60
            s = reminder % 60
            length = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

            model.append((page["page"], page["part"], length, bvid))


        popover.set_relative_to(vrow.label_title)
        popover.popup()

    def on_show_video_playlist_pages(self, action, param, udata):
        """Show pages of a video"""
        if (bvid := self.selected_video_bvid) is not None:
            def get_page_and_pop():
                json_data = self.extractor.get_video_playlist_pages(bvid)
                if 'data' in json_data:
                    video_page_info_list = json_data["data"]
                    main_thread_run(self.pop_video_pages, bvid, video_page_info_list)
            future = self.executor_misc_loader.submit(get_page_and_pop)
            future.add_done_callback(debug_future_exception)
            self.load_misc_futures.add(future)

    def on_popup_menu(self, action, param, udata):
        mbutton = self.builder_object("menubutton_main")
        mbutton.emit("clicked")

    def on_setup_player_bin(self, action, param, udata):
        """Ask user to setup media player command line"""
        dlg = self.builder_object("dialog_setup_player_bin")
        entry = self.builder_object("entry_player_bin")

        player_bin = self.db.get_config("player_bin")
        if not player_bin:
            player_bin = PLAYER_BIN
            self.db.set_config("player_bin", player_bin)
        entry.set_text(player_bin)

        resp = dlg.run()
        dlg.hide()

        if resp == Gtk.ResponseType.OK:
            player_str = entry.get_text()
            if len(player_str) > 0 and player_str != player_bin:
                self.db.set_config("player_bin", player_str)
                self.player_bin = shlex.split(player_str)

    def get_cache_dir(self, mid, mkdir=True):
        """Return cache directory for a member"""
        cache_dir = os.path.join(CACHE_DIR, f"mid-{mid}")
        if mkdir:
            os.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    def load_matched_all_videos(self, pattern):
        """Load videos match a given pattern"""
        video_list = self.db.get_matched_video(pattern)
        # print([x["title"] for x in video_list])
        self.load_videos_to_listbox(None, video_list, clear=True, reverse_insert=False)

    def load_matched_member_videos(self, pattern, mid=None):
        """Load videos match a given pattern"""
        if mid is None:
            mid = self.member_id_at_cursor
        if mid > 0:
            video_list = self.db.get_matched_member_video(mid, pattern)
            # print([x["title"] for x in video_list])
            self.load_videos_to_listbox(None, video_list, clear=True, reverse_insert=False)

    def do_quit(self):
        """Clean up on quit signal"""
        self.executor_image_loader.shutdown(cancel_futures=True)
        self.executor_member_loader.shutdown(cancel_futures=True)
        self.executor_misc_loader.shutdown(cancel_futures=True)

class Handler:
    """Signal handle connected to Gtk.Builder"""
    def __init__(self, app):
        self.app = app
        self.controller = self.app.controller
        self.record_order_id = -1

    def treeview_member_cursor_changed_cb(self, tree):
        cursor = tree.get_cursor()
        if cursor.path is None:
            return
        model = tree.get_model()
        mid = model[cursor.path][MEMBER_COL_MID]

        bulk_size = 100
        video_list = self.controller.db.get_member_videos(mid, count=bulk_size)

        self.controller.load_videos_to_listbox(mid, video_list, reverse_insert=False)

    def do_record_member_order(self):
        save_interval = 5
        def do_it():
            self.controller.save_member_order()
            self.record_order_id = -1

        if not self.controller.loading_member and self.record_order_id < 0:
            self.record_order_id = GLib.timeout_add_seconds(save_interval, do_it)

    def liststore_members_row_inserted_cb(self, model, path, miter):
        """Reorder signal paired with row-deleted"""
        self.do_record_member_order()

    def liststore_members_row_deleted_cb(self, model, path):
        """Reorder signal paired with row-inserted"""
        self.do_record_member_order()

    def searchentry_search_video_focus_in_event_cb(self, widget, evt):
        self.controller.accel_manager.toggle_single_key_accels(True)
        return False

    def searchentry_search_video_focus_out_event_cb(self, widget, evt):
        self.controller.accel_manager.toggle_single_key_accels(False)
        return False

    def searchentry_search_video_activate_cb(self, entry):
        text = entry.get_text().strip()
        if len(text) > 0:
            if text.startswith("@"):
                self.controller.load_matched_all_videos(text[1:])
            else:
                self.controller.load_matched_member_videos(text)
        else:
            tree = self.controller.builder_object("treeview_member")
            self.treeview_member_cursor_changed_cb(tree)

    def searchentry_search_video_stop_search_cb(self, entry):
        wid = self.controller.listbox.get_focus_child()
        if not wid:
            wid = self.controller.listbox.get_row_at_index(0)
        if not wid:
            wid = self.controller.builder_object("treeview_member")
        wid.grab_focus()

class Application(Gtk.Application):
    """Main entry point for the application"""
    __gtype_name__ = "Application"
    def __init__(self, *args, **kwargs):
        super().__init__(*args,
                application_id=f"org.mozbugbox.{APPNAME}",
                flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
                **kwargs)
        self.window = None
        self.db = dbase.DataBase(DB_PATH)


        self.add_main_option("Debug", ord("D"),
            GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Debug mode on",
            None,)

    def do_startup(self):
        builder = Gtk.Builder()
        builder.add_from_file(f"{PKG_DIR}/interface.ui")
        Gtk.Application.do_startup(self)
        actions = [
                ["quit", self.on_quit],
        ]
        Gio.ActionMap.add_action_entries(self, actions)

        accels = [
                ("app.quit", ["<primary>q"])
        ]
        for accel in accels:
            self.set_accels_for_action(accel[0], accel[1]);

        self.builder = builder

        return True

    def do_activate(self):
        """Real startup from here."""
        log.debug(f"{CACHE_DIR=}, {DATA_DIR=}, {DB_PATH=}")
        Gtk.Application.do_activate(self)

        if self.window is None:
            self.window = self.builder.get_object("main_window")
            self.add_window(self.window)

        hbar = self.builder_object("headerbar_main")
        hbar.set_title(APPNAMEFULL)

        self.controller = Controller(self, self.db)
        self.builder.connect_signals(Handler(self))
        self.window.present()

        def delay_activate():
            """Init actions after main window pops"""
            self.controller.load_members()

            menu_builder = Gtk.Builder()
            menu_builder.add_from_file(f"{PKG_DIR}/menu.ui")
            win_menu = menu_builder.get_object("win-menu")
            mbutton = self.builder_object("menubutton_main")
            mbutton.set_menu_model(win_menu)

            treeview = self.builder_object("treeview_member")
            treeview.set_search_equal_func(utils.model_search_func)

        GLib.timeout_add(16, delay_activate)

        return True

    def do_command_line(self, command_line):
        Gtk.Application.do_command_line(self, command_line)
        options = command_line.get_options_dict()
        if options:
            options = options.end().unpack()
            if options.get("Debug", False):
                setup_log(logging.DEBUG)

            log.debug(f"{options=}")

        self.activate()
        return True

    def builder_object(self, name):
        """Return widget object from Gtk.Builder"""
        return self.builder.get_object(name)

    def on_about(self, action, param):
        pass

    def on_quit(self, action, param, udata):
        self.controller.do_quit()
        self.quit()

    def status_show_message(self, context_text, msg):
        """Show message on status bar"""
        sbar = self.builder_object("statusbar_main")
        cid = sbar.get_context_id(context_text)
        sbar.pop(cid)
        sbar.push(cid, msg)

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

    GLib.set_prgname(APPNAME)
    GLib.set_application_name(APPNAMEFULL)

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    app = Application()
    app.run(sys.argv)

if __name__ == '__main__':
    main()


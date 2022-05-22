#!/usr/bin/python3
# vim:fileencoding=utf-8:sw=4:et
"""
Database operations
"""

import sys
import os
import io
import logging

import json
import time
import sqlite3

from . import utils

NATIVE = sys.getfilesystemencoding()
DB_PATH = "biti-wo.sqlite3"

class DataBase:
    """The database for the application"""
    def __init__(self, path=None):
        if path is None:
            path = DB_PATH

        self.db_version = "0.1"
        self.table_columns = {}

        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

        self.conn.create_function("MATCH", 2, utils.match_func)

        self.cur = self.conn.cursor()
        self.setup_tables()
        self.update_table_scheme()

    def create_table(self, table_name, columns):
        """Create a db table with the given name and columns"""
        column_query = ", \n  ".join(columns)
        self.cur.execute(f"CREATE TABLE IF NOT EXISTS {table_name}(\n  {column_query}\n  );")

    def setup_tables(self):
        """Create required db tables"""

        # Store application configuration
        self.table_columns["config"] = [
                    "key TEXT UNIQUE",
                    "value TEXT"
                ]
        self.create_table("config", self.table_columns["config"])

        # Store member information
        self.table_columns["members"] = [
                    "mid INTEGER UNIQUE",
                    "name TEXT",
                    "last_update REAL"
                ]
        self.create_table("members", self.table_columns["members"])

        # Store video information
        self.table_columns["videos"] = [
                    "aid INTEGER",
                    "bvid TEXT UNIQUE ON CONFLICT REPLACE",
                    "mid INTEGER",
                    "created INTEGER",
                    "title TEXT",
                    "description TEXT",
                    "length TEXT",
                    "picture_url TEXT",
                    "view_count INTEGER",
                    "comment INTEGER",
                    "visited INTEGER"
                ]
        self.create_table("videos", self.table_columns["videos"])

        # Store member that have been removed but still has cache file left for later deletion
        self.table_columns["removed_member"] = [
                    "mid INTEGER UNIQUE",
                    "time_stamp REAL"
                ]
        self.create_table("removed_member", self.table_columns["removed_member"])
        self.conn.commit()

    def get_table_columns(self, table):
        rows = self.cur.execute(f"SELECT name FROM PRAGMA_TABLE_INFO('{table}')")
        cols = [x[0] for x in rows.fetchall()]
        return cols

    def update_table_scheme(self):
        """Migrat the old version of db to the latest version"""
        db_version = self.get_config("db_version")
        log.debug(f"DB: {self.db_version=}, {db_version=}")
        if db_version == self.db_version:
            return

        for table in ["members", "videos", "removed_member"]:
            new_cols = {c.lstrip().split()[0]: c for c in self.table_columns[table]}
            old_cols = self.get_table_columns(table)

            for col in new_cols:
                if col not in old_cols:
                    log.debug(f"DB: Add {col=} To {table=}")
                    self.cur.execute(f"ALTER TABLE {table} ADD COLUMN " + new_cols[col])

        self.set_config("db_version", self.db_version)
        self.conn.commit()

    def restruct_table(self, table):
        """Create table with current schema and fill with old data"""
        tmp_table = table + "_tmp"
        old_cols = self.get_table_columns(table)
        cols_query = ", ".join(old_cols)

        self.create_table(tmp_table, self.table_columns[table])
        self.cur.execute(f"INSERT INTO {tmp_table}({cols_query}) SELECT {cols_query} FROM {table};")
        self.cur.execute(f"DROP TABLE {table};")
        self.cur.execute(f"ALTER TABLE {tmp_table} RENAME TO {table};")

        self.conn.commit()

    def add_member(self, mid, name):
        """Insert new member"""
        self.cur.execute("""INSERT INTO members VALUES (?, ?, 0)""", (mid, name))
        self.conn.commit()

    def delete_member(self, mid):
        """Delete a member"""
        self.cur.execute("""DELETE FROM members WHERE mid=?;""", (mid,))
        self.conn.commit()

    def get_member_list(self):
        """Return list of members"""
        self.cur.execute("""SELECT * FROM members;""")
        results = self.cur.fetchall()
        return results

    def get_member_info(self, mid):
        """Get member information"""
        self.cur.execute("""SELECT * FROM members WHERE mid=?;""", (mid, ))
        res = self.cur.fetchone()
        return res

    def get_member_videos(self, mid, count=-1, before=-1):
        """
        Return video list for a member
        @count: only load count number of videos
        @since: load videos with created < before
        """
        QBASE = """SELECT videos.*, name FROM videos INNER JOIN members USING(mid)"""
        if count < 0 and before < 0:
            self.cur.execute(QBASE + """ WHERE videos.mid=? ORDER BY created DESC;""", (mid,))

        elif count < 0:
            # no count
            self.cur.execute(QBASE + """ WHERE videos.mid=? AND created < ?
                    ORDER BY created DESC;""", (mid, before))

        elif before < 0:
            # no time
            self.cur.execute(QBASE + """ WHERE videos.mid=?
                    ORDER BY created DESC LIMIT ?;""", (mid, count))
        else:
            # time and count
            self.cur.execute(QBASE + """ WHERE videos.mid=? AND created < ?
                    ORDER BY created DESC LIMIT ?;""", (mid, before, count))

        results = self.cur.fetchall()
        return results

    def get_video_info(self, bvid):
        """Return information of a given video id (bvid)"""
        self.cur.execute("SELECT * FROM videos WHERE bvid=?;", (bvid,))
        return self.cur.fetchone()

    def get_last_update(self, mid):
        """Return last update timestamp of a member"""
        self.cur.execute("SELECT MAX(created) FROM videos WHERE mid=?;", (mid,))
        last_update = self.cur.fetchone()
        if last_update is not None:
            last_update = last_update[0]
        return last_update

    def get_all_member_status(self):
        """Reture member info with some video status
        STATUS:
            * video new count
            * last created video timestamp
        """
        self.cur.execute("""SELECT members.*, u.cnt AS new_count, l.last As last_created
                FROM members
                LEFT JOIN
                    (SELECT mid, COUNT(*) as cnt
                    FROM videos WHERE visited = 0 GROUP by mid
                    ) AS u
                    ON members.mid = u.mid
                LEFT JOIN
                    (SELECT mid, MAX(created) as last
                    FROM videos GROUP by mid
                    ) AS l
                    ON members.mid = l.mid
                """)
        return self.cur.fetchall()

    def get_member_status(self, mid):
        """Reture member info with some video status
        STATUS:
            * video new count
            * last created video timestamp
        """
        self.cur.execute("""SELECT members.*, u.cnt AS new_count, l.last As last_created
                FROM members
                LEFT JOIN
                    (SELECT mid, COUNT(*) as cnt
                    FROM videos WHERE visited = 0 GROUP by mid
                    ) AS u
                    ON members.mid = u.mid
                LEFT JOIN
                    (SELECT mid, MAX(created) as last
                    FROM videos GROUP by mid
                    ) AS l
                    ON members.mid = l.mid
                WHERE members.mid = ?
                """, (mid,))
        return dict(self.cur.fetchone())

    def get_all_bvid_of_mid(self, mid):
        """Return a set of all the bvid for the given mid"""
        self.cur.execute("SELECT bvid FROM videos WHERE mid=?;", (mid,))
        bvid_set = {x["bvid"] for x in self.cur.fetchall()}
        return bvid_set

    def get_newer_video_by_mid(self, mid, last_update):
        """Return videos created after a timestamp for a member"""
        QBASE = """SELECT videos.*, name FROM videos INNER JOIN members USING(mid)"""
        self.cur.execute(QBASE + """ WHERE mid=? and created > ?
                ORDER BY created DESC;""", (mid, last_update))
        video_list = self.cur.fetchall()
        return video_list

    def get_matched_video(self, pattern, limit=300):
        """Search video matched a given pattern"""
        QBASE = """SELECT videos.*, name FROM videos INNER JOIN members USING(mid)"""
        self.cur.execute(QBASE + """ WHERE (title MATCH ?)
                ORDER BY created DESC LIMIT ?;""", (pattern, limit))
        video_list = self.cur.fetchall()
        return video_list

    def get_matched_member_video(self, mid, pattern):
        """Search member video matched a given pattern"""
        QBASE = """SELECT videos.*, name FROM videos INNER JOIN members USING(mid)"""
        self.cur.execute(QBASE + """ WHERE mid=? and ((title MATCH ?) OR (description MATCH ?))
                ORDER BY created DESC;""", (mid, pattern, pattern))
        video_list = self.cur.fetchall()
        return video_list

    def get_video_count(self, mid):
        """Return total video for a member"""
        self.cur.execute("SELECT COUNT(*) FROM videos WHERE mid=?;", (mid,))
        count = self.cur.fetchone()
        if count is not None:
            count = count[0]
        return count

    def get_unvisited_count(self, mid):
        """Return total unvisited video for a member"""
        self.cur.execute("SELECT COUNT(*) FROM videos WHERE mid=? AND visited=0;", (mid,))
        count = self.cur.fetchone()
        if count is not None:
            count = count[0]
        return count

    def set_video_visited(self, bvid, visited=1):
        """Mark a video visited"""
        self.cur.execute("UPDATE videos SET visited=? WHERE bvid=?;", (visited, bvid))
        self.conn.commit()

    def set_member_videos_visited(self, mid, visited=1):
        """Set all video as visited for a member"""
        self.cur.execute("UPDATE videos SET visited=? WHERE mid=?;", (visited, mid))
        self.conn.commit()

    def add_member_videos(self, video_list):
        """Insert a list of videos"""
        if len(video_list) < 1:
            return
        self.cur.executemany("INSERT INTO videos VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                video_list)
        self.conn.commit()

    def delete_member_videos(self, mid):
        """Delete all the videos for a member"""
        self.cur.execute("DELETE FROM videos WHERE mid=?;", (mid,))
        self.conn.commit()

    def get_config(self, key):
        """Lookup value for a config key"""
        self.cur.execute("SELECT value FROM config WHERE key=?;", (key,))
        value = self.cur.fetchone()

        if value is not None:
            value = value[0]
            if key in ["member_order"]:
                value = json.loads(value)

        return value

    def set_config(self, key, value):
        """Set value for a config key"""
        if key in ["member_order"]:
            value = json.dumps(value)

        value = str(value)
        if value == self.get_config(key):
            return

        self.cur.execute("""INSERT INTO config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value;""", (key, value))
        self.conn.commit()

    def add_removed_member(self, mid):
        """Insert member into the removed_member table"""
        time_stamp = time.time()
        self.cur.execute("INSERT INTO removed_member VALUES(mid, time_stamp);", (mid, time_stamp))
        self.conn.commit()

    def get_removed_member(self, mid):
        """get member list from the removed_member table"""
        self.cur.execute("SELECT * FROM removed_member WHERE bvid=?;", (mid,))
        return self.cur.fetchone()

    def delete_removed_member(self, mid):
        """Delete a member from removed_member table"""
        self.cur.execute("DELETE FROM removed_member WHERE mid=?;", (mid,))
        self.conn.commit()

    def get_all_removed_member(self):
        """Get all the members from removed_member table"""
        self.cur.execute("SELECT * FROM removed_member;")
        return self.cur.fetchall()

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

    test_path = "test-db.sqlite3"
    db = DataBase(test_path)

if __name__ == '__main__':
    main()


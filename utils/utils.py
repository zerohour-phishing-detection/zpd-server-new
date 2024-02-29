"""
Just some misc util functions that might be universally used
"""

import os
import time

import requests
from bs4 import BeautifulSoup

# Setup logging
from utils.custom_logger import CustomLogger

main_logger = CustomLogger().main_logger


def to_file(filename: str, string: str):
    with open(filename, "w") as f:
        f.write(string)


def time_format(inp):
    m, s = divmod(inp, 60)
    h, m = divmod(m, 60)
    return f"{h:2.0f}h {m:2.0f}m {s:5.2f}s"


def get_search_term(path, hash):
    html_file = os.path.join(path, hash, "page.html")
    if not os.path.isfile(html_file):
        return ""
    with open(html_file, "r") as f:
        soup = BeautifulSoup(f, "html.parser")
        if soup.title:
            if soup.title.string:
                return soup.title.string.strip()
            else:
                return soup.title.string
        else:
            return ""


def time_string(time_start, i, n):
    now = time.time()
    elapsed = now - time_start
    total_time_expected = max((elapsed / i) * (n - 1), elapsed)
    remaining_time = max(total_time_expected - elapsed, 0)
    return f"Elapsed: {time_format(elapsed)} - Remaining: {time_format(remaining_time)} - Expected: {time_format(total_time_expected)}"


def get_ip():
    response = requests.post("http://ident.me")
    return response.text


startupip = get_ip()


def setstatus(status):
    to_file(f"log/status-{startupip}.txt", status)


def fix_entries(search, def_db):
    """
    main_logger.info("Adding entries not in new DB")
    search.conn_storage.execute(f"ATTACH '{def_db}' as dba")
    sql = "select sha1 from dba.urls where sha1 not in (select sha1 from brand_table)"
    results = search.conn_storage.execute(sql).fetchall()
    issue = set()
    start1 = time.time()
    for row in results:
        issue.add(row[0])
    main_logger.error(f"{len(issue)} missing entries found.")
    cnt = 0
    for row in issue:
        cnt += 1
        setstatus(f"{cnt}/{len(issue)} ({cnt/len(issue)*100}%) - {timeString(start1, cnt, len(issue))}")
        search.handle_folder(os.path.join(search.folder, row), row)
    search.conn_storage.execute("detach database dba")
    """

    main_logger.info("Double checking where no title results exist")
    search.mode = "text"
    sql = "select distinct sha1 from brand_table where sha1 not in (select distinct filepath from search_result_text)"
    results = search.conn_storage.execute(sql).fetchall()
    issue2 = set()
    start2 = time.time()

    for row in results:
        issue2.add(row[0])

    main_logger.error(f"Stage Text: {len(issue2)} missing entries found.")
    cnt = 0

    for row in issue2:
        cnt += 1
        setstatus(
            f"Stage Title: {cnt}/{len(issue2)} ({cnt / len(issue2) * 100}%) - {time_string(start2, cnt, len(issue2))}"
        )
        search.handle_folder(os.path.join(search.folder, row), row)

    main_logger.info("Double checking where no image results exist")

    search.mode = "image"
    sql = "select distinct sha1 from brand_table where sha1 not in (select distinct filepath from search_result_image)"
    results = search.conn_storage.execute(sql).fetchall()
    issue3 = set()
    start3 = time.time()

    for row in results:
        issue3.add(row[0])

    main_logger.error(f"{len(issue3)} missing image found.")
    cnt = 0

    for row in issue3:
        cnt += 1
        setstatus(
            f"Stage Image: {cnt}/{len(issue3)} ({cnt / len(issue3) * 100}%) - {time_string(start3, cnt, len(issue3))}"
        )
        search.handle_folder(os.path.join(search.folder, row), row)

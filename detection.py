import hashlib
import os
import sqlite3
import time

import joblib
from flask import jsonify
from requests_html import HTMLSession
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

import utils.classifiers as cl
from engines.google import GoogleReverseImageSearchEngine
from parsing import Parsing
from utils import domains
from utils.custom_logger import CustomLogger
from utils.reverse_image_search import ReverseImageSearch
from utils.sessions import SessionStorage
from utils.timing import TimeIt

# Option for saving the taken screenshots
SAVE_SCREENSHOT_FILES = False
# Whether to use the Clearbit logo API (see https://clearbit.com/logo)
USE_CLEARBIT_LOGO_API = True

# Where to store temporary session files, such as screenshots
SESSION_FILE_STORAGE_PATH = "files/"
# Database path for the operational output (?)
DB_PATH_OUTPUT = "db/output_operational.db"
# Database path for the sessions
DB_PATH_SESSIONS = "db/sessions.db"
if not os.path.isdir("db"):
    os.mkdir("db")

# Page loading timeout for web driver
WEB_DRIVER_PAGE_LOAD_TIMEOUT = 5

# The storage interface for the sessions
session_storage = SessionStorage(DB_PATH_SESSIONS, False)

# The main logger for the whole program, singleton
main_logger = CustomLogger().main_logger

# The HTTP + HTML session to use for reverse image search
html_session = HTMLSession()
html_session.browser  # TODO why is this here

# The logo classifier, deserialized from file
logo_classifier = joblib.load("saved-classifiers/gridsearch_clf_rt_recall.joblib")


def test(url, screenshot_url, uuid, pagetitle, image64) -> "DetectionResult":
    main_logger.info(f"""

##########################################################
##### Request received for URL:\t{url}
##########################################################
""")

    url_domain = domains.get_hostname(url)
    url_registered_domain = domains.get_registered_domain(url_domain)
    # TODO: switch to better hash, cause SHA-1 broken?
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()

    session_file_path = os.path.join(SESSION_FILE_STORAGE_PATH, url_hash)
    session = session_storage.get_session(uuid, url)

    with TimeIt("cache check"):
        # Check if URL is in cache or still processing
        cache_result = session.get_state()

        if cache_result is not None:
            # Request is already in cache, use result from that (possibly waiting until it is finished)
            if cache_result.result == "processing":
                time.sleep(4)  # TODO: oh god

            main_logger.info(f"[RESULT] {cache_result.result}, for url {url}, served from cache")

            return DetectionResult(url, url_hash, cache_result.result)

    # Update the current state in the session storage
    session.set_state("processing", "textsearch")

    with TimeIt("taking screenshot"):
        # Take screenshot of requested page
        parsing = Parsing(
            SAVE_SCREENSHOT_FILES,
            pagetitle,
            image64,
            screenshot_url,
            store=session_file_path,
        )
        screenshot_width, screenshot_height = parsing.get_size()

    db_conn_output = sqlite3.connect(DB_PATH_OUTPUT)

    # Perform text search of the screenshot
    with TimeIt("text-only reverse page search"):
        # Initiate text-only reverse image search instance
        search = ReverseImageSearch(
            storage=DB_PATH_OUTPUT,
            search_engines=[GoogleReverseImageSearchEngine()],
            folder=SESSION_FILE_STORAGE_PATH,
            upload=False,
            mode="text",
            htmlsession=html_session,
            clf=logo_classifier,
        )

        search.handle_folder(session_file_path, url_hash)

        # Get result from the above search
        url_list_text = db_conn_output.execute(
            "SELECT DISTINCT result FROM search_result_text WHERE filepath = ?",
            [url_hash],
        ).fetchall()
        url_list_text = [url[0] for url in url_list_text]

        # Handle results of search from above
        if check_search_results(url_registered_domain, url_list_text):
            main_logger.info(
                f"[RESULT] Not phishing, for url {url}, due to registered domain validation"
            )
            session.set_state("not phishing", "")

            return DetectionResult(url, url_hash, "not phishing")

    # No match through text, move on to image search
    session.set_state("processing", "imagesearch")

    with TimeIt("image-only reverse page search"):
        search = ReverseImageSearch(
            storage=DB_PATH_OUTPUT,
            search_engines=[GoogleReverseImageSearchEngine()],
            folder=SESSION_FILE_STORAGE_PATH,
            upload=True,
            mode="image",
            htmlsession=html_session,
            clf=logo_classifier,
            clearbit=USE_CLEARBIT_LOGO_API,
            tld=url_registered_domain,
        )
        search.handle_folder(session_file_path, url_hash)

        # Get results from above search
        url_list_img = db_conn_output.execute(
            "SELECT DISTINCT result FROM search_result_image WHERE filepath = ?",
            [url_hash],
        ).fetchall()
        url_list_img = [url[0] for url in url_list_img]

        # Handle results
        if check_search_results(url_registered_domain, url_list_img):
            main_logger.info(
                f"[RESULT] Not phishing, for url {url}, due to registered domain validation"
            )
            session.set_state("not phishing", "")

            return DetectionResult(url, url_hash, "not phishing")

    # No match through images, go on to image comparison per URL
    with TimeIt("image comparisons"):
        session.set_state("processing", "imagecompare")

        out_dir = os.path.join("compare_screens", url_hash)
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)

        # Initialize web driver
        options = Options()
        options.add_argument("--headless")

        driver = webdriver.Chrome(options=options)
        driver.set_window_size(screenshot_width, screenshot_height)
        driver.set_page_load_timeout(WEB_DRIVER_PAGE_LOAD_TIMEOUT)

        # Check all found URLs
        for index, resulturl in enumerate(url_list_text + url_list_img):
            if check_image(driver, out_dir, index, session_file_path, resulturl):
                # Match for found images, so conclude as phishing
                driver.quit()

                main_logger.info(f"[RESULT] Phishing, for url {url}, due to image comparisons")

                session.set_state("phishing", "")

                return DetectionResult(url, url_hash, "phishing")

        driver.quit()

    # If the inconclusive stems from google blocking:
    #   e.g. blocked == True
    #   result: inconclusive_blocked

    main_logger.info(f"[RESULT] Inconclusive, for url {url}")

    session.set_state("inconclusive", "")
    return DetectionResult(url, url_hash, "inconclusive")


def check_image(driver, out_dir, index, session_file_path, resulturl):
    # Take screenshot of URL and save it
    try:
        driver.get(resulturl)
    except Exception:
        return False
    driver.save_screenshot(out_dir + "/" + str(index) + ".png")

    # Image compare
    path_a = os.path.join(session_file_path, "screen.png")
    path_b = out_dir + "/" + str(index) + ".png"

    emd, s_sim = None, None
    try:
        emd = cl.earth_movers_distance(path_a, path_b)
    except Exception as err:
        main_logger.error(err)

    try:
        s_sim = cl.structural_sim(path_a, path_b)
    except Exception as err:
        main_logger.error(err)

    main_logger.info(f"Compared url '{resulturl}'")
    main_logger.info(f"Finished comparing:  emd = '{emd}', structural_sim = '{s_sim}'")

    if ((emd < 0.001) and (s_sim > 0.70)) or ((emd < 0.002) and (s_sim > 0.80)):
        return True

    return False


def check_search_results(url_registered_domain, found_urls) -> "DetectionResult":
    with TimeIt("SAN domain check"):
        for url in found_urls:
            # For each found URL, get the hostname
            domain = domains.get_hostname(url)

            # Get the Subject Alternative Names (all associated domains, e.g. google.com, google.nl, google.de) for all websites
            try:
                san_names = domains.get_san_names(domain)
            except Exception:
                main_logger.error(f"Error in SAN for {domain} (from URL {url})", exc_info=1)
                continue

            main_logger.debug(f"Domain of URL `{url}` is {domain}, with SAN names {san_names}")

            for hostname in [domain] + san_names:
                # Check if any of the domains found matches the input domain
                registered_domain = domains.get_registered_domain(hostname)
                if url_registered_domain == registered_domain:
                    return True

        # If no match, no results yet
        return False


# TODO overlaps with State in sessions.py, merge them or sth
class DetectionResult:
    url: str
    url_hash: str

    status: str  # 'not phishing', 'phishing', 'inconclusive', 'processing'

    def __init__(self, url: str, url_hash: str, status: str):
        self.url = url
        self.url_hash = url_hash
        self.status = status

    def to_json_str(self):
        # TODO return object doesnt need to specify the type of hash (rename to just 'hash' or sth instead of 'sha1')
        obj = [{"url": self.url, "status": self.status, "sha1": self.url_hash}]
        return jsonify(obj)

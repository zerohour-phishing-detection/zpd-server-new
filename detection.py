import asyncio
import hashlib
import os

from registry import DECISION_STRATEGIES, DETECTION_METHODS
from settings.detection import DetectionSettings
from settings.storage import SettingsStorage
from utils.archive import Archive
from utils.logging import main_logger
from utils.result import DetectionResult, ResultType
from utils.sessions import SessionStorage

# Where to store temporary session files, such as screenshots
SESSION_FILE_STORAGE_PATH = "files/"

if not os.path.isdir("db"):
    os.mkdir("db")

# Database path for the sessions
DB_PATH_SESSIONS = "db/sessions.db"

# Database path for the settings
DB_PATH_SETTINGS = "db/settings.db"

# Result archive path
ARCHIVE_PATH = "db/archive.csv"

# The storage interface for the sessions
session_storage = SessionStorage(DB_PATH_SESSIONS, True)

# The storage interface for the settings per user
settings_storage = SettingsStorage(DB_PATH_SETTINGS)

# The instance used for archiving results
archive = Archive(ARCHIVE_PATH)

# Instantiate a logger for the phishing detection
logger = main_logger.getChild("detection")


class DetectionData:
    url: str
    screenshot_url: str
    pagetitle: str

    def __init__(self, url: str = "", screenshot_url: str = "", pagetitle: str = ""):
        self.url = url
        self.screenshot_url = screenshot_url
        self.pagetitle = pagetitle

    @staticmethod
    def from_json(json) -> "DetectionData":
        # DEPRECATED
        if "URL" in json:
            url = json["URL"]
            screenshot_url = json["URL"]
        # NEW
        else:
            url = json["url"]
            screenshot_url = json["url"]

        # extra json field for evaluation purposes
        # the hash computed in the DB is the this one
        if "phishURL" in json:  # TODO: only allow this on a testing environment, not prod
            url = json["phishURL"]
            logger.info(f"Real URL changed to phishURL: {url}\n")

        pagetitle = json["pagetitle"]

        return DetectionData(url, screenshot_url, pagetitle)


def check(uuid: str, data: DetectionData) -> DetectionResult:
    url_hash = hashlib.sha256(data.url.encode("utf-8")).hexdigest()

    logger.info(f"""

##########################################################
##### Request received:
#####   for URL:\t{data.url}
#####   with hash:\t{url_hash}
#####   from UUID:\t{uuid}
##########################################################
""")
    session = session_storage.get_session(uuid, data.url)

    settings_json = settings_storage.get_settings(uuid)

    settings = DetectionSettings()
    if settings_json is not None:
        settings = DetectionSettings.from_json(settings_json)

    if not settings.bypass_cache:
        # Check if URL is in cache or still processing
        cache_result = session.get_state()

        if cache_result is not None:
            logger.info(
                f"[STATE] {cache_result.state} [RESULT] {cache_result.result}, for url {data.url}, served from cache"
            )

            return DetectionResult(
                data.url, url_hash, cache_result.state, ResultType[cache_result.result]
            )

    # Update the current state in the session storage
    session.set_state(ResultType.PROCESSING.name, "STARTED")

    results = []

    for method in settings.detection_methods:
        if method not in DETECTION_METHODS:
            raise ValueError(f"The detection method {method} doesn't exist.")

        logger.info(f"Started running method {method}")

        results.append(
            asyncio.run(
                DETECTION_METHODS[method].run(
                    data.url, data.screenshot_url, data.pagetitle, settings.methods_settings[method]
                )
            )
        )

    result = DECISION_STRATEGIES[settings.decision_strategy].decide(results)
    session.set_state(result.name, "DONE")

    archive.append(uuid, data.url, settings_json, result.name)

    return DetectionResult(data.url, url_hash, "DONE", result)

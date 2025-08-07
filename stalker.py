import requests
import hashlib
import time
from collections import OrderedDict
from urllib.parse import quote, urlparse, urlencode
import json
import logging
import re
import random
import string
import pytz
from datetime import datetime
from typing import Optional, List, Dict, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from tqdm import tqdm  # Importing tqdm for the progress bar

# Configure logging
logging.basicConfig(level=logging.INFO)  # Set to DEBUG for detailed logs
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# CUSTOM EXCEPTIONS
# -------------------------------------------------------------------------
class StalkerPortalError(Exception):
    """Base exception for StalkerPortal errors."""
    pass

class StreamCreationError(StalkerPortalError):
    """Exception raised when creating a stream link fails."""
    pass

class OrderedListError(StalkerPortalError):
    """Exception raised when fetching the ordered list fails."""
    pass

# -------------------------------------------------------------------------
# STALKERPORTAL CLASS
# -------------------------------------------------------------------------
class StalkerPortal:
    # Precompiled regular expressions for performance
    SERIAL_REGEX = re.compile(r'^[A-Z0-9]{13}$')
    DEVICE_ID_REGEX = re.compile(r'^[0-9A-F]{64}$', re.IGNORECASE)
    URL_REGEX = re.compile(
        r'^(http|https|rtsp|rtmp|mms|custom)://'
        r'(([A-Z0-9][A-Z0-9_-]*)(\.[A-Z0-9][A-Z0-9_-]*)+):?(\d+)?/?',
        re.IGNORECASE
    )

    def __init__(
        self,
        portal_url: str,
        mac: str,
        serial: Optional[str] = None,
        stream_base_url: Optional[str] = None,
        device_id: Optional[str] = None,
        timezone: Optional[str] = "Europe/Paris",  # Set default to Europe/Paris
        token_validity_period: int = 3600,  # Configurable token validity in seconds
        retries: int = 1,                  # Configurable number of retries
        backoff_factor: float = 1,         # Configurable backoff factor for retries
        timeout: float = 10,               # Configurable timeout for requests in seconds
        num_threads: int = 10,             # Number of threads for concurrent operations
        progress_callback: Optional[Callable[[int], None]] = None  # Callback for progress updates (0-100)
    ):
        """
        Initialize the StalkerPortal instance.

        Parameters:
            portal_url (str): Base URL of the Stalker portal.
            mac (str): MAC address of the device.
            serial (Optional[str]): 13-character alphanumeric serial number.
            stream_base_url (Optional[str]): Base URL for streaming.
            device_id (Optional[str]): 64-character hexadecimal device ID.
            timezone (Optional[str]): Timezone string (e.g., "Europe/London").
            token_validity_period (int): Token validity period in seconds.
            retries (int): Number of retries for HTTP requests.
            backoff_factor (float): Backoff factor for retries.
            timeout (float): Timeout for HTTP requests in seconds.
            num_threads (int): Number of threads for concurrent operations.
            progress_callback (Optional[Callable[[int], None]]): Function to call with progress updates (0-100).
        """
        self.portal_url = portal_url.rstrip("/")
        self.mac = mac.strip()

        # Validate and assign serial
        if serial:
            if not self.SERIAL_REGEX.match(serial):
                raise ValueError("Serial number must be a 13-character alphanumeric string.")
            self.serial = serial.upper()
            logger.debug(f"Using provided serial: {self.serial}")
        else:
            self.serial = self.generate_serial(self.mac)
            logger.debug(f"Generated serial: {self.serial}")

        # Validate and assign device_id
        if device_id:
            if not self.DEVICE_ID_REGEX.match(device_id):
                raise ValueError("Device ID must be a 64-character hexadecimal string.")
            self.device_id = device_id.upper()
            logger.debug(f"Using provided device_id: {self.device_id}")
        else:
            self.device_id = self.generate_device_id()
            logger.debug(f"Generated device_id: {self.device_id}")

        self.device_id1 = self.device_id
        self.device_id2 = self.device_id

        # Derive or assign stream_base_url
        if stream_base_url:
            self.stream_base_url = stream_base_url.rstrip("/")
            logger.debug(f"Using provided stream_base_url: {self.stream_base_url}")
        else:
            parsed_url = urlparse(self.portal_url)
            derived_path = "/vod4"
            self.stream_base_url = f"{parsed_url.scheme}://{parsed_url.netloc}{derived_path}"
            logger.debug(f"Derived stream_base_url: {self.stream_base_url}")

        self.session = requests.Session()
        self.token: Optional[str] = None
        self.token_timestamp: Optional[float] = None
        self.bearer_token: Optional[str] = None
        self.random: Optional[str] = None

        # Configurable parameters with validation
        if not isinstance(token_validity_period, int) or token_validity_period <= 0:
            raise ValueError("token_validity_period must be a positive integer.")
        self.token_validity_period = token_validity_period

        if not isinstance(retries, int) or retries < 0:
            raise ValueError("retries must be a non-negative integer.")
        self.retries = retries

        if not isinstance(backoff_factor, (int, float)) or backoff_factor < 0:
            raise ValueError("backoff_factor must be a non-negative number.")
        self.backoff_factor = backoff_factor

        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number.")
        self.timeout = timeout

        # Timezone handling
        if timezone:
            if timezone not in pytz.all_timezones:
                raise ValueError("Invalid timezone provided.")
            self.timezone = pytz.timezone(timezone)
            logger.debug(f"Using timezone: {self.timezone}")
        else:
            self.timezone = pytz.utc
            logger.debug("Using default timezone: UTC")

        # Threading setup
        if not isinstance(num_threads, int) or num_threads < 1:
            raise ValueError("num_threads must be a positive integer.")
        self.num_threads = num_threads
        self.progress_callback = progress_callback
        self.progress_lock = Lock()

    def __enter__(self):
        """Enable use as a context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Ensure the session is closed when exiting the context."""
        self.session.close()
        logger.debug("HTTP session closed.")

    # -------------------------------------------------------------------------
    # GENERATION & VALIDATION
    # -------------------------------------------------------------------------

    def generate_serial(self, mac: str) -> str:
        """
        Generate a 13-character serial based on the MD5 hash of the MAC address.

        Parameters:
            mac (str): MAC address.

        Returns:
            str: Generated serial.
        """
        # Create an MD5 hash of the MAC address
        md5_hash = hashlib.md5(mac.encode()).hexdigest()
        
        # Use the first 13 characters of the hash as the serial
        serial = md5_hash[:13].upper()  # Convert to uppercase for consistency
        
        logger.debug(f"Generated serial from MAC {mac}: {serial}")
        return serial

    def generate_device_id(self) -> str:
        """
        Generate a 64-character hexadecimal device ID based on the MAC address.

        Returns:
            str: Generated device ID.
        """
        mac_exact = self.mac.strip()
        sha256_hash = hashlib.sha256(mac_exact.encode()).hexdigest().upper()
        logger.debug(f"Generated device_id using MAC {mac_exact}: {sha256_hash}")
        return sha256_hash

    def generate_random_value(self) -> str:
        """
        Generate a 40-character random hexadecimal string.

        Returns:
            str: Generated random value.
        """
        return ''.join(random.choices('0123456789abcdef', k=40))

    # -------------------------------------------------------------------------
    # HEADERS & COOKIES
    # -------------------------------------------------------------------------

    def generate_headers(self, include_auth: bool = False, include_token: bool = True, custom_headers: Optional[Dict[str, str]] = None) -> OrderedDict:
        """
        Generate Stalker-related headers.

        Parameters:
            include_auth (bool): If True, include Bearer token in 'Authorization'.
            include_token (bool): If True, include 'token' in the Cookie.
            custom_headers (Optional[Dict[str, str]]): Additional headers to include.

        Returns:
            OrderedDict: Generated headers.
        """
        headers = OrderedDict()
        headers["Accept"] = "*/*"
        headers["User-Agent"] = (
            "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) "
            "MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
        )
        headers["Referer"] = f"{self.portal_url}/stalker_portal/c/index.html"
        headers["Accept-Language"] = "en-US,en;q=0.5"
        headers["Pragma"] = "no-cache"
        headers["X-User-Agent"] = "Model: MAG250; Link: WiFi"
        headers["Host"] = self.get_host()

        if include_auth and self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        headers["Cookie"] = self.generate_cookies(include_token=include_token)
        headers["Connection"] = "Close"
        headers["Accept-Encoding"] = "gzip, deflate"

        if custom_headers:
            headers.update(custom_headers)

        logger.debug(f"Generated headers: {headers}")
        return headers

    def generate_cookies(self, include_token: bool = True) -> str:
        """
        Generate cookie string.

        Parameters:
            include_token (bool): If True, include 'token' in the Cookie.

        Returns:
            str: Generated cookie string.
        """
        cookies = {
            "mac": quote(self.mac),
            "stb_lang": "en",
            "timezone": quote("Europe/Paris"),  # Explicitly set timezone to Europe/Paris
        }
        if include_token and self.bearer_token:
            cookies["token"] = quote(self.bearer_token)
        cookie_str = "; ".join([f"{key}={value}" for key, value in cookies.items()])
        logger.debug(f"Generated cookies: {cookie_str}")
        return cookie_str

    def get_host(self) -> str:
        """
        Extract the host from the portal URL.

        Returns:
            str: Host extracted from the portal URL.
        """
        parsed_url = urlparse(self.portal_url)
        host = parsed_url.netloc
        logger.debug(f"Extracted host: {host}")
        return host

    # -------------------------------------------------------------------------
    # HTTP & JSON HELPERS
    # -------------------------------------------------------------------------

    def safe_json_parse(self, response: Optional[requests.Response]) -> Optional[Dict]:
        """
        Safely parse JSON response.

        Parameters:
            response (Optional[requests.Response]): HTTP response object.

        Returns:
            Optional[Dict]: Parsed JSON data or None if parsing fails.
        """
        if not response:
            logger.error("No response object.")
            return None
        try:
            json_response = response.json()
            if not isinstance(json_response, dict):
                logger.error("Response JSON is not a dictionary.")
                logger.debug("Response text: " + response.text)
                return None
            return json_response
        except (json.JSONDecodeError, ValueError):
            logger.error("Failed to decode JSON.")
            logger.debug("Response text: " + response.text)
            return None

    def make_request_with_retries(self, url: str, params: Optional[Dict] = None, headers: Optional[Dict] = None) -> Optional[requests.Response]:
        """
        Attempt a GET request multiple times if an exception occurs.

        Parameters:
            url (str): The URL to request.
            params (Optional[Dict]): Query parameters.
            headers (Optional[Dict]): Request headers.

        Returns:
            Optional[requests.Response]: HTTP response object or None if all retries fail.
        """
        response = None
        for attempt in range(1, self.retries + 1):
            try:
                logger.debug(f"Attempt {attempt}: GET {url} with params={params}")
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                logger.debug(f"Received response: {response.status_code}")
                return response
            except requests.exceptions.HTTPError as http_err:
                if response and response.status_code == 404:
                    logger.warning(f"Received 404 Not Found for URL {url}.")
                    # Specific handling for 404 can be done outside
                    return response
                logger.warning(f"Attempt {attempt} HTTP error for URL {url}: {http_err}")
            except requests.exceptions.RequestException as e:
                logger.warning(f"Attempt {attempt} failed for URL {url}: {e}")
            if attempt < self.retries:
                sleep_time = self.backoff_factor * (2 ** (attempt - 1))
                logger.debug(f"Retrying after {sleep_time} seconds...")
                time.sleep(sleep_time)
            else:
                logger.error(f"All {self.retries} attempts failed for URL {url}")
                if response is not None and hasattr(response, 'text'):
                    logger.debug("Final response text: " + response.text)
                return None

    def safe_json_list(self, response: Optional[requests.Response], expected_key: str = "js") -> List[Dict]:
        """
        Parse a JSON response and return the list stored under `expected_key`.

        Parameters:
            response (Optional[requests.Response]): HTTP response object.
            expected_key (str): Key under which the list is stored.

        Returns:
            List[Dict]: List of items or empty list if parsing fails.
        """
        if not response:
            return []
        json_response = self.safe_json_parse(response)
        if not json_response:
            return []
        data = json_response.get(expected_key, [])
        if isinstance(data, dict):
            # In some portals, data might come back as a single dict
            logger.warning(f"{expected_key} field is a dictionary, converting to single-item list.")
            data = [data]
        elif not isinstance(data, list):
            logger.error(f"{expected_key} field is neither a list nor a dictionary.")
            logger.debug("Full JSON: " + json.dumps(json_response))
            return []
        return data

    # -------------------------------------------------------------------------
    # AUTH & PROFILE
    # -------------------------------------------------------------------------

    def handshake(self) -> None:
        """
        Initiates handshake to obtain a token from the server.
        Handles 404 by generating a token and prehash, then retries handshake.
        """
        initial_url = f"{self.portal_url}/stalker_portal/server/load.php?type=stb&action=handshake&token=&JsHttpRequest=1-xml"
        headers = self.generate_headers(include_auth=False)
        logger.debug(f"Handshake - Initial GET {initial_url}")
        
        try:
            response = self.session.get(initial_url, headers=headers, timeout=self.timeout)
            if response.status_code == 404:
                logger.warning("Initial handshake returned 404. Generating token and prehash for retry.")
                token = self.generate_token()
                prehash = self.generate_prehash(token)
                retry_url = f"{self.portal_url}/stalker_portal/server/load.php?type=stb&action=handshake&token={token}&prehash={prehash}&JsHttpRequest=1-xml"
                logger.debug(f"Retry handshake with URL: {retry_url}")
                response = self.make_request_with_retries(retry_url, headers=headers)
                if response is None:
                    raise ConnectionError("Failed to perform handshake after retry with token and prehash.")
            else:
                response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"Handshake request failed: {e}")
            raise ConnectionError("Failed to perform handshake due to request exception.") from e

        json_response = self.safe_json_parse(response)
        if not json_response or "js" not in json_response:
            raise ConnectionError("Failed to perform handshake - invalid response.")

        js_data = json_response.get("js", {})
        self.token = js_data.get("token")
        random_value = js_data.get("random", None)

        # If the portal doesn't return a 'random', generate one
        if random_value:
            self.random = random_value.lower()
        else:
            self.random = self.generate_random_value()

        self.token_timestamp = time.time()
        if not self.token:
            raise ValueError("Failed to retrieve token during handshake.")

        # Use the token for subsequent requests
        self.bearer_token = self.token
        logger.debug(f"Handshake successful. Token: {self.token}, Random: {self.random}")

    def generate_token(self) -> str:
        """
        Generate a random token string.

        Returns:
            str: Generated token.
        """
        token_length = 32  # Example length
        token = ''.join(random.choices(string.ascii_uppercase + string.digits, k=token_length))
        logger.debug(f"Generated token: {token}")
        return token

    def generate_prehash(self, token: str) -> str:
        """
        Generate a prehash based on the token.

        Parameters:
            token (str): The token string.

        Returns:
            str: Generated prehash.
        """
        # Example prehash generation using SHA1
        hash_object = hashlib.sha1(token.encode())
        prehash = hash_object.hexdigest()
        logger.debug(f"Generated prehash from token: {prehash}")
        return prehash

    def ensure_token(self) -> None:
        """
        Check if token is valid. If not, re-handshake and update profile.
        """
        current_time = time.time()
        if self.token is None or (current_time - self.token_timestamp) > self.token_validity_period:
            logger.debug("Token expired or not present. Performing handshake to obtain new token.")
            self.handshake()
            logger.debug("Token refreshed. Fetching profile.")
            self.get_profile()  # Update profile after refreshing the token
        else:
            logger.debug("Existing token is still valid.")

    def get_profile(self) -> None:
        """
        Fetch user profile after ensuring a valid token.
        """
        self.ensure_token()
        url = f"{self.portal_url}/stalker_portal/server/load.php"
        params = {
            "type": "stb",
            "action": "get_profile",
            "hd": "1",
            "ver": (
                "ImageDescription: 0.2.18-r23-250; ImageDate: Thu Sep 13 11:31:16 EEST 2018; "
                "PORTAL version: 5.6.2; API Version: JS API version: 343; STB API version: 146; "
                "Player Engine version: 0x58c"
            ),
            "num_banks": "2",
            "sn": self.serial,
            "stb_type": "MAG250",
            "client_type": "STB",
            "image_version": "218",
            "video_out": "hdmi",
            "device_id": self.device_id1,
            "device_id2": self.device_id2,
            "signature": self.generate_signature(),
            "auth_second_step": "1",
            "hw_version": "1.7-BD-00",
            "not_valid_token": "0",
            "metrics": self.generate_metrics(),
            "hw_version_2": hashlib.sha1(self.mac.encode()).hexdigest(),
            "timestamp": int(time.time()),
            "api_signature": "262",
            "prehash": "",
            "JsHttpRequest": "1-xml",
        }
        headers = self.generate_headers(include_auth=True, include_token=False)
        logger.debug(f"Get Profile - GET {url} with params {params}")
        response = self.make_request_with_retries(url, params=params, headers=headers)
        json_response = self.safe_json_parse(response)
        if not json_response:
            logger.error("Failed to fetch profile.")
            return
        js_data = json_response.get("js", {})
        token = js_data.get("token")
        if token:
            self.token = token
            self.bearer_token = token
            self.token_timestamp = time.time()
            logger.debug(f"Profile token updated: {self.token}")

        logger.info("Profile fetched successfully.")
        return js_data

    def generate_signature(self) -> str:
        """
        Generate signature for profile request.

        Returns:
            str: Generated signature.
        """
        data = f"{self.mac}{self.serial}{self.device_id1}{self.device_id2}"
        signature = hashlib.sha256(data.encode()).hexdigest().upper()
        logger.debug(f"Generated signature: {signature}")
        return signature

    def generate_metrics(self) -> str:
        """
        Generate metrics for profile request.

        Returns:
            str: JSON-formatted metrics string.
        """
        if not self.random:
            self.random = self.generate_random_value()
        metrics = {
            "mac": self.mac,
            "sn": self.serial,
            "type": "STB",
            "model": "MAG250",
            "uid": "",
            "random": self.random
        }
        metrics_str = json.dumps(metrics)
        logger.debug(f"Generated metrics: {metrics_str}")
        return metrics_str

    def get_account_info(self) -> Dict:
        """
        Fetch account info.

        Returns:
            Dict: Account information or empty dictionary if failed.
        """
        self.ensure_token()
        url = f"{self.portal_url}/stalker_portal/server/load.php?type=account_info&action=get_main_info&JsHttpRequest=1-xml"
        headers = self.generate_headers(include_auth=True)
        response = self.make_request_with_retries(url, headers=headers)
        if not response:
            logger.error("Failed to fetch account info.")
            return {}
        json_response = self.safe_json_parse(response)
        if not json_response:
            return {}
        return json_response.get("js", {})

    # -------------------------------------------------------------------------
    # CATEGORY FETCHING
    # -------------------------------------------------------------------------

    def get_categories(self, category_type: str = "itv") -> List[Dict]:
        """
        Generic method to fetch categories by type: "itv", "vod", or "series".

        Parameters:
            category_type (str): Type of category to fetch.

        Returns:
            List[Dict]: List of categories.
        """
        category_type = category_type.lower().strip()
        if category_type == "itv":
            return self.get_itv_categories()
        elif category_type == "vod":
            return self.get_vod_categories()
        elif category_type == "series":
            return self.get_series_categories()
        else:
            logger.error(f"Unknown category_type: {category_type}")
            return []

    def get_vod_categories(self) -> List[Dict]:
        """
        Fetch VOD (Movies) categories by excluding categories that look like TV Shows.

        Returns:
            List[Dict]: List of VOD categories.
        """
        def is_movie_category(cat_name: str) -> bool:
            exclude_keywords = ['tv', 'series', 'show']
            return not any(keyword in cat_name.lower() for keyword in exclude_keywords)

        self.ensure_token()
        url = f"{self.portal_url}/stalker_portal/server/load.php?type=vod&action=get_categories&JsHttpRequest=1-xml"
        headers = self.generate_headers(include_auth=True)
        response = self.make_request_with_retries(url, headers=headers)
        categories_data = self.safe_json_list(response)
        categories = []
        for category in categories_data:
            if not isinstance(category, dict):
                continue
            name = category.get("title") or category.get("name") or category.get("category_name")
            category_id = category.get("id") or category.get("category_id")
            if not (name and category_id):
                continue

            # EXCLUDE TV/Series type categories
            if is_movie_category(name):
                categories.append({
                    "name": name,
                    "category_type": "VOD",
                    "category_id": category_id,
                })

        categories.sort(key=lambda x: x["name"])
        logger.debug(f"Fetched VOD categories: {categories}")
        return categories

    def get_series_categories(self) -> List[Dict]:
        """
        Fetch Series (TV Shows) categories by including only categories that look like TV/Series.

        Returns:
            List[Dict]: List of Series categories.
        """
        def is_series_category(cat_name: str) -> bool:
            include_keywords = ['tv', 'series', 'show']
            return any(keyword in cat_name.lower() for keyword in include_keywords)

        self.ensure_token()
        url = f"{self.portal_url}/stalker_portal/server/load.php?type=vod&action=get_categories&JsHttpRequest=1-xml"
        headers = self.generate_headers(include_auth=True)
        response = self.make_request_with_retries(url, headers=headers)
        categories_data = self.safe_json_list(response)
        categories = []
        for category in categories_data:
            if not isinstance(category, dict):
                continue
            name = category.get("title") or category.get("name") or category.get("category_name")
            category_id = category.get("id") or category.get("category_id")
            if not (name and category_id):
                continue

            # ONLY categories that contain 'tv', 'series', or 'show'
            if is_series_category(name):
                categories.append({
                    "name": name,
                    "category_type": "Series",
                    "category_id": category_id,
                })

        categories.sort(key=lambda x: x["name"])
        logger.debug(f"Fetched Series categories: {categories}")
        return categories

    def get_itv_categories(self) -> List[Dict]:
        """
        Fetch Live TV categories (type=itv) from 'action=get_genres'.

        Returns:
            List[Dict]: List of IPTV categories.
        """
        self.ensure_token()
        url = f"{self.portal_url}/stalker_portal/server/load.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
        headers = self.generate_headers(include_auth=True)
        response = self.make_request_with_retries(url, headers=headers)
        raw_categories = self.safe_json_list(response)
        categories = []
        for cat in raw_categories:
            if not isinstance(cat, dict):
                continue
            name = cat.get("title")
            category_id = cat.get("id")
            if name and category_id:
                categories.append({
                    "name": name,
                    "category_type": "IPTV",
                    "category_id": category_id
                })
        categories.sort(key=lambda x: x["name"])
        logger.debug(f"Fetched IPTV categories: {categories}")
        return categories

    # -------------------------------------------------------------------------
    # UNIFIED PAGINATION for Movies (VOD), Series, and IPTV
    # -------------------------------------------------------------------------

    def fetch_all_pages(
        self,
        category_type: str,
        category_id: str,
        max_pages: Optional[int] = None,
        only_series: Optional[bool] = None
    ) -> List[Dict]:
        """
        Unified pagination method for:
          - VOD (Movies)
          - Series (TV Shows)
          - IPTV (Live Channels)

        Parameters:
            category_type (str): Type of category ("VOD", "Series", "IPTV").
            category_id (str): ID of the category.
            max_pages (Optional[int]): Maximum number of pages to fetch.
            only_series (Optional[bool]): 
                - True: keep only items with is_series="1"
                - False: keep only items with is_series!="1"
                - None: keep all

        Returns:
            List[Dict]: List of items.
        """
        self.ensure_token()
        base_url = f"{self.portal_url}/stalker_portal/server/load.php"

        if category_type == "IPTV":
            item_type = "channel"
            param_key = "genre"
            param_value = category_id
            type_param = "itv"
        elif category_type == "VOD":
            item_type = "vod"
            param_key = "category"
            param_value = category_id
            type_param = "vod"
        elif category_type == "Series":
            item_type = "series"
            param_key = "category"
            param_value = category_id
            type_param = "vod"
        else:
            logger.error("Unknown category_type.")
            return []

        headers = self.generate_headers(include_auth=True)
        items = []
        page_number = 1

        # Determine total pages first
        initial_params = {
            "type": type_param,
            "action": "get_ordered_list",
            param_key: param_value,
            "JsHttpRequest": "1-xml",
            "p": page_number
        }
        logger.debug(f"Fetching initial page {page_number} to determine total pages.")
        response = self.make_request_with_retries(base_url, params=initial_params, headers=headers)
        if not response:
            logger.error(f"Failed to fetch initial page {page_number} for category {category_type} ID {category_id}.")
            return []

        json_response = self.safe_json_parse(response)
        if not json_response:
            logger.error(f"Invalid JSON on initial page {page_number} for category {category_type} ID {category_id}.")
            return []

        js_data = json_response.get("js", {})
        total_items_str = js_data.get("total_items", "0")
        try:
            total_items = int(total_items_str)
        except ValueError:
            total_items = len(js_data.get("data", []))
        items_per_page = len(js_data.get("data", []))
        total_pages = (total_items + items_per_page - 1) // items_per_page
        logger.debug(f"Total items: {total_items}, Items per page: {items_per_page}, Total pages: {total_pages}")

        # Adjust total_pages based on max_pages
        if max_pages:
            total_pages = min(total_pages, max_pages)
            logger.debug(f"Adjusted total_pages based on max_pages={max_pages}: {total_pages}")

        # Fetch all pages concurrently
        with ThreadPoolExecutor(max_workers=self.num_threads) as executor:
            future_to_page = {}
            for p in range(1, total_pages + 1):
                params = {
                    "type": type_param,
                    "action": "get_ordered_list",
                    param_key: param_value,
                    "JsHttpRequest": "1-xml",
                    "p": p
                }
                future = executor.submit(self.make_request_with_retries, base_url, params=params, headers=headers)
                future_to_page[future] = p

            completed_pages = 0
            for future in as_completed(future_to_page):
                page = future_to_page[future]
                try:
                    resp = future.result()
                    if not resp:
                        logger.error(f"Failed to fetch page {page} for category {category_type} ID {category_id}.")
                        continue
                    json_response = self.safe_json_parse(resp)
                    if not json_response:
                        logger.error(f"Invalid JSON on page {page} for category {category_type} ID {category_id}.")
                        continue
                    js_data = json_response.get("js", {})
                    data = js_data.get("data", [])

                    if not data:
                        logger.debug(f"No data found on page {page}, skipping.")
                        continue

                    for item in data:
                        # Mark item type
                        item["item_type"] = item_type

                        # Correct assignment of 'movie_id'
                        if category_type == "Series":
                            # For series (seasons), 'video_id' corresponds to 'movie_id'
                            item["movie_id"] = item.get("video_id")
                        elif category_type == "VOD":
                            # For VOD (movies), 'id' corresponds to 'movie_id'
                            item["movie_id"] = item.get("id") or item.get("movie_id")
                        elif category_type == "IPTV":
                            # For IPTV, 'id' corresponds to 'channel_id' or similar
                            item["channel_id"] = item.get("id") or item.get("channel_id")
                        else:
                            item["movie_id"] = None

                        # Filter by is_series if requested
                        is_series_value = item.get("is_series")
                        if is_series_value is not None:
                            is_series_str = str(is_series_value).lower()
                        else:
                            is_series_str = "0"

                        if only_series is True and is_series_str != "1":
                            continue
                        if only_series is False and is_series_str == "1":
                            continue

                        items.append(item)
                        logger.debug(f"Processed item on page {page}: {item.get('name', 'Unnamed')}")

                except Exception as e:
                    logger.exception(f"Exception occurred while fetching page {page}: {e}")
                finally:
                    completed_pages += 1
                    progress_percent = int((completed_pages / total_pages) * 100)
                    self.report_progress(progress_percent)

        # Remove duplicates by 'id' or 'channel_id' based on category type
        unique = {}
        for i in items:
            if category_type == "IPTV":
                cid = i.get("channel_id")
            else:
                cid = i.get("id") or i.get("movie_id")
            if cid and cid not in unique:
                unique[cid] = i

        final_list = list(unique.values())
        final_list.sort(key=lambda x: x.get("name", ""))
        logger.debug(f"Fetched {len(final_list)} items in total for category {category_type} ID {category_id}")
        return final_list

    def get_vod_in_category(self, category_id: str, max_pages: Optional[int] = None) -> List[Dict]:
        """
        Fetch only non-series (movies) from a given VOD category (is_series != "1").

        Parameters:
            category_id (str): ID of the VOD category.
            max_pages (Optional[int]): Maximum number of pages to fetch.

        Returns:
            List[Dict]: List of VOD items.
        """
        return self.fetch_all_pages("VOD", category_id, max_pages=max_pages, only_series=False)

    def get_series_in_category(self, category_id: str, max_pages: Optional[int] = None) -> List[Dict]:
        """
        Fetch only series items (is_series = "1") from a given category.

        Parameters:
            category_id (str): ID of the category.
            max_pages (Optional[int]): Maximum number of pages to fetch.

        Returns:
            List[Dict]: List of series items.
        """
        return self.fetch_all_pages("Series", category_id, max_pages=max_pages, only_series=True)

    def get_channels_in_category(self, category_id: str, max_pages: Optional[int] = None) -> List[Dict]:
        """
        Fetch live channels (IPTV) from a given category.

        Parameters:
            category_id (str): ID of the IPTV category.
            max_pages (Optional[int]): Maximum number of pages to fetch.

        Returns:
            List[Dict]: List of IPTV channel items.
        """
        return self.fetch_all_pages("IPTV", category_id, max_pages=max_pages)

    # -------------------------------------------------------------------------
    # SEASON FETCHING (Separated as per request)
    # -------------------------------------------------------------------------

    def fetch_season_pages(self, movie_id: str, max_pages: Optional[int] = None) -> List[Dict]:
        """
        Gather data from multiple pages for seasons (where is_season=True).

        Parameters:
            movie_id (str): The ID of the movie.
            max_pages (Optional[int]): Maximum number of pages to fetch.

        Returns:
            List[Dict]: List of season items.
        """
        logger.debug(f"Fetching seasons for movie_id={movie_id}")
        self.ensure_token()
        base_url = f"{self.portal_url}/stalker_portal/server/load.php"
        headers = self.generate_headers(include_auth=True)

        items = []
        page_number = 1

        # Fetch initial page to determine total pages
        initial_params = {
            "type": "vod",
            "action": "get_ordered_list",
            "movie_id": movie_id,
            "season_id": "0",
            "episode_id": "0",
            "JsHttpRequest": "1-xml",
            "p": page_number
        }
        logger.debug(f"Fetching initial season page {page_number}")
        initial_resp = self.make_request_with_retries(base_url, params=initial_params, headers=headers)
        if not initial_resp:
            logger.error(f"Failed to fetch initial season page {page_number} for movie_id={movie_id}.")
            return []

        initial_json = self.safe_json_parse(initial_resp)
        if not initial_json:
            logger.error(f"Invalid JSON on initial season page {page_number} for movie_id={movie_id}.")
            return []

        js_data = initial_json.get("js", {})
        total_items_str = js_data.get("total_items", "0")
        try:
            total_items = int(total_items_str)
        except ValueError:
            total_items = len(js_data.get("data", []))
        items_per_page = len(js_data.get("data", []))
        total_pages = (total_items + items_per_page - 1) // items_per_page
        logger.debug(f"Total season items: {total_items}, Items per page: {items_per_page}, Total pages: {total_pages}")

        # Adjust total_pages based on max_pages
        if max_pages:
            total_pages = min(total_pages, max_pages)
            logger.debug(f"Adjusted total_pages for seasons based on max_pages={max_pages}: {total_pages}")

        # Fetch all pages concurrently
        with ThreadPoolExecutor(max_workers=self.num_threads) as executor:
            future_to_page = {}
            for p in range(1, total_pages + 1):
                params = {
                    "type": "vod",
                    "action": "get_ordered_list",
                    "movie_id": movie_id,
                    "season_id": "0",
                    "episode_id": "0",
                    "JsHttpRequest": "1-xml",
                    "p": p
                }
                future = executor.submit(self.make_request_with_retries, base_url, params=params, headers=headers)
                future_to_page[future] = p

            completed_pages = 0
            for future in as_completed(future_to_page):
                page = future_to_page[future]
                try:
                    resp = future.result()
                    if not resp:
                        logger.error(f"Failed to fetch season page {page} for movie_id={movie_id}.")
                        continue
                    json_response = self.safe_json_parse(resp)
                    if not json_response:
                        logger.error(f"Invalid JSON on season page {page} for movie_id={movie_id}.")
                        continue
                    js_data = json_response.get("js", {})
                    data = js_data.get("data", [])

                    if not data:
                        logger.debug(f"No data found on season page {page}, skipping.")
                        continue

                    for item in data:
                        if item.get("is_season"):
                            season_id = item.get("id")  # Extract the season ID
                            video_id = item.get("video_id") or item.get("movie_id")  # Extract the parent movie ID

                            # **Fix:** If video_id is same as season_id, assign the correct movie_id
                            if video_id == season_id:
                                logger.warning(f"video_id ({video_id}) is same as season_id ({season_id}). Assigning movie_id={movie_id}.")
                                video_id = movie_id

                            if not (season_id and video_id):
                                logger.warning(f"Season item missing 'season_id' or 'video_id': {item}")
                                continue

                            item["item_type"] = "season"  # Ensure correct item_type
                            item["season_id"] = season_id
                            item["movie_id"] = video_id
                            items.append(item)
                            logger.debug(f"Processed Season: {item.get('name')} (Season ID: {season_id}, Movie ID: {video_id})")

                except Exception as e:
                    logger.exception(f"Exception occurred while fetching season page {page}: {e}")
                finally:
                    completed_pages += 1
                    progress_percent = int((completed_pages / total_pages) * 100)
                    self.report_progress(progress_percent)

        # Log the final results
        logger.info(f"Fetched {len(items)} seasons for movie_id={movie_id}")
        return items

    def get_seasons(self, movie_id: str, max_pages: Optional[int] = None) -> List[Dict]:
        """
        Return only the items with 'is_season' = True from all pages for the given 'movie_id'.

        Parameters:
            movie_id (str): The ID of the movie.
            max_pages (Optional[int]): Maximum number of pages to fetch.

        Returns:
            List[Dict]: List of season items.
        """
        all_items = self.fetch_season_pages(movie_id, max_pages=max_pages)
        seasons = []
        for it in all_items:
            is_season_value = it.get("is_season")
            if is_season_value in [True, 1, "1", "true", "True", "yes", "Yes"]:
                season_id = it.get("season_id") or it.get("id")
                video_id = it.get("video_id") or it.get("movie_id")
                if not (season_id and video_id):
                    logger.warning(f"Season item missing 'season_id' or 'video_id': {it}")
                    continue
                it["item_type"] = "season"  # Changed from "series" to "season"
                it["season_id"] = season_id
                it["movie_id"] = video_id
                logger.debug(f"Processed Season: {it.get('name')} (Season ID: {season_id}, Movie ID: {video_id})")
                seasons.append(it)
        logger.info(f"Total seasons fetched: {len(seasons)}")
        return seasons

    # -------------------------------------------------------------------------
    # EPISODE FETCHING
    # -------------------------------------------------------------------------

    def fetch_episode_pages(self, movie_id: str, season_id: str, max_pages: Optional[int] = None) -> List[Dict]:
        """
        Gather data from multiple pages for episodes within a season.

        Parameters:
            movie_id (str): The ID of the movie.
            season_id (str): The ID of the season.
            max_pages (Optional[int]): Maximum number of pages to fetch.

        Returns:
            List[Dict]: List of episode items.
        """
        logger.debug(f"Fetching episodes for movie_id={movie_id}, season_id={season_id}")
        self.ensure_token()
        base_url = f"{self.portal_url}/stalker_portal/server/load.php"
        headers = self.generate_headers(include_auth=True)

        items = []
        page_number = 1

        # Fetch initial page to determine total pages
        initial_params = {
            "type": "vod",
            "action": "get_ordered_list",
            "movie_id": movie_id,
            "season_id": season_id,
            "episode_id": "0",
            "JsHttpRequest": "1-xml",
            "p": page_number
        }
        logger.debug(f"Fetching initial episode page {page_number}")
        initial_resp = self.make_request_with_retries(base_url, params=initial_params, headers=headers)
        if not initial_resp:
            logger.error(f"Failed to fetch initial episode page {page_number} for movie_id={movie_id}, season_id={season_id}.")
            return []

        initial_json = self.safe_json_parse(initial_resp)
        if not initial_json:
            logger.error(f"Invalid JSON on initial episode page {page_number} for movie_id={movie_id}, season_id={season_id}.")
            return []

        js_data = initial_json.get("js", {})
        total_items_str = js_data.get("total_items", "0")
        try:
            total_items = int(total_items_str)
        except ValueError:
            total_items = len(js_data.get("data", []))
        items_per_page = len(js_data.get("data", []))
        total_pages = (total_items + items_per_page - 1) // items_per_page
        logger.debug(f"Total episode items: {total_items}, Items per page: {items_per_page}, Total pages: {total_pages}")

        # Adjust total_pages based on max_pages
        if max_pages:
            total_pages = min(total_pages, max_pages)
            logger.debug(f"Adjusted total_pages for episodes based on max_pages={max_pages}: {total_pages}")

        # Fetch all pages concurrently
        with ThreadPoolExecutor(max_workers=self.num_threads) as executor:
            future_to_page = {}
            for p in range(1, total_pages + 1):
                params = {
                    "type": "vod",
                    "action": "get_ordered_list",
                    "movie_id": movie_id,
                    "season_id": season_id,
                    "episode_id": "0",
                    "JsHttpRequest": "1-xml",
                    "p": p
                }
                future = executor.submit(self.make_request_with_retries, base_url, params=params, headers=headers)
                future_to_page[future] = p

            completed_pages = 0
            for future in as_completed(future_to_page):
                page = future_to_page[future]
                try:
                    resp = future.result()
                    if not resp:
                        logger.error(f"Failed to fetch episode page {page} for movie_id={movie_id}, season_id={season_id}.")
                        continue
                    json_response = self.safe_json_parse(resp)
                    if not json_response:
                        logger.error(f"Invalid JSON on episode page {page} for movie_id={movie_id}, season_id={season_id}.")
                        continue
                    js_data = json_response.get("js", {})
                    data = js_data.get("data", [])

                    if not data:
                        logger.debug(f"No data found on episode page {page}, skipping.")
                        continue

                    for item in data:
                        # Assuming each item represents an episode
                        episode_id = item.get("id")
                        if not episode_id:
                            logger.warning(f"Episode item missing 'id': {item}")
                            continue

                        item["item_type"] = "episode"
                        item["episode_id"] = episode_id
                        item["movie_id"] = movie_id
                        item["season_id"] = season_id
                        item["episode_number"] = item.get("series_number")  # Add this line
                        items.append(item)
                        logger.debug(f"Processed Episode: {item.get('name')} (Episode ID: {episode_id})")

                except Exception as e:
                    logger.exception(f"Exception occurred while fetching episode page {page}: {e}")
                finally:
                    completed_pages += 1
                    progress_percent = int((completed_pages / total_pages) * 100)
                    self.report_progress(progress_percent)

        # Log the final results
        logger.info(f"Fetched {len(items)} episodes for movie_id={movie_id}, season_id={season_id}")
        return items

    def get_episodes(self, movie_id: str, season_id: str, max_pages: Optional[int] = None) -> List[Dict]:
        """
        Retrieve all episodes for a given movie and season.

        Parameters:
            movie_id (str): The ID of the movie.
            season_id (str): The ID of the season.
            max_pages (Optional[int]): Maximum number of pages to fetch.

        Returns:
            List[Dict]: List of episode items.
        """
        episodes = self.fetch_episode_pages(movie_id, season_id, max_pages=max_pages)
        return episodes

    # -------------------------------------------------------------------------
    # STREAM LINK GENERATION
    # -------------------------------------------------------------------------

    def get_stream_link(self, item: Dict) -> Optional[str]:
        """
        Creates a playable link (for IPTV or standard VOD movies).
        If 'is_series'=1, use the seasons/episodes approach.

        Parameters:
            item (Dict): Item dictionary containing necessary information.

        Returns:
            Optional[str]: Playable stream URL or None if not applicable.
        """
        
        cmd = item.get("cmd")
        item_type = item.get("item_type", "")
        is_series = item.get("is_series", "0")

        # If is_series = "1", user should navigate via seasons/episodes
        if is_series == "1":
            logger.warning("Item is a series. Use get_seasons(), get_episodes(), and get_episode_stream_link(...).")
            return None

        # For VOD or IPTV, we rely on either `movie_id` or `cmd`
        if item_type == "vod":
            # VOD approach
            movie_id = item.get("movie_id")
            if not movie_id:
                logger.error("VOD item must have 'movie_id'.")
                return None

            # Fetch stream URL directly using existing method
            stream_url = self.get_vod_stream_url(movie_id)
            return stream_url

        elif item_type == "channel":
            # IPTV approach
            if not cmd:
                logger.error("IPTV channel must have 'cmd'.")
                return None
            url = (
                f"{self.portal_url}/stalker_portal/server/load.php?action=create_link&type=itv&cmd={quote(cmd)}&JsHttpRequest=1-xml"
            )
            headers = self.generate_headers(include_auth=True)
            logger.debug(f"Creating IPTV stream link - GET {url}")
            response = self.make_request_with_retries(url, headers=headers)
            if not response:
                logger.error("Failed to create IPTV stream link - no response.")
                return None
            json_response = self.safe_json_parse(response)
            if not json_response:
                logger.error("Invalid JSON for IPTV stream link.")
                return None

            js = json_response.get("js", {})
            url_link = js.get("url")
            cmd_value = js.get("cmd")

            if url_link:
                # If there's a direct 'url' field
                stream_url = url_link
            elif cmd_value:
                # If there's a 'cmd' field, handle "ffmpeg " prefix or direct URL
                stream_url = cmd_value.strip()

                # ---- FIX: Remove "ffmpeg" prefix if present ----
                # e.g. "ffmpeg http://..." or "ffmpeghttp://..."
                if re.match(r'(?i)^ffmpeg\s*(.*)', stream_url):
                    logger.debug(f"Stripping 'ffmpeg' prefix from cmd: {stream_url}")
                    stream_url = re.sub(r'(?i)^ffmpeg\s*', '', stream_url).strip()

                # If still not an absolute URL, construct from stream_base_url
                if not re.match(r'^https?://', stream_url, re.IGNORECASE):
                    # In some portals, the cmd_value might already be a full URL.
                    # But if it's not, we build it:
                    stream_url = f"{self.stream_base_url}/{stream_url.lstrip('/')}"
                    logger.debug(f"Constructed absolute URL by prepending stream_base_url: {stream_url}")
            else:
                logger.error("Neither 'url' nor 'cmd' found in IPTV stream link response.")
                logger.debug(f"Full 'js' response: {json.dumps(js, indent=2)}")
                return None

            # Final cleanup: validate & return
            if isinstance(stream_url, str):
                logger.debug(f"Final IPTV stream URL before validation: {stream_url}")
            if self.validate_stream_url(stream_url):
                logger.info(f"Successfully created IPTV stream link: {stream_url}")
                return stream_url
            else:
                logger.error(f"Invalid stream URL generated: {stream_url}")
                return None

        else:
            logger.error(f"Unhandled item_type: {item_type}")
            return None

    def get_vod_stream_url(self, movie_id: str) -> Optional[str]:
        """
        Fetch the playable stream URL for a VOD movie.

        Parameters:
            movie_id (str): The ID of the movie.

        Returns:
            Optional[str]: The final stream URL or None if failed.
        """
        self.ensure_token()
        url = f"{self.portal_url}/stalker_portal/server/load.php"
        params = {
            "type": "vod",
            "action": "get_ordered_list",
            "movie_id": movie_id,
            "JsHttpRequest": "1-xml"
        }
        headers = self.generate_headers(include_auth=True)
        logger.debug(f"Fetching ordered list - GET {url} with params {params}")
        response = self.make_request_with_retries(url, params=params, headers=headers)

        if not response:
            logger.error("Failed to fetch ordered list - no response.")
            self.report_progress(100)  # Report completion even on failure
            return None

        json_response = self.safe_json_parse(response)
        if not json_response:
            logger.error("Invalid JSON response for ordered list.")
            self.report_progress(100)  # Report completion even on failure
            return None

        js_data = json_response.get("js", {})
        data = js_data.get("data", [])

        if not data:
            logger.error("No data found in ordered list response.")
            self.report_progress(100)  # Report completion even on failure
            return None

        # Assuming you want the first available stream
        stream_item = data[0]
        stream_id = stream_item.get("id")

        if not stream_id:
            logger.error("No 'id' found in the first stream item.")
            self.report_progress(100)  # Report completion even on failure
            return None

        logger.debug(f"Stream ID obtained: {stream_id}")

        # Create the stream link using the stream ID
        try:
            stream_url = self.create_stream_link(stream_id)
            self.report_progress(100)  # Report completion
            return stream_url
        except StreamCreationError as e:
            logger.error(f"Error creating stream link: {e}")
            self.report_progress(100)  # Report completion even on failure
            return None

    def create_stream_link(self, stream_id: str) -> str:
        """
        Create a playable stream link using the provided stream ID.

        Parameters:
            stream_id (str): The 'id' from the ordered list response.

        Returns:
            str: The final stream URL.

        Raises:
            StreamCreationError: If the stream link creation fails.
        """
        self.ensure_token()
        url = f"{self.portal_url}/stalker_portal/server/load.php"
        params = {
            "action": "create_link",
            "type": "vod",
            "cmd": f"/media/file_{stream_id}.mpg",
            "JsHttpRequest": "1-xml"
        }
        headers = self.generate_headers(include_auth=True)
        logger.debug(f"Creating stream link - GET {url} with params {params}")
        response = self.make_request_with_retries(url, params=params, headers=headers)

        if not response:
            logger.error(f"Failed to create stream link for stream_id={stream_id}")
            raise StreamCreationError("No response received while creating stream link.")

        json_response = self.safe_json_parse(response)
        if not json_response:
            logger.error(f"Invalid JSON response while creating stream link for stream_id={stream_id}")
            raise StreamCreationError("Invalid JSON response received.")

        js_data = json_response.get("js", {})
        stream_url = js_data.get("url")  # Attempt to get 'url' first
        cmd_value = js_data.get("cmd")

        if not stream_url:
            # If 'url' is not present, attempt to use 'cmd'
            if cmd_value:
                potential_url = cmd_value.strip()
                # ---- FIX: Remove "ffmpeg" prefix if present ----
                if re.match(r'(?i)^ffmpeg\s*(.*)', potential_url):
                    logger.debug(f"Stripping 'ffmpeg' prefix from cmd: {potential_url}")
                    potential_url = re.sub(r'(?i)^ffmpeg\s*', '', potential_url).strip()

                # If not absolute, build from stream_base_url
                if not re.match(r'^https?://', potential_url, re.IGNORECASE):
                    potential_url = f"{self.stream_base_url}/{potential_url.lstrip('/')}"
                    logger.debug(f"Constructed absolute URL: {potential_url}")

                stream_url = potential_url
            else:
                logger.error(f"No 'url' or 'cmd' found in create_link response for stream_id={stream_id}")
                logger.debug(f"Full 'js' response: {json.dumps(js_data, indent=2)}")
                raise StreamCreationError("Stream URL not found in the response.")

        # Final cleanup: remove leftover "ffmpeg " if any
        if stream_url.lower().startswith("ffmpeg "):
            logger.debug(f"Stripping unexpected 'ffmpeg ' prefix from stream_url: {stream_url}")
            stream_url = stream_url[7:].strip()

        # Validate the stream URL
        logger.debug(f"Final VOD stream URL before validation: {stream_url}")
        if self.validate_stream_url(stream_url):
            logger.info(f"Successfully created stream link: {stream_url}")
            return stream_url
        else:
            logger.error(f"Invalid stream URL generated: {stream_url}")
            raise StreamCreationError("Generated stream URL is invalid.")

    def validate_stream_url(self, url: str) -> bool:
        """
        Validate the stream URL using a regular expression.

        Parameters:
            url (str): The URL to validate.

        Returns:
            bool: True if valid, False otherwise.
        """
        if re.match(self.URL_REGEX, url):
            logger.debug(f"Stream URL is valid: {url}")
            return True
        else:
            logger.warning(f"Stream URL is invalid: {url}")
            return False

    # -------------------------------------------------------------------------
    # MOVIE DETAILS
    # -------------------------------------------------------------------------

    def get_movie_details(self, movie_id: str) -> Optional[Dict]:
        """
        Fetch movie details. 
        If the item is is_series="1", use get_seasons() instead.

        Parameters:
            movie_id (str): The ID of the movie.

        Returns:
            Optional[Dict]: Movie details or None if failed.
        """
        self.ensure_token()
        base_url = f"{self.portal_url}/stalker_portal/server/load.php"
        params = {
            "type": "vod",
            "action": "get_movie_details",
            "movie_id": movie_id,
            "JsHttpRequest": "1-xml"
        }
        headers = self.generate_headers(include_auth=True)
        logger.debug(f"Fetching movie details for movie_id={movie_id}")
        response = self.make_request_with_retries(base_url, params=params, headers=headers)
        if not response:
            logger.error(f"Failed to fetch details for movie_id={movie_id}")
            return None
        json_response = self.safe_json_parse(response)
        if not json_response:
            logger.error(f"Invalid JSON response for movie_id={movie_id}")
            return None
        movie_details = json_response.get("js", {})
        if not movie_details:
            logger.error(f"No 'js' data found in response for movie_id={movie_id}")
            return None
        return movie_details

    # -------------------------------------------------------------------------
    # REPORT PROGRESS
    # -------------------------------------------------------------------------

    def report_progress(self, progress: int) -> None:
        """
        Report progress through the progress_callback.

        Parameters:
            progress (int): Progress percentage (0-100).
        """
        if self.progress_callback:
            with self.progress_lock:
                # Ensure progress does not exceed 100
                progress = min(max(progress, 0), 100)
                self.progress_callback(progress)
                logger.debug(f"Reported progress: {progress}%")

    # -------------------------------------------------------------------------
    # IMPLEMENTATION OF select_movie_and_get_stream METHOD
    # -------------------------------------------------------------------------

    def select_movie_and_get_stream(self, items: List[Dict], selection_index: int = 0) -> Optional[str]:
        """
        Select an item (movie, channel, or episode) from the list and get its stream link.

        Parameters:
            items (List[Dict]): List of items to select from.
            selection_index (int): Index of the selected item.

        Returns:
            Optional[str]: Playable stream URL or None if not applicable.
        """
        if not items:
            logger.error("No items available to select.")
            return None
        if selection_index < 0 or selection_index >= len(items):
            logger.error(f"Selection index {selection_index} is out of range.")
            return None

        selected_item = items[selection_index]
        item_type = selected_item.get("item_type")
        item_id = (
            selected_item.get("id") or
            selected_item.get("movie_id") or
            selected_item.get("video_id") or
            selected_item.get("season_id") or
            selected_item.get("channel_id")
        )
        logger.info(f"Selected {item_type.capitalize()} ID: {item_id}")

        if item_type == "series":
            logger.info("Selected item is a series. Streaming for series is not handled.")
            return None
        elif item_type == "season":
            # Fetch stream link for the selected season
            stream_link = self.get_season_stream_link(item_id)
            return stream_link
        elif item_type == "vod":
            # Fetch stream link for a movie
            movie_details = self.get_movie_details(item_id)
            if not movie_details:
                logger.error("Failed to fetch movie details.")
                return None

            if movie_details.get("is_series") in [True, "1", "true", "True"]:
                logger.info("Selected movie is a series. Use get_seasons() and get_episodes().")
                return None
            else:
                # Fetch stream link directly for movies
                stream_link = self.get_stream_link(selected_item)
                return stream_link
        elif item_type == "channel":
            # Fetch stream link for the selected channel
            stream_link = self.get_stream_link(selected_item)
            return stream_link
        elif item_type == "episode":
            # Fetch stream link for the selected episode
            movie_id = selected_item.get("movie_id")
            season_id = selected_item.get("season_id")
            episode_id = selected_item.get("id")
            stream_link = self.get_episode_stream_url(movie_id, season_id, episode_id)
            return stream_link
        else:
            logger.error(f"Unknown item_type: {item_type}")
            return None

    # -------------------------------------------------------------------------
    # SEASON STREAM LINK
    # -------------------------------------------------------------------------

    def get_season_stream_link(self, season_id: str) -> Optional[str]:
        """
        Fetch the stream link for a specific season.

        Parameters:
            season_id (str): The ID of the season.

        Returns:
            Optional[str]: Stream command/url or None if failed.
        """
        self.ensure_token()
        url = f"{self.portal_url}/stalker_portal/server/load.php"
        params = {
            "type": "vod",
            "action": "get_season_stream",
            "season_id": season_id,
            "JsHttpRequest": "1-xml"
        }
        headers = self.generate_headers(include_auth=True)
        logger.debug(f"Fetching stream link for season_id={season_id}")
        response = self.make_request_with_retries(url, params=params, headers=headers)
        if not response:
            logger.error(f"Failed to fetch stream link for season_id={season_id}")
            return None
        json_response = self.safe_json_parse(response)
        if not json_response:
            logger.error(f"Invalid JSON response while fetching stream link for season_id={season_id}")
            return None
        js_data = json_response.get("js", {})
        stream_cmd = js_data.get("cmd") or js_data.get("url")
        if not stream_cmd:
            logger.error(f"No stream command/url found for season_id={season_id}")
            return None
        logger.debug(f"Stream command/url for season_id={season_id}: {stream_cmd}")
        return stream_cmd

    # -------------------------------------------------------------------------
    # EPISODE STREAM LINK
    # -------------------------------------------------------------------------

    def get_episode_stream_link(self, episode_id: str) -> Optional[str]:
        """
        Fetch the stream link for a specific episode.

        Parameters:
            episode_id (str): The ID of the episode.

        Returns:
            Optional[str]: Stream command/url or None if failed.
        """
        self.ensure_token()
        url = f"{self.portal_url}/stalker_portal/server/load.php"
        params = {
            "type": "vod",
            "action": "get_episode_stream",
            "episode_id": episode_id,
            "JsHttpRequest": "1-xml"
        }
        headers = self.generate_headers(include_auth=True)
        logger.debug(f"Fetching stream link for episode_id={episode_id}")
        response = self.make_request_with_retries(url, params=params, headers=headers)
        if not response:
            logger.error(f"Failed to fetch stream link for episode_id={episode_id}")
            return None
        json_response = self.safe_json_parse(response)
        if not json_response:
            logger.error(f"Invalid JSON response while fetching stream link for episode_id={episode_id}")
            return None
        js_data = json_response.get("js", {})
        stream_cmd = js_data.get("cmd") or js_data.get("url")
        if not stream_cmd:
            logger.error(f"No stream command/url found for episode_id={episode_id}")
            return None
        logger.debug(f"Stream command/url for episode_id={episode_id}: {stream_cmd}")
        return stream_cmd

    def get_episode_stream_url(self, movie_id: str, season_id: str, episode_id: str) -> Optional[str]:
        """
        Fetch the stream URL for a specific episode.

        Parameters:
            movie_id (str): The ID of the movie.
            season_id (str): The ID of the season.
            episode_id (str): The ID of the episode.

        Returns:
            Optional[str]: The final stream URL or None if failed.
        """
        self.ensure_token()

        # Step 1: Fetch Episode Data
        ordered_list_url = f"{self.portal_url}/stalker_portal/server/load.php"
        ordered_list_params = {
            "action": "get_ordered_list",
            "type": "vod",
            "movie_id": movie_id,
            "season_id": season_id,
            "episode_id": episode_id,
            "JsHttpRequest": "1-xml"
        }
        headers = self.generate_headers(include_auth=True)
        logger.debug(f"Fetching episode data - GET {ordered_list_url} with params {ordered_list_params}")
        response = self.make_request_with_retries(ordered_list_url, params=ordered_list_params, headers=headers)
        if not response:
            logger.error("Failed to fetch episode data.")
            return None

        json_response = self.safe_json_parse(response)
        if not json_response or "js" not in json_response:
            logger.error("Invalid JSON response while fetching episode data.")
            return None

        episode_data = json_response["js"].get("data", [])
        if not episode_data:
            logger.error("No episode data found in the response.")
            return None

        # Assuming only one episode is returned
        episode_info = episode_data[0]
        stream_id = episode_info.get("id")
        if not stream_id:
            logger.error("Episode 'id' not found in the response.")
            return None

        logger.debug(f"Extracted stream_id: {stream_id}")

        # Step 2: Create Stream Link
        create_link_url = f"{self.portal_url}/stalker_portal/server/load.php"
        create_link_params = {
            "action": "create_link",
            "type": "vod",
            "cmd": f"/media/file_{stream_id}.mpg",
            "JsHttpRequest": "1-xml"
        }
        logger.debug(f"Creating stream link - GET {create_link_url} with params {create_link_params}")
        create_link_response = self.make_request_with_retries(create_link_url, params=create_link_params, headers=headers)
        if not create_link_response:
            logger.error("Failed to create stream link.")
            return None

        create_link_json = self.safe_json_parse(create_link_response)
        if not create_link_json or "js" not in create_link_json:
            logger.error("Invalid JSON response while creating stream link.")
            return None

        cmd_url = create_link_json["js"].get("cmd")
        if not cmd_url:
            logger.error("Stream 'cmd' URL not found in the create_link response.")
            return None

        logger.debug(f"Generated stream URL: {cmd_url}")

        # ---- FIX: Remove "ffmpeg" prefix if present ----
        cmd_url = cmd_url.strip()
        if re.match(r'(?i)^ffmpeg\s*(.*)', cmd_url):
            logger.debug(f"Stripping 'ffmpeg' prefix from cmd: {cmd_url}")
            cmd_url = re.sub(r'(?i)^ffmpeg\s*', '', cmd_url).strip()

        # If not absolute, build from stream_base_url
        if not re.match(r'^https?://', cmd_url, re.IGNORECASE):
            cmd_url = f"{self.stream_base_url}/{cmd_url.lstrip('/')}"
            logger.debug(f"Constructed absolute URL: {cmd_url}")

        # Validate the stream URL
        if self.validate_stream_url(cmd_url):
            return cmd_url
        else:
            logger.error("Invalid stream URL generated.")
            return None

    # -------------------------------------------------------------------------
    # STREAM LINK GENERATION END
    # -------------------------------------------------------------------------

# -------------------------------------------------------------------------
# EXAMPLE USAGE WITH LIVE PROGRESS BAR
# -------------------------------------------------------------------------
def main():
    # Initialize the tqdm progress bar
    progress_bar = tqdm(total=100, desc="Fetching Categories and Items", unit="%", ncols=100)

    # Initialize a mutable object to keep track of the last progress
    last_progress = [0]

    def progress_callback(progress):
        """
        Update the tqdm progress bar based on the progress percentage.

        Args:
            progress (int): Progress percentage (0-100).
        """
        delta = progress - last_progress[0]
        if delta > 0:
            progress_bar.update(delta)
            last_progress[0] = progress

    # Replace these with your actual portal URL and MAC address
    PORTAL_URL = "http://example.com"
    MAC_ADDRESS = "00:1A:2B:3C:4D:5E"

    # Create an instance of StalkerPortal with the progress callback
    with StalkerPortal(
        portal_url=PORTAL_URL,
        mac=MAC_ADDRESS,
        progress_callback=progress_callback
    ) as portal:
        try:
            # Fetch IPTV categories
            logger.info("Fetching IPTV categories...")
            iptv_categories = portal.get_itv_categories()

            # Fetch VOD categories (Movies)
            logger.info("Fetching VOD categories...")
            vod_categories = portal.get_vod_categories()

            # Fetch Series categories (TV Shows)
            logger.info("Fetching Series categories...")
            series_categories = portal.get_series_categories()

            # Combine all categories
            all_categories = iptv_categories + vod_categories + series_categories
            total_categories = len(all_categories)
            logger.info(f"Total categories fetched: {total_categories}")

            # Update progress for category fetching (assuming this took 10% of the total progress)
            progress_bar.update(10)

            # Fetch items within each category
            for idx, category in enumerate(all_categories, start=1):
                category_type = category["category_type"]
                category_id = category["category_id"]
                category_name = category["name"]
                logger.info(f"Fetching items for category {idx}/{total_categories}: {category_name} ({category_type})")

                if category_type == "IPTV":
                    items = portal.get_channels_in_category(category_id)
                elif category_type == "VOD":
                    items = portal.get_vod_in_category(category_id)
                elif category_type == "Series":
                    items = portal.get_series_in_category(category_id)
                else:
                    items = []

                # Process the items as needed
                # For demonstration, we'll just log the number of items fetched
                logger.info(f"Fetched {len(items)} items in category '{category_name}'")

                # Update progress based on the number of categories processed
                progress_increment = 90 / total_categories  # Remaining 90% allocated to fetching items
                progress_bar.update(progress_increment)

        except Exception as e:
            logger.exception(f"An error occurred: {e}")
        finally:
            # Ensure the progress bar reaches 100%
            progress_bar.n = 100
            progress_bar.refresh()
            progress_bar.close()
            logger.info("All categories and items have been fetched successfully.")

if __name__ == "__main__":
    main()

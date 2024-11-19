import sys
import traceback
import requests
import subprocess
import logging
import re
import time
from PyQt5.QtCore import (
    QSettings,
    Qt,
    QThread,
    pyqtSignal,
    QPropertyAnimation,
    QEasingCurve,
    QCoreApplication,
    QTimer,
)
from PyQt5.QtWidgets import (
    QMessageBox,
    QLabel,
    QMainWindow,
    QApplication,
    QListView,
    QFileDialog,
    QVBoxLayout,
    QWidget,
    QLineEdit,
    QHBoxLayout,
    QPushButton,
    QAbstractItemView,
    QTabWidget,
    QProgressBar,
    QSpinBox,
    QCheckBox,
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from urllib.parse import quote, urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Configure the logging module
logging.basicConfig(level=logging.INFO)  # Set to DEBUG for detailed logs


def get_token(session, url, mac_address):
    try:
        handshake_url = f"{url}/portal.php?type=stb&action=handshake&JsHttpRequest=1-xml"
        cookies = {
            "mac": mac_address,
            "stb_lang": "en",
            "timezone": "Europe/London",
            # Token is not available yet
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
            "AppleWebKit/533.3 (KHTML, like Gecko) "
            "MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
        }
        response = session.get(
            handshake_url, cookies=cookies, headers=headers, timeout=10
        )
        response.raise_for_status()
        token = response.json().get("js", {}).get("token")
        if token:
            logging.debug(f"Token retrieved: {token}")
            return token
        else:
            logging.error("Token not found in handshake response.")
            return None
    except Exception as e:
        logging.error(f"Error getting token: {e}")
        return None


class RequestThread(QThread):
    request_complete = pyqtSignal(dict)  # Signal to emit when request is complete
    update_progress = pyqtSignal(int)  # Signal to emit progress updates
    channels_loaded = pyqtSignal(list)  # Signal to emit channels when loaded

    def __init__(
        self,
        base_url,
        mac_address,
        session,
        token,
        category_type=None,
        category_id=None,
        num_threads=5,
    ):
        super().__init__()
        self.base_url = base_url
        self.mac_address = mac_address
        self.session = session
        self.token = token
        self.category_type = category_type
        self.category_id = category_id
        self.num_threads = num_threads

    def run(self):
        try:
            logging.debug("RequestThread started.")
            session = self.session
            url = self.base_url
            mac_address = self.mac_address
            token = self.token

            # Define cookies and headers for subsequent requests, including the token
            cookies = {
                "mac": mac_address,
                "stb_lang": "en",
                "timezone": "Europe/London",
                "token": token,  # Add token to cookies
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
                "AppleWebKit/533.3 (KHTML, like Gecko) "
                "MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                "Authorization": f"Bearer {token}",  # Add token to headers
            }

            # Fetch profile and account info
            try:
                # First GET request: get_profile
                profile_url = (
                    f"{url}/portal.php?type=stb&action=get_profile&JsHttpRequest=1-xml"
                )
                logging.debug(f"Fetching profile from {profile_url}")
                response_profile = session.get(
                    profile_url, cookies=cookies, headers=headers, timeout=10
                )
                response_profile.raise_for_status()
                profile_data = response_profile.json()
                logging.debug(f"Profile data: {profile_data}")
            except Exception as e:
                logging.error(f"Error fetching profile: {e}")
                profile_data = {}

            try:
                # Second GET request: get_main_info
                account_info_url = f"{url}/portal.php?type=account_info&action=get_main_info&JsHttpRequest=1-xml"
                logging.debug(f"Fetching account info from {account_info_url}")
                response_account_info = session.get(
                    account_info_url, cookies=cookies, headers=headers, timeout=10
                )
                response_account_info.raise_for_status()
                account_info_data = response_account_info.json()
                logging.debug(f"Account info data: {account_info_data}")
            except Exception as e:
                logging.error(f"Error fetching account info: {e}")
                account_info_data = {}

            if self.category_type and self.category_id:
                # Fetch channels in a category
                self.update_progress.emit(0)  # Start of channel fetching
                logging.debug("Fetching channels.")
                channels = self.get_channels(
                    session,
                    url,
                    mac_address,
                    token,
                    self.category_type,
                    self.category_id,
                    self.num_threads,
                    cookies,
                    headers,
                )
                self.update_progress.emit(100)
                self.channels_loaded.emit(channels)
            else:
                # Fetch playlist (Live, Movies, Series) concurrently
                data = {}
                progress_lock = Lock()
                progress = 0

                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = {
                        executor.submit(
                            self.get_genres,
                            session,
                            url,
                            mac_address,
                            token,
                            cookies,
                            headers,
                        ): "Live",
                        executor.submit(
                            self.get_vod_categories,
                            session,
                            url,
                            mac_address,
                            token,
                            cookies,
                            headers,
                        ): "Movies",
                        executor.submit(
                            self.get_series_categories,
                            session,
                            url,
                            mac_address,
                            token,
                            cookies,
                            headers,
                        ): "Series",
                    }

                    total_tasks = len(futures) + 2  # +2 for get_profile and get_main_info
                    completed_tasks = 2  # Since get_profile and get_main_info are already done
                    self.update_progress.emit(int((completed_tasks / total_tasks) * 100))

                    for future in as_completed(futures):
                        tab_name = futures[future]
                        try:
                            result = future.result()
                            data[tab_name] = result
                        except Exception as e:
                            logging.error(f"Error fetching {tab_name}: {e}")
                            data[tab_name] = []
                        finally:
                            with progress_lock:
                                completed_tasks += 1
                                progress_percent = int(
                                    (completed_tasks / total_tasks) * 100
                                )
                                self.update_progress.emit(progress_percent)
                                logging.debug(f"Progress: {progress_percent}%")

                self.request_complete.emit(data)

        except Exception as e:
            logging.error(f"Request thread error: {str(e)}")
            traceback.print_exc()
            self.request_complete.emit({})  # Emit empty data in case of an error
            self.update_progress.emit(0)  # Reset progress on error

    def get_genres(
        self, session, url, mac_address, token, cookies, headers
    ):
        try:
            genres_url = (
                f"{url}/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
            )
            response = session.get(
                genres_url, cookies=cookies, headers=headers, timeout=10
            )
            response.raise_for_status()
            genre_data = response.json().get("js", [])
            if genre_data:
                genres = [
                    {
                        "name": i["title"],
                        "category_type": "IPTV",
                        "category_id": i["id"],
                    }
                    for i in genre_data
                ]
                # Sort genres alphabetically by name
                genres.sort(key=lambda x: x["name"])
                logging.debug(f"Genres fetched: {genres}")
                return genres
            else:
                logging.warning("No genres data found.")
                return []
        except Exception as e:
            logging.error(f"Error getting genres: {e}")
            return []

    def get_vod_categories(
        self, session, url, mac_address, token, cookies, headers
    ):
        try:
            vod_url = (
                f"{url}/portal.php?type=vod&action=get_categories&JsHttpRequest=1-xml"
            )
            response = session.get(
                vod_url, cookies=cookies, headers=headers, timeout=10
            )
            response.raise_for_status()
            categories_data = response.json().get("js", [])
            if categories_data:
                categories = [
                    {
                        "name": category["title"],
                        "category_type": "VOD",
                        "category_id": category["id"],
                    }
                    for category in categories_data
                ]
                # Sort categories alphabetically by name
                categories.sort(key=lambda x: x["name"])
                logging.debug(f"VOD categories fetched: {categories}")
                return categories
            else:
                logging.warning("No VOD categories data found.")
                return []
        except Exception as e:
            logging.error(f"Error getting VOD categories: {e}")
            return []

    def get_series_categories(
        self, session, url, mac_address, token, cookies, headers
    ):
        try:
            series_url = (
                f"{url}/portal.php?type=series&action=get_categories&JsHttpRequest=1-xml"
            )
            response = session.get(
                series_url, cookies=cookies, headers=headers, timeout=10
            )
            response.raise_for_status()
            response_json = response.json()
            logging.debug(f"Series categories response: {response_json}")
            if not isinstance(response_json, dict) or "js" not in response_json:
                logging.error("Unexpected response structure for series categories.")
                return []

            categories_data = response_json.get("js", [])
            categories = [
                {
                    "name": category["title"],
                    "category_type": "Series",
                    "category_id": category["id"],
                }
                for category in categories_data
            ]
            # Sort categories alphabetically by name
            categories.sort(key=lambda x: x["name"])
            logging.debug(f"Series categories fetched: {categories}")
            return categories
        except Exception as e:
            logging.error(f"Error getting series categories: {e}")
            return []

    def get_channels(
        self,
        session,
        url,
        mac_address,
        token,
        category_type,
        category_id,
        num_threads,
        cookies,
        headers,
    ):
        try:
            channels = []
            # First, get total number of items
            page_number = 0
            total_items = None
            initial_url = ""
            if category_type == "IPTV":
                initial_url = f"{url}/portal.php?type=itv&action=get_ordered_list&genre={category_id}&JsHttpRequest=1-xml&p=0"
            elif category_type == "VOD":
                initial_url = f"{url}/portal.php?type=vod&action=get_ordered_list&category={category_id}&JsHttpRequest=1-xml&p=0"
            elif category_type == "Series":
                initial_url = f"{url}/portal.php?type=series&action=get_ordered_list&category={category_id}&p=0&JsHttpRequest=1-xml"

            response = session.get(
                initial_url, cookies=cookies, headers=headers, timeout=10
            )
            response.raise_for_status()
            response_json = response.json()
            total_items = response_json.get("js", {}).get("total_items", 0)
            items_per_page = len(response_json.get("js", {}).get("data", []))
            total_pages = (total_items + items_per_page - 1) // items_per_page

            logging.debug(
                f"Total items: {total_items}, items per page: {items_per_page}, total pages: {total_pages}"
            )

            # Add first page data
            channels_data = response_json.get("js", {}).get("data", [])
            for channel in channels_data:
                channel["item_type"] = (
                    "series"
                    if category_type == "Series"
                    else "vod"
                    if category_type == "VOD"
                    else "channel"
                )
            channels.extend(channels_data)
            self.update_progress.emit(int(1 / max(total_pages, 1) * 100))

            # Prepare page numbers to fetch (exclude page 0 which is already fetched)
            if total_pages > 1:
                page_numbers = list(range(1, total_pages))
            else:
                page_numbers = []

            # Use ThreadPoolExecutor to fetch pages concurrently
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = []
                progress_lock = Lock()
                progress = 1  # Already fetched page 0
                for p in page_numbers:
                    if category_type == "IPTV":
                        channels_url = f"{url}/portal.php?type=itv&action=get_ordered_list&genre={category_id}&JsHttpRequest=1-xml&p={p}"
                    elif category_type == "VOD":
                        channels_url = f"{url}/portal.php?type=vod&action=get_ordered_list&category={category_id}&JsHttpRequest=1-xml&p={p}"
                    elif category_type == "Series":
                        channels_url = f"{url}/portal.php?type=series&action=get_ordered_list&category={category_id}&p={p}&JsHttpRequest=1-xml"
                    else:
                        logging.error(f"Unknown category_type: {category_type}")
                        continue
                    futures.append(
                        executor.submit(
                            self.fetch_page,
                            channels_url,
                            cookies,
                            headers,
                            category_type,
                            p,
                        )
                    )

                total_pages = max(total_pages, 1)
                for future in as_completed(futures):
                    page_channels = future.result()
                    channels.extend(page_channels)
                    # Update progress
                    with progress_lock:
                        progress += 1
                        progress_percent = int((progress / total_pages) * 100)
                        self.update_progress.emit(progress_percent)
                        logging.debug(f"Progress: {progress_percent}%")

            # Deduplicate channels based on their unique identifiers
            unique_channels = {}
            for channel in channels:
                channel_id = channel.get('id')
                if channel_id not in unique_channels:
                    unique_channels[channel_id] = channel
            channels = list(unique_channels.values())

            # Sort channels alphabetically by name
            channels.sort(key=lambda x: x.get("name", ""))
            logging.debug(f"Total channels fetched: {len(channels)}")
            return channels
        except Exception as e:
            logging.error(f"An error occurred while retrieving channels: {str(e)}")
            return []

    def fetch_page(self, url, cookies, headers, category_type, page_number):
        try:
            logging.debug(f"Fetching page {page_number} from URL: {url}")
            session = requests.Session()
            response = session.get(url, cookies=cookies, headers=headers, timeout=10)
            response.raise_for_status()
            response_json = response.json()
            channels_data = response_json.get("js", {}).get("data", [])
            for channel in channels_data:
                channel["item_type"] = (
                    "series"
                    if category_type == "Series"
                    else "vod"
                    if category_type == "VOD"
                    else "channel"
                )
            return channels_data
        except Exception as e:
            logging.error(f"Error fetching page {page_number}: {e}")
            return []


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MAC IPTV Player by MY-1 BETA")
        self.setGeometry(100, 100, 550, 560)  # Increased window height for the progress bar

        self.settings = QSettings("MyCompany", "IPTVPlayer")

        

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout(central_widget)

        top_layout = QVBoxLayout()
        layout.addLayout(top_layout)

        hostname_label = QLabel("Hostname:")
        top_layout.addWidget(hostname_label)

        self.hostname_input = QLineEdit()
        top_layout.addWidget(self.hostname_input)

        mac_label = QLabel("MAC:")
        top_layout.addWidget(mac_label)

        self.mac_input = QLineEdit()
        top_layout.addWidget(self.mac_input)

        media_player_layout = QHBoxLayout()
        top_layout.addLayout(media_player_layout)

        self.media_player_input = QLineEdit()
        media_player_layout.addWidget(self.media_player_input)

        self.choose_player_button = QPushButton("Choose Player")
        media_player_layout.addWidget(self.choose_player_button)
        self.choose_player_button.clicked.connect(self.open_file_dialog)

        # Number of Threads Input
        threads_layout = QHBoxLayout()
        top_layout.addLayout(threads_layout)

        threads_label = QLabel("Number of Threads:")
        threads_layout.addWidget(threads_label)

        self.threads_input = QSpinBox()
        self.threads_input.setMinimum(1)
        self.threads_input.setMaximum(20)
        self.threads_input.setValue(5)  # Default value
        threads_layout.addWidget(self.threads_input)

        self.get_playlist_button = QPushButton("Get Playlist")
        layout.addWidget(self.get_playlist_button)
        self.get_playlist_button.clicked.connect(self.get_playlist)

        # Create a QTabWidget
        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        # Dictionary to hold tab data
        self.tabs = {}

        for tab_name in ["Live", "Movies", "Series"]:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            playlist_view = QListView()
            playlist_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
            tab_layout.addWidget(playlist_view)

            playlist_model = QStandardItemModel(playlist_view)
            playlist_view.setModel(playlist_model)

            # Connect double-click signal
            playlist_view.doubleClicked.connect(self.on_playlist_selection_changed)

            # Add the tab to the tab widget
            self.tab_widget.addTab(tab, tab_name)

            # Store tab data
            self.tabs[tab_name] = {
                "tab_widget": tab,
                "playlist_view": playlist_view,
                "playlist_model": playlist_model,
                "current_category": None,
                "navigation_stack": [],
                "playlist_data": [],
                "current_channels": [],
                "current_series_info": [],
                "current_view": "categories",
            }

        # Create a purple progress bar at the bottom
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(
            """
            QProgressBar {
                text-align: center;  /* Center the text */
                color: white;  /* Set the text color to white */
            }
            QProgressBar::chunk {
                background-color: purple;  /* Set the progress bar chunk color */
            }
            """
        )
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Initialize QPropertyAnimation for the progress bar
        self.progress_animation = QPropertyAnimation(self.progress_bar, b"value")
        self.progress_animation.setDuration(200)  # Duration in milliseconds
        self.progress_animation.setEasingCurve(QEasingCurve.InOutQuad)  # Smooth curve

        # Initialize session and token
        self.session = None
        self.token = None
        self.token_timestamp = None

        # Connect the update_progress signal to the set_progress slot
        self.current_request_thread = None  # To keep track of the current thread

        # Add 'Always on Top' checkbox
        self.always_on_top_checkbox = QCheckBox("Always on Top")
        layout.addWidget(self.always_on_top_checkbox)
        self.always_on_top_checkbox.stateChanged.connect(self.toggle_always_on_top)

        # Load settings
        self.load_settings()
        

        # Load the saved state of the checkbox
        always_on_top = self.settings.value("always_on_top", False, type=bool)
        self.always_on_top_checkbox.setChecked(always_on_top)
        if always_on_top:
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
            self.show()
            
    def load_settings(self):
        self.hostname_input.setText(self.settings.value("hostname", ""))
        self.mac_input.setText(self.settings.value("mac_address", ""))
        self.media_player_input.setText(self.settings.value("media_player", ""))
        self.threads_input.setValue(int(self.settings.value("num_threads", 5)))
        always_on_top = self.settings.value("always_on_top", False, type=bool)
        self.always_on_top_checkbox.setChecked(always_on_top)
        if always_on_top:
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
            self.show()

   


    def closeEvent(self, event):
        self.save_settings()
        event.accept()

    def save_settings(self):
        self.settings.setValue("hostname", self.hostname_input.text())
        self.settings.setValue("mac_address", self.mac_input.text())
        self.settings.setValue("media_player", self.media_player_input.text())
        self.settings.setValue("num_threads", self.threads_input.value())
        self.settings.setValue("always_on_top", self.always_on_top_checkbox.isChecked())

    def toggle_always_on_top(self, state):
        if state == Qt.Checked:
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
            self.settings.setValue("always_on_top", True)
        else:
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)
            self.settings.setValue("always_on_top", False)
        self.show()
        
    def show_error_message(self, message):
        QMessageBox.critical(self, "Error", message)

    


    def open_file_dialog(self):
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        file_dialog = QFileDialog()
        file_dialog.setFileMode(QFileDialog.ExistingFile)
        file_dialog.setNameFilter("Executable Files (*.exe)")
        if file_dialog.exec_():
            file_names = file_dialog.selectedFiles()
            if file_names:
                media_player = file_names[0]
                self.media_player_input.setText(media_player)
                self.settings.setValue("media_player", media_player)
                logging.debug(f"Media player selected: {media_player}")

    def get_playlist(self):
        self.set_progress(0)  # Reset the progress bar to 0 at the start
        hostname_input = self.hostname_input.text().strip()
        mac_address = self.mac_input.text().strip().upper()
        media_player = self.media_player_input.text().strip()
        num_threads = self.threads_input.value()

        if not hostname_input or not mac_address or not media_player:
            QMessageBox.warning(
                self,
                "Warning",
                "Please enter the Hostname, MAC Address, and Media Player.",
            )
            logging.warning(
                "User attempted to get playlist without entering all required fields."
            )
            return

        parsed_url = urlparse(hostname_input)
        if not parsed_url.scheme and not parsed_url.netloc:
            parsed_url = urlparse(f"http://{hostname_input}")
        elif not parsed_url.scheme:
            parsed_url = parsed_url._replace(scheme="http")

        self.base_url = urlunparse(
            (parsed_url.scheme, parsed_url.netloc, "", "", "", "")
        )
        self.mac_address = mac_address

        # Initialize session and get token
        self.session = requests.Session()
        self.token = get_token(self.session, self.base_url, self.mac_address)
        self.token_timestamp = time.time()

        if not self.token:
            QMessageBox.critical(
                self,
                "Error",
                "Failed to retrieve token. Please check your MAC address and URL.",
            )
            return

        # Initialize RequestThread for fetching playlist
        if (
            self.current_request_thread is not None
            and self.current_request_thread.isRunning()
        ):
            QMessageBox.warning(
                self,
                "Warning",
                "A playlist request is already in progress. Please wait.",
            )
            logging.warning(
                "User attempted to start a new playlist request while one is already running."
            )
            return

        self.request_thread = RequestThread(
            self.base_url,
            mac_address,
            self.session,
            self.token,
            num_threads=num_threads,
        )
        self.request_thread.request_complete.connect(self.on_initial_playlist_received)
        self.request_thread.update_progress.connect(self.set_progress)
        self.request_thread.start()
        self.current_request_thread = self.request_thread
        logging.debug("Started RequestThread for playlist.")


    def set_progress(self, value):
        # Animate the progress bar to the new value
        if self.progress_animation.state() == QPropertyAnimation.Running:
            self.progress_animation.stop()
        start_val = self.progress_bar.value()
        self.progress_animation.setStartValue(start_val)
        self.progress_animation.setEndValue(value)
        self.progress_animation.start()
        logging.debug(f"Animating progress bar from {start_val} to {value}.")

    def on_initial_playlist_received(self, data):
        if self.current_request_thread != self.sender():
            logging.debug("Received data from an old thread. Ignoring.")
            return  # Ignore signals from older threads

        if not data:
            self.show_error_message(
                "Failed to retrieve playlist data. Check your connection and try again."
            )
            logging.error("Playlist data is empty.")
            self.current_request_thread = None
            return
        for tab_name, tab_data in data.items():
            tab_info = self.tabs.get(tab_name)
            if not tab_info:
                logging.warning(f"Unknown tab name: {tab_name}")
                continue
            tab_info["playlist_data"] = tab_data
            tab_info["current_category"] = None
            tab_info["navigation_stack"] = []
            self.update_playlist_view(tab_name)
        logging.debug("Playlist data loaded into tabs.")
        self.current_request_thread = None  # Reset the current thread

    def update_playlist_view(self, tab_name, scroll_position=0):
        tab_info = self.tabs[tab_name]
        playlist_model = tab_info["playlist_model"]
        playlist_view = tab_info["playlist_view"]

        playlist_model.clear()
        tab_info["current_view"] = "categories"

        if tab_info["navigation_stack"]:
            go_back_item = QStandardItem("Go Back")
            playlist_model.appendRow(go_back_item)

        if tab_info["current_category"] is None:
            for item in tab_info["playlist_data"]:
                name = item["name"]
                list_item = QStandardItem(name)
                list_item.setData(item, Qt.UserRole)
                list_item.setData("category", Qt.UserRole + 1)
                playlist_model.appendRow(list_item)
            # Restore scroll position after model is populated
            QTimer.singleShot(0, lambda: playlist_view.verticalScrollBar().setValue(scroll_position))
        else:
            self.retrieve_channels(tab_name, tab_info["current_category"])

    def retrieve_channels(self, tab_name, category):
        tab_info = self.tabs[tab_name]
        category_type = category["category_type"]
        category_id = category.get("category_id") or category.get("genre_id")
        num_threads = self.threads_input.value()
        try:
            # Instead of setting progress directly, emit 0
            self.set_progress(0)
            if (
                self.current_request_thread is not None
                and self.current_request_thread.isRunning()
            ):
                QMessageBox.warning(
                    self,
                    "Warning",
                    "A channel request is already in progress. Please wait.",
                )
                logging.warning(
                    "User attempted to start a new channel request while one is already running."
                )
                return  # Prevent starting multiple channel requests

            # Check if token is still valid
            if not self.is_token_valid():
                self.token = get_token(self.session, self.base_url, self.mac_address)
                self.token_timestamp = time.time()
                if not self.token:
                    QMessageBox.critical(
                        self,
                        "Error",
                        "Failed to retrieve token. Please check your MAC address and URL.",
                    )
                    return

            self.request_thread = RequestThread(
                self.base_url,
                self.mac_address,
                self.session,
                self.token,
                category_type,
                category_id,
                num_threads=num_threads,
            )
            self.request_thread.update_progress.connect(self.set_progress)
            self.request_thread.channels_loaded.connect(
                lambda channels: self.on_channels_loaded(tab_name, channels)
            )
            self.request_thread.start()
            self.current_request_thread = self.request_thread
            logging.debug(
                f"Started RequestThread for channels in category {category_id}."
            )
        except Exception as e:
            traceback.print_exc()
            self.show_error_message("An error occurred while retrieving channels.")
            logging.error(f"Exception in retrieve_channels: {e}")

    def on_channels_loaded(self, tab_name, channels):
        if self.current_request_thread != self.sender():
            logging.debug("Received channels from an old thread. Ignoring.")
            return  # Ignore signals from older threads

        tab_info = self.tabs[tab_name]
        tab_info["current_channels"] = channels
        self.update_channel_view(tab_name)
        logging.debug(
            f"Channels loaded for tab {tab_name}: {len(channels)} items."
        )
        self.current_request_thread = None  # Reset the current thread

    def update_channel_view(self, tab_name, scroll_position=0):
        tab_info = self.tabs[tab_name]
        playlist_model = tab_info["playlist_model"]
        playlist_view = tab_info["playlist_view"]

        playlist_model.clear()
        tab_info["current_view"] = "channels"

        if tab_info["navigation_stack"]:
            go_back_item = QStandardItem("Go Back")
            playlist_model.appendRow(go_back_item)

        for channel in tab_info["current_channels"]:
            channel_name = channel["name"]
            list_item = QStandardItem(channel_name)
            list_item.setData(channel, Qt.UserRole)
            item_type = channel.get("item_type", "channel")
            list_item.setData(item_type, Qt.UserRole + 1)
            playlist_model.appendRow(list_item)

        # Restore scroll position after model is populated
        QTimer.singleShot(0, lambda: playlist_view.verticalScrollBar().setValue(scroll_position))

    def on_playlist_selection_changed(self, index):
        sender = self.sender()
        current_tab = None
        for tab_name, tab_info in self.tabs.items():
            if sender == tab_info["playlist_view"]:
                current_tab = tab_name
                break
        else:
            logging.error("Unknown sender for on_playlist_selection_changed")
            return

        tab_info = self.tabs[current_tab]
        playlist_model = tab_info["playlist_model"]
        playlist_view = tab_info["playlist_view"]

        if index.isValid():
            item = playlist_model.itemFromIndex(index)
            item_text = item.text()

            if item_text == "Go Back":
                # Handle 'Go Back' functionality
                if tab_info["navigation_stack"]:
                    nav_state = tab_info["navigation_stack"].pop()
                    tab_info["current_category"] = nav_state["category"]
                    tab_info["current_view"] = nav_state["view"]
                    tab_info[
                        "current_series_info"
                    ] = nav_state["series_info"]  # Restore series_info
                    scroll_position = nav_state.get("scroll_position", 0)
                    logging.debug(f"Go Back to view: {tab_info['current_view']}")
                    if tab_info["current_view"] == "categories":
                        self.update_playlist_view(current_tab, scroll_position)
                    elif tab_info["current_view"] == "channels":
                        self.update_channel_view(current_tab, scroll_position)
                    elif tab_info["current_view"] in ["seasons", "episodes"]:
                        self.update_series_view(current_tab, scroll_position)
                else:
                    logging.debug("Navigation stack is empty. Cannot go back.")
                    QMessageBox.information(
                        self, "Info", "No previous view to go back to."
                    )
            else:
                item_data = item.data(Qt.UserRole)
                item_type = item.data(Qt.UserRole + 1)
                logging.debug(f"Item data: {item_data}, item type: {item_type}")

                # Store current scroll position before navigating
                current_scroll_position = playlist_view.verticalScrollBar().value()

                if item_type == "category":
                    # Navigate into a category
                    tab_info["navigation_stack"].append(
                        {
                            "category": tab_info["current_category"],
                            "view": tab_info["current_view"],
                            "series_info": tab_info["current_series_info"],  # Preserve current_series_info
                            "scroll_position": current_scroll_position,
                        }
                    )
                    tab_info["current_category"] = item_data
                    logging.debug(f"Navigating to category: {item_data.get('name')}")
                    self.retrieve_channels(current_tab, tab_info["current_category"])

                elif item_type == "series":
                    # User selected a series, retrieve its seasons
                    tab_info["navigation_stack"].append(
                        {
                            "category": tab_info["current_category"],
                            "view": tab_info["current_view"],
                            "series_info": tab_info["current_series_info"],  # Preserve current_series_info
                            "scroll_position": current_scroll_position,
                        }
                    )
                    tab_info["current_category"] = item_data
                    logging.debug(f"Navigating to series: {item_data.get('name')}")
                    self.retrieve_series_info(current_tab, item_data)

                elif item_type == "season":
                    # User selected a season, set navigation context
                    tab_info["navigation_stack"].append(
                        {
                            "category": tab_info["current_category"],
                            "view": tab_info["current_view"],
                            "series_info": tab_info["current_series_info"],  # Preserve current_series_info
                            "scroll_position": current_scroll_position,
                        }
                    )
                    tab_info["current_category"] = item_data

                    # Update view to 'seasons'
                    tab_info["current_view"] = "seasons"
                    self.update_series_view(current_tab)

                    # Retrieve episodes using the season data
                    logging.debug(
                        f"Fetching episodes for season {item_data['season_number']} in series {item_data['name']}"
                    )
                    self.retrieve_series_info(
                        current_tab,
                        item_data,
                        season_number=item_data["season_number"],
                    )

                elif item_type == "episode":
                    # User selected an episode, play it
                    logging.debug(f"Playing episode: {item_data.get('name')}")
                    self.play_channel(item_data)

                elif item_type in ["channel", "vod"]:
                    # This is an IPTV channel or VOD, play it
                    logging.debug(f"Playing channel/VOD: {item_data.get('name')}")
                    self.play_channel(item_data)

                else:
                    logging.error("Unknown item type")

    def retrieve_series_info(self, tab_name, context_data, season_number=None):
        tab_info = self.tabs[tab_name]
        try:
            session = self.session
            url = self.base_url
            mac_address = self.mac_address

            # Check if token is still valid
            if not self.is_token_valid():
                self.token = get_token(session, url, mac_address)
                self.token_timestamp = time.time()
                if not self.token:
                    QMessageBox.critical(
                        self,
                        "Error",
                        "Failed to retrieve token. Please check your MAC address and URL.",
                    )
                    return

            token = self.token

            cookies = {
                "mac": mac_address,
                "stb_lang": "en",
                "timezone": "Europe/London",
                "token": token,  # Include token in cookies
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
                "AppleWebKit/533.3 (KHTML, like Gecko) "
                "MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                "Authorization": f"Bearer {token}",
            }

            series_id = context_data.get("id")
            if not series_id:
                logging.error(f"Series ID missing in context data: {context_data}")
                return

            if season_number is None:
                # Fetch seasons
                all_seasons = []
                page_number = 0
                while True:
                    seasons_url = f"{url}/portal.php?type=series&action=get_ordered_list&movie_id={series_id}&season_id=0&episode_id=0&JsHttpRequest=1-xml&p={page_number}"
                    logging.debug(
                        f"Fetching seasons URL: {seasons_url}, headers: {headers}, cookies: {cookies}"
                    )
                    response = session.get(
                        seasons_url, cookies=cookies, headers=headers, timeout=10
                    )
                    logging.debug(f"Seasons response: {response.text}")
                    if response.status_code == 200:
                        seasons_data = response.json().get("js", {}).get(
                            "data", []
                        )
                        if not seasons_data:
                            break
                        for season in seasons_data:
                            season_id = season.get("id", "")
                            season_number_extracted = None
                            if season_id.startswith("season"):
                                match = re.match(r"season(\d+)", season_id)
                                if match:
                                    season_number_extracted = int(
                                        match.group(1)
                                    )
                                else:
                                    logging.error(
                                        f"Unexpected season id format: {season_id}"
                                    )
                            else:
                                match = re.match(r"\d+:(\d+)", season_id)
                                if match:
                                    season_number_extracted = int(
                                        match.group(1)
                                    )
                                else:
                                    logging.error(
                                        f"Unexpected season id format: {season_id}"
                                    )

                            season["season_number"] = season_number_extracted
                            season["item_type"] = "season"
                        all_seasons.extend(seasons_data)
                        total_items = response.json().get("js", {}).get(
                            "total_items", len(all_seasons)
                        )
                        logging.debug(
                            f"Fetched {len(all_seasons)} seasons out of {total_items}."
                        )
                        if len(all_seasons) >= total_items:
                            break
                        page_number += 1
                    else:
                        logging.error(
                            f"Failed to fetch seasons for page {page_number} with status code {response.status_code}"
                        )
                        break

                if all_seasons:
                    # Sort seasons by season_number
                    all_seasons.sort(key=lambda x: x.get('season_number', 0))
                    tab_info["current_series_info"] = all_seasons
                    tab_info["current_view"] = "seasons"
                    self.update_series_view(tab_name)
            else:
                # Fetch episodes for the given season
                series_list = context_data.get("series", [])
                if not series_list:
                    logging.info("No episodes found in this season.")
                    return

                logging.debug(f"Series episodes found: {series_list}")
                all_episodes = []
                for episode_number in series_list:
                    episode = {
                        "id": f"{series_id}:{episode_number}",
                        "series_id": series_id,
                        "season_number": season_number,
                        "episode_number": episode_number,
                        "name": f"Episode {episode_number}",
                        "item_type": "episode",
                        "cmd": context_data.get("cmd"),
                    }
                    logging.debug(f"Episode details: {episode}")
                    all_episodes.append(episode)

                if all_episodes:
                    # Sort episodes by episode_number
                    all_episodes.sort(key=lambda x: x.get('episode_number', 0))
                    tab_info["current_series_info"] = all_episodes
                    tab_info["current_view"] = "episodes"
                    self.update_series_view(tab_name)
                else:
                    logging.info("No episodes found.")
        except KeyError as e:
            logging.error(f"KeyError retrieving series info: {str(e)}")
        except Exception as e:
            logging.error(f"Error retrieving series info: {str(e)}")

    def is_token_valid(self):
        # Assuming token is valid for 10 minutes
        if self.token and (time.time() - self.token_timestamp) < 600:
            return True
        return False

    def play_channel(self, channel):
        cmd = channel.get("cmd")
        if not cmd:
            logging.error(f"No command found for channel: {channel}")
            return
        if cmd.startswith("ffmpeg "):
            cmd = cmd[len("ffmpeg ") :]

        item_type = channel.get("item_type", "channel")

        if item_type == "channel":
            needs_create_link = False
            if "/ch/" in cmd and cmd.endswith("_"):
                needs_create_link = True

            if needs_create_link:
                try:
                    session = self.session
                    url = self.base_url
                    mac_address = self.mac_address

                    # Check if token is still valid
                    if not self.is_token_valid():
                        self.token = get_token(session, url, mac_address)
                        self.token_timestamp = time.time()
                        if not self.token:
                            QMessageBox.critical(
                                self,
                                "Error",
                                "Failed to retrieve token. Please check your MAC address and URL.",
                            )
                            return

                    token = self.token

                    cmd_encoded = quote(cmd)
                    cookies = {
                        "mac": mac_address,
                        "stb_lang": "en",
                        "timezone": "Europe/London",
                        "token": token,  # Include token in cookies
                    }
                    headers = {
                        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
                        "AppleWebKit/533.3 (KHTML, like Gecko) "
                        "MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                        "Authorization": f"Bearer {token}",
                    }
                    create_link_url = f"{url}/portal.php?type=itv&action=create_link&cmd={cmd_encoded}&JsHttpRequest=1-xml"
                    logging.debug(f"Create link URL: {create_link_url}")
                    response = session.get(
                        create_link_url,
                        cookies=cookies,
                        headers=headers,
                        timeout=10,
                    )
                    response.raise_for_status()
                    json_response = response.json()
                    logging.debug(f"Create link response: {json_response}")
                    cmd_value = json_response.get("js", {}).get("cmd")
                    if cmd_value:
                        if cmd_value.startswith("ffmpeg "):
                            cmd_value = cmd_value[len("ffmpeg ") :]
                        stream_url = cmd_value
                        self.launch_media_player(stream_url)
                    else:
                        logging.error("Stream URL not found in the response.")
                        QMessageBox.critical(
                            self, "Error", "Stream URL not found in the response."
                        )
                except Exception as e:
                    logging.error(f"Error creating stream link: {e}")
                    QMessageBox.critical(
                        self, "Error", f"Error creating stream link: {e}"
                    )
            else:
                self.launch_media_player(cmd)

        elif item_type in ["episode", "vod"]:
            try:
                session = self.session
                url = self.base_url
                mac_address = self.mac_address

                # Check if token is still valid
                if not self.is_token_valid():
                    self.token = get_token(session, url, mac_address)
                    self.token_timestamp = time.time()
                    if not self.token:
                        QMessageBox.critical(
                            self,
                            "Error",
                            "Failed to retrieve token. Please check your MAC address and URL.",
                        )
                        return

                token = self.token

                cmd_encoded = quote(cmd)
                cookies = {
                    "mac": mac_address,
                    "stb_lang": "en",
                    "timezone": "Europe/London",
                    "token": token,  # Include token in cookies
                }
                headers = {
                    "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
                    "AppleWebKit/533.3 (KHTML, like Gecko) "
                    "MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                    "Authorization": f"Bearer {token}",
                }
                if item_type == "episode":
                    episode_number = channel.get("episode_number")
                    if episode_number is None:
                        logging.error("Episode number is missing.")
                        QMessageBox.critical(
                            self, "Error", "Episode number is missing."
                        )
                        return
                    create_link_url = f"{url}/portal.php?type=vod&action=create_link&cmd={cmd_encoded}&series={episode_number}&JsHttpRequest=1-xml"
                else:
                    create_link_url = f"{url}/portal.php?type=vod&action=create_link&cmd={cmd_encoded}&JsHttpRequest=1-xml"
                logging.debug(f"Create link URL: {create_link_url}")
                response = session.get(
                    create_link_url,
                    cookies=cookies,
                    headers=headers,
                    timeout=10,
                )
                response.raise_for_status()
                json_response = response.json()
                logging.debug(f"Create link response: {json_response}")
                cmd_value = json_response.get("js", {}).get("cmd")
                if cmd_value:
                    if cmd_value.startswith("ffmpeg "):
                        cmd_value = cmd_value[len("ffmpeg ") :]
                    stream_url = cmd_value
                    self.launch_media_player(stream_url)
                else:
                    logging.error("Stream URL not found in the response.")
                    QMessageBox.critical(
                        self, "Error", "Stream URL not found in the response."
                    )
            except Exception as e:
                logging.error(f"Error creating stream link: {e}")
                QMessageBox.critical(
                    self, "Error", f"Error creating stream link: {e}"
                )
        else:
            logging.error(f"Unknown item type: {item_type}")
            QMessageBox.critical(
                self, "Error", f"Unknown item type: {item_type}"
            )

    def update_series_view(self, tab_name, scroll_position=0):
        tab_info = self.tabs[tab_name]
        playlist_model = tab_info["playlist_model"]
        playlist_view = tab_info["playlist_view"]

        playlist_model.clear()

        if tab_info["navigation_stack"]:
            go_back_item = QStandardItem("Go Back")
            playlist_model.appendRow(go_back_item)

        for item in tab_info["current_series_info"]:
            item_type = item.get("item_type")
            if item_type == "season":
                name = f"Season {item['season_number']}"
            elif item_type == "episode":
                name = f"Episode {item['episode_number']}"
            else:
                name = item.get("name") or item.get("title")
            list_item = QStandardItem(name)
            list_item.setData(item, Qt.UserRole)
            list_item.setData(item_type, Qt.UserRole + 1)
            playlist_model.appendRow(list_item)

        # Restore scroll position after model is populated
        QTimer.singleShot(0, lambda: playlist_view.verticalScrollBar().setValue(scroll_position))

    def launch_media_player(self, stream_url):
        media_player = self.settings.value("media_player", "")
        if media_player:
            try:
                subprocess.Popen([media_player, stream_url])
                logging.debug(f"Launching media player with URL: {stream_url}")
            except Exception as e:
                logging.error(f"Error opening media player: {e}")
                QMessageBox.critical(
                    self, "Error", f"Failed to launch media player: {e}"
                )
        else:
            logging.error("Media player executable path not found in settings.")
            QMessageBox.critical(
                self,
                "Error",
                "Media player executable path not found in settings.",
            )

    def resizeEvent(self, event):
        pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # Correctly set the application style
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

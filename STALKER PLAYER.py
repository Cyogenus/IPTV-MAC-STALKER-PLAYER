import sys
import traceback
import requests
import subprocess
import logging
import tempfile
import re
import time
import html
import qdarkstyle
from urllib.parse import quote, urlparse, urlunparse, urljoin
import os
import hashlib
from PyQt5.QtCore import QUrl, QTemporaryDir
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkRequest

import concurrent.futures  # Added for parallel execution
from datetime import datetime
from Epg import EpgManager, format_epg_tooltip

from stalker import StalkerPortal
from PyQt5.QtCore import (
    QSettings,
    Qt,
    QThread,
    pyqtSignal,
    QTimer,  # Changed from QPropertyAnimation
    QCoreApplication,
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
    QListWidget,
    QListWidgetItem,
    QInputDialog,
    QDialog,
    QStyle,
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QIcon
from urllib.parse import quote, urlparse, urlunparse

# Remove existing handlers
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

# Reconfigure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
   # ---- EPG roles/keys ----
ROLE_ITEM_TYPE   = Qt.UserRole + 1
ROLE_EPG_TEXT    = Qt.UserRole + 2
ROLE_CHANNEL_KEY = Qt.UserRole + 3
# Reintroduce get_token for non-stalker portals
def get_token(session, url, mac_address):
    try:
        handshake_url = f"{url}/portal.php?type=stb&action=handshake&JsHttpRequest=1-xml"
        cookies = {
            "mac": mac_address,
            "stb_lang": "en",
            "timezone": "Europe/London",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
                          "AppleWebKit/533.3 (KHTML, like Gecko) "
                          "MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
        }
        response = session.get(handshake_url, cookies=cookies, headers=headers, timeout=15)
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
    request_complete = pyqtSignal(dict, dict, dict)
    update_progress = pyqtSignal(int)
    channels_loaded = pyqtSignal(list)

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

            cookies = {
                "mac": mac_address,
                "stb_lang": "en",
                "timezone": "Europe/London",
                "token": token,
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) "
                              "AppleWebKit/533.3 (KHTML, like Gecko) "
                              "MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                "Authorization": f"Bearer {token}",
            }

            self.update_progress.emit(0)

            # Fetch profile and account info
            try:
                profile_url = f"{url}/portal.php?type=stb&action=get_profile&JsHttpRequest=1-xml"
                response_profile = session.get(profile_url, cookies=cookies, headers=headers, timeout=10)
                response_profile.raise_for_status()
                profile_data = response_profile.json()
            except Exception as e:
                profile_data = {}

            try:
                account_info_url = f"{url}/portal.php?type=account_info&action=get_main_info&JsHttpRequest=1-xml"
                response_account_info = session.get(account_info_url, cookies=cookies, headers=headers, timeout=10)
                response_account_info.raise_for_status()
                account_info_data = response_account_info.json()
            except Exception as e:
                account_info_data = {}

            if self.category_type and self.category_id:
                logging.debug("Fetching channels.")
                channels = self.get_channels(
                    session, url, mac_address, token, self.category_type,
                    self.category_id, self.num_threads, cookies, headers,
                )
                self.update_progress.emit(100)
                self.channels_loaded.emit(channels)
            else:
                # Fetch playlist (Live, Movies, Series) in parallel
                data = {}
                total_categories = 3
                completed_categories = 0

                fetch_methods = [
                    (self.get_genres, "Live"),
                    (self.get_vod_categories, "Movies"),
                    (self.get_series_categories, "Series"),
                ]

                with concurrent.futures.ThreadPoolExecutor(max_workers=self.num_threads) as executor:
                    future_to_tab = {
                        executor.submit(method, session, url, mac_address, token, cookies, headers): tab_name
                        for method, tab_name in fetch_methods
                    }
                    for future in concurrent.futures.as_completed(future_to_tab):
                        tab_name = future_to_tab[future]
                        try:
                            result = future.result()
                        except Exception as e:
                            logging.error(f"Error fetching {tab_name} categories: {e}")
                            result = []
                        data[tab_name] = result
                        completed_categories += 1
                        progress_percent = int((completed_categories / total_categories) * 100)
                        self.update_progress.emit(progress_percent)
                        logging.debug(f"Progress: {progress_percent}%")

                # Emit the complete data and account info ONCE
                self.request_complete.emit(data, account_info_data, profile_data)

        except Exception as e:
            logging.error(f"Request thread error: {str(e)}")
            traceback.print_exc()
            self.request_complete.emit({}, {})
            self.update_progress.emit(0)



    def get_genres(self, session, url, mac_address, token, cookies, headers):
        try:
            genres_url = f"{url}/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
            response = session.get(genres_url, cookies=cookies, headers=headers, timeout=10)
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
                genres.sort(key=lambda x: x["name"])
                logging.debug(f"Genres fetched: {genres}")
                return genres
            else:
                logging.warning("No genres data found.")
                return []
        except Exception as e:
            logging.error(f"Error getting genres: {e}")
            return []

    def get_vod_categories(self, session, url, mac_address, token, cookies, headers):
        try:
            vod_url = f"{url}/portal.php?type=vod&action=get_categories&JsHttpRequest=1-xml"
            response = session.get(vod_url, cookies=cookies, headers=headers, timeout=10)
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
                categories.sort(key=lambda x: x["name"])
                logging.debug(f"VOD categories fetched: {categories}")
                return categories
            else:
                logging.warning("No VOD categories data found.")
                return []
        except Exception as e:
            logging.error(f"Error getting VOD categories: {e}")
            return []

    def get_series_categories(self, session, url, mac_address, token, cookies, headers):
        try:
            series_url = f"{url}/portal.php?type=series&action=get_categories&JsHttpRequest=1-xml"
            response = session.get(series_url, cookies=cookies, headers=headers, timeout=10)
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
            initial_url = ""
            if category_type == "IPTV":
                initial_url = f"{url}/portal.php?type=itv&action=get_ordered_list&genre={category_id}&JsHttpRequest=1-xml&p=0"
            elif category_type == "VOD":
                initial_url = f"{url}/portal.php?type=vod&action=get_ordered_list&category={category_id}&JsHttpRequest=1-xml&p=0"
            elif category_type == "Series":
                initial_url = f"{url}/portal.php?type=series&action=get_ordered_list&category={category_id}&p=0&JsHttpRequest=1-xml"

            response = session.get(initial_url, cookies=cookies, headers=headers, timeout=10)
            response.raise_for_status()
            response_json = response.json()
            total_items = response_json.get("js", {}).get("total_items", 0)
            items_per_page = len(response_json.get("js", {}).get("data", []))
            total_pages = (total_items + items_per_page - 1) // items_per_page if items_per_page else 1

            channels_data = response_json.get("js", {}).get("data", [])
            for c in channels_data:
                c["item_type"] = ("series" if category_type == "Series"
                                  else "vod" if category_type == "VOD"
                                  else "channel")
            channels.extend(channels_data)
            self.update_progress.emit(int((1 / max(total_pages, 1)) * 100))  # Initial progress

            # Fetch remaining pages in parallel
            pages_to_fetch = list(range(1, total_pages))
            if pages_to_fetch:
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.num_threads) as executor:
                    future_to_page = {}
                    for p in pages_to_fetch:
                        if category_type == "IPTV":
                            channels_url = f"{url}/portal.php?type=itv&action=get_ordered_list&genre={category_id}&JsHttpRequest=1-xml&p={p}"
                        elif category_type == "VOD":
                            channels_url = f"{url}/portal.php?type=vod&action=get_ordered_list&category={category_id}&JsHttpRequest=1-xml&p={p}"
                        elif category_type == "Series":
                            channels_url = f"{url}/portal.php?type=series&action=get_ordered_list&category={category_id}&p={p}&JsHttpRequest=1-xml"
                        else:
                            logging.error(f"Unknown category_type: {category_type}")
                            continue
                        logging.debug(f"Fetching page {p} URL: {channels_url}")
                        future = executor.submit(self.fetch_channel_page, session, channels_url, cookies, headers, category_type)
                        future_to_page[future] = p

                    for future in concurrent.futures.as_completed(future_to_page):
                        p = future_to_page[future]
                        try:
                            page_channels = future.result()
                            channels.extend(page_channels)
                            logging.debug(f"Fetched page {p} with {len(page_channels)} channels.")
                        except Exception as e:
                            logging.error(f"Error fetching page {p}: {e}")
                            continue
                        # Emit progress after each page
                        progress_percent = int(((p + 1) / total_pages) * 100)
                        self.update_progress.emit(progress_percent)
                        logging.debug(f"Progress: {progress_percent}%")

            # Remove duplicate channels based on 'id'
            unique_channels = {}
            for ch in channels:
                cid = ch.get('id')
                if cid and cid not in unique_channels:
                    unique_channels[cid] = ch
            channels = list(unique_channels.values())
            channels.sort(key=lambda x: x.get("name", ""))
            logging.debug(f"Total channels fetched: {len(channels)}")
            return channels
        except Exception as e:
            logging.error(f"An error occurred while retrieving channels: {str(e)}")
            return []

    def fetch_channel_page(self, session, channels_url, cookies, headers, category_type):
        response = session.get(channels_url, cookies=cookies, headers=headers, timeout=10)
        response.raise_for_status()
        response_json = response.json()
        page_channels = response_json.get("js", {}).get("data", [])
        for ch in page_channels:
            ch["item_type"] = ("series" if category_type == "Series"
                               else "vod" if category_type == "VOD"
                               else "channel")
        return page_channels


class StalkerRequestThread(QThread):
    stalker_request_complete = pyqtSignal(dict)
    stalker_update_progress = pyqtSignal(int)
    stalker_error = pyqtSignal(str)

    def __init__(self, portal: StalkerPortal):
        super().__init__()
        self.portal = portal

    def run(self):
        try:
            logging.debug("StalkerRequestThread started.")
            self.stalker_update_progress.emit(0)  # Reset progress at the start

            # Perform handshake and get profile
            self.portal.handshake()
            self.stalker_update_progress.emit(10)
            logging.debug("Handshake completed.")

            self.portal.get_profile()
            self.stalker_update_progress.emit(20)
            logging.debug("Profile fetched successfully.")

            # Fetch categories with incremental progress updates
            categories = {}
            total_steps = 3  # Number of category types
            current_step = 0

            try:
                logging.debug("Fetching Live categories...")
                categories["Live"] = self.portal.get_itv_categories()
                current_step += 1
                progress = 20 + int((current_step / total_steps) * 80)  # Between 20% and 100%
                self.stalker_update_progress.emit(progress)
                logging.debug(f"Fetched Live categories. Progress: {progress}%")
            except Exception as e:
                logging.error(f"Error fetching Live categories: {e}")

            try:
                logging.debug("Fetching Movies categories...")
                categories["Movies"] = self.portal.get_vod_categories()
                current_step += 1
                progress = 20 + int((current_step / total_steps) * 80)  # Between 20% and 100%
                self.stalker_update_progress.emit(progress)
                logging.debug(f"Fetched Movies categories. Progress: {progress}%")
            except Exception as e:
                logging.error(f"Error fetching Movies categories: {e}")

            try:
                logging.debug("Fetching Series categories...")
                categories["Series"] = self.portal.get_series_categories()
                current_step += 1
                progress = 20+ int((current_step / total_steps) * 80)  # Between 20% and 100%
                self.stalker_update_progress.emit(progress)
                logging.debug(f"Fetched Series categories. Progress: {progress}%")
            except Exception as e:
                logging.error(f"Error fetching Series categories: {e}")

            # Emit the fetched categories
            self.stalker_request_complete.emit(categories)
            self.stalker_update_progress.emit(100)  # Complete progress
            logging.debug("All categories fetched successfully.")

        except Exception as e:
            logging.error(f"StalkerRequestThread encountered an error: {e}")
            self.stalker_error.emit(str(e))
            self.stalker_update_progress.emit(0)  # Reset progress on error


class ProfileDialog(QDialog):
    profile_selected = pyqtSignal(dict)
    profiles_updated = pyqtSignal(list)

    def __init__(self, profiles, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Profile Manager")
        self.setFixedSize(300, 400)
        self.profiles = profiles

        layout = QVBoxLayout(self)
        self.profile_list = QListWidget()
        layout.addWidget(self.profile_list)
        self.load_profile_list()

        buttons_layout = QHBoxLayout()
        layout.addLayout(buttons_layout)

        add_button = QPushButton("Add")
        edit_button = QPushButton("Edit")
        delete_button = QPushButton("Delete")
        buttons_layout.addWidget(add_button)
        buttons_layout.addWidget(edit_button)
        buttons_layout.addWidget(delete_button)

        add_button.clicked.connect(self.add_profile)
        edit_button.clicked.connect(self.edit_profile)
        delete_button.clicked.connect(self.delete_profile)
        self.profile_list.itemDoubleClicked.connect(self.on_item_double_clicked)

        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(
            "QProgressBar {text-align: center; color: white;} QProgressBar::chunk {background-color: cyan;}"
        )
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)  # Initially hidden
        layout.addWidget(self.progress_bar)

    def load_profile_list(self):
        self.profile_list.clear()
        for profile in self.profiles:
            item = QListWidgetItem(profile["name"])
            item.setData(Qt.UserRole, profile)
            self.profile_list.addItem(item)

    def add_profile(self):
        name, ok = QInputDialog.getText(self, "Add Profile", "Enter profile name:")
        if ok and name:
            url, ok_url = QInputDialog.getText(self, "Add Profile", "Enter URL:")
            if ok_url and url:
                mac, ok_mac = QInputDialog.getText(self, "Add Profile", "Enter MAC Address:")
                if ok_mac and mac:
                    profile = {"name": name, "url": url, "mac": mac}
                    self.profiles.append(profile)
                    self.load_profile_list()
                    self.profiles_updated.emit(self.profiles)
                else:
                    QMessageBox.warning(self, "Warning", "Invalid MAC address.")
            else:
                QMessageBox.warning(self, "Warning", "Invalid URL.")
        else:
            QMessageBox.warning(self, "Warning", "Invalid profile name.")

    def edit_profile(self):
        current_item = self.profile_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "Warning", "Select a profile to edit!")
            return
        profile = current_item.data(Qt.UserRole)
        name, ok = QInputDialog.getText(self, "Edit Profile", "Enter profile name:", text=profile["name"])
        if ok and name:
            url, ok_url = QInputDialog.getText(self, "Edit Profile", "Enter URL:", text=profile["url"])
            if ok_url and url:
                mac, ok_mac = QInputDialog.getText(self, "Edit Profile", "Enter MAC Address:", text=profile["mac"])
                if ok_mac and mac:
                    profile["name"] = name
                    profile["url"] = url
                    profile["mac"] = mac
                    current_item.setText(name)
                    current_item.setData(Qt.UserRole, profile)
                    self.profiles_updated.emit(self.profiles)
                else:
                    QMessageBox.warning(self, "Warning", "Invalid MAC address.")
            else:
                QMessageBox.warning(self, "Warning", "Invalid URL.")
        else:
            QMessageBox.warning(self, "Warning", "Invalid profile name.")

    def delete_profile(self):
        current_item = self.profile_list.currentItem()
        if current_item:
            profile = current_item.data(Qt.UserRole)
            reply = QMessageBox.question(
                self,
                "Delete Profile",
                f"Are you sure you want to delete profile '{profile['name']}'?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.profiles.remove(profile)
                self.load_profile_list()
                self.profiles_updated.emit(self.profiles)
        else:
            QMessageBox.warning(self, "Warning", "Select a profile to delete!")

    def on_item_double_clicked(self, item):
        profile = item.data(Qt.UserRole)
        self.profile_selected.emit(profile)
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MAC IPTV Player by MY-1 v4.0")
        self.setGeometry(100, 100, 610, 560)

        self.settings = QSettings("MyCompany", "IPTVPlayer")
        self.profiles = []
        self.load_profiles()

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        top_layout = QVBoxLayout()
        layout.addLayout(top_layout)

        hostname_label = QLabel("Hostname:")
        top_layout.addWidget(hostname_label)

        hostname_layout = QHBoxLayout()
        top_layout.addLayout(hostname_layout)

        self.hostname_input = QLineEdit()
        hostname_layout.addWidget(self.hostname_input)

        self.profile_button = QPushButton()
        self.profile_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogListView))
        self.profile_button.setFixedSize(24, 24)
        hostname_layout.addWidget(self.profile_button)
        self.profile_button.clicked.connect(self.open_profile_dialog)

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

        threads_layout = QHBoxLayout()
        top_layout.addLayout(threads_layout)

        threads_label = QLabel("Threads:")
        threads_layout.addWidget(threads_label)

        self.threads_input = QSpinBox()
        self.threads_input.setMinimum(1)
        self.threads_input.setMaximum(20)
        self.threads_input.setValue(5)
        self.threads_input.setFixedWidth(60)
        threads_layout.addWidget(self.threads_input)
        threads_layout.addStretch()

        self.get_playlist_button = QPushButton("Get Playlist")
        layout.addWidget(self.get_playlist_button)
        self.get_playlist_button.clicked.connect(self.get_playlist)

        # **Add Search Bar Above the Tabs with SP_FileDialogContentsView Icon inside**
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search...")

        # Add the icon inside the QLineEdit to the left
        search_icon = self.style().standardIcon(QStyle.SP_FileDialogContentsView)
        action = self.search_input.addAction(search_icon, QLineEdit.LeadingPosition)

        # Optionally, set a stylesheet for better appearance (adjust padding if necessary)
        # self.search_input.setStyleSheet("""
        #     QLineEdit {
        #         padding-left: 25px;  /* Adjust padding to accommodate the icon */
        #     }
        # """)

        # Add the search input to the main layout
        layout.addWidget(self.search_input)

        # Connect the search input to the search functionality
        self.search_input.textChanged.connect(self.perform_search)

        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        self.tooltip_max_width = 520   # overall tooltip width cap
        self.tooltip_image_w  = 140    # poster width
        self.tooltip_font_px  = 12     # base font size inside the HTML
        self.tooltip_desc_max = 500    # max description chars (prevents HUGE tips)


        self.tabs = {}
        for tab_name in ["Live", "Movies", "Series", "Info"]:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)

            if tab_name == "Info":
                info_label = QLabel("No info loaded yet.")
                info_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                info_label.setWordWrap(True)
                tab_layout.addWidget(info_label)
                self.tab_widget.addTab(tab, tab_name)
                self.tabs[tab_name] = {
                    "tab_widget": tab,
                    "info_label": info_label,
                    "info_data": {},
                }
            else:
                playlist_view = QListView()
                playlist_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
                playlist_view.setMouseTracking(True)  # enable smooth hover tooltips
                tab_layout.addWidget(playlist_view)

                playlist_model = QStandardItemModel(playlist_view)
                playlist_view.setModel(playlist_model)

                # we only handle double-click for play/navigation
                playlist_view.doubleClicked.connect(self.on_playlist_selection_changed)

                # --- Movies: fetch & inject poster into tooltip on hover ---
                if tab_name == "Movies":
                    playlist_view.entered.connect(
                        lambda idx, tn=tab_name: self._maybe_update_movie_tooltip_on_hover(tn, idx)
                    )

                # --- Series: same idea for series/seasons/episodes ---
                if tab_name == "Series":
                    playlist_view.entered.connect(
                        lambda idx, tn=tab_name: self._maybe_update_media_tooltip_on_hover(tn, idx)
                    )

                self.tab_widget.addTab(tab, tab_name)
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

        # ---- create after the loop (only once) ----
        self.epg = None
        self._epg_row_by_key = {}   # key -> QStandardItem in Live tab


        # **Rework Progress Bar to Use QTimer for Smooth Progression**
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(
            "QProgressBar {text-align: center; color: white;} QProgressBar::chunk {background-color: cyan;}"
        )
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)  # Initially hidden
        layout.addWidget(self.progress_bar)

        # Initialize QTimer for progress updates
        self.progress_timer = QTimer()
        self.progress_timer.timeout.connect(self.update_progress_bar)
        self.progress_target = 100  # Target progress value
        self.progress_step = 1  # Increased progress increment step for faster updates

        # Initialize a separate timer for non-Stalker progress
        self.non_stalker_progress_timer = QTimer()
        self.non_stalker_progress_timer.timeout.connect(self.update_non_stalker_progress_bar)
        self.non_stalker_progress_target = 100
        self.non_stalker_progress_step = 1

        self.session = None
        self.token = None
        self.token_timestamp = None
        self.current_request_thread = None
        self.current_stalker_thread = None

        # Add a layout for bottom controls
        bottom_layout = QHBoxLayout()
        layout.addLayout(bottom_layout)

        # Add the "Always on Top" checkbox
        self.always_on_top_checkbox = QCheckBox("Always on Top")
        bottom_layout.addWidget(self.always_on_top_checkbox)
        self.always_on_top_checkbox.stateChanged.connect(self.toggle_always_on_top)

        # Add the "Enable Dark Theme" checkbox
        self.dark_theme_checkbox = QCheckBox("Enable Dark Theme")
        bottom_layout.addWidget(self.dark_theme_checkbox)
        self.dark_theme_checkbox.stateChanged.connect(self.toggle_dark_theme)

        # Align the bottom controls to the right
        bottom_layout.addStretch()

        self._live_epg_prefetch_timer = None  # single timer instance for Live prefetch
        self._epg_generation = 0 

        # Load saved settings
        self.load_settings()
        always_on_top = self.settings.value("always_on_top", False, type=bool)
        self.always_on_top_checkbox.setChecked(always_on_top)
        if always_on_top:
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
            self.show()

        # Load dark theme setting
        is_dark_theme = self.settings.value("dark_theme", False, type=bool)
        self.dark_theme_checkbox.setChecked(is_dark_theme)
        if is_dark_theme:
            self.apply_dark_theme()
        else:
            self.apply_light_theme()

    def _poster_cache_path(self, url: str) -> str:
        if not url:
            return ""
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()
        cache_dir = os.path.join(tempfile.gettempdir(), "maciptv_posters")
        os.makedirs(cache_dir, exist_ok=True)
        ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
        return os.path.join(cache_dir, f"{h}{ext}")

    def _poster_local_file(self, url: str) -> str:
        try:
            if not url:
                return ""
            path = self._poster_cache_path(url)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                sess = self.session or requests.Session()
                r = sess.get(url, timeout=8)
                r.raise_for_status()
                with open(path, "wb") as f:
                    f.write(r.content)
            return path
        except Exception as e:
            logging.debug(f"Poster download/cache failed: {e}")
            return ""

    def _file_url(self, path: str) -> str:
        # Correctly formats file:/// URLs on Windows/mac/Linux
        return QUrl.fromLocalFile(path).toString() if path else ""


    def _absolute_url(self, maybe_url: str) -> str:
        """
        Turn relative paths like '/stalker_portal/...' into absolute URLs based on self.base_url.
        If already absolute, return as-is.
        """
        if not maybe_url:
            return ""
        u = str(maybe_url).strip()
        if u.lower().startswith(("http://", "https://")):
            return u
        base = (self.base_url or "").rstrip("/") + "/"
        return urljoin(base, u.lstrip("/"))

    def _best_image_url(self, obj: dict) -> str:
        """
        Try common keys used by VOD/Series/Episodes. Fallback to parent poster if present.
        """
        if not isinstance(obj, dict):
            return ""
        for key in ("screenshot_uri", "pic", "cover", "poster", "logo", "icon", "image"):
            if obj.get(key):
                return self._absolute_url(obj.get(key))
        # parent-propagated poster for seasons/episodes
        if obj.get("_parent_poster"):
            return self._absolute_url(obj["_parent_poster"])
        return ""




    def _format_movie_tooltip(self, m: dict) -> str:
        if not isinstance(m, dict):
            return ""

        title = m.get("name") or m.get("o_name") or "Unknown title"
        year_raw = (m.get("year") or "").strip()
        year = year_raw[:4] if len(year_raw) >= 4 and year_raw[:4].isdigit() else ""
        genres = m.get("genres_str") or ""
        director = m.get("director") or ""
        description = (m.get("description") or "").strip()
        if self.tooltip_desc_max and len(description) > self.tooltip_desc_max:
            description = description[: self.tooltip_desc_max].rstrip() + "…"
        rating = m.get("rating_imdb") or ""
        added = m.get("added") or ""
        duration_min = m.get("time")
        duration_txt = f"{duration_min} min" if isinstance(duration_min, int) and duration_min > 0 else ""
        age = m.get("age") or ""

        actors = (m.get("actors") or "").strip()
        if actors:
            actor_list = [a.strip() for a in actors.split(",") if a.strip()]
            actors = ", ".join(actor_list[:8]) + ("…" if len(actor_list) > 8 else "")

        local_path = m.get("_local_poster_path", "")
        poster_html = (
            f"<img src='{self._file_url(local_path)}' width='{self.tooltip_image_w}'>"
            if local_path else ""
        )

        # Table layout is the most reliable for Qt rich text sizing.
        html = f"""
        <qt>
          <div style="max-width:{self.tooltip_max_width}px; font-size:{self.tooltip_font_px}px; line-height:1.25;">
            <table cellspacing="0" cellpadding="0">
              <tr>
                <td style="vertical-align:top; padding-right:8px;">{poster_html}</td>
                <td style="vertical-align:top;">
                  <div style="font-size:{self.tooltip_font_px + 1}px; font-weight:bold;">
                    {title}{' ('+year+')' if year else ''}
                  </div>
                  <div style="color:#9aa; margin-top:2px;">{genres}</div>
                  <div style="margin-top:6px;">
                    <b>IMDb:</b> {rating or '—'}
                    {' &nbsp;•&nbsp; ' + '<b>Duration:</b> ' + duration_txt if duration_txt else ''}
                    {' &nbsp;•&nbsp; ' + '<b>Age:</b> ' + age if age else ''}
                  </div>
                  {"<div style='margin-top:4px;'><b>Director:</b> " + director + "</div>" if director else ""}
                  {"<div style='margin-top:2px;'><b>Actors:</b> " + actors + "</div>" if actors else ""}
                  {"<div style='margin-top:6px;'>" + description + "</div>" if description else ""}
                  {"<div style='margin-top:6px;color:#9aa;'><i>Added:</i> " + added + "</div>" if added else ""}
                </td>
              </tr>
            </table>
          </div>
        </qt>
        """
        return html



    def _prefetch_live_epg_for_current_list(self, tab_name: str, *, batch_size: int = 25, interval_ms: int = 10):
        """
        Prefetch EPG for the current Live list in small timed batches.
        Older prefetch gets canceled automatically when a new category/list is shown.
        """
        if not self.epg or tab_name != "Live":
            return

        tab_info = self.tabs.get(tab_name) or {}
        model: QStandardItemModel = tab_info.get("playlist_model")
        if not model:
            return

        # gather pending channel items
        pending_items = []
        for row in range(model.rowCount()):
            item = model.item(row)
            if not item:
                continue
            item_type = item.data(ROLE_ITEM_TYPE)
            if item_type != "channel":
                continue
            if item.text().strip().lower() == "go back":
                continue
            key = item.data(ROLE_CHANNEL_KEY)
            epg_text = item.data(ROLE_EPG_TEXT)
            if not epg_text or "Loading EPG" in str(epg_text):
                ch = item.data(Qt.UserRole) or {}
                pending_items.append((key, ch, item))

        if not pending_items:
            return

        # Stop previous timer if any
        if self._live_epg_prefetch_timer:
            try:
                self._live_epg_prefetch_timer.stop()
                self._live_epg_prefetch_timer.deleteLater()
            except Exception:
                pass
            self._live_epg_prefetch_timer = None

        # Capture the generation for this list
        my_gen = self._epg_generation
        queue = pending_items[:]  # shallow copy

        t = QTimer(self)
        t.setInterval(interval_ms)
        self._live_epg_prefetch_timer = t

        def _tick():
            nonlocal queue, my_gen
            # If category/view has changed, abort this prefetch
            if my_gen != self._epg_generation:
                t.stop()
                t.deleteLater()
                if self._live_epg_prefetch_timer is t:
                    self._live_epg_prefetch_timer = None
                return

            # Model vanished or changed? abort safely
            if not model or model is not self.tabs["Live"]["playlist_model"]:
                t.stop()
                t.deleteLater()
                if self._live_epg_prefetch_timer is t:
                    self._live_epg_prefetch_timer = None
                return

            batch = queue[:batch_size]
            queue = queue[batch_size:]

            for key, ch, item in batch:
                try:
                    # refresh loading label if needed
                    if not item.data(ROLE_EPG_TEXT):
                        item.setData("Loading EPG…", ROLE_EPG_TEXT)
                        name = (ch.get("name") or "Unknown")
                        item.setText(f"{name}    — Loading EPG…")
                    # enqueue EPG fetch
                    self.epg.request(ch, size=6)
                except Exception:
                    pass

            if not queue:
                t.stop()
                t.deleteLater()
                if self._live_epg_prefetch_timer is t:
                    self._live_epg_prefetch_timer = None

        t.timeout.connect(_tick)
        t.start()


     

    def _epg_channel_key(self, channel: dict) -> str:
        """
        Mirror EpgManager's key logic so we can map EPG updates to list rows.
        """
        try:
            if isinstance(channel, dict):
                for k in ("ch_id", "id", "cid", "ch", "number", "cmd", "name"):
                    v = channel.get(k)
                    if v is not None and str(v).strip():
                        return str(v)
                return "|".join(f"{k}={channel[k]}" for k in sorted(channel))
            return str(channel)
        except Exception:
            return str(channel)

    def _compact_epg_line(self, items: list, max_items: int = 3) -> str:
        """
        Inline example (title first, 12h):
        'The News 3:00 PM – 3:30 PM • Game Time 3:30 PM – 4:00 PM'
        Robust against missing/unnormalized datetimes.
        """
        if not isinstance(items, list) or not items:
            return "No EPG"

        def _to_dt(val):
            if isinstance(val, datetime):
                return val
            if isinstance(val, (int, float)):
                try:
                    if val > 10_000_000_000:  # looks like ms
                        val = val / 1000.0
                    return datetime.fromtimestamp(val)
                except Exception:
                    return None
            if isinstance(val, str) and val.strip().isdigit():
                try:
                    n = int(val.strip())
                    if n > 10_000_000_000:
                        n = n / 1000.0
                    return datetime.fromtimestamp(n)
                except Exception:
                    return None
            return None

        def _first_dt(d: dict, keys: tuple[str, ...]):
            for k in keys:
                if k in d:
                    dt = _to_dt(d.get(k))
                    if dt:
                        return dt
            return None

        def _fmt12(dt):
            return dt.strftime("%I:%M %p").lstrip("0") if isinstance(dt, datetime) else "??:??"

        parts = []
        for it in items[:max_items]:
            if not isinstance(it, dict):
                continue

            name = (it.get("name") or it.get("title") or it.get("progname") or it.get("program") or "—").strip()

            # Prefer normalized keys; fall back to raw fields
            start_dt = it.get("start_dt")
            end_dt   = it.get("end_dt")
            if not isinstance(start_dt, datetime):
                start_dt = _first_dt(it, ("start_dt", "start", "start_timestamp", "t_time"))
            if not isinstance(end_dt, datetime):
                end_dt = _first_dt(it, ("end_dt", "end", "end_timestamp", "s_time"))

            # Derive end from duration if missing
            if not end_dt and start_dt:
                dur = it.get("duration") or it.get("prog_duration")
                try:
                    dur_i = int(dur) if dur is not None else None
                    if dur_i and 0 < dur_i < 24 * 3600:
                        end_dt = datetime.fromtimestamp(int(start_dt.timestamp()) + dur_i)
                except Exception:
                    pass

            s = _fmt12(start_dt)
            e = _fmt12(end_dt)

            parts.append(name if (s == "??:??" and e == "??:??") else f"{name} {s} – {e}")

        return " • ".join(parts) if parts else "No EPG"


    def toggle_dark_theme(self, state):
        if state == Qt.Checked:
            self.apply_dark_theme()
            self.settings.setValue("dark_theme", True)
        else:
            self.apply_light_theme()
            self.settings.setValue("dark_theme", False)

    def apply_dark_theme(self):
        # Load and customize qdarkstyle's stylesheet
        qss = qdarkstyle.load_stylesheet(qt_api='pyqt5')
        qss = (
            qss
            .replace('#232629', '#1b2332')      # Main background to dark blue-gray
            .replace('#31363b', '#253046')      # Widget background to lighter blue-gray
            .replace('#2a82da', '#00dfff')      # Highlight/selection to cyan
            .replace('#3daee9', '#9befff')      # Lighter highlight/secondary accent (light blue-cyan)
            .replace('#c8c8c8', '#e3f6ff')      # Main text to a softer white-blue
            .replace('#eff0f1', '#e3f6ff')      # Widget text
            .replace('#787878', '#5fd6ff')      # Disabled text to pale cyan
        )
        # You can append custom tweaks here if you want:
        qss += """
        QPushButton {
            background-color: #23395d;
            color: #e3f6ff;
            border: 1px solid #00dfff;
            border-radius: 6px;
            padding: 4px 12px;
        }
        QPushButton:pressed, QPushButton:checked {
            background-color: #00dfff;
            color: #1b2332;
        }
        QComboBox, QLineEdit, QTextEdit, QAbstractItemView, QListWidget, QPlainTextEdit {
            background-color: #253046;
            color: #e3f6ff;
            border: 1px solid #00dfff;
        }
        QComboBox QAbstractItemView {
            background-color: #1b2332;
            color: #00dfff;
            selection-background-color: #00dfff;
            selection-color: #1b2332;
        }
        QCheckBox::indicator:checked {
            border: 2px solid #00dfff;
            background: #253046;
        }
        """


        qss += """
        QToolTip {
            padding: 6px 8px;
            border: 1px solid #00dfff;
            background: #1b2332;
            color: #e3f6ff;
            font-size: 12px;       /* change size here */
        }
        """

        # Now apply the custom stylesheet to your window (or app)
        self.setStyleSheet(qss)


    def apply_light_theme(self):
        # Reset to Qt defaults, then add only the tooltip styling you want
        qss = """
        QToolTip {
            padding: 6px 8px;
            border: 1px solid #888;
            background: #ffffff;
            color: #222;
            font-size: 12px;   /* change size here */
        }
        """
        self.setStyleSheet(qss)


    def get_icon_for_item(self, item_type):
        style = QApplication.style()
        if item_type == "category":
            return style.standardIcon(QStyle.SP_DirIcon)
        elif item_type == "channel":
            return style.standardIcon(QStyle.SP_ComputerIcon)
        elif item_type == "vod":
            return style.standardIcon(QStyle.SP_FileIcon)
        elif item_type == "series":
            return style.standardIcon(QStyle.SP_FileDialogDetailedView)
        elif item_type == "season":
            return style.standardIcon(QStyle.SP_DriveHDIcon)
        elif item_type == "episode":
            return style.standardIcon(QStyle.SP_FileLinkIcon)
        elif item_type == "Go Back":
            return style.standardIcon(QStyle.SP_ArrowBack)
        else:
            return QIcon()

    def handle_stalker_progress(self, progress: int):
        logging.debug(f"Progress update received: {progress}%")
        if progress >= 0:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(progress)
            self.progress_bar.repaint()

    def handle_non_stalker_progress(self, progress: int) -> None:
        """
        Handler for updating the progress bar for non-Stalker-related operations.

        - Sets the target progress value.
        - Starts the separate QTimer if not already running.
        - Ensures the progress bar only moves forward.
        """
        if progress > self.non_stalker_progress_target:
            self.non_stalker_progress_target = progress
            if not self.progress_bar.isVisible():
                self.progress_bar.setVisible(True)
            if not self.non_stalker_progress_timer.isActive():
                self.non_stalker_progress_timer.start(5)  # Update every 5 ms
        elif progress == 0:
            self.non_stalker_progress_target = 0
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(False)
            self.non_stalker_progress_timer.stop()

    def update_progress_bar(self):
        """
        Slot connected to QTimer to incrementally update the progress bar towards the target for Stalker.
        """
        if self.progress_bar.value() < self.progress_target:
            # Increase progress bar value faster by using a larger step
            self.progress_bar.setValue(self.progress_bar.value() + self.progress_step)
            # Ensure it does not exceed the target
            if self.progress_bar.value() > self.progress_target:
                self.progress_bar.setValue(self.progress_target)
        else:
            self.progress_timer.stop()
            if self.progress_bar.value() >= 100:
                # Ensure it's set to 100
                self.progress_bar.setValue(100)
                # Hide the progress bar after reaching 100%
                QTimer.singleShot(500, lambda: self.progress_bar.setVisible(False))
                # Optionally, perform additional actions here if needed
                logging.debug("Progress bar reached 100% (Stalker).")

    def update_non_stalker_progress_bar(self):
        """
        Slot connected to QTimer to incrementally update the progress bar towards the target for non-Stalker.
        """
        if self.progress_bar.value() < self.non_stalker_progress_target:
            # Increase progress bar value faster by using a larger step
            self.progress_bar.setValue(self.progress_bar.value() + self.non_stalker_progress_step)
            # Ensure it does not exceed the target
            if self.progress_bar.value() > self.non_stalker_progress_target:
                self.progress_bar.setValue(self.non_stalker_progress_target)
        else:
            self.non_stalker_progress_timer.stop()
            if self.progress_bar.value() >= 100:
                # Ensure it's set to 100
                self.progress_bar.setValue(100)
                # Hide the progress bar after reaching 100%
                QTimer.singleShot(500, lambda: self.progress_bar.setVisible(False))
                # Optionally, perform additional actions here if needed
                logging.debug("Progress bar reached 100% (Non-Stalker).")



    def _absolute_url(self, maybe_url: str) -> str:
        """
        Turn relative paths like '/stalker_portal/...' into absolute URLs based on self.base_url.
        If it's already absolute, return as-is.
        """
        if not maybe_url:
            return ""
        u = str(maybe_url).strip()
        if u.lower().startswith(("http://", "https://")):
            return u
        # ensure trailing slash so urljoin behaves well
        base = (self.base_url or "").rstrip("/") + "/"
        return urljoin(base, u.lstrip("/"))


    def load_profiles(self):
        self.profiles = self.settings.value("profiles", [])
        if not isinstance(self.profiles, list):
            self.profiles = []

    def save_profiles(self):
        self.settings.setValue("profiles", self.profiles)

    def open_profile_dialog(self):
        dialog = ProfileDialog(self.profiles, self)
        dialog.profile_selected.connect(self.populate_profile_fields)
        dialog.profiles_updated.connect(self.update_profiles)
        dialog.exec_()

    def populate_profile_fields(self, profile):
        self.hostname_input.setText(profile["url"])
        self.mac_input.setText(profile["mac"])

    def update_profiles(self, profiles):
        self.profiles = profiles
        self.save_profiles()

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
        self.save_profiles()

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
        # **Always Reset Progress to 0 at the Start of Playlist Fetch**
        hostname_input = self.hostname_input.text().strip()
        mac_address = self.mac_input.text().strip()
        media_player = self.media_player_input.text().strip()
        num_threads = self.threads_input.value()

        if not hostname_input or not mac_address or not media_player:
            QMessageBox.warning(self, "Warning", "Please enter Hostname, MAC, and Media Player.")
            logging.warning("User attempted get playlist without full info.")
            return

        self.tabs["Info"]["info_label"].setText("Loading info...")
        self.tabs["Info"]["info_data"] = {}

        parsed_url = urlparse(hostname_input)
        if not parsed_url.scheme and not parsed_url.netloc:
            parsed_url = urlparse(f"http://{hostname_input}")
        elif not parsed_url.scheme:
            parsed_url = parsed_url._replace(scheme="http")

        self.base_url = urlunparse((parsed_url.scheme, parsed_url.netloc, "", "", "", ""))
        self.mac_address = mac_address

        self.portal = None

        if "/stalker_portal/" in hostname_input:
            # Stalker portal logic
            try:
                # ensure a session exists (EPG manager needs one)
                if self.session is None:
                    self.session = requests.Session()

                self.portal = StalkerPortal(
                    portal_url=self.base_url,
                    mac=self.mac_address,
                    progress_callback=self.handle_stalker_progress
                )

                # Build or refresh the EPG manager (Stalker)
                if self.epg is None:
                    self.epg = EpgManager(
                        mode="stalker",              # <-- this is the switch
                        base_url=self.base_url,      # e.g. http://new.gprod.co   (no path is fine)
                        session=self.session,
                        mac=self.mac_address,
                        token_provider=self.make_epg_token_provider(),   # <<< change here
                        cache_ttl=60.0,
                        max_items_default=6
                    )
                    self.epg.epg_ready.connect(self.on_epg_ready)
                else:
                    self.epg.reconfigure(
                        base_url=self.base_url,
                        session=self.session,
                        mac=self.mac_address,
                        token_provider=self.make_epg_token_provider()    # <<< and here

                    )

                # Initialize and start the StalkerRequestThread
                self.stalker_thread = StalkerRequestThread(self.portal)
                self.stalker_thread.stalker_request_complete.connect(self.on_stalker_playlist_received)
                self.stalker_thread.stalker_update_progress.connect(self.handle_stalker_progress)
                self.stalker_thread.stalker_error.connect(self.on_stalker_error)
                self.stalker_thread.start()
                self.current_stalker_thread = self.stalker_thread
                logging.debug("Started StalkerRequestThread for playlist.")
            except Exception as e:
                logging.error(f"Error initializing StalkerPortal: {e}")
                self.show_error_message(f"Error initializing StalkerPortal: {e}")
                self.handle_stalker_progress(0)  # Reset progress on error
        else:
            # Non-stalker logic
            self.session = requests.Session()
            self.token = get_token(self.session, self.base_url, self.mac_address)
            self.token_timestamp = time.time()

            if not self.token:
                QMessageBox.critical(self, "Error", "Failed to retrieve token. Check MAC/URL.")
                return

            # Build or refresh the EPG manager (Generic)
            if self.epg is None:
                self.epg = EpgManager(
                    mode="generic",
                    base_url=self.base_url,
                    session=self.session,
                    mac=self.mac_address,
                    token_provider=self._epg_token_provider,
                    cache_ttl=60.0,
                    max_items_default=6
                )
                self.epg.epg_ready.connect(self.on_epg_ready)
            else:
                self.epg.reconfigure(
                    base_url=self.base_url,
                    session=self.session,
                    mac=self.mac_address,
                    token_provider=self._epg_token_provider
                )

            if (self.current_request_thread is not None and self.current_request_thread.isRunning()):
                QMessageBox.warning(self, "Warning", "A playlist request is already in progress.")
                return

            self.handle_non_stalker_progress(0)

            self.request_thread = RequestThread(
                self.base_url, mac_address, self.session, self.token, num_threads=num_threads,
            )
            self.request_thread.request_complete.connect(self.on_initial_playlist_received)
            self.request_thread.update_progress.connect(self.handle_non_stalker_progress)
            self.request_thread.start()
            self.current_request_thread = self.request_thread
            logging.debug("Started RequestThread for playlist (non-stalker).")


    def on_stalker_playlist_received(self, categories):
        if self.current_stalker_thread != self.sender():
            logging.debug("Received data from old Stalker thread. Ignoring.")
            return

        if not categories:
            self.show_error_message("Failed to retrieve playlist data from Stalker portal. Check connection.")
            logging.error("Stalker playlist data empty.")
            self.handle_stalker_progress(0)  # Reset progress on error
            self.current_stalker_thread = None
            return

        # --- SAFELY FETCH PROFILE INFO ---
        profile_info = None
        try:
            # Try get_profile() first (may update self.portal.profile as a side effect)
            if hasattr(self.portal, 'get_profile'):
                result = self.portal.get_profile()
                # Use return value if dict, else fallback to self.portal.profile
                if isinstance(result, dict) and result:
                    profile_info = result
                elif hasattr(self.portal, "profile") and isinstance(self.portal.profile, dict):
                    profile_info = self.portal.profile
            elif hasattr(self.portal, "profile") and isinstance(self.portal.profile, dict):
                profile_info = self.portal.profile
            else:
                profile_info = {}
        except Exception as e:
            logging.warning(f"Failed to fetch Stalker profile info: {e}")
            profile_info = {}

        # Make sure it's always a dict
        if not isinstance(profile_info, dict):
            profile_info = {}

        # Wrap as {"js": ...} for update_info_tab compatibility
        self.update_info_tab({"js": profile_info})

        # --- LOAD CATEGORY DATA ---
        for tab_name, tab_data in categories.items():
            tab_info = self.tabs.get(tab_name)
            if not tab_info:
                self.show_error_message(f"Unknown tab: {tab_name}")
                logging.warning(f"Unknown tab: {tab_name}")
                continue
            tab_info["playlist_data"] = tab_data
            tab_info["current_category"] = None
            tab_info["navigation_stack"] = []
            self.update_playlist_view(tab_name)

        logging.debug("Stalker playlist data loaded into tabs.")
        self.handle_stalker_progress(100)  # Finalize progress
        self.current_stalker_thread = None




    def on_stalker_error(self, error_message):
        self.show_error_message(f"Error using StalkerPortal: {error_message}")
        logging.error(f"Stalker portal error: {error_message}")
        self.handle_stalker_progress(0)  # Reset progress on error
        self.current_stalker_thread = None

    def on_initial_playlist_received(self, data, account_info_data, profile_data):
        if self.current_request_thread != self.sender():
            logging.debug("Received data from old thread. Ignoring.")
            return

        if not data:
            self.show_error_message("Failed to retrieve playlist data. Check connection.")
            logging.error("Playlist data empty.")
            self.handle_non_stalker_progress(0)  # Reset progress on error
            self.current_request_thread = None
            return

        for tab_name, tab_data in data.items():
            tab_info = self.tabs.get(tab_name)
            if not tab_info:
                self.show_error_message(f"Unknown tab: {tab_name}")
                logging.warning(f"Unknown tab: {tab_name}")
                continue
            tab_info["playlist_data"] = tab_data
            tab_info["current_category"] = None
            tab_info["navigation_stack"] = []
            self.update_playlist_view(tab_name)

        logging.debug("Playlist data loaded into tabs.")
        self.handle_non_stalker_progress(100)
        self.current_request_thread = None

        # NEW: Pass both account_info_data and profile_data to update_info_tab
        self.update_info_tab(account_info_data, profile_data)



    def update_info_tab(self, account_info, profile_info=None):
        info_label = self.tabs["Info"]["info_label"]

        js_account = account_info.get("js", {}) if isinstance(account_info, dict) else {}
        js_profile = profile_info.get("js", {}) if isinstance(profile_info, dict) else {}

        # Helper to check for real/non-junk values
        def is_real(val):
            return bool(val and str(val).strip() not in {"", "0", "000000000", "none", "None", "0000-00-00 00:00:00"})

        # MAC address
        mac = (
            js_account.get("mac")
            or js_account.get("device_mac")
            or js_profile.get("mac_address")
            or js_profile.get("device_mac")
            or "N/A"
        )

        # Parental password, prefer profile for non-stalker
        parent_password = (
            js_profile.get("parent_password")
            or js_account.get("parent_password")
            or "N/A"
        )

        # Expiry (avoid junk)
        def get_expiry(*fields):
            for f in fields:
                if is_real(f):
                    return f
            return "N/A"

        expire = get_expiry(
            js_account.get("expire_date"),
            js_account.get("exp_date"),
            js_account.get("phone"),
            js_profile.get("expire_date"),
            js_profile.get("exp_date"),
            js_profile.get("phone")
        )

        # --- Stalker fields (show only if real) ---
        fname = js_account.get("fname") or js_profile.get("fname")
        expire_billing_date = js_account.get("expire_billing_date") or js_profile.get("expire_billing_date")
        if not is_real(expire_billing_date):
            expire_billing_date = None
        storages = js_account.get("storages") or js_profile.get("storages")
        max_online_val = None
        if storages and isinstance(storages, dict):
            nums = []
            for storage in storages.values():
                v = storage.get("max_online")
                try:
                    if is_real(v):
                        nums.append(int(str(v)))
                except Exception:
                    pass
            if nums:
                max_online_val = max(nums)
        if max_online_val is None:
            v = js_account.get("max_online") or js_profile.get("max_online")
            try:
                if is_real(v):
                    max_online_val = int(str(v))
            except Exception:
                pass

        # Only show stalker layout if *real* fields are present
        stalker_present = any([
            is_real(fname),
            is_real(expire_billing_date),
            max_online_val is not None
        ])

        if stalker_present:
            info_text = f"""
            <b>Name:</b> {fname or 'N/A'}<br><br>
            <b>Mac:</b> {mac or 'N/A'}<br><br>
            <b>Max Online:</b> {max_online_val if max_online_val is not None else 'N/A'}<br><br>
            <b>Parental Password:</b> {parent_password or 'N/A'}<br><br>
            <b>Expire Date:</b> {expire_billing_date or expire or 'N/A'}
            """
        else:
            info_text = f"""
            <b>Mac:</b> {mac}<br><br>
            <b>Parental Password:</b> {parent_password}<br><br>
            <b>Expire date:</b> {expire}
            """

        info_label.setText(info_text)
        self.tabs["Info"]["info_data"] = {**js_account, **js_profile}



    def _epg_token_provider(self):
        """
        Called by EpgManager to get a fresh token/headers/cookies.
        """
        # Refresh token if needed
        if not self.is_token_valid():
            self.token = get_token(self.session, self.base_url, self.mac_address)
            self.token_timestamp = time.time()

        token = self.token or ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        cookies = {"token": token} if token else {}
        return token, headers, cookies








    def update_playlist_view(self, tab_name, scroll_position=0):
        tab_info = self.tabs[tab_name]
        playlist_model = tab_info["playlist_model"]
        playlist_view = tab_info["playlist_view"]

        playlist_model.clear()
        tab_info["current_view"] = "categories"

        if tab_info["navigation_stack"]:
            go_back_item = QStandardItem("Go Back")
            go_back_item.setIcon(self.get_icon_for_item("Go Back"))
            playlist_model.appendRow(go_back_item)

        if tab_info["current_category"] is None:
            for item in tab_info["playlist_data"]:
                name = item["name"]
                list_item = QStandardItem(name)
                list_item.setData(item, Qt.UserRole)
                list_item.setData("category", Qt.UserRole + 1)
                list_item.setIcon(self.get_icon_for_item("category"))
                playlist_model.appendRow(list_item)
            QTimer.singleShot(0, lambda: playlist_view.verticalScrollBar().setValue(scroll_position))
        else:
            self.retrieve_channels(tab_name, tab_info["current_category"], scroll_position)

    def perform_search(self, text):
        """
        Filters the items in the current tab based on the search text.
        """
        # Get current tab
        current_tab = self.tab_widget.currentIndex()
        tab_name = self.tab_widget.tabText(current_tab)
        tab_info = self.tabs.get(tab_name)

        if not tab_info:
            logging.error(f"No tab info found for tab '{tab_name}'")
            return

        # Determine current view
        current_view = tab_info.get("current_view", "categories")

        # Get the full data based on current view
        if current_view == "categories":
            data = tab_info.get("playlist_data", [])
            filter_key = "name"
        elif current_view == "channels":
            data = tab_info.get("current_channels", [])
            filter_key = "name"
        elif current_view in ["seasons", "episodes"]:
            data = tab_info.get("current_series_info", [])
            filter_key = "name"
        else:
            data = []
            filter_key = "name"

        # Filter the data based on search text
        if text:
            filtered_data = [item for item in data if text.lower() in item.get(filter_key, "").lower()]
        else:
            filtered_data = data

        # Update the view with filtered data
        self.update_view_with_search(tab_name, filtered_data)

    def update_view_with_search(self, tab_name, filtered_data):
        """
        Updates the QListView of the specified tab with the filtered data.
        For Live tab, we also wire EPG rows and prefetch EPG for everything in view.
        """
        tab_info = self.tabs.get(tab_name)
        if not tab_info:
            logging.error(f"No tab info found for tab '{tab_name}'")
            return

        playlist_model = tab_info["playlist_model"]
        playlist_view = tab_info["playlist_view"]

        playlist_model.clear()

        # Add "Go Back" if navigation_stack not empty
        if tab_info["navigation_stack"]:
            go_back_item = QStandardItem("Go Back")
            go_back_item.setIcon(self.get_icon_for_item("Go Back"))
            playlist_model.appendRow(go_back_item)

        # Add filtered items
        for obj in filtered_data:
            name = obj.get("name") or obj.get("title", "Unknown")
            list_item = QStandardItem(name)
            list_item.setData(obj, Qt.UserRole)
            item_type = obj.get("item_type", "category")
            list_item.setData(item_type, ROLE_ITEM_TYPE)
            list_item.setIcon(self.get_icon_for_item(item_type))

            # Movies tab (VOD) -> tooltip from JSON
            if tab_name == "Movies" and item_type == "vod":
                list_item.setToolTip(self._format_movie_tooltip(obj))

            # Live tab: inline EPG handling
            if tab_name == "Live" and item_type == "channel":
                key = self._epg_channel_key(obj)
                self._epg_row_by_key[key] = list_item
                existing = list_item.data(ROLE_EPG_TEXT) or "Loading EPG…"
                list_item.setData(key, ROLE_CHANNEL_KEY)
                list_item.setData(existing, ROLE_EPG_TEXT)
                list_item.setText(f"{name}    — {existing}")

            playlist_model.appendRow(list_item)

        # Prefetch EPG for visible Live channels (batching for smoothness)
        if tab_name == "Live":
            self._prefetch_live_epg_for_current_list(tab_name)



    def closeEvent(self, event):
        self.save_settings()
        try:
            if self.epg:
                self.epg.stop()
        except Exception:
            pass
        event.accept()




    def retrieve_channels(self, tab_name, category, scroll_position=0):
        tab_info = self.tabs[tab_name]
        category_type = category["category_type"]
        category_id = category.get("category_id")

        # **Always Reset Progress to 0 Before Starting Channel Fetch**
        # Decide which progress handler to reset based on the portal type
        hostname_input = self.hostname_input.text().strip()
        if "/stalker_portal/" in hostname_input and self.portal:
            # StalkerPortal logic
            self.handle_stalker_progress(0)
        else:
            # Non-Stalker logic
            self.handle_non_stalker_progress(0)

        # Decide logic based on portal type:
        if "/stalker_portal/" in hostname_input and self.portal:
            # StalkerPortal logic
            channels = []
            try:
                if category_type == "IPTV":
                    channels = self.portal.get_channels_in_category(category_id)
                elif category_type == "VOD":
                    channels = self.portal.get_vod_in_category(category_id)
                elif category_type == "Series":
                    channels = self.portal.get_series_in_category(category_id)
                else:
                    logging.error(f"Unknown category_type: {category_type}")
            except Exception as e:
                logging.error(f"Error retrieving channels from StalkerPortal: {e}")
                self.show_error_message(f"Error retrieving channels: {e}")
                # Reset progress
                if category_type in ["IPTV", "VOD", "Series"]:
                    self.handle_stalker_progress(0)
                return

            tab_info["current_channels"] = channels
            self.update_channel_view(tab_name, scroll_position)
            self.handle_stalker_progress(100)  # Finalize progress after channel retrieval
        else:
            # Non-Stalker logic: Use RequestThread for channels
            if (self.current_request_thread is not None and self.current_request_thread.isRunning()):
                QMessageBox.warning(self, "Warning", "A channel request is already in progress.")
                return

            # Check token validity (10 mins)
            if not (self.token and (time.time() - self.token_timestamp) < 600):
                self.token = get_token(self.session, self.base_url, self.mac_address)
                self.token_timestamp = time.time()
                if not self.token:
                    QMessageBox.critical(self, "Error", "Failed to retrieve token. Check MAC/URL.")
                    return

            num_threads = self.threads_input.value()
            self.request_thread = RequestThread(
                self.base_url, self.mac_address, self.session, self.token, category_type, category_id, num_threads=num_threads
            )
            self.request_thread.update_progress.connect(self.handle_non_stalker_progress)
            self.request_thread.channels_loaded.connect(
                lambda channels: self.on_channels_loaded(tab_name, channels)
            )
            self.request_thread.start()
            self.current_request_thread = self.request_thread
            logging.debug(f"Started RequestThread for channels in category {category_id} (non-stalker).")



 


    def on_channels_loaded(self, tab_name, channels):
        if self.current_request_thread != self.sender():
            logging.debug("Received channels from old thread. Ignoring.")
            return
        tab_info = self.tabs[tab_name]
        tab_info["current_channels"] = channels
        self.update_channel_view(tab_name)
        logging.debug(f"Channels loaded for {tab_name}: {len(channels)}")
        self.handle_non_stalker_progress(100)  # Finalize progress
        self.current_request_thread = None

    def update_channel_view(self, tab_name, scroll_position=0):
        tab_info = self.tabs[tab_name]
        playlist_model = tab_info["playlist_model"]
        playlist_view = tab_info["playlist_view"]

        # If we’re about to rebuild Live, stop any ongoing prefetch and clear pending EPG
        if tab_name == "Live":
            self._epg_generation += 1  # new generation for this list
            if self._live_epg_prefetch_timer:
                try:
                    self._live_epg_prefetch_timer.stop()
                    self._live_epg_prefetch_timer.deleteLater()
                except Exception:
                    pass
                self._live_epg_prefetch_timer = None
            if self.epg and hasattr(self.epg, "cancel_pending"):
                try:
                    self.epg.cancel_pending()
                except Exception:
                    pass
            self._epg_row_by_key.clear()

        playlist_model.clear()
        tab_info["current_view"] = "channels"

        if tab_info["navigation_stack"] and playlist_model.rowCount() == 0:
            go_back_item = QStandardItem("Go Back")
            go_back_item.setIcon(self.get_icon_for_item("Go Back"))
            playlist_model.appendRow(go_back_item)

        for channel in tab_info["current_channels"]:
            channel_name = channel.get("name", "Unknown")
            item = QStandardItem(channel_name)
            item.setData(channel, Qt.UserRole)
            item_type = channel.get("item_type", "channel")
            item.setData(item_type, ROLE_ITEM_TYPE)
            item.setIcon(self.get_icon_for_item(item_type))

            # Movies tab (VOD) -> tooltip from JSON on hover
            if tab_name == "Movies" and item_type == "vod":
                item.setToolTip(self._format_movie_tooltip(channel))

            if tab_name == "Live" and item_type == "channel":
                key = self._epg_channel_key(channel)
                item.setData(key, ROLE_CHANNEL_KEY)
                item.setData("Loading EPG…", ROLE_EPG_TEXT)
                self._epg_row_by_key[key] = item
                item.setText(f"{channel_name}    — Loading EPG…")

            playlist_model.appendRow(item)

        # Prefetch EPG for visible Live channels (batched) for THIS generation only
        if tab_name == "Live" and tab_info["current_channels"]:
            self._prefetch_live_epg_for_current_list(tab_name, batch_size=25, interval_ms=10)

        QTimer.singleShot(0, lambda: playlist_view.verticalScrollBar().setValue(scroll_position))


    def on_current_changed(self, tab_name, current, previous):
        """
        Selection change no longer triggers EPG requests.
        Prefetching now happens automatically when the channel list is shown.
        """
        return

 
        return ""

    def on_epg_ready(self, key: str, items: list):
        """
        Safely update the mapped Live list row with a compact one-liner;
        keep the full multiline tooltip for hover.
        """
        item = self._epg_row_by_key.get(key)
        if not item:
            return

        # If the model was rebuilt, the item might be orphaned — guard it
        try:
            model = item.model()
        except Exception:
            return
        live_tab = self.tabs.get("Live")
        if not live_tab or model is not live_tab["playlist_model"]:
            return

        # Build the one-line inline EPG (Title 3:00 PM – 3:30 PM • ...)
        one_liner = self._compact_epg_line(items, max_items=3)

        # Update text + tooltip (defensive in case the row disappears mid-update)
        try:
            ch = item.data(Qt.UserRole) or {}
            name = ch.get("name", "Unknown")
            item.setData(one_liner, ROLE_EPG_TEXT)
            item.setText(f"{name}    — {one_liner}")
            try:
                # Uses your Epg.format_epg_tooltip (already 12h/title-first per your earlier change)
                item.setToolTip(format_epg_tooltip(items))
            except Exception:
                pass
        except Exception:
            return



    def on_playlist_selection_changed(self, index):
        sender = self.sender()
        current_tab = None

        # Determine the current tab based on the sender
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

        if tab_name == "Movies":
            playlist_view.setMouseTracking(True)  # hover events
            playlist_view.entered.connect(lambda idx, tn=tab_name: self._maybe_update_movie_tooltip_on_hover(tn, idx))


        if not index.isValid():
            logging.error("Invalid index selected")
            return

        item = playlist_model.itemFromIndex(index)
        item_data = item.data(Qt.UserRole)
        item_type = item.data(Qt.UserRole + 1)
        item_text = item.text()

        # Handle "Go Back" functionality
        if item_text == "Go Back":

            self._stop_live_epg_prefetch()
            if tab_info["navigation_stack"]:
                nav_state = tab_info["navigation_stack"].pop()
                tab_info["current_category"] = nav_state["category"]
                tab_info["current_view"] = nav_state["view"]
                tab_info["current_series_info"] = nav_state["series_info"]
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
                QMessageBox.information(self, "Info", "No previous view to go back to.")
            return

        # **Always Reset Progress to 0 Before Starting a New Operation**
        # Determine which progress handler to reset based on the portal type
        hostname_input = self.hostname_input.text().strip()
        if "/stalker_portal/" in hostname_input and self.portal:
            self.handle_stalker_progress(0)
        else:
            self.handle_non_stalker_progress(0)

        # Store the current scroll position
        current_scroll_position = playlist_view.verticalScrollBar().value()

        # Handle navigation based on item type
        if item_type == "category":
            tab_info["navigation_stack"].append(
                {
                    "category": tab_info["current_category"],
                    "view": tab_info["current_view"],
                    "series_info": tab_info["current_series_info"],
                    "scroll_position": current_scroll_position,
                }
            )
            tab_info["current_category"] = item_data
            logging.debug(f"Navigating to category: {item_data.get('name')}")
            self.retrieve_channels(current_tab, tab_info["current_category"])
            # **Progress will be handled by the thread**

        elif item_type == "series":
            tab_info["navigation_stack"].append(
                {
                    "category": tab_info["current_category"],
                    "view": tab_info["current_view"],
                    "series_info": tab_info["current_series_info"],
                    "scroll_position": current_scroll_position,
                }
            )
            tab_info["current_category"] = item_data

            hostname_input = self.hostname_input.text().strip()
            if "/stalker_portal/" in hostname_input and self.portal:
                # Use Stalker-specific method
                logging.debug(f"Using Stalker logic for series: {item_data.get('name')}")
                self.stalker_retrieve_series_info(current_tab, item_data)
            else:
                # Use generic method
                logging.debug(f"Using generic logic for series: {item_data.get('name')}")
                self.retrieve_series_info(current_tab, item_data)

            # **Progress will be handled by the thread**

        elif item_type == "season":
            tab_info["navigation_stack"].append(
                {
                    "category": tab_info["current_category"],
                    "view": tab_info["current_view"],
                    "series_info": tab_info["current_series_info"],
                    "scroll_position": current_scroll_position,
                }
            )
            tab_info["current_category"] = item_data
            tab_info["current_view"] = "episodes"

            season_number = item_data.get("season_number")
            if season_number is not None:
                season_number = str(season_number)

            logging.debug(f"Fetching episodes for season: {season_number}")
            self.stalker_retrieve_series_info(current_tab, item_data, season_number=season_number)

            # **Progress will be handled by the thread**

        elif item_type == "episode":
            logging.debug(f"Playing episode: {item_data.get('name')}")
            self.play_channel(item_data)
            # Do not update progress bar during playback
        elif item_type in ["channel", "vod"]:
            logging.debug(f"Playing channel/VOD: {item_data.get('name')}")
            self.play_channel(item_data)
            # Do not update progress bar during playback
        else:
            logging.error("Unknown item type")





    def _maybe_update_movie_tooltip_on_hover(self, tab_name, idx):
        """Only build tooltip for real VOD rows; ignore categories and 'Go Back'."""
        try:
            if tab_name != "Movies" or not idx.isValid():
                return
            tab = self.tabs.get(tab_name)
            if not tab:
                return
            # Do nothing while browsing categories
            if tab.get("current_view") != "channels":
                return

            model = tab["playlist_model"]
            item = model.itemFromIndex(idx)
            if not item:
                return
            if item.text().strip().lower() == "go back":
                return

            item_type = item.data(ROLE_ITEM_TYPE)
            if item_type != "vod":
                return

            m = item.data(Qt.UserRole) or {}
            # Cache poster locally once
            if not m.get("_local_poster_path"):
                img_url = self._best_image_url(m)
                if img_url:
                    local = self._poster_local_file(img_url)
                    if local:
                        m["_local_poster_path"] = local
                        item.setData(m, Qt.UserRole)

            item.setToolTip(self._format_movie_tooltip(m))
        except Exception as e:
            logging.debug(f"hover(Movies) tooltip skipped: {e}")



    def _maybe_update_media_tooltip_on_hover(self, tab_name, idx):
        """Only build tooltip for series/seasons/episodes; ignore categories and 'Go Back'."""
        try:
            if tab_name != "Series" or not idx.isValid():
                return
            tab = self.tabs.get(tab_name)
            if not tab:
                return
            # Valid views for series tooltips
            if tab.get("current_view") not in ("channels", "seasons", "episodes"):
                return

            model = tab["playlist_model"]
            item = model.itemFromIndex(idx)
            if not item:
                return
            if item.text().strip().lower() == "go back":
                return

            item_type = item.data(ROLE_ITEM_TYPE)
            if item_type not in ("series", "season", "episode"):
                return

            obj = item.data(Qt.UserRole) or {}
            # Cache poster locally once (supports relative /stalker_portal/ paths)
            if not obj.get("_local_poster_path"):
                img_url = self._best_image_url(obj)  # tries screenshot_uri/pic/... and _parent_poster
                if img_url:
                    local = self._poster_local_file(img_url)
                    if local:
                        obj["_local_poster_path"] = local
                        item.setData(obj, Qt.UserRole)

            # Reuse movie tooltip builder (title/desc/year look good for shows too)
            item.setToolTip(self._format_movie_tooltip(obj))
        except Exception as e:
            logging.debug(f"hover(Series) tooltip skipped: {e}")





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
                QMessageBox.critical(self, "Error", "Series ID is missing.")
                return

            # NEW: normalize & capture a parent poster once (works for Stalker relative paths too)
            parent_poster = self._absolute_url(
                context_data.get("screenshot_uri") or context_data.get("pic") or ""
            )

            if season_number is None:
                # Fetch seasons
                all_seasons = []
                page_number = 0
                while True:
                    seasons_url = (
                        f"{url}/portal.php?type=series&action=get_ordered_list"
                        f"&movie_id={series_id}&season_id=0&episode_id=0&JsHttpRequest=1-xml&p={page_number}"
                    )
                    logging.debug(
                        f"Fetching seasons URL: {seasons_url}, headers: {headers}, cookies: {cookies}"
                    )
                    response = session.get(seasons_url, cookies=cookies, headers=headers, timeout=10)
                    logging.debug(f"Seasons response: {response.text}")
                    if response.status_code == 200:
                        seasons_data = response.json().get("js", {}).get("data", [])
                        if not seasons_data:
                            break
                        for season in seasons_data:
                            # Ensure season_id is a string
                            season_id_raw = season.get("id", "")
                            season_id = str(season_id_raw)
                            logging.debug(
                                f"Processing season_id: {season_id_raw} (type: {type(season_id_raw)}) converted to string: {season_id}"
                            )
                            season_number_extracted = None
                            if season_id.startswith("season"):
                                match = re.match(r"season(\d+)", season_id)
                                if match:
                                    season_number_extracted = int(match.group(1))
                                else:
                                    logging.error(f"Unexpected season id format: {season_id}")
                            else:
                                match = re.match(r"\d+:(\d+)", season_id)
                                if match:
                                    season_number_extracted = int(match.group(1))
                                else:
                                    logging.error(f"Unexpected season id format: {season_id}")

                            season["season_number"] = season_number_extracted
                            season["item_type"] = "season"

                            # NEW: propagate parent poster so seasons have an image on hover
                            if parent_poster:
                                season["_parent_poster"] = parent_poster

                        all_seasons.extend(seasons_data)
                        total_items = response.json().get("js", {}).get("total_items", len(all_seasons))
                        logging.debug(f"Fetched {len(all_seasons)} seasons out of {total_items}.")
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
                    all_seasons.sort(key=lambda x: x.get("season_number", 0))
                    tab_info["current_series_info"] = all_seasons
                    tab_info["current_view"] = "seasons"
                    self.update_series_view(tab_name)
            else:
                # Fetch episodes for the given season
                series_list = context_data.get("series", [])
                if not series_list:
                    logging.info("No episodes found in this season.")
                    QMessageBox.information(self, "Info", "No episodes found in this season.")
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

                    # NEW: propagate parent poster so episodes have an image on hover
                    if parent_poster:
                        episode["_parent_poster"] = parent_poster

                    logging.debug(f"Episode details: {episode}")
                    all_episodes.append(episode)

                if all_episodes:
                    # Sort episodes by episode_number in ascending order
                    all_episodes.sort(key=lambda x: x.get("episode_number", 0))
                    tab_info["current_series_info"] = all_episodes
                    tab_info["current_view"] = "episodes"
                    self.update_series_view(tab_name)
                else:
                    logging.info("No episodes found.")

        except Exception as e:
            logging.error(f"Error retrieving series info: {str(e)}")


    def stalker_retrieve_series_info(self, tab_name, context_data, season_number=None):
        tab_info = self.tabs[tab_name]
        hostname_input = self.hostname_input.text().strip()

        # Compute a parent poster once (handles Stalker relative paths too)
        parent_poster = self._absolute_url(
            context_data.get("screenshot_uri") or context_data.get("pic") or ""
        )

        if "/stalker_portal/" in hostname_input and self.portal:
            # **Stalker Portal Logic**
            try:
                if tab_info["current_view"] == "episodes":
                    # Fetch episode stream links using StalkerPortal
                    series_id = context_data.get("movie_id") or context_data.get("id")
                    season_id = context_data.get("season_id")
                    if not series_id or not season_id:
                        logging.error(f"Series ID or Season ID missing in context data: {context_data}")
                        QMessageBox.critical(self, "Error", "Series ID or Season ID is missing.")
                        return

                    episodes = self.portal.fetch_episode_pages(movie_id=series_id, season_id=season_id)
                    if episodes:
                        # Process and sort episodes
                        processed_episodes = self.process_and_sort_episodes(episodes)

                        # NEW: propagate parent poster to each episode
                        if parent_poster:
                            for e in processed_episodes:
                                if isinstance(e, dict):
                                    e.setdefault("_parent_poster", parent_poster)
                                    # mark type if missing
                                    e.setdefault("item_type", "episode")

                        tab_info["current_series_info"] = processed_episodes
                        tab_info["current_view"] = "episodes"  # ensure correct view
                        self.update_series_view(tab_name)
                    else:
                        logging.warning("No episodes found for the selected season.")
                        QMessageBox.information(self, "Info", "No episodes found for the selected season.")
                else:
                    # Fetch seasons using StalkerPortal
                    series_id = context_data.get("movie_id") or context_data.get("id")
                    if not series_id:
                        logging.error(f"Series ID missing in context data: {context_data}")
                        QMessageBox.critical(self, "Error", "Series ID is missing.")
                        return

                    seasons = self.portal.get_seasons(series_id)
                    if seasons:
                        # NEW: propagate parent poster + mark type
                        if parent_poster:
                            for s in seasons:
                                if isinstance(s, dict):
                                    s.setdefault("_parent_poster", parent_poster)
                                    s.setdefault("item_type", "season")

                        # Optional: sort if there's a numeric season number field
                        try:
                            seasons.sort(key=lambda x: x.get("season_number", 0) if isinstance(x, dict) else 0)
                        except Exception:
                            pass

                        tab_info["current_series_info"] = seasons
                        tab_info["current_view"] = "seasons"
                        self.update_series_view(tab_name)
                    else:
                        logging.warning("No seasons found for the selected series.")
                        QMessageBox.information(self, "Info", "No seasons found for the selected series.")
            except Exception as e:
                logging.error(f"Error retrieving series info from Stalker portal: {e}")
                QMessageBox.critical(self, "Error", f"Failed to retrieve series information: {e}")
            return

        # **Non-Stalker Portal Logic (Existing Implementation)**
        try:
            session = self.session
            url = self.base_url
            mac_address = self.mac_address

            # Check if token is still valid
            if not self.is_token_valid():
                self.token = get_token(session, url, mac_address)
                self.token_timestamp = time.time()
                if not self.token:
                    QMessageBox.critical(self, "Error",
                                         "Failed to retrieve token. Please check your MAC address and URL.")
                    return

            token = self.token

            cookies = {
                "mac": mac_address,
                "stb_lang": "en",
                "timezone": "Europe/London",
                "token": token,
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
                QMessageBox.critical(self, "Error", "Series ID is missing.")
                return

            if season_number is None:
                # Fetch seasons
                all_seasons = []
                page_number = 0
                while True:
                    seasons_url = (
                        f"{url}/portal.php?type=series&action=get_ordered_list"
                        f"&movie_id={series_id}&season_id=0&episode_id=0&JsHttpRequest=1-xml&p={page_number}"
                    )
                    logging.debug(f"Fetching seasons URL: {seasons_url}, headers: {headers}, cookies: {cookies}")
                    response = session.get(seasons_url, cookies=cookies, headers=headers, timeout=10)
                    logging.debug(f"Seasons response: {response.text}")
                    if response.status_code == 200:
                        seasons_data = response.json().get("js", {}).get("data", [])
                        if not seasons_data:
                            break
                        for season in seasons_data:
                            # Ensure season_id is a string
                            season_id_raw = season.get("id", "")
                            season_id = str(season_id_raw)
                            logging.debug(f"Processing season_id: {season_id_raw} (type: {type(season_id_raw)}) converted to string: {season_id}")
                            season_number_extracted = None
                            if season_id.startswith("season"):
                                match = re.match(r"season(\d+)", season_id)
                                if match:
                                    season_number_extracted = int(match.group(1))
                                else:
                                    logging.error(f"Unexpected season id format: {season_id}")
                            else:
                                match = re.match(r"\d+:(\d+)", season_id)
                                if match:
                                    season_number_extracted = int(match.group(1))
                                else:
                                    logging.error(f"Unexpected season id format: {season_id}")

                            season["season_number"] = season_number_extracted
                            season["item_type"] = "season"

                            # NEW: propagate parent poster so seasons have an image on hover
                            if parent_poster:
                                season.setdefault("_parent_poster", parent_poster)

                        all_seasons.extend(seasons_data)
                        total_items = response.json().get("js", {}).get("total_items", len(all_seasons))
                        logging.debug(f"Fetched {len(all_seasons)} seasons out of {total_items}.")
                        if len(all_seasons) >= total_items:
                            break
                        page_number += 1
                    else:
                        logging.error(f"Failed to fetch seasons for page {page_number} with status code {response.status_code}")
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
                    QMessageBox.information(self, "Info", "No episodes found in this season.")
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

                    # NEW: propagate parent poster so episodes have an image on hover
                    if parent_poster:
                        episode.setdefault("_parent_poster", parent_poster)

                    logging.debug(f"Episode details: {episode}")
                    all_episodes.append(episode)

                if all_episodes:
                    # Process and sort episodes (keep your existing utility)
                    processed_episodes = self.process_and_sort_episodes(all_episodes)

                    tab_info["current_series_info"] = processed_episodes
                    tab_info["current_view"] = "episodes"
                    self.update_series_view(tab_name)
                else:
                    logging.info("No episodes found.")

        except KeyError as e:
            logging.error(f"KeyError retrieving series info: {str(e)}")
        except Exception as e:
            logging.error(f"Error retrieving series info: {str(e)}")


    def process_and_sort_episodes(self, episodes):
        """
        Processes a list of episodes by ensuring episode_number is an integer
        and sorts them in ascending order based on episode_number.

        Args:
            episodes (list): List of episode dictionaries.

        Returns:
            list: Processed and sorted list of episodes.
        """
        for ep in episodes:
            # Ensure episode_number is an integer
            try:
                ep["episode_number"] = int(ep.get("episode_number", 0))
            except ValueError:
                logging.warning(f"Invalid episode_number for episode: {ep}")
                ep["episode_number"] = 0  # Default or handle as needed

            ep["item_type"] = "episode"

        # Log episodes before sorting
        logging.debug(f"Episodes before sorting: {episodes}")

        # Sort episodes by episode_number in ascending order
        episodes.sort(key=lambda x: x.get("episode_number", 0))

        # Log episodes after sorting
        logging.debug(f"Episodes after sorting: {episodes}")

        return episodes

    def is_token_valid(self):
        # Assuming token is valid for 10 minutes
        if self.token and (time.time() - self.token_timestamp) < 600:
            return True
        return False

    def make_epg_token_provider(self):
        def provider():
            # If we have a StalkerPortal, try to use its token/cookies first
            if getattr(self, "portal", None):
                try:
                    token = getattr(self.portal, "token", None)
                    if token:
                        return token, {"Authorization": f"Bearer {token}"}, {"token": token}
                except Exception:
                    pass
            # Fallback to the normal reuse/refresh path
            if not self.session:
                self.session = requests.Session()
            if not self.is_token_valid():
                try:
                    self.token = get_token(self.session, self.base_url, self.mac_address)
                    self.token_timestamp = time.time()
                except Exception as e:
                    logging.debug(f"EPG token refresh failed (non-fatal): {e}")
            token = self.token or ""
            return token, ({"Authorization": f"Bearer {token}"} if token else {}), ({"token": token} if token else {})
        return provider


    def play_channel(self, channel):
        """
        Plays a selected channel, movie, or episode.

        For Stalker Portal episodes, it uses the StalkerPortal's get_episode_stream_url method.
        For non-Stalker content, it continues to use the existing 'cmd' based logic.
        """
        hostname_input = self.hostname_input.text().strip()

        # Determine if the current portal is a Stalker Portal
        is_stalker = "/stalker_portal/" in hostname_input and hasattr(self, 'portal') and self.portal

        item_type = channel.get("item_type", "channel").lower()

        if is_stalker:
            if item_type == "episode":
                # Extract necessary IDs for Stalker episodes
                movie_id = channel.get("movie_id")
                season_id = channel.get("season_id")
                episode_id = channel.get("id")

                if not (movie_id and season_id and episode_id):
                    logging.error("Missing movie_id, season_id, or episode_id for Stalker episode.")
                    QMessageBox.critical(self, "Error", "Incomplete episode information.")
                    return

                # Fetch the stream URL using StalkerPortal
                try:
                    stream_url = self.portal.get_episode_stream_url(movie_id, season_id, episode_id)
                    if stream_url:
                        self.launch_media_player(stream_url)
                    else:
                        QMessageBox.critical(self, "Error", "Failed to get stream URL from Stalker portal.")
                except Exception as e:
                    logging.error(f"Error fetching stream URL from StalkerPortal: {e}")
                    QMessageBox.critical(self, "Error", f"Failed to get stream URL: {e}")
                return  # Exit after handling Stalker episode

            else:
                # Handle other Stalker content types (VOD, Channel)
                try:
                    stream_url = self.portal.get_stream_link(channel)
                    if stream_url:
                        self.launch_media_player(stream_url)
                    else:
                        QMessageBox.critical(self, "Error", "Failed to get stream URL from Stalker portal.")
                except Exception as e:
                    logging.error(f"Error fetching stream URL from StalkerPortal: {e}")
                    QMessageBox.critical(self, "Error", f"Failed to get stream URL: {e}")
                return  # Exit after handling Stalker content

        # Non-Stalker logic
        cmd = channel.get("cmd")
        if not cmd:
            logging.error(f"No command found for channel/episode: {channel}")
            QMessageBox.critical(self, "Error", "No command found for the selected item.")
            return

        item_type = item_type  # Already set to lowercase earlier

        if item_type == "channel":
            needs_create_link = False
            if "/ch/" in cmd and cmd.endswith("_"):
                needs_create_link = True

            if needs_create_link:
                try:
                    session = self.session
                    url = self.base_url
                    mac_address = self.mac_address

                    # Refresh token if needed
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
                        if cmd.lower().startswith("ffmpeg"):
                            cmd = cmd[6:].strip()
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
                # Strip 'ffmpeg ' prefix if present
                if cmd.startswith("ffmpeg "):
                    cmd = cmd[len("ffmpeg "):]
                    logging.debug("Stripped 'ffmpeg ' prefix from cmd.")
                self.launch_media_player(cmd)

        elif item_type in ["vod", "episode"]:
            try:
                session = self.session
                url = self.base_url
                mac_address = self.mac_address

                # Refresh token if needed
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
                            self,
                            "Error",
                            "Episode number is missing."
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
                    # Remove the first word (e.g., 'ffmpeg') and join the rest
                    cmd_value = ' '.join(cmd_value.split(' ')[1:])

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
            go_back_item.setIcon(self.get_icon_for_item("Go Back"))  # Set icon
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
            list_item.setIcon(self.get_icon_for_item(item_type))  # Set icon
            playlist_model.appendRow(list_item)

        # Restore scroll position after model is populated
        QTimer.singleShot(0, lambda: playlist_view.verticalScrollBar().setValue(scroll_position))

    def launch_media_player(self, stream_url):
        # List of known prefixes to strip
        known_prefixes = ["ffmpeg ", "ffrt3 "]  # Add any other prefixes here

        # Strip any known prefix
        original_url = stream_url
        stream_url = stream_url.strip()  # Remove extra spaces
        for prefix in known_prefixes:
            if stream_url.lower().startswith(prefix.lower()):
                stream_url = stream_url[len(prefix):].strip()
                logging.debug(f"Removed prefix '{prefix}' from stream_url. New URL: {stream_url}")

        # Log the final URL
        logging.debug(f"Launching media player with cleaned URL: {stream_url}")

        # Retrieve media player executable path
        media_player = self.settings.value("media_player", "")  # Ensure VLC is set here
        if media_player:
            try:
                # Define the VLC user-agent parameter
                user_agent = "Lavf53.32.100"  # Customize this if needed
                vlc_command = [media_player, stream_url, f":http-user-agent={user_agent}"]

                # Launch the media player with the cleaned URL and user-agent
                subprocess.Popen(vlc_command)
                logging.debug(f"Successfully launched media player with URL: {stream_url} and User-Agent: {user_agent}")
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

    def _stop_live_epg_prefetch(self):
        """Stop timer, bump generation, clear mapping, and drop queued EPG fetches."""
        # bump generation so any in-flight timer ticks become no-ops
        self._epg_generation += 1

        # stop batch timer
        if getattr(self, "_live_epg_prefetch_timer", None):
            try:
                self._live_epg_prefetch_timer.stop()
                self._live_epg_prefetch_timer.deleteLater()
            except Exception:
                pass
            self._live_epg_prefetch_timer = None

        # clear queued network work (if supported by EpgManager)
        if self.epg and hasattr(self.epg, "cancel_pending"):
            try:
                self.epg.cancel_pending()
            except Exception:
                pass

        # nothing to update anymore
        self._epg_row_by_key.clear()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # Correctly set the application style
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

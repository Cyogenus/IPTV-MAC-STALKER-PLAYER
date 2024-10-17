import sys
import traceback
import requests
import subprocess
import logging
from PyQt5.QtCore import QSettings, Qt, QThread, pyqtSignal
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
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5 import QtCore
from urllib.parse import quote, urlparse, urlunparse

# Configure the logging module
logging.basicConfig(level=logging.INFO)  # Set the desired log level


def get_token(session, url, mac_address):
    try:
        handshake_url = f"{url}/portal.php?type=stb&action=handshake&JsHttpRequest=1-xml"
        cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
        headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)"}
        response = session.get(handshake_url, cookies=cookies, headers=headers)
        token = response.json()["js"]["token"]
        if token:
            return token
    except Exception as e:
        logging.error(f"Error getting token: {e}")
        return None


class RequestThread(QThread):
    request_complete = pyqtSignal(dict)  # Signal to emit when request is complete
    update_progress = pyqtSignal(int)  # Signal to emit progress updates
    channels_loaded = pyqtSignal(list)  # Signal to emit channels when loaded

    def __init__(self, base_url, mac_address, category_type=None, category_id=None):
        super().__init__()
        self.base_url = base_url
        self.mac_address = mac_address
        self.category_type = category_type
        self.category_id = category_id

    def run(self):
        try:
            session = requests.Session()
            url = self.base_url
            mac_address = self.mac_address
            token = get_token(session, url, mac_address)
            if token:
                if self.category_type and self.category_id:
                    # Fetch channels in a category
                    channels = self.get_channels(session, url, mac_address, token, self.category_type, self.category_id)
                    self.update_progress.emit(100)
                    self.channels_loaded.emit(channels)
                else:
                    # Fetch playlist (Live, Movies, Series)
                    data = {
                        'Live': [],
                        'Movies': [],
                        'Series': []
                    }

                    # Retrieve IPTV genres for Live tab
                    genres = self.get_genres(session, url, mac_address, token)
                    if genres:
                        data['Live'].extend(genres)

                    self.update_progress.emit(30)  # Update progress

                    # Retrieve VOD categories for Movies tab
                    vod_categories = self.get_vod_categories(session, url, mac_address, token)
                    if vod_categories:
                        data['Movies'].extend(vod_categories)

                    self.update_progress.emit(60)  # Update progress

                    # Retrieve Series categories for Series tab
                    series_categories = self.get_series_categories(session, url, mac_address, token)
                    if series_categories:
                        data['Series'].extend(series_categories)

                    self.update_progress.emit(100)  # Update progress to complete
                    self.request_complete.emit(data)
            else:
                self.request_complete.emit({})
                self.update_progress.emit(0)  # Reset progress if token fails

        except Exception as e:
            traceback.print_exc()
            self.request_complete.emit({})  # Emit empty data in case of an error
            self.update_progress.emit(0)  # Reset progress on error

    def get_genres(self, session, url, mac_address, token):
        try:
            genres_url = f"{url}/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
                "Authorization": "Bearer " + token,
            }
            response = session.get(genres_url, cookies=cookies, headers=headers)
            genre_data = response.json()["js"]
            if genre_data:
                genres = []
                for i in genre_data:
                    gid = i["id"]
                    name = i["title"]
                    genres.append({"name": name, "category_type": "IPTV", "category_id": gid})
                return genres
        except Exception as e:
            logging.error(f"Error getting genres: {e}")
            return []

    def get_vod_categories(self, session, url, mac_address, token):
        try:
            vod_url = f"{url}/portal.php?type=vod&action=get_categories&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
                "Authorization": "Bearer " + token,
            }
            response = session.get(vod_url, cookies=cookies, headers=headers)
            categories_data = response.json()["js"]
            if categories_data:
                categories = []
                for category in categories_data:
                    category_id = category["id"]
                    name = category["title"]
                    categories.append({"name": name, "category_type": "VOD", "category_id": category_id})
                return categories
        except Exception as e:
            logging.error(f"Error getting VOD categories: {e}")
            return []

    def get_series_categories(self, session, url, mac_address, token):
        try:
            series_url = f"{url}/portal.php?type=series&action=get_categories&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
                "Authorization": "Bearer " + token,
            }
            response = session.get(series_url, cookies=cookies, headers=headers)
            response_json = response.json()

            logging.debug(f"Series categories response: {response_json}")
            if not isinstance(response_json, dict) or 'js' not in response_json:
                logging.error("Unexpected response structure for series categories.")
                return []

            categories_data = response_json['js']
            if isinstance(categories_data, list):
                categories = []
                for category in categories_data:
                    category_id = category.get("id")
                    name = category.get("title")
                    if category_id and name:
                        categories.append({"name": name, "category_type": "Series", "category_id": category_id})
                return categories
            else:
                logging.error("Series categories data is not a list.")
                return []

        except Exception as e:
            logging.error(f"Error getting series categories: {e}")
            return []

    def get_channels(self, session, url, mac_address, token, category_type, category_id):
        try:
            channels = []
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
                "Authorization": "Bearer " + token,
            }

            page_number = 0
            self.update_progress.emit(10)  # Set initial progress for channel loading
            while True:
                page_number += 1
                if category_type == "IPTV":
                    channels_url = (
                        f"{url}/portal.php?type=itv&action=get_ordered_list&genre={category_id}&force_ch_link_check=&fav=0&"
                        f"sortby=number&hd=0&p={page_number}&JsHttpRequest=1-xml&from_ch_id=0"
                    )
                elif category_type == "VOD":
                    channels_url = (
                        f"{url}/portal.php?type=vod&action=get_ordered_list&category={category_id}&p={page_number}&JsHttpRequest=1-xml"
                    )
                elif category_type == "Series":
                    channels_url = (
                        f"{url}/portal.php?type=series&action=get_ordered_list&category={category_id}&p={page_number}&JsHttpRequest=1-xml"
                    )
                else:
                    break

                response = session.get(channels_url, cookies=cookies, headers=headers)
                if response.status_code == 200:
                    try:
                        response_json = response.json()
                        channels_data = response_json["js"]["data"]
                        if not channels_data:
                            break
                        # Set item_type based on category_type
                        if category_type == "Series":
                            for channel in channels_data:
                                channel['item_type'] = 'series'
                        elif category_type == "VOD":
                            for channel in channels_data:
                                channel['item_type'] = 'vod'
                        else:
                            for channel in channels_data:
                                channel['item_type'] = 'channel'
                        channels.extend(channels_data)
                        total_items = response_json["js"]["total_items"]
                        if len(channels) >= total_items:
                            break
                    except ValueError:
                        logging.error("Invalid JSON format in response")
                        break
                else:
                    logging.error(f"Request failed for page {page_number}")
                    break

                self.update_progress.emit(50)  # Update progress while loading channels

            self.update_progress.emit(100)  # Set final progress for channel loading
            return channels

        except Exception as e:
            logging.error(f"An error occurred while retrieving channels: {str(e)}")
            return []


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MAC IPTV Player by MY-1 BETA")
        self.setGeometry(100, 100, 510, 550)  # Increased window height for the progress bar

        # Set the Fusion theme
        app.setStyle("Fusion")
        # Create QSettings object

        
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

        self.get_playlist_button = QPushButton("Get Playlist")
        layout.addWidget(self.get_playlist_button)
        self.get_playlist_button.clicked.connect(self.get_playlist)

        # Create a QTabWidget
        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        # Dictionary to hold tab data
        self.tabs = {}

        for tab_name in ['Live', 'Movies', 'Series']:
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
                'tab_widget': tab,
                'playlist_view': playlist_view,
                'playlist_model': playlist_model,
                # Initialize navigation variables for this tab
                'current_category': None,
                'navigation_stack': [],
                'playlist_data': [],
                'current_channels': [],
                'current_series_info': [],
                'current_view': 'categories',  # Track the current view
            }

        # Create a purple progress bar at the bottom
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                text-align: center;  /* Center the text */
                color: white;  /* Set the text color to white */
            }
            QProgressBar::chunk {
                background-color: purple;  /* Set the progress bar chunk color */
            }
        """)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)


        # Load settings
        self.load_settings()

    def load_settings(self):
        # Load hostname from settings
        hostname = self.settings.value("hostname", "")
        self.hostname_input.setText(hostname)

        # Load MAC address from settings
        mac_address = self.settings.value("mac_address", "")
        self.mac_input.setText(mac_address)

        # Load media player from settings
        media_player = self.settings.value("media_player", "")
        self.media_player_input.setText(media_player)

    def closeEvent(self, event):
        # Save settings before closing the application
        self.save_settings()
        event.accept()

    def save_settings(self):
        self.settings.setValue("hostname", self.hostname_input.text())
        self.settings.setValue("mac_address", self.mac_input.text())
        self.settings.setValue("media_player", self.media_player_input.text())

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

    def get_playlist(self):
        hostname_input = self.hostname_input.text()
        mac_address = self.mac_input.text()
        media_player = self.media_player_input.text()

        if not hostname_input or not mac_address or not media_player:
            QMessageBox.warning(
                self, "Warning", "Please enter the Hostname, MAC Address, and Media Player."
            )
            return

        # Parse the hostname input
        parsed_url = urlparse(hostname_input)
        if not parsed_url.scheme and not parsed_url.netloc:
            # If no scheme and netloc, assume http and parse again
            parsed_url = urlparse(f"http://{hostname_input}")
        elif not parsed_url.scheme:
            # If scheme is missing but netloc exists
            parsed_url = parsed_url._replace(scheme='http')

        # Reconstruct the base URL
        self.base_url = urlunparse((parsed_url.scheme, parsed_url.netloc, '', '', '', ''))
        self.mac_address = mac_address

        self.request_thread = RequestThread(self.base_url, mac_address)
        self.request_thread.request_complete.connect(self.on_initial_playlist_received)
        self.request_thread.update_progress.connect(self.progress_bar.setValue)  # Update progress bar
        self.request_thread.start()

    def on_initial_playlist_received(self, data):
        if not data:
            self.show_error_message("Failed to retrieve playlist data. Check your connection and try again.")
            return
        for tab_name, tab_data in data.items():
            tab_info = self.tabs[tab_name]
            tab_info['playlist_data'] = tab_data
            tab_info['current_category'] = None
            tab_info['navigation_stack'] = []
            self.update_playlist_view(tab_name)

    def update_playlist_view(self, tab_name):
        tab_info = self.tabs[tab_name]
        playlist_model = tab_info['playlist_model']
        playlist_model.clear()
        tab_info['current_view'] = 'categories'

        if tab_info['navigation_stack']:
            # Add "Go Back" item
            go_back_item = QStandardItem("Go Back")
            playlist_model.appendRow(go_back_item)

        if tab_info['current_category'] is None:
            # At the top level, show categories
            for item in tab_info['playlist_data']:
                name = item["name"]
                list_item = QStandardItem(name)
                list_item.setData(item, QtCore.Qt.UserRole)
                list_item.setData('category', QtCore.Qt.UserRole + 1)
                playlist_model.appendRow(list_item)
        else:
            # Inside a category, retrieve channels
            self.retrieve_channels(tab_name, tab_info['current_category'])

    def retrieve_channels(self, tab_name, category):
        tab_info = self.tabs[tab_name]
        category_type = category["category_type"]
        category_id = category.get("category_id") or category.get("genre_id")
        try:
            self.progress_bar.setValue(0)  # Reset progress bar
            self.request_thread = RequestThread(self.base_url, self.mac_address, category_type, category_id)
            self.request_thread.update_progress.connect(self.progress_bar.setValue)  # Update progress bar
            self.request_thread.channels_loaded.connect(lambda channels: self.on_channels_loaded(tab_name, channels))
            self.request_thread.start()
        except Exception as e:
            traceback.print_exc()
            self.show_error_message("An error occurred while retrieving channels.")

    def on_channels_loaded(self, tab_name, channels):
        tab_info = self.tabs[tab_name]
        tab_info['current_channels'] = channels
        self.update_channel_view(tab_name)

    def update_channel_view(self, tab_name):
        tab_info = self.tabs[tab_name]
        playlist_model = tab_info['playlist_model']
        playlist_model.clear()
        tab_info['current_view'] = 'channels'

        if tab_info['navigation_stack']:
            # Add "Go Back" item
            go_back_item = QStandardItem("Go Back")
            playlist_model.appendRow(go_back_item)
        for channel in tab_info['current_channels']:
            channel_name = channel["name"]
            list_item = QStandardItem(channel_name)
            list_item.setData(channel, QtCore.Qt.UserRole)
            item_type = channel.get('item_type', 'channel')
            list_item.setData(item_type, QtCore.Qt.UserRole + 1)
            playlist_model.appendRow(list_item)

    def on_playlist_selection_changed(self, index):
        sender = self.sender()
        for tab_name, tab_info in self.tabs.items():
            if sender == tab_info['playlist_view']:
                current_tab = tab_name
                break
        else:
            logging.error("Unknown sender for on_playlist_selection_changed")
            return

        tab_info = self.tabs[current_tab]
        playlist_model = tab_info['playlist_model']

        if index.isValid():
            item = playlist_model.itemFromIndex(index)
            item_text = item.text()

            if item_text == "Go Back":
                if tab_info['navigation_stack']:
                    nav_state = tab_info['navigation_stack'].pop()
                    tab_info['current_category'] = nav_state['category']
                    tab_info['current_view'] = nav_state['view']
                    if tab_info['current_view'] == 'categories':
                        self.update_playlist_view(current_tab)
                    elif tab_info['current_view'] == 'channels':
                        self.update_channel_view(current_tab)
                    elif tab_info['current_view'] in ['seasons', 'episodes']:
                        self.update_series_view(current_tab)
            else:
                item_data = item.data(QtCore.Qt.UserRole)
                item_type = item.data(QtCore.Qt.UserRole + 1)
                logging.debug(f"Item data: {item_data}, item type: {item_type}")
                if item_type == 'category':
                    tab_info['navigation_stack'].append({'category': tab_info['current_category'], 'view': tab_info['current_view']})
                    tab_info['current_category'] = item_data
                    self.retrieve_channels(current_tab, tab_info['current_category'])
                elif item_type == 'series':
                    tab_info['navigation_stack'].append({'category': tab_info['current_category'], 'view': tab_info['current_view']})
                    tab_info['current_category'] = item_data
                    self.retrieve_series_info(current_tab, item_data)
                elif item_type == 'season':
                    tab_info['navigation_stack'].append({'category': tab_info['current_category'], 'view': tab_info['current_view']})
                    tab_info['current_category'] = item_data
                    series_data = tab_info['navigation_stack'][-1]['category']
                    self.retrieve_series_info(current_tab, series_data, season_number=item_data["season_number"])
                elif item_type == 'episode':
                    self.play_channel(item_data)
                elif item_type == 'channel' or item_type == 'vod':
                    self.play_channel(item_data)

    def retrieve_series_info(self, tab_name, series_data, season_number=None):
        tab_info = self.tabs[tab_name]
        try:
            session = requests.Session()
            url = self.base_url
            mac_address = self.mac_address
            token = get_token(session, url, mac_address)
            if token:
                series_id = series_data.get("id")
                if not series_id:
                    logging.error(f"Series ID missing in series data: {series_data}")
                    return

                cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
                headers = {
                    "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
                    "Authorization": "Bearer " + token,
                }

                if season_number is None:
                    all_seasons = []
                    page_number = 0

                    seasons_url = (
                        f"{url}/portal.php?type=series&action=get_ordered_list"
                        f"&movie_id={series_id}"
                        f"&season_id=0&episode_id=0"
                        f"&JsHttpRequest=1-xml&p={page_number}"
                    )

                    response = session.get(seasons_url, cookies=cookies, headers=headers)
                    response.raise_for_status()

                    response_json = response.json()
                    response_js = response_json.get("js", {})
                    seasons = response_js.get("data", [])
                    total_items = response_js.get("total_items", 0)
                    max_page_items = response_js.get("max_page_items", 0)

                    all_seasons.extend(seasons)

                    if max_page_items:
                        total_pages = (total_items + max_page_items - 1) // max_page_items
                    else:
                        logging.error("max_page_items is zero or undefined.")
                        return

                    for i in range(1, total_pages):
                        page_number = i
                        seasons_url = (
                            f"{url}/portal.php?type=series&action=get_ordered_list"
                            f"&movie_id={series_id}"
                            f"&season_id=0&episode_id=0"
                            f"&JsHttpRequest=1-xml&p={page_number}"
                        )

                        response = session.get(seasons_url, cookies=cookies, headers=headers)
                        response.raise_for_status()

                        response_json = response.json()
                        response_js = response_json.get("js", {})
                        seasons = response_js.get("data", [])

                        all_seasons.extend(seasons)

                    if all_seasons:
                        for index, season in enumerate(all_seasons, start=1):
                            season['season_number'] = season.get('season_number', index)
                            season['series_id'] = series_id
                            season['item_type'] = 'season'
                        tab_info['current_series_info'] = all_seasons
                        tab_info['current_view'] = 'seasons'
                        self.update_series_view(tab_name)
                else:
                    all_episodes = []
                    page_number = 0

                    episodes_url = (
                        f"{url}/portal.php?type=series&action=get_ordered_list"
                        f"&movie_id={series_id}"
                        f"&season_id={season_number}&episode_id=0"
                        f"&JsHttpRequest=1-xml&p={page_number}"
                    )

                    response = session.get(episodes_url, cookies=cookies, headers=headers)
                    response.raise_for_status()

                    response_json = response.json()
                    response_js = response_json.get("js", {})
                    episodes = response_js.get("data", [])
                    total_items = response_js.get("total_items", 0)
                    max_page_items = response_js.get("max_page_items", 0)

                    all_episodes.extend(episodes)

                    if max_page_items:
                        total_pages = (total_items + max_page_items - 1) // max_page_items
                    else:
                        logging.error("max_page_items is zero or undefined.")
                        return

                    for i in range(1, total_pages):
                        page_number = i
                        episodes_url = (
                            f"{url}/portal.php?type=series&action=get_ordered_list"
                            f"&movie_id={series_id}"
                            f"&season_id={season_number}&episode_id=0"
                            f"&JsHttpRequest=1-xml&p={page_number}"
                        )

                        response = session.get(episodes_url, cookies=cookies, headers=headers)
                        response.raise_for_status()

                        response_json = response.json()
                        response_js = response_json.get("js", {})
                        episodes = response_js.get("data", [])

                        all_episodes.extend(episodes)

                    if all_episodes:
                        for episode in all_episodes:
                            episode_id = episode.get("id")
                            if not episode_id:
                                logging.error(f"Episode ID missing in episode data: {episode}")
                                continue

                            episode['series_id'] = series_id
                            episode['season_number'] = season_number
                            episode['episode_id'] = episode_id
                            episode['episode_number'] = episode.get("episode_number", "Unknown")
                            episode['item_type'] = 'episode'

                        tab_info['current_series_info'] = all_episodes
                        tab_info['current_view'] = 'episodes'
                        self.update_series_view(tab_name)

            else:
                logging.error("Failed to retrieve token.")
        except KeyError as e:
            logging.error(f"KeyError retrieving series info: {str(e)}")
        except Exception as e:
            logging.error(f"Error retrieving series info: {str(e)}")

    def update_series_view(self, tab_name):
        tab_info = self.tabs[tab_name]
        playlist_model = tab_info['playlist_model']
        playlist_model.clear()

        if tab_info['navigation_stack']:
            go_back_item = QStandardItem("Go Back")
            playlist_model.appendRow(go_back_item)

        for item in tab_info['current_series_info']:
            item_type = item.get('item_type')
            if item_type == 'season':
                name = f"Season {item['season_number']}"
            elif item_type == 'episode':
                name = f"Episode {item['episode_number']}: {item.get('name', '')}"
            else:
                name = item.get('name') or item.get('title')
            list_item = QStandardItem(name)
            list_item.setData(item, QtCore.Qt.UserRole)
            list_item.setData(item_type, QtCore.Qt.UserRole + 1)
            playlist_model.appendRow(list_item)

    def play_channel(self, channel):
        cmd = channel.get("cmd")
        if not cmd:
            logging.error(f"No command found for channel: {channel}")
            return
        if cmd.startswith("ffmpeg "):
            cmd = cmd[len("ffmpeg "):]

        item_type = channel.get('item_type', 'channel')

        if item_type == 'channel':
            needs_create_link = False
            if '/ch/' in cmd and cmd.endswith('_'):
                needs_create_link = True

            if needs_create_link:
                try:
                    session = requests.Session()
                    url = self.base_url
                    mac_address = self.mac_address
                    token = get_token(session, url, mac_address)
                    if token:
                        cmd_encoded = quote(cmd)
                        cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
                        headers = {
                            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
                            "Authorization": "Bearer " + token,
                        }
                        create_link_url = (
                            f"{url}/portal.php?type=itv&action=create_link&cmd={cmd_encoded}&JsHttpRequest=1-xml"
                        )
                        response = session.get(create_link_url, cookies=cookies, headers=headers)
                        response.raise_for_status()
                        json_response = response.json()

                        logging.debug(f"Create link response: {json_response}")
                        cmd_value = json_response.get("js", {}).get("cmd")
                        if cmd_value:
                            if cmd_value.startswith("ffmpeg "):
                                cmd_value = cmd_value[len("ffmpeg "):]
                            stream_url = cmd_value
                            self.launch_media_player(stream_url)
                        else:
                            logging.error("Stream URL not found in the response.")
                    else:
                        logging.error("Failed to retrieve token.")
                except Exception as e:
                    logging.error(f"Error creating stream link: {e}")
            else:
                self.launch_media_player(cmd)

        elif item_type in ['episode', 'vod']:
            try:
                session = requests.Session()
                url = self.base_url
                mac_address = self.mac_address
                token = get_token(session, url, mac_address)
                if token:
                    cmd_encoded = quote(cmd)
                    cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
                    headers = {
                        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
                        "Authorization": "Bearer " + token,
                    }
                    if item_type == 'episode':
                        create_link_url = (
                            f"{url}/portal.php?type=vod&action=create_link&cmd={cmd_encoded}&series=1&JsHttpRequest=1-xml"
                        )
                    else:
                        create_link_url = (
                            f"{url}/portal.php?type=vod&action=create_link&cmd={cmd_encoded}&JsHttpRequest=1-xml"
                        )
                    response = session.get(create_link_url, cookies=cookies, headers=headers)
                    response.raise_for_status()
                    json_response = response.json()

                    logging.debug(f"Create link response: {json_response}")
                    cmd_value = json_response.get("js", {}).get("cmd")
                    if cmd_value:
                        if cmd_value.startswith("ffmpeg "):
                            cmd_value = cmd_value[len("ffmpeg "):]
                        stream_url = cmd_value
                        self.launch_media_player(stream_url)
                    else:
                        logging.error("Stream URL not found in the response.")
                else:
                    logging.error("Failed to retrieve token.")
            except Exception as e:
                logging.error(f"Error creating stream link: {e}")

    def launch_media_player(self, stream_url):
        media_player = self.settings.value("media_player", "")

        if media_player:
            try:
                subprocess.Popen([media_player, stream_url])
            except Exception as e:
                logging.error(f"Error opening media player: {e}")
        else:
            logging.error("Media player executable path not found in settings.")

    def resizeEvent(self, event):
        pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

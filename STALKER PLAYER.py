import sys
import traceback
import requests
import subprocess
import logging
import re
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
        token = response.json().get("js", {}).get("token")
        if token:
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
            logging.error(f"Request thread error: {str(e)}")
            traceback.print_exc()
            self.request_complete.emit({})  # Emit empty data in case of an error
            self.update_progress.emit(0)  # Reset progress on error

    def get_genres(self, session, url, mac_address, token):
        try:
            genres_url = f"{url}/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": "Bearer " + token}
            response = session.get(genres_url, cookies=cookies, headers=headers)
            genre_data = response.json().get("js", [])
            if genre_data:
                genres = [{"name": i["title"], "category_type": "IPTV", "category_id": i["id"]} for i in genre_data]
                return genres
        except Exception as e:
            logging.error(f"Error getting genres: {e}")
            return []

    def get_vod_categories(self, session, url, mac_address, token):
        try:
            vod_url = f"{url}/portal.php?type=vod&action=get_categories&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": "Bearer " + token}
            response = session.get(vod_url, cookies=cookies, headers=headers)
            categories_data = response.json().get("js", [])
            if categories_data:
                categories = [{"name": category["title"], "category_type": "VOD", "category_id": category["id"]} for category in categories_data]
                return categories
        except Exception as e:
            logging.error(f"Error getting VOD categories: {e}")
            return []

    def get_series_categories(self, session, url, mac_address, token):
        try:
            series_url = f"{url}/portal.php?type=series&action=get_categories&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": "Bearer " + token}
            response = session.get(series_url, cookies=cookies, headers=headers)
            response_json = response.json()

            logging.debug(f"Series categories response: {response_json}")
            if not isinstance(response_json, dict) or 'js' not in response_json:
                logging.error("Unexpected response structure for series categories.")
                return []

            categories_data = response_json.get('js', [])
            categories = [{"name": category["title"], "category_type": "Series", "category_id": category["id"]} for category in categories_data]
            return categories
        except Exception as e:
            logging.error(f"Error getting series categories: {e}")
            return []

    def get_channels(self, session, url, mac_address, token, category_type, category_id):
        try:
            channels = []
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": f"Bearer {token}"}
            page_number = 0
            while True:
                page_number += 1
                if category_type == "IPTV":
                    channels_url = f"{url}/portal.php?type=itv&action=get_ordered_list&genre={category_id}&JsHttpRequest=1-xml&p={page_number}"
                elif category_type == "VOD":
                    channels_url = f"{url}/portal.php?type=vod&action=get_ordered_list&category={category_id}&JsHttpRequest=1-xml&p={page_number}"
                elif category_type == "Series":
                    channels_url = f"{url}/portal.php?type=series&action=get_ordered_list&category={category_id}&p={page_number}&JsHttpRequest=1-xml"
                else:
                    break

                response = session.get(channels_url, cookies=cookies, headers=headers)
                if response.status_code == 200:
                    channels_data = response.json().get("js", {}).get("data", [])
                    if not channels_data:
                        break
                    for channel in channels_data:
                        channel['item_type'] = 'series' if category_type == "Series" else 'vod' if category_type == "VOD" else 'channel'
                    channels.extend(channels_data)
                    total_items = response.json().get("js", {}).get("total_items", len(channels))
                    if len(channels) >= total_items:
                        break
                else:
                    logging.error(f"Request failed for page {page_number}")
                    break
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
                'current_category': None,
                'navigation_stack': [],
                'playlist_data': [],
                'current_channels': [],
                'current_series_info': [],
                'current_view': 'categories',
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
        self.hostname_input.setText(self.settings.value("hostname", ""))
        self.mac_input.setText(self.settings.value("mac_address", ""))
        self.media_player_input.setText(self.settings.value("media_player", ""))

    def closeEvent(self, event):
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
            QMessageBox.warning(self, "Warning", "Please enter the Hostname, MAC Address, and Media Player.")
            return

        parsed_url = urlparse(hostname_input)
        if not parsed_url.scheme and not parsed_url.netloc:
            parsed_url = urlparse(f"http://{hostname_input}")
        elif not parsed_url.scheme:
            parsed_url = parsed_url._replace(scheme='http')

        self.base_url = urlunparse((parsed_url.scheme, parsed_url.netloc, '', '', '', ''))
        self.mac_address = mac_address

        self.request_thread = RequestThread(self.base_url, mac_address)
        self.request_thread.request_complete.connect(self.on_initial_playlist_received)
        self.request_thread.update_progress.connect(self.progress_bar.setValue)
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
            go_back_item = QStandardItem("Go Back")
            playlist_model.appendRow(go_back_item)

        if tab_info['current_category'] is None:
            for item in tab_info['playlist_data']:
                name = item["name"]
                list_item = QStandardItem(name)
                list_item.setData(item, QtCore.Qt.UserRole)
                list_item.setData('category', QtCore.Qt.UserRole + 1)
                playlist_model.appendRow(list_item)
        else:
            self.retrieve_channels(tab_name, tab_info['current_category'])

    def retrieve_channels(self, tab_name, category):
        tab_info = self.tabs[tab_name]
        category_type = category["category_type"]
        category_id = category.get("category_id") or category.get("genre_id")
        try:
            self.progress_bar.setValue(0)
            self.request_thread = RequestThread(self.base_url, self.mac_address, category_type, category_id)
            self.request_thread.update_progress.connect(self.progress_bar.setValue)
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
                # Handle 'Go Back' functionality
                if tab_info['navigation_stack']:
                    nav_state = tab_info['navigation_stack'].pop()
                    tab_info['current_category'] = nav_state['category']
                    tab_info['current_view'] = nav_state['view']
                    tab_info['current_series_info'] = nav_state['series_info']  # Restore series_info
                    logging.debug(f"Go Back to view: {tab_info['current_view']}")
                    if tab_info['current_view'] == 'categories':
                        self.update_playlist_view(current_tab)
                    elif tab_info['current_view'] == 'channels':
                        self.update_channel_view(current_tab)
                    elif tab_info['current_view'] in ['seasons', 'episodes']:
                        self.update_series_view(current_tab)
                else:
                    logging.debug("Navigation stack is empty. Cannot go back.")
                    QMessageBox.information(self, "Info", "No previous view to go back to.")
            else:
                item_data = item.data(QtCore.Qt.UserRole)
                item_type = item.data(QtCore.Qt.UserRole + 1)
                logging.debug(f"Item data: {item_data}, item type: {item_type}")

                if item_type == 'category':
                    # Navigate into a category
                    tab_info['navigation_stack'].append({
                        'category': tab_info['current_category'],
                        'view': tab_info['current_view'],
                        'series_info': tab_info['current_series_info']  # Preserve current_series_info
                    })
                    tab_info['current_category'] = item_data
                    logging.debug(f"Navigating to category: {item_data.get('name')}")
                    self.retrieve_channels(current_tab, tab_info['current_category'])

                elif item_type == 'series':
                    # User selected a series, retrieve its seasons
                    tab_info['navigation_stack'].append({
                        'category': tab_info['current_category'],
                        'view': tab_info['current_view'],
                        'series_info': tab_info['current_series_info']  # Preserve current_series_info
                    })
                    tab_info['current_category'] = item_data
                    logging.debug(f"Navigating to series: {item_data.get('name')}")
                    self.retrieve_series_info(current_tab, item_data)

                elif item_type == 'season':
                    # User selected a season, set navigation context
                    tab_info['navigation_stack'].append({
                        'category': tab_info['current_category'],
                        'view': tab_info['current_view'],
                        'series_info': tab_info['current_series_info']  # Preserve current_series_info
                    })
                    tab_info['current_category'] = item_data

                    # Update view to 'seasons'
                    tab_info['current_view'] = 'seasons'
                    self.update_series_view(current_tab)

                    # Retrieve episodes using the season data
                    logging.debug(f"Fetching episodes for season {item_data['season_number']} in series {item_data['name']}")
                    self.retrieve_series_info(current_tab, item_data, season_number=item_data["season_number"])

                elif item_type == 'episode':
                    # User selected an episode, play it
                    logging.debug(f"Playing episode: {item_data.get('name')}")
                    self.play_channel(item_data)

                elif item_type == 'channel' or item_type == 'vod':
                    # This is an IPTV channel or VOD, play it
                    logging.debug(f"Playing channel/VOD: {item_data.get('name')}")
                    self.play_channel(item_data)

                else:
                    logging.error("Unknown item type")

    def retrieve_series_info(self, tab_name, context_data, season_number=None):
        tab_info = self.tabs[tab_name]
        try:
            session = requests.Session()
            url = self.base_url
            mac_address = self.mac_address
            token = get_token(session, url, mac_address)

            if token:
                series_id = context_data.get("id")
                if not series_id:
                    logging.error(f"Series ID missing in context data: {context_data}")
                    return

                cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
                headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": f"Bearer {token}"}

                if season_number is None:
                    # Fetch seasons
                    all_seasons = []
                    page_number = 0
                    seasons_url = f"{url}/portal.php?type=series&action=get_ordered_list&movie_id={series_id}&season_id=0&episode_id=0&JsHttpRequest=1-xml&p={page_number}"
                    logging.debug(f"Fetching seasons URL: {seasons_url}, headers: {headers}, cookies: {cookies}")

                    while True:
                        response = session.get(seasons_url, cookies=cookies, headers=headers)
                        logging.debug(f"Seasons response: {response.text}")
                        if response.status_code == 200:
                            seasons_data = response.json().get("js", {}).get("data", [])
                            if not seasons_data:
                                break
                            for season in seasons_data:
                                season_id = season.get('id', '')
                                season_number_extracted = None
                                if season_id.startswith('season'):
                                    match = re.match(r'season(\d+)', season_id)
                                    if match:
                                        season_number_extracted = int(match.group(1))
                                    else:
                                        logging.error(f"Unexpected season id format: {season_id}")
                                else:
                                    match = re.match(r'\d+:(\d+)', season_id)
                                    if match:
                                        season_number_extracted = int(match.group(1))
                                    else:
                                        logging.error(f"Unexpected season id format: {season_id}")

                                season['season_number'] = season_number_extracted
                                season['item_type'] = 'season'
                            all_seasons.extend(seasons_data)
                            total_items = response.json().get("js", {}).get("total_items", len(all_seasons))
                            if len(all_seasons) >= total_items:
                                break
                        else:
                            logging.error(f"Failed to fetch seasons for page {page_number}")
                            break

                    if all_seasons:
                        tab_info['current_series_info'] = all_seasons
                        tab_info['current_view'] = 'seasons'
                        self.update_series_view(tab_name)
                else:
                    series_list = context_data.get("series", [])
                    if not series_list:
                        logging.info("No episodes found in this season.")
                        return

                    logging.debug(f"Series episodes found: {series_list}")
                    all_episodes = []
                    for episode_number in series_list:
                        episode = {
                            'id': f"{series_id}:{episode_number}",
                            'series_id': series_id,
                            'season_number': season_number,
                            'episode_number': episode_number,
                            'name': f"Episode {episode_number}",
                            'item_type': 'episode',
                            'cmd': context_data.get("cmd")
                        }
                        logging.debug(f"Episode details: {episode}")
                        all_episodes.append(episode)

                    if all_episodes:
                        tab_info['current_series_info'] = all_episodes
                        tab_info['current_view'] = 'episodes'
                        tab_info['episodes_loaded'] = True
                        self.update_series_view(tab_name)
                    else:
                        logging.info("No episodes found.")
            else:
                logging.error("Failed to retrieve token.")
        except KeyError as e:
            logging.error(f"KeyError retrieving series info: {str(e)}")
        except Exception as e:
            logging.error(f"Error retrieving series info: {str(e)}")

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
                        headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": f"Bearer {token}"}
                        create_link_url = f"{url}/portal.php?type=itv&action=create_link&cmd={cmd_encoded}&JsHttpRequest=1-xml"
                        logging.debug(f"Create link URL: {create_link_url}")
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
                    headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": f"Bearer {token}"}
                    if item_type == 'episode':
                        episode_number = channel.get('episode_number')
                        if episode_number is None:
                            logging.error("Episode number is missing.")
                            return
                        create_link_url = f"{url}/portal.php?type=vod&action=create_link&cmd={cmd_encoded}&series={episode_number}&JsHttpRequest=1-xml"
                    else:
                        create_link_url = f"{url}/portal.php?type=vod&action=create_link&cmd={cmd_encoded}&JsHttpRequest=1-xml"
                    logging.debug(f"Create link URL: {create_link_url}")
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
            logging.error(f"Unknown item type: {item_type}")

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
                name = f"Episode {item['episode_number']}"
            else:
                name = item.get('name') or item.get('title')
            list_item = QStandardItem(name)
            list_item.setData(item, QtCore.Qt.UserRole)
            list_item.setData(item_type, QtCore.Qt.UserRole + 1)
            playlist_model.appendRow(list_item)

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

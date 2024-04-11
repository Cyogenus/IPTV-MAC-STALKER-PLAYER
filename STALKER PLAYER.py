
import sys
import json
import traceback
import requests
import subprocess
import logging
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import QMessageBox, QLabel
from PyQt5.QtWidgets import QMainWindow, QApplication, QListWidgetItem, QLineEdit, QHBoxLayout,  QPushButton, QMessageBox, QListView, QFileDialog
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from PyQt5 import QtCore
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtWidgets import QAbstractItemView
from PyQt5.QtCore import QProcess
from PyQt5.QtCore import QThread, pyqtSignal

class RequestThread(QThread):
    request_complete = pyqtSignal(list)

    def __init__(self, hostname, mac_address):
        super().__init__()
        self.hostname = hostname
        self.mac_address = mac_address
        self.playlist_view = QListView()
        
        self.model = QStandardItemModel()
        self.playlist_view.setModel(self.model)
        
    # Configure the logging module
    logging.basicConfig(level=logging.DEBUG)  # Set the desired log level

    # Usage example
    logging.debug("Debug message")
    logging.info("Info message")
    logging.warning("Warning message")
    logging.error("Error message")

    

    def run(self):
        try:
            session = requests.Session()

            url = f"http://{self.hostname}"
            mac_address = self.mac_address

            token = self.get_token(session, url, mac_address)
            
            if token:
                playlist = []

                # Specify the category type and ID for the channels you want to retrieve
                category_type = "namme"
                category_id = "cmd"

                # Retrieve IPTV channels for the specified category
                channels = self.get_channels(session, url, mac_address, token, category_type, category_id)
                if channels:
                    playlist.extend(channels)



                # Retrieve IPTV genres
                genres = self.get_genres(session, url, mac_address, token)
                if genres:
                    playlist.extend(genres)

                # Retrieve VOD categories
                vod_categories = self.get_vod_categories(session, url, mac_address, token)
                if vod_categories:
                    playlist.extend(vod_categories)

                # Retrieve series categories
                series_categories = self.get_series_categories(session, url, mac_address, token)
                if series_categories:
                    playlist.extend(series_categories)

                self.request_complete.emit(playlist)
            else:
                self.request_complete.emit([])

        except Exception as e:
            traceback.print_exc()
            self.request_complete.emit([])  # Emit an empty playlist in case of an error

    def get_token(self, session, url, mac_address):
        try:
            url = f"{url}/portal.php?type=stb&action=handshake&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)"}
            response = session.get(url, cookies=cookies, headers=headers)
            token = response.json()["js"]["token"]
            if token:
                return token
        except:
            pass

    def get_genres(self, session, url, mac_address, token):
        try:
            url = f"{url}/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": "Bearer " + token}
            response = session.get(url, cookies=cookies, headers=headers)
            genre_data = response.json()["js"]
            if genre_data:
                genres = []
                for i in genre_data:
                    gid = i["id"]
                    name = i["title"]
                    genres.append({'name': name, 'category_type': 'IPTV', 'genre_id': gid})
                return genres
        except:
            pass

    def get_vod_categories(self, session, url, mac_address, token):
        try:
            url = f"{url}/portal.php?type=vod&action=get_categories&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": "Bearer " + token}
            response = session.get(url, cookies=cookies, headers=headers)
            categories_data = response.json()["js"]
            if categories_data:
                categories = []
                for category in categories_data:
                    category_id = category["id"]
                    name = category["title"]
                    categories.append({'name': name, 'category_type': 'VOD', 'category_id': category_id})
                return categories
        except:
            pass

    def get_series_categories(self, session, url, mac_address, token):
        try:
            url = f"{url}/portal.php?type=series&action=get_categories&JsHttpRequest=1-xml"
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": "Bearer " + token}
            response = session.get(url, cookies=cookies, headers=headers)
            categories_data = response.json()["js"]
            if categories_data:
                categories = []
                for category in categories_data:
                    category_id = category["id"]
                    name = category["title"]
                    categories.append({'name': name, 'category_type': 'Series', 'category_id': category_id})
                return categories
        except:
            pass


    
    def get_channels(self, session, url, mac_address, token, category_type, category_id):
        try:
            channels = []
            cookies = {"mac": mac_address, "stb_lang": "en", "timezone": "Europe/London"}
            headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Authorization": "Bearer " + token}

            if category_type == 'IPTV':
                page_number = 1
                while True:
                    url = f"{url}/portal.php?type=itv&action=get_ordered_list&genre={category_id}&force_ch_link_check=&fav=0&sortby=number&hd=0&p={page_number}&JsHttpRequest=1-xml&from_ch_id=0"
                    response = session.get(url, cookies=cookies, headers=headers)
                    if response.status_code == 200:
                        try:
                            response_json = response.json()
                            channels_data = response_json["js"]["data"]
                           
                            channels.extend(channels_data)
                            total_items = response_json["js"]["total_items"]
                            max_page_items = response_json["js"]["max_page_items"]
                            if len(channels) >= total_items:
                                break
                            page_number += 1
                        except ValueError:
                            print("Invalid JSON format in response")
                            break
                    else:
                        print(f"IPTV Request failed for page {page_number}")
                        break

            elif category_type == 'VOD' or category_type == 'Series':
                page_number = 1
                while True:
                    url = f"{url}/server/load.php?type={category_type.lower()}&action=get_ordered_list&category={category_id}&genre={category_id}&p={page_number}&JsHttpRequest=1-xml"
                    response = session.get(url, cookies=cookies, headers=headers)
                    if response.status_code == 200:
                        try:
                            response_json = response.json()
                            channels_data = response_json["js"]["data"]
                            print(channels_data)
                            
                            channels.extend(channels_data)
                            total_items = response_json["js"]["total_items"]
                            max_page_items = response_json["js"]["max_page_items"]
                            if len(channels) >= total_items:
                                break
                            page_number += 1
                        except ValueError:
                            print("Invalid JSON format in response")
                            break
                    else:
                        print(f"{category_type} Request failed for page {page_number}")
                        break

           
            # Clear the model before adding new items
            self.model.clear()
            
            # Add channels to the model
            for channel in channels:
                channel_name = channel['name']
                item = QStandardItem(channel_name)
                self.model.appendRow(item)

            return channels
        
        except Exception as e:
            print(f"An error occurred while retrieving channels: {str(e)}")
            return []
    





class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MAC IPTV Player by MY-1 BETA")
        self.setGeometry(100, 100, 510, 500)

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

        self.playlist_view = QListView()

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

        




        

        self.playlist_view = QListView()
        layout.addWidget(self.playlist_view)
        self.playlist_view.doubleClicked.connect(self.on_playlist_selection_changed)
        

          # Create channel model and connect it to the list view
        self.channel_model = QStandardItemModel(self.playlist_view)
        self.playlist_view.setModel(self.channel_model)



     

        self.resizeEvent = self.on_resize

    def on_resize(self, event):
        # Implement the resizing logic here
        pass



        


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




    def show_error_message(self, message):
        QMessageBox.critical(self, "Error", message)




    def retrieve_channels(self, category_type, category_id):
        try:
            session = requests.Session()
            url = f"http://{self.hostname_input.text()}"
            mac_address = self.mac_input.text()
            token = self.request_thread.get_token(session, url, mac_address)
            
            if token:
                channels = self.request_thread.get_channels(session, url, mac_address, token, category_type, category_id)
                
                if channels:
                    self.playlist_model = QStandardItemModel(self.playlist_view)
                    self.playlist_view.setModel(self.playlist_model)
                    
                    for channel in channels:
                        channel_name = channel['name']
                        channel_url = channel['cmd']
                        list_item = QStandardItem(channel_name)
                        list_item.setData(channel_url, Qt.UserRole)  # Store the channel URL in item's data
                        self.playlist_model.appendRow(list_item)
                        
                    self.playlist_view.clicked.connect(self.on_playlist_item_clicked)  # Connect the clicked signal to a slot method
                
                    
            else:
                self.show_error_message("Failed to retrieve token.")
        except Exception as e:
            traceback.print_exc()
            self.show_error_message("An error occurred while retrieving channels.")

    def on_playlist_item_clicked(self, index):
        channel_item = self.playlist_model.itemFromIndex(index)
        channel_url = channel_item.data(Qt.UserRole)
        
        # Check if the channel_url starts with "ffmpeg " and remove it if it does
        if channel_url.startswith("ffmpeg "):
            channel_url = channel_url[len("ffmpeg "):]  # Remove "ffmpeg " prefix

        # Retrieve VLC executable path from your settings
        vlc_executable = self.settings.value("media_player", "")

        if vlc_executable:
            try:
                # Launch VLC with the channel's URL
                subprocess.Popen([vlc_executable, channel_url])
            except Exception as e:
                print(f"Error opening VLC player: {e}")
        else:
            print("VLC executable path not found in settings.")


            






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
        hostname = self.hostname_input.text()
        mac_address = self.mac_input.text()
        media_player = self.media_player_input.text()

        if not hostname or not mac_address or not media_player:
            QMessageBox.warning(self, "Warning", "Please enter the Hostname, MAC Address, and Media Player.")
            return

        self.request_thread = RequestThread(hostname, mac_address)
        self.request_thread.request_complete.connect(self.update_playlist)
        self.request_thread.start()

        


    def update_playlist(self, playlist):
        self.playlist_model = QStandardItemModel(self.playlist_view)
        self.playlist_view.setModel(self.playlist_model)

        # Add a "Go Back" item at the top
        go_back_item = QStandardItem("Go Back")
        self.playlist_model.appendRow(go_back_item)

        for item in playlist:
            name = item['name']

            category_type = item['category_type']
            category_id = item.get('genre_id') or item.get('category_id')
            list_item = QStandardItem(f"{category_type}: {name} (ID: {category_id})")
            list_item.setData(category_id, QtCore.Qt.UserRole)  # Store category ID as item data

            self.playlist_model.appendRow(list_item)






    def on_playlist_selection_changed(self, index):
        if index.isValid():
            item = self.playlist_model.itemFromIndex(index)
            item_text = item.text()

            if item_text == "Go Back":
                # Handle the "Go Back" action, e.g., return to the previous view
                self.return_to_previous_view()
            else:
                category_type = item_text.split(":")[0].strip()
                print("Selected Category Type:", category_type)
                category_id = item.data(QtCore.Qt.UserRole)
                print("Selected Category ID:", category_id)
                self.retrieve_channels(category_type, category_id)

                # Check if you are inside a category and add/remove "Go Back" accordingly
                if category_id:
                    go_back_item = QStandardItem("Go Back")
                    self.playlist_model.insertRow(0, go_back_item)

                    # Make an HTTP request to the specified URL
                    url = f"http://{self.hostname_input.text()}/portal.php?type=vod&action=create_link&cmd={category_id}&series=&forced_storage=&disable_ad=0&download=0&force_ch_link_check=0&JsHttpRequest=1-xml&mac={self.mac_input.text()}"
                    headers = {'Accept': 'application/json'}

                    try:
                        response = requests.get(url, headers=headers)
                        response.raise_for_status()

                        json_response = response.json()

                        # Extract the movie file URL from the response
                        cmd_value = json_response.get("js", {}).get("cmd")

                        if cmd_value:
                            # Extracting the movie file URL from the cmd value
                            movie_file_url = cmd_value.split(' ')[1]
                            print("Movie File URL:", movie_file_url)

                            # Retrieve VLC executable path from your settings
                            vlc_executable = self.settings.value("media_player", "")

                            if vlc_executable:
                                try:
                                    # Launch VLC with the movie file URL
                                    subprocess.Popen([vlc_executable, movie_file_url])
                                except Exception as e:
                                    print(f"Error opening VLC player: {e}")
                            else:
                                print("VLC executable path not found in settings.")
                        else:
                            print("Movie File URL not found in the response.")

                    except requests.exceptions.RequestException as e:
                        print(f"Error making request: {e}")

                else:
                    # Remove "Go Back" when not inside a category
                    self.playlist_model.removeRow(0)




            





    

    def play_channel(self, index):
        link = index.data(QtCore.Qt.UserRole)
        media_player = self.media_player_input.text()

        if link and media_player:
            try:
                # Launch VLC with the channel's URL
                subprocess.Popen([media_player, link])
            except Exception as e:
                traceback.print_exc()







if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


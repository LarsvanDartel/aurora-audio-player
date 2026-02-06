import os
import time
import socketio
from dotenv import load_dotenv
import vlc
import time
import requests
import logging
import traceback
import math
from threading import Thread
from urllib.parse import urljoin

load_dotenv()
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL)

namespace = '/audio'
running = True
sio = socketio.Client(logger=True)
player: vlc.MediaPlayer | None = None

sync_thread: Thread | None = None
sync_thread_running = False
last_audio_sync = 0

auth_cookie = ''
audio_listener_id = -1

status_thread: Thread | None = None
status_thread_running = False
start_time = 0
latency_ms = 0


def get_headers():
    global auth_cookie
    return {'cookie': 'connect.sid=' + auth_cookie}


def sync_audio_timings():
    global sync_thread_running, last_audio_sync

    while sync_thread_running:
        now = time.time()
        # Synchronize every 30 seconds if audio is playing
        if now - last_audio_sync >= 30 and player is not None and player.is_playing() and player.get_time() >= 0:
            last_audio_sync = now
            player_ms = player.get_time()
            now_ms = math.floor(time.time_ns() / 1000000)
            sio.emit('sync_audio_timings', {
                'startTime': now_ms,
                'timestamp': player_ms,
            }, namespace=namespace)

        time.sleep(0.1)


def create_sync_loop():
    global sync_thread, sync_thread_running

    sync_thread_running = True
    sync_thread = Thread(target=sync_audio_timings)
    sync_thread.daemon = True
    sync_thread.start()


def stop_sync_loop():
    global sync_thread, sync_thread_running

    if sync_thread is None:
        return

    sync_thread_running = False
    sync_thread.join()


def send_status_updates():
    global status_thread_running, sio

    while status_thread_running:
        uptime_seconds = int(time.time() - start_time)
        system_timestamp = math.floor(time.time_ns() / 1000000)
        last_send_time = system_timestamp

        def status_update_callback():
            global latency_ms
            callback_time = time.time_ns() / 1000000
            rtt = callback_time - last_send_time
            latency_ms = int(rtt / 2)
            logging.info(f"Latency: {latency_ms} ms")

        sio.emit('status:update', {
            'uptimeSeconds': uptime_seconds,
            'systemTimestamp': system_timestamp,
            'latencyMilliseconds': latency_ms,
        }, namespace='/', callback=status_update_callback)

        time.sleep(5)


def create_status_loop():
    global status_thread, status_thread_running

    status_thread_running = True
    status_thread = Thread(target=send_status_updates)
    status_thread.daemon = True
    status_thread.start()


def stop_status_loop():
    global status_thread, status_thread_running

    if status_thread is None:
        return

    status_thread_running = False
    status_thread.join()


def main():
    global sio, player, running, auth_cookie, audio_listener_id, start_time

    start_time = time.time()

    url = urljoin(os.environ['URL'], '/api/auth/key')
    result = requests.post(url, {'key': os.environ['API_KEY']})

    json = result.json()
    if result.status_code != 200:
        raise Exception("Could not authenticate with core: [HTTP {}]: {}".format(
            result.status_code,
            json['details'] if json['details'] else json['message']),
        )

    audio_listener_id = json['audioId']
    auth_cookie = result.cookies.get('connect.sid')

    # Initialize SocketIO
    sio.connect(os.environ['URL'], headers=get_headers,
                namespaces=['/', namespace])

    logging.info('Connected')

    try:
        while running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        running = False
        stop_audio()
        stop_status_loop()


def set_audio_playing(playing: bool):
    url = urljoin(os.environ['URL'], "/api/audio/{}/playing".format(audio_listener_id))
    try:
        requests.post(url, { 'playing': playing }, headers=get_headers())
    except Exception as e:
        logging.error(e)


@sio.event(namespace=namespace)
def play_audio(url: str, seconds=0):
    global player
    logging.info('receive play event')

    load_audio(url)

    if player is None:
        return

    if player.play() < 0:
        raise Exception('Could not start playback')

    if seconds is not None:
        skip_to(seconds)

    # Start a synchronization worker
    create_sync_loop()
    set_audio_playing(True)


@sio.event(namespace=namespace)
def stop_audio():
    global player

    logging.info('receive stop event')

    # Stop the synchronization thread
    stop_sync_loop()

    if player is not None and player.is_playing():
        player.pause()

    set_audio_playing(False)


@sio.event(namespace=namespace)
def skip_to(seconds):
    global player

    logging.info('receive skip event: ' + str(seconds))

    if player is None:
        return

    position = int(seconds * 1000)
    player.set_time(position)


def load_audio(url: str):
    global player

    if url.startswith('http'):
        full_url = url
    else:
        full_url = urljoin(os.environ['URL'], url)

    logging.info('load audio: ' + full_url)

    if player:
        player.stop()

    try:
        # creating a vlc instance
        vlc_instance: vlc.Instance = vlc.Instance()

        # creating a media player
        player = vlc_instance.media_player_new()

        # creating a media
        media: vlc.Media = vlc_instance.media_new(full_url)

        # setting media to the player
        player.set_media(media)

        logging.info('Audio file initialized!')
    except Exception as e:
        logging.error(traceback.format_exc())


@sio.event
def disconnect():
    global player

    if player:
        player.stop()
        player = None

    set_audio_playing(False)
    stop_status_loop()


@sio.event
def connect():
    create_status_loop()


if __name__ == '__main__':
    while running:
        try:
            main()
        except Exception as e:
            logging.error(traceback.format_exc())
            print('Something went wrong. Try again after 5 seconds...')
            time.sleep(5)


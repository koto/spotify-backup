#!/usr/bin/env python3
from mutagen.oggvorbis import OggVorbis
from mutagen.flac import Picture
import argparse
import logging
import json
from jsonpath_ng.ext import parse
import requests
import base64
import copy
from spotify_backup import SpotifyAPI, CLIENT_ID, SCOPE

logging.basicConfig(level=20, datefmt='%I:%M:%S',
                    format='[%(asctime)s] %(message)s')


class SpotifyInfo(object):
    def __init__(self):
        self.title = None
        self.artist = None
        self.album = None
        self.tracknumber = None
        self.image = None
        self.year = None
        self.spotify_id = None

    def __getitem__(self, name):
        return getattr(self, name)

    def __setitem__(self, name, value):
        return setattr(self, name, value)

    def __delitem__(self, name):
        return delattr(self, name)

    def __contains__(self, name):
        return hasattr(self, name)

    def __str__(self):
        return (', '.join("%s: %s" % item for item in vars(self).items()))


class Tagger(object):
    def __init__(self, cache, spotify):
        self.cache = cache
        self.spotify = spotify

    def get_spotify_info(self, spotify_id) -> SpotifyInfo:
        entry = None
        if spotify_id in self.cache:
            entry = self.cache[spotify_id]
        elif self.spotify:
            try:
                entry = self.spotify.get(f'tracks/{spotify_id}aa', tries=2)
            except Exception as err:
                logging.error(err)

        if entry is None:
            return None

        info = SpotifyInfo()
        info.spotify_id = spotify_id
        if len(entry['album']['images']) > 1:
            info.image = entry['album']['images'][1]
        elif len(entry['album']['images']) == 1:
            info.image = entry['album']['images'][0]
        info.tracknumber = str(entry['track_number'])
        info.album = entry['album']['name']
        info.year = entry['album']['release_date'][0:4]
        info.artist = [a['name'] for a in entry['artists']]
        info.title = entry['name']
        return info

    def write_tags(self, filename):
        logging.debug(f'Opening {filename}')
        ogg = OggVorbis(filename)
        logging.debug(f'Existing tags: {ogg.tags}')

        if 'spotify_id' not in ogg.tags:
            logging.info(f'No SPOTIFY_ID tag present in {filename}, skipping')
            return

        spotify_id = ogg.tags.get('spotify_id')[0]
        info = self.get_spotify_info(spotify_id)

        if info is None:
            logging.error(
                f'Cannot get spotify info for {spotify_id}, skipping {filename} for now')
            return

        changed = False
        for var in vars(info):
            if info[var] is None:
                continue
            if var == 'image':
                if not ogg.get('metadata_block_picture'):  # need to fetch the picture
                    logging.info(
                        f'Fetching {info.image["url"]} ({info.image["width"]}x{info.image["height"]})')
                    r = requests.get(info.image['url'])
                    picture = Picture()
                    picture.data = r.content
                    picture.description = "coverart"
                    picture.type = 3
                    picture.width = info.image['width']
                    picture.height = info.image['height']
                    picture.mime = r.headers.get('content-type')
                    picture_data = picture.write()
                    encoded_data = base64.b64encode(picture_data)
                    vcomment_value = encoded_data.decode("ascii")

                    changed = True
                    ogg["metadata_block_picture"] = [vcomment_value]
                continue

            arr = info[var] if isinstance(info[var], list) else [info[var]]
            if arr != ogg.get(var, []):
                logging.info(f"Setting {var} from {ogg.get(var)} to {arr}")
                changed = True
                ogg[var] = arr

        if changed:
            logging.info(f"Saving changes to {filename}")
            ogg.save()


def main():
    # Parse arguments.
    parser = argparse.ArgumentParser(
        description='Adds tags to .ogg files with the data taken from Spotify')
    parser.add_argument('--token', metavar='OAUTH_TOKEN',
                        help='use a Spotify OAuth token')
    parser.add_argument('--json', metavar="FILE",
                        help="spotify-backup json file (used to avoid querying Spotify API)")
    parser.add_argument('--offline', action=argparse.BooleanOptionalAction,
                        help="Don't query Spotify API (requires --json)")
    parser.add_argument('files', metavar='FILE', nargs='*',
                        help='files to write tags to')
    args = parser.parse_args()

    # Log into the Spotify API.
    spotify = None
    if not args.offline:
        if args.token:
            spotify = SpotifyAPI(args.token)
        else:
            spotify = SpotifyAPI.authorize(client_id=CLIENT_ID, scope=SCOPE)

    track_cache = {}
    if args.json:
        f = open(args.json, 'r')
        logging.info(f'Opening {args.json}')
        jsonobj = json.load(f)

        jsonpath_expression = parse("$..track where [id]")
        for match in jsonpath_expression.find(jsonobj):
            track_cache[match.value.get('id')] = match.value

        jsonpath_expression = parse("$..album where [tracks]")
        for match in jsonpath_expression.find(jsonobj):
            for track in match.value['tracks']['items']:
                t = copy.deepcopy(track)
                t['album'] = match.value
                track_cache[t.get('id')] = t

    tagger = Tagger(track_cache, spotify)

    for fname in args.files:
        tagger.write_tags(fname)


if __name__ == '__main__':
    main()

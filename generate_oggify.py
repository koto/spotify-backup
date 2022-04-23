#!/usr/bin/env python3
from enum import Enum
import argparse
from shlex import quote
import json
import logging
import os
import sys
import stat
import textwrap


logging.basicConfig(level=20, datefmt='%I:%M:%S',
                    format='[%(asctime)s] %(message)s')

parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                 description="Converts https://github.com/caseychu/spotify-backup file to a Bash script backing up the playlists / albums using https://github.com/pisto/oggify.")
parser.add_argument('json', metavar="JSON_INPUT_FILE",
                    help="spotify-backup json file")

parser.add_argument('bash', metavar="BASH_OUTPUT_FILE",
                    help="Bash file to write to")

parser.add_argument('filter', nargs='?', metavar="FILTER",
                    help='output only playlists/albums matching a filter (name substring or ID)')
parser.add_argument('-o', '--owner', metavar="SPOTIFY_LOGIN",
                    help='only output playlists owned by SPOTIFY_LOGIN')

parser.add_argument('-p', '--playlists',
                    action=argparse.BooleanOptionalAction, help='export playlists')
parser.add_argument(
    '-a', '--albums', action=argparse.BooleanOptionalAction, help='export albums')
parser.add_argument('-f', '--force', action=argparse.BooleanOptionalAction,
                    help='overwrite BASH_OUTPUT_FILE if it exists')
parser.add_argument('--oggify-bin',
                    default='oggify', help='oggify bin path')

args = parser.parse_args()

logging.info(f"Arguments: {args}")


class Folder(object):
    def __init__(self, name):
        self.name = name
        self.songs = []

    def add_song(self, spotify_id):
        if not spotify_id.startswith('spotify:track:'):
            return
        self.songs.append(spotify_id)

    def __repr__(self):
        return f'{self.name} ({len(self.songs)})'


def sanitize_filename(fn):
    validchars = "-_.()&'\"[],!+ "
    out = ""
    for c in fn:
        if str.isalpha(c) or str.isdigit(c) or (c in validchars):
            out += c
        else:
            if c == "–":
                out += "-"
            else:
                out += "_"
    return quote(out)


logging.info(f'Parsing {args.json}...')
with open(args.json, 'r') as f:
    jsonobj = json.load(f)

    folders = []

    things_to_export = []
    if args.playlists:
        logging.info(f'Exporting playlists')
        things_to_export += jsonobj['playlists']
    if args.albums:
        logging.info(f'Exporting albums')
        things_to_export += jsonobj['albums']

    for p in things_to_export:
        is_album = 'album' in p
        if is_album:
            folder_name = f"{', '.join([a['name'] for a in p['album']['artists']])} - {p['album']['name']} ({p['album']['release_date'][0:4]})"
        else:
            folder_name = p['name']

        if args.filter:
            if args.filter.lower() not in folder_name.lower() and ('id' not in p or p['id'] != args.filter):
                logging.info(f"Skipping {folder_name}")
                continue
        if args.owner:
            if 'owner' in p and p['owner']['id'] != args.owner:
                logging.info(f"Skipping {folder_name}")
                continue

        folder = Folder(folder_name)
        # playlist_data = [ p for p in playlist_data if p['owner']['id'] == me['id'] ]

        tracks = p['album']['tracks']['items'] if is_album else p['tracks']
        for t in tracks:
            folder.add_song(t['uri'] if 'uri' in t else t['track']['uri'])

        if len(folder.songs):
            folders.append(folder)

if len(folders) == 0:
    logging.error(
        "Nothing to export, make sure you used --playlists or --albums, and that --filter is correct")
    sys.exit(1)
else:
    logging.info(f"Exporting {len(folders)} folders.")

if os.path.isfile(args.bash) and not args.force:
    logging.error(f"{args.bash} exists, exiting. Use --force to overwrite")
    sys.exit(1)

# tag_ogg script from oggify. Creates a file with "artist - title.ogg" name, pipes to it, and writes some vorbis tags after.
# Crucially, the SPOTIFY_ID tag is written, that can later on be reused by add_spotify_tags.py
TAG_OGG = f"""#!/bin/bash
fname="${{4}} - ${{2}}.ogg"
fname="${{fname//\//-}}"
cat > "${{fname}}"
{{
	echo "SPOTIFY_ID=${{1}}"
	echo "TITLE=${{2//'\\n'/' '}}"
	echo "ALBUM=${{3//'\\n'/' '}}"
	shift 3
	for artist in "$@"; do
		echo "ARTIST=${{artist//'\\n'/' '}}"
	done
}} | vorbiscomment -a "${{fname}}"
echo "${{fname}}"
"""

with open(args.bash, 'w') as bash:
    print("#!/bin/bash", file=bash)
    print(textwrap.dedent(f"""\
        set -e # enable errexit option

        command -v vorbiscomment >/dev/null 2>&1 || {{ echo >&2 "vorbiscomment is required, but it's not installed.  Aborting."; exit 1; }}

        if [ -z "$SPOTIFY_USER" -o -z "$SPOTIFY_PASS" ]; then
            echo "SPOTIFY_USER and SPOTIFY_PASS envvars must be set"
            exit 1
        fi

        OGGIFY_BIN="${{1:-{quote(args.oggify_bin)}}}"

        echo "Creating tag_ogg script..."
        temp_file=$(mktemp)
        cat << 'EOF' > ${{temp_file}}"""), file=bash)
    print(TAG_OGG, file=bash)
    print(textwrap.dedent(f"""\
        EOF
        chmod u+x ${{temp_file}}

        # Returns spotify URL value for all .ogg files in current directory. 
        # Since oggify writes tags after downloading tracks, this represents all completed downloads.
        # Requires vorbiscomment from vorbis-tools.
        downloaded_tracks() {{
            if command -v vorbiscomment &> /dev/null; then
                for fname in *.ogg
                do
                    [ -f "$fname" ] || continue 
                    vorbiscomment -l "$fname" | grep SPOTIFY_ID | awk -F= ' {{print "spotify:track:" $2}}'
                done
            fi
        }}

        # Returns spotify URLs of files that were passed in stdin but are not already downloaded.
        # Used when the script resumes downloading.
        skip_downloaded() {{
            comm -13 <(downloaded_tracks | sort | uniq) <(cat /dev/stdin | sort | uniq)
        }}"""), file=bash)

    newline = "\n"
    for f in sorted(folders, key=lambda f: len(f.songs)):
        logging.info(f"Processing {f.name} (songs: {len(f.songs)})...")
        folder = sanitize_filename(f.name)
        song_ids = newline.join([id for id in f.songs])

        # Create all files in current directory, then move them to the target one.
        print(textwrap.dedent(f"""
            if [ ! -d {folder} ]; then
                echo Processing {folder} - {len(f.songs)} files:
                skip_downloaded <<< """) + f'"{song_ids}" | "$OGGIFY_BIN" "$SPOTIFY_USER" "$SPOTIFY_PASS" "$temp_file"'
              + textwrap.dedent(f"""
                if compgen -G "*.ogg" > /dev/null; then
                    mkdir {folder}
                    mv *.ogg {folder}
                fi
            fi"""), file=bash)

    print(textwrap.dedent("""\
        echo Cleaning up...
        rm ${temp_file}
        echo Done. Use \"add_spotify_tags.py --json spotify-backup.json ./**/*.ogg\" to add Spotify tags to the files.
        """), file=bash)

# chmod u+x to Bash file
os.chmod(args.bash, os.stat(args.bash).st_mode | stat.S_IXUSR)

logging.info(f"Generated {args.bash}.")
logging.info(
    f"Execute it like so: SPOTIFY_USER=foo SPOTIFY_PASS=bar {args.bash} [/path/to/oggify]")

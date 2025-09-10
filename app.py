from flask import Flask, render_template, redirect, request, session, url_for, jsonify, Response
from datetime import datetime
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import os
from flask_sqlalchemy import SQLAlchemy
from threading import Lock
import time
import json
import base64
import logging

logging.basicConfig(level=logging.DEBUG)

print("DEBUG - Starting app initialization...")

try:
    app = Flask(__name__)
    print("DEBUG - Flask app created successfully")
    
    app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here')
    print("DEBUG - Secret key set")
    
    # Your Spotify credentials
    CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', "3132130fb7504d2b80302528403d082a")
    CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', "d5d94ba238d84675bcabb28dabae49d4")
    REDIRECT_URI = os.environ.get('REDIRECT_URI', "http://127.0.0.1:5000/callback")
    print("DEBUG - Environment variables loaded")
    
except Exception as e:
    print(f"ERROR - Failed during app initialization: {e}")
    raise



# Your Spotify credentials
CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', "3132130fb7504d2b80302528403d082a")
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', "d5d94ba238d84675bcabb28dabae49d4")
REDIRECT_URI = os.environ.get('REDIRECT_URI', "http://127.0.0.1:5000/callback")
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here')
SCOPE = "playlist-modify-public playlist-read-private user-library-read user-library-modify user-read-playback-state"
# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///spotify_tags.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ADD THESE DEBUG LINES:
print(f"DEBUG - Database URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
print(f"DEBUG - Working directory: {os.getcwd()}")
print(f"DEBUG - App instance path: {app.instance_path}")


# Global cache storage (better than session for large data)
liked_songs_cache = {}
cache_lock = Lock()

caching_in_progress = {}
caching_lock = Lock()

caching_progress = {}

# Association table for many-to-many relationship between songs and tags
song_tags = db.Table('song_tags',
    db.Column('song_id', db.Integer, db.ForeignKey('song.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True)
)

# Database Models
class Song(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    spotify_id = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    artist = db.Column(db.String(200), nullable=False)
    album = db.Column(db.String(200), nullable=False)
    duration_ms = db.Column(db.Integer)
    # Manual song attributes (1=Very Low, 2=Low, 3=Medium, 4=High, 5=Very High)
    tempo = db.Column(db.Integer, default=None)  # Default to Medium
    energy = db.Column(db.Integer, default=None)  # Default to Medium
    mood = db.Column(db.Integer, default=None)  # Default to Chill/Neutral (Valence)
    # NEW: Spotify audio features
    spotify_tempo = db.Column(db.Float)  # BPM from Spotify
    spotify_energy = db.Column(db.Float)  # 0-1 energy from Spotify
    spotify_valence = db.Column(db.Float)  # 0-1 valence from Spotify
    # Many-to-many relationship with tags
    tags = db.relationship('Tag', secondary=song_tags, backref='songs')

class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    color = db.Column(db.String(7), default='#1db954')  # Hex color for tag display


@app.route('/health')
def health_check():
    return {'status': 'ok'}, 200


def save_song_to_db(spotify_track_data, sp=None):
    """Save song to database if it doesn't already exist"""
    existing_song = Song.query.filter_by(spotify_id=spotify_track_data['id']).first()
    
    if not existing_song:
        new_song = Song(
            spotify_id=spotify_track_data['id'],
            name=spotify_track_data['name'],
            artist=', '.join([artist['name'] for artist in spotify_track_data['artists']]),
            album=spotify_track_data['album']['name'],
            duration_ms=spotify_track_data['duration_ms']
        )
        db.session.add(new_song)
        db.session.commit()
        return new_song
    return existing_song


@app.route('/get-audio-features/<int:song_id>')
def get_audio_features(song_id):
    """Get audio features for a specific song on-demand"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    song = Song.query.get(song_id)
    if not song:
        return {'error': 'Song not found'}, 404
    
    # Return cached features if we have them
    if song.spotify_tempo is not None and song.spotify_energy is not None and song.spotify_valence is not None:
        return {
            'tempo': song.spotify_tempo,
            'energy': song.spotify_energy,
            'valence': song.spotify_valence,
            'cached': True
        }
    
    # Fetch from reccobeats API
    try:
        import requests
        url = "https://api.reccobeats.com/v1/audio-features"
        r = requests.get(url, params={"ids": song.spotify_id}, timeout=15)
        data = r.json()
        features = data.get("content", [])
        
        if features:
            f = features[0]
            song.spotify_tempo = f.get("tempo")
            song.spotify_energy = f.get("energy")
            song.spotify_valence = f.get("valence")
            db.session.commit()
            
            return {
                'tempo': song.spotify_tempo,
                'energy': song.spotify_energy,
                'valence': song.spotify_valence,
                'cached': False
            }
        else:
            return {'error': 'No features found'}
            
    except Exception as e:
        print(f"Error fetching audio features for song {song_id}: {e}")
        return {'error': str(e)}
    


# Helper function to get or create a tag
def get_or_create_tag(tag_name):
    """Get existing tag or create new one"""
    tag = Tag.query.filter_by(name=tag_name).first()
    if not tag:
        tag = Tag(name=tag_name)
        db.session.add(tag)
        db.session.commit()
    return tag



@app.route('/update-song-attributes', methods=['POST'])
def update_song_attributes():
    """Update tempo, energy, and mood attributes for a song"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    song_id = request.form.get('song_id')
    tempo = request.form.get('tempo')
    energy = request.form.get('energy')
    mood = request.form.get('mood')
    
    print(f"DEBUG - Updating song {song_id}: tempo={tempo}, energy={energy}, mood={mood}")
    
    if not song_id:
        return {'error': 'Song ID required'}, 400
    
    song = Song.query.get(song_id)
    if not song:
        return {'error': 'Song not found'}, 404
    
    # Update attributes if provided
    if tempo is not None:
        song.tempo = int(tempo)
    if energy is not None:
        song.energy = int(energy)
    if mood is not None:
        song.mood = int(mood)
    
    db.session.commit()
    print(f"DEBUG - Song attributes updated successfully")
    
    return {'success': True, 'tempo': song.tempo, 'energy': song.energy, 'mood': song.mood}



@app.route('/get-song-attributes/<int:song_id>')
def get_song_attributes(song_id):
    """Get current attributes for a specific song"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    song = Song.query.get(song_id)
    if not song:
        return {'error': 'Song not found'}, 404
    
    return {
        'tempo': song.tempo,
        'energy': song.energy,
        'mood': song.mood
    }


@app.route('/')
def home():
    """Main page - redirect to tag liked songs as default"""
    if 'token_info' not in session:
        return redirect(url_for('login'))
    
    return redirect(url_for('tag_liked_songs'))


@app.route('/login')
def login():
    sp_oauth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE
    )
    auth_url = sp_oauth.get_authorize_url()
    return redirect(auth_url)



@app.route('/callback')
def callback():
    sp_oauth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE
    )
    session.clear()
    code = request.args.get('code')
    token_info = sp_oauth.get_access_token(code)
    session['token_info'] = token_info
    return redirect(url_for('spotify'))




@app.route('/playlist/<playlist_id>')
def view_playlist(playlist_id):
    # Check if user is logged in
    if 'token_info' not in session:
        return redirect(url_for('login'))
    
    try:
        # Create Spotify client with user's token
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        # Get playlist details
        playlist = sp.playlist(playlist_id)
        
        # Process the playlist description
        playlist_description = playlist.get('description', '')
        human_readable_description = ''
        
        if playlist_description:
            # Check if this is a smart playlist with embedded criteria
            if '[ST:' in playlist_description:
                # Extract only the human-readable part (before the [ST: marker)
                human_readable_description = playlist_description.split('[ST:')[0].strip()
                print(f"DEBUG - Smart playlist detected, showing human description: '{human_readable_description}'")
            else:
                # Regular playlist, show full description
                human_readable_description = playlist_description
                print(f"DEBUG - Regular playlist, showing full description: '{human_readable_description}'")
        
        # Get all tracks from the playlist (handle pagination)
        tracks = []
        results = sp.playlist_tracks(playlist_id)
        
        while results:
            for item in results['items']:
                
                # Save song to database
                saved_song = save_song_to_db(item['track'], sp)

                if item['track']:  # Check if track exists (sometimes it can be null)
                    track_info = {
                        'name': item['track']['name'],
                        'artist': ', '.join([artist['name'] for artist in item['track']['artists']]),
                        'album': item['track']['album']['name'],
                        'duration_ms': item['track']['duration_ms'],
                        'url': item['track']['external_urls']['spotify'],
                        'id': item['track']['id'],
                        'db_id': saved_song.id,  # Add database ID for tag operations
                        'tags': saved_song.tags  # Add current tags for display
                    }
                    tracks.append(track_info)
            
            # Get next batch if available
            if results['next']:
                results = sp.next(results)
            else:
                break
        
        # Convert duration from milliseconds to minutes:seconds format
        for track in tracks:
            duration_ms = track['duration_ms']
            minutes = duration_ms // 60000
            seconds = (duration_ms % 60000) // 1000
            track['duration'] = f"{minutes}:{seconds:02d}"
        
        # Get user's playlists again for the sidebar
        playlist_list = []
        playlists = sp.current_user_playlists(limit=50, offset=0)
        
        while playlists:
            for pl in playlists['items']:
                playlist_info = {
                    'name': pl['name'],
                    'track_count': pl['tracks']['total'],
                    'id': pl['id']  # We need the ID now for linking
                }
                playlist_list.append(playlist_info)
            
            if playlists['next']:
                playlists = sp.next(playlists)
            else:
                break
        
        return render_template('spotify.html', 
                             authenticated=True, 
                             playlists=playlist_list,
                             selected_playlist=playlist,
                             playlist_description=human_readable_description,
                             tracks=tracks)
        
    except Exception as e:
        # Handle error - redirect back to spotify page with error
        return redirect(url_for('spotify'))


@app.route('/add-tag', methods=['POST'])
def add_tag():
    """Add a tag to a song"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    song_id = request.form.get('song_id')  # Database song ID
    tag_name = request.form.get('tag_name').strip()
    
    # Debug prints
    print(f"DEBUG - song_id: {song_id}")
    print(f"DEBUG - tag_name: '{tag_name}'")
    
    if not song_id or not tag_name:
        print("DEBUG - Missing song_id or tag_name")
        # Check if this is an AJAX request
        if request.headers.get('Content-Type') == 'application/x-www-form-urlencoded':
            return jsonify({'error': 'Missing song_id or tag_name'}), 400
        return redirect(request.referrer)
    
    # Get the song from database
    song = Song.query.get(song_id)
    print(f"DEBUG - Found song: {song.name if song else 'None'}")
    
    if not song:
        print("DEBUG - Song not found in database")
        if request.headers.get('Content-Type') == 'application/x-www-form-urlencoded':
            return jsonify({'error': 'Song not found'}), 404
        return redirect(request.referrer)
    
    # Get or create the tag
    tag = get_or_create_tag(tag_name)
    print(f"DEBUG - Tag: {tag.name}, ID: {tag.id}")
    
    # Add tag to song if not already there
    if tag not in song.tags:
        song.tags.append(tag)
        db.session.commit()
        print(f"DEBUG - Tag added successfully! Song now has {len(song.tags)} tags")
    else:
        print("DEBUG - Tag already exists on this song")
    
    # Return JSON for AJAX requests
    if request.headers.get('Content-Type') == 'application/x-www-form-urlencoded':
        return jsonify({'success': True, 'tag_id': tag.id, 'tag_name': tag.name})
    
    return redirect(request.referrer)  # Fallback for non-AJAX


@app.route('/remove-tag', methods=['POST'])
def remove_tag():
    """Remove a tag from a song"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    song_id = request.form.get('song_id')  # Database song ID
    tag_id = request.form.get('tag_id')
    
    if not song_id or not tag_id:
        if request.headers.get('Content-Type') == 'application/x-www-form-urlencoded':
            return jsonify({'error': 'Missing song_id or tag_id'}), 400
        return redirect(request.referrer)
    
    # Get the song and tag from database
    song = Song.query.get(song_id)
    tag = Tag.query.get(tag_id)
    
    if song and tag and tag in song.tags:
        song.tags.remove(tag)
        db.session.commit()
        print(f"DEBUG - Removed tag {tag.name} from song {song.name}")
    
    # Return JSON for AJAX requests
    if request.headers.get('Content-Type') == 'application/x-www-form-urlencoded':
        return jsonify({'success': True})
    
    return redirect(request.referrer)  # Fallback for non-AJAX




@app.route('/get-recent-tags')
def get_recent_tags():
    """Get all available tags ordered by most recent first"""
    if 'token_info' not in session:
        return {'tags': []}
    
    # Get ALL tags, ordered by ID (most recent first) - removed limit
    recent_tags = Tag.query.order_by(Tag.id.desc()).all()
    
    tag_data = [{'id': tag.id, 'name': tag.name, 'color': tag.color} for tag in recent_tags]
    
    # Debug print
    print(f"DEBUG - Found {len(tag_data)} tags: {[t['name'] for t in tag_data]}")
    
    return {'tags': tag_data}



@app.route('/get-song-tags/<int:song_id>')
def get_song_tags(song_id):
    """Get current tags for a specific song"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    song = Song.query.get(song_id)
    if not song:
        return {'error': 'Song not found'}, 404
    
    # Return song tags in the same format as the template expects
    tags_data = [{'id': tag.id, 'name': tag.name, 'color': tag.color} for tag in song.tags]
    
    return {'tags': tags_data}


@app.route('/tag-liked-songs')
def tag_liked_songs():
    """Show liked songs tagging within main spotify page"""
    if 'token_info' not in session:
        return redirect(url_for('login'))
    
    try:
        # Check if token needs refreshing
        if is_token_expired(session['token_info']):
            print("DEBUG - Token expired, attempting refresh")
            session['token_info'] = refresh_access_token(session['token_info'])

        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        # Get parameters
        offset = int(request.args.get('offset', 0))
        untagged_only = request.args.get('untagged_only', 'false') == 'true'
        
        print(f"DEBUG - Starting liked songs at offset {offset}, untagged_only: {untagged_only}")
        
        # Get user's playlists for the sidebar (same as main spotify page)
        playlist_list = []
        try:
            user_id = sp.current_user()['id']
            playlists = sp.current_user_playlists(limit=50, offset=0)
            
            while playlists:
                for playlist in playlists['items']:
                    # Only include playlists owned by the user
                    if playlist['owner']['id'] == user_id:
                        playlist_info = {
                            'name': playlist['name'],
                            'track_count': playlist['tracks']['total'],
                            'id': playlist['id']
                        }
                        playlist_list.append(playlist_info)
                
                if playlists['next']:
                    playlists = sp.next(playlists)
                else:
                    break
                    
            print(f"DEBUG - Loaded {len(playlist_list)} playlists for sidebar")
        except Exception as playlist_error:
            print(f"ERROR - Failed to load playlists: {playlist_error}")
            playlist_list = []
        
        # Find the current song logic (same as before)
        current_position = offset
        songs_examined = 0
        max_search = 200
        
        while songs_examined < max_search:
            results = sp.current_user_saved_tracks(limit=50, offset=current_position)
            
            if not results['items']:
                # No more songs
                return render_template('spotify.html',
                                     authenticated=True,
                                     playlists=playlist_list,
                                     liked_songs_mode=True,
                                     no_more_songs=True,
                                     offset=offset,
                                     untagged_only=untagged_only)
            
            # Look through this batch
            for i, item in enumerate(results['items']):
                if item['track']:
                    actual_position = current_position + i
                    songs_examined += 1
                    
                    print(f"DEBUG - Examining position {actual_position}: {item['track']['name']}")
                    
                    saved_song = save_song_to_db(item['track'])
                    
                    if untagged_only and len(saved_song.tags) > 0:
                        print(f"DEBUG - Skipping tagged song at position {actual_position}")
                        continue
                    
                    print(f"DEBUG - Found matching song at position {actual_position}")
                    
                    # Format the song data
                    duration_ms = item['track']['duration_ms']
                    minutes = duration_ms // 60000
                    seconds = (duration_ms % 60000) // 1000
                    duration_formatted = f"{minutes}:{seconds:02d}"
                    
                    current_song = {
                        'name': item['track']['name'],
                        'artist': ', '.join([artist['name'] for artist in item['track']['artists']]),
                        'album': item['track']['album']['name'],
                        'duration': duration_formatted,
                        'id': item['track']['id'],
                        'db_id': saved_song.id,
                        'tags': saved_song.tags,
                        'actual_position': actual_position
                    }
                    
                    return render_template('spotify.html',
                                         authenticated=True,
                                         playlists=playlist_list,
                                         liked_songs_mode=True,
                                         current_song=current_song,
                                         offset=actual_position,
                                         untagged_only=untagged_only,
                                         no_more_songs=False)
            
            # No matching song in this batch, get next batch
            if results['next']:
                current_position += 50
            else:
                break
        
        # If we get here, no matching songs found
        return render_template('spotify.html',
                             authenticated=True,
                             playlists=playlist_list,
                             liked_songs_mode=True,
                             no_more_songs=True,
                             offset=offset,
                             untagged_only=untagged_only)
        
    except Exception as e:
        print(f"Error in tag_liked_songs: {e}")
        
        # If it's a token issue, redirect to login
        if "401" in str(e) or "token" in str(e).lower() or "expired" in str(e).lower():
            print("DEBUG - Token issue detected, clearing session and redirecting to login")
            session.clear()
            return redirect(url_for('login'))
        
        # For other errors, redirect to main spotify page (but this might cause a loop)
        return redirect(url_for('login'))
    

def is_token_expired(token_info):
    """Check if the access token is expired"""
    now = int(time.time())
    return token_info['expires_at'] - now < 60  # Refresh if less than 60 seconds left

def refresh_access_token(token_info):
    """Refresh the access token"""
    import requests
    
    refresh_token = token_info['refresh_token']
    
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()
    
    headers = {
        'Authorization': f'Basic {auth_b64}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token
    }
    
    response = requests.post('https://accounts.spotify.com/api/token', headers=headers, data=data)
    
    if response.status_code == 200:
        new_token_info = response.json()
        new_token_info['expires_at'] = int(time.time()) + new_token_info['expires_in']
        new_token_info['refresh_token'] = refresh_token  # Keep the original refresh token
        print("DEBUG - Token refreshed successfully")
        return new_token_info
    else:
        print(f"ERROR - Token refresh failed: {response.status_code}")
        raise Exception("Token refresh failed")
    

@app.route('/delete-tag', methods=['POST'])
def delete_tag():
    """Delete a tag and remove it from all songs"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    tag_id = request.form.get('tag_id')
    
    if not tag_id:
        return {'error': 'Tag ID required'}, 400
    
    try:
        tag = Tag.query.get(tag_id)
        if not tag:
            return {'error': 'Tag not found'}, 404
        
        tag_name = tag.name
        print(f"DEBUG - Deleting tag: {tag_name} (ID: {tag_id})")
        
        # Remove tag from all songs (the many-to-many relationship handles this)
        # Clear all associations first
        tag.songs.clear()
        
        # Delete the tag itself
        db.session.delete(tag)
        db.session.commit()
        
        print(f"DEBUG - Successfully deleted tag: {tag_name}")
        return {'success': True, 'message': f'Tag "{tag_name}" deleted successfully'}
        
    except Exception as e:
        print(f"ERROR - Failed to delete tag: {e}")
        db.session.rollback()
        return {'error': f'Failed to delete tag: {str(e)}'}, 500
    


@app.route('/cache-liked-songs')
def cache_liked_songs():
    """Pre-load and cache all liked songs for fast searching"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    # Use session-based cache key instead of token hash
    if 'cache_key' not in session:
        user_token = session['token_info']['access_token']
        session['cache_key'] = f"user_{hash(user_token[:20]) % 10000}_{int(time.time() / 3600)}"  # Stable for 1 hour
    
    cache_key = session['cache_key']
    print(f"DEBUG - Using cache key: {cache_key}")
    
    # Check if caching is already in progress for this user
    with caching_lock:
        if cache_key in caching_in_progress:
            print(f"DEBUG - Caching already in progress for user {cache_key}")
            return {
                'status': 'caching_in_progress', 
                'message': 'Cache operation already running',
                'count': 0,
                'total_songs': 0,
                'total_untagged': 0
            }
        
        # Mark caching as in progress
        caching_in_progress[cache_key] = True
        print(f"DEBUG - Starting cache operation for user {cache_key}")
    
    try:
        # Check if already cached and not too old (cache for 1 hour)
        with cache_lock:
            if cache_key in liked_songs_cache:
                cache_time = liked_songs_cache[cache_key].get('cache_time', 0)
                current_time = time.time()
                if current_time - cache_time < 3600:  # 1 hour cache
                    cache_data = liked_songs_cache[cache_key]
                    print(f"DEBUG - Using existing cache for user {cache_key}")
                    return {
                        'status': 'already_cached', 
                        'count': len(cache_data['songs']),
                        'total_songs': cache_data.get('total_songs', 0),
                        'total_untagged': cache_data.get('total_untagged', 0)
                    }
        
        # Start fresh caching
        user_token = session['token_info']['access_token']
        sp = spotipy.Spotify(auth=user_token)
        
        print("DEBUG - Starting to cache liked songs...")
        cached_songs = []
        offset = 0
        limit = 50
        
        while True:
            batch = sp.current_user_saved_tracks(limit=limit, offset=offset)
            
            if not batch['items']:
                break
            
            for i, item in enumerate(batch['items']):
                if item['track']:
                    track = item['track']
                    
                    # Save to database to get db_id
                    saved_song = save_song_to_db(track)
                    
                    cached_song = {
                        'name': track['name'],
                        'artist': ', '.join([artist['name'] for artist in track['artists']]),
                        'album': track['album']['name'],
                        'spotify_id': track['id'],
                        'db_id': saved_song.id,
                        'position': offset + i,
                        'tags': [{'id': tag.id, 'name': tag.name, 'color': tag.color} for tag in saved_song.tags],
                        'search_text': (track['name'] + ' ' + ', '.join([artist['name'] for artist in track['artists']])).lower()
                    }
                    cached_songs.append(cached_song)
            
            print(f"DEBUG - Cached {len(cached_songs)} songs so far...")
            
            if batch['next']:
                offset += limit
            else:
                break
        
        # Calculate totals for progress tracking
        total_songs = len(cached_songs)
        total_untagged = sum(1 for song in cached_songs if len(song['tags']) == 0)
        
        # Store in global cache
        with cache_lock:
            liked_songs_cache[cache_key] = {
                'songs': cached_songs,
                'cache_time': time.time(),
                'total_songs': total_songs,
                'total_untagged': total_untagged
            }
        
        print(f"DEBUG - Finished caching {len(cached_songs)} liked songs ({total_untagged} untagged)")
        return {
            'status': 'cached', 
            'count': len(cached_songs),
            'total_songs': total_songs,
            'total_untagged': total_untagged
        }
        
    except Exception as e:
        print(f"Error caching liked songs: {e}")
        return {'error': str(e)}, 500
        
    finally:
        # Always remove from in-progress tracker when done (success or failure)
        with caching_lock:
            if cache_key in caching_in_progress:
                del caching_in_progress[cache_key]
                print(f"DEBUG - Removed cache progress tracker for user {cache_key}")

@app.route('/get-cache-progress')
def get_cache_progress():
    """Get current caching progress"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    cache_key = session.get('cache_key')
    if not cache_key:
        return {'status': 'no_cache_key'}
    
    if cache_key in caching_progress:
        progress = caching_progress[cache_key]
        return {
            'status': progress['status'],
            'songs_cached': progress['songs_cached']
        }
    else:
        return {'status': 'not_caching'}


@app.route('/search-cached-liked-songs')
def search_cached_liked_songs():
    """Fast search through cached liked songs"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    # Get cache key from session (same as caching route)
    cache_key = session.get('cache_key')
    if not cache_key:
        print("DEBUG - No cache key in session")
        return {'error': 'No cache key. Please load cache first.'}, 400
    
    print(f"DEBUG - Searching with cache key: {cache_key}")
    
    # Check if we have cached data
    with cache_lock:
        if cache_key not in liked_songs_cache:
            print(f"DEBUG - No cached data for key: {cache_key}")
            return {'error': 'No cached data. Please load cache first.'}, 400
        
        cache_data = liked_songs_cache[cache_key]
        cached_songs = cache_data['songs']
        total_songs = cache_data.get('total_songs', 0)
        total_untagged = cache_data.get('total_untagged', 0)
    
    query = request.args.get('q', '').strip().lower()
    print(f"DEBUG - Searching cached data for: '{query}'")
    
    if not query or len(query) < 2:
        return {'results': []}
    
    print(f"DEBUG - Searching through {len(cached_songs)} cached songs")
    
    results = []
    start_time = time.time()
    
    # Fast search through cached data
    for song in cached_songs:
        if query in song['search_text']:
            results.append({
                'name': song['name'],
                'artist': song['artist'],
                'album': song['album'],
                'spotify_id': song['spotify_id'],
                'db_id': song['db_id'],
                'position': song['position'],
                'tags': song['tags']
            })
            
            if len(results) >= 20:  # Limit results
                break
    
    end_time = time.time()
    search_duration = end_time - start_time
    print(f"DEBUG - Found {len(results)} results in {search_duration:.3f} seconds")
    
    return {
        'results': results, 
        'search_time': search_duration, 
        'cached_songs_count': len(cached_songs),
        'total_songs': total_songs,
        'total_untagged': total_untagged
    }



@app.route('/next-liked-song')
def next_liked_song():
    """Move to next liked song"""
    current_offset = int(request.args.get('offset', 0))
    untagged_only = request.args.get('untagged_only', 'false') == 'true'  # Convert to boolean like main route
    
    print(f"DEBUG Next - current: {current_offset}, untagged_only: {untagged_only}")
    
    # Start searching from next position
    search_offset = current_offset + 1
    
    # If untagged_only is False, just go to next position (no filtering)
    if not untagged_only:
        return redirect(f'/tag-liked-songs?offset={search_offset}&untagged_only=false')
    
    # If untagged_only is True, search forward for the next untagged song
    max_search = 200
    songs_checked = 0
    
    while songs_checked < max_search:
        try:
            sp = spotipy.Spotify(auth=session['token_info']['access_token'])
            
            results = sp.current_user_saved_tracks(limit=1, offset=search_offset)
            
            if not results['items']:
                # No more songs
                return redirect(f'/tag-liked-songs?offset={search_offset}&untagged_only=true')
                
            item = results['items'][0]
            if item['track']:
                saved_song = save_song_to_db(item['track'])
                
                # If this song is untagged, use it
                if len(saved_song.tags) == 0:
                    print(f"DEBUG - Found next untagged song at offset {search_offset}")
                    return redirect(f'/tag-liked-songs?offset={search_offset}&untagged_only=true')
            
            search_offset += 1
            songs_checked += 1
            
        except Exception as e:
            print(f"Error checking offset {search_offset}: {e}")
            return redirect(f'/tag-liked-songs?offset={search_offset}&untagged_only=true')
    
    # If we've searched too many songs, just go to the next position
    return redirect(f'/tag-liked-songs?offset={search_offset}&untagged_only=true')



@app.route('/prev-liked-song')
def prev_liked_song():
    """Move to previous liked song"""
    current_offset = int(request.args.get('offset', 0))
    untagged_only = request.args.get('untagged_only', 'false') == 'true'  # Convert to boolean
    
    print(f"DEBUG Prev - current: {current_offset}, untagged_only: {untagged_only}")
    
    if current_offset <= 0:
        # Already at the beginning
        return redirect(f'/tag-liked-songs?offset=0&untagged_only=false')
    
    # Start from current position - 1 and work backwards
    search_offset = current_offset - 1
    
    # If untagged_only is False, just go back one position (no filtering)
    if not untagged_only:
        return redirect(f'/tag-liked-songs?offset={search_offset}&untagged_only=false')
    
    # If untagged_only is True, search backwards for the previous untagged song
    while search_offset >= 0:
        try:
            sp = spotipy.Spotify(auth=session['token_info']['access_token'])
            
            results = sp.current_user_saved_tracks(limit=1, offset=search_offset)
            
            if not results['items']:
                search_offset -= 1
                continue
                
            item = results['items'][0]
            if item['track']:
                saved_song = save_song_to_db(item['track'])
                
                # If this song is untagged, use it
                if len(saved_song.tags) == 0:
                    print(f"DEBUG - Found previous untagged song at offset {search_offset}")
                    return redirect(f'/tag-liked-songs?offset={search_offset}&untagged_only=true')
            
            search_offset -= 1
            
        except Exception as e:
            print(f"Error checking offset {search_offset}: {e}")
            search_offset -= 1
    
    # If we get here, no previous untagged song found, go to beginning
    return redirect(f'/tag-liked-songs?offset=0&untagged_only=true')



@app.route('/filter-liked-songs')
def filter_liked_songs():
    """Filter liked songs based on attribute ranges and selected tags"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    print("DEBUG - Starting filter_liked_songs route")
    
    try:
        # Get filter parameters
        tempo_min = int(request.args.get('tempo_min', 1))
        tempo_max = int(request.args.get('tempo_max', 5))
        energy_min = int(request.args.get('energy_min', 1))
        energy_max = int(request.args.get('energy_max', 5))
        mood_min = int(request.args.get('mood_min', 1))
        mood_max = int(request.args.get('mood_max', 5))
        include_tag_ids_str = request.args.get('include_tag_ids', '')
        exclude_tag_ids_str = request.args.get('exclude_tag_ids', '')
        
        print(f"DEBUG - Filter criteria: Tempo {tempo_min}-{tempo_max}, Energy {energy_min}-{energy_max}, Mood {mood_min}-{mood_max}, Include Tag IDs: '{include_tag_ids_str}', Exclude Tag IDs: '{exclude_tag_ids_str}'")

        # Parse tag IDs
        include_tag_ids = []
        exclude_tag_ids = []
        
        if include_tag_ids_str:
            include_tag_ids = [int(id.strip()) for id in include_tag_ids_str.split(',') if id.strip()]
        if exclude_tag_ids_str:
            exclude_tag_ids = [int(id.strip()) for id in exclude_tag_ids_str.split(',') if id.strip()]

        print(f"DEBUG - Parsed include tag IDs: {include_tag_ids}")
        print(f"DEBUG - Parsed exclude tag IDs: {exclude_tag_ids}")
        
        # Get user's liked songs from cache
        cache_key = session.get('cache_key')
        if not cache_key or cache_key not in liked_songs_cache:
            print("DEBUG - No cached liked songs, need to load cache first")
            return jsonify({'error': 'Please load your liked songs cache first (search box should trigger this)'}), 400
        
        with cache_lock:
            cached_songs = liked_songs_cache[cache_key]['songs']
        
        print(f"DEBUG - Found {len(cached_songs)} cached liked songs")
        
        # Filter songs by attributes and tags
        filtered_songs = []
        
        for cached_song in cached_songs:
            # Get the full song data from database
            song = Song.query.get(cached_song['db_id'])
            if not song:
                continue
            
            # Handle NULL attributes - skip songs that haven't been set yet
            if song.tempo is None or song.energy is None or song.mood is None:
                print(f"DEBUG - Skipping song {song.name} - has NULL attributes")
                continue
            
            # Check attribute ranges (now safe since we excluded NULLs)
            if song.tempo < tempo_min or song.tempo > tempo_max:
                continue
            if song.energy < energy_min or song.energy > energy_max:
                continue
            if song.mood < mood_min or song.mood > mood_max:
                continue
            
            # Get song's tag IDs
            song_tag_ids = {tag.id for tag in song.tags}
            
            # Check include tags: song must have ALL selected include tags
            if include_tag_ids:
                required_include_tag_ids = set(include_tag_ids)
                if not required_include_tag_ids.issubset(song_tag_ids):
                    continue
            
            # Check exclude tags: song must NOT have ANY selected exclude tags
            if exclude_tag_ids:
                excluded_tag_ids = set(exclude_tag_ids)
                if excluded_tag_ids.intersection(song_tag_ids):
                    continue
            
            # Song matches all criteria
            filtered_songs.append({
                'id': song.id,
                'spotify_id': song.spotify_id,
                'name': song.name,
                'artist': song.artist,
                'tempo': song.tempo,
                'energy': song.energy,
                'mood': song.mood,
                'tags': [{'id': tag.id, 'name': tag.name} for tag in song.tags]
            })
        
        print(f"DEBUG - Filtered to {len(filtered_songs)} songs matching criteria")
        
        return jsonify({
            'success': True,
            'songs': filtered_songs,
            'count': len(filtered_songs)
        })
        
    except Exception as e:
        print(f"ERROR - Exception in filter_liked_songs: {str(e)}")
        import traceback
        print(f"ERROR - Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500
    
    

@app.route('/import-all-songs-to-liked', methods=['POST'])
def import_all_songs_to_liked():
    """Import all songs from all playlists to Liked Songs"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        print("DEBUG - Starting import all songs to liked songs")
        
        # Get all user's playlists
        all_playlist_tracks = []
        playlists = sp.current_user_playlists(limit=50, offset=0)
        
        total_playlists = 0
        while playlists:
            total_playlists += len(playlists['items'])
            
            for playlist in playlists['items']:
                # Skip playlists we don't own (collaborative playlists we can't modify)
                if playlist['owner']['id'] != sp.current_user()['id']:
                    continue
                    
                print(f"DEBUG - Processing playlist: {playlist['name']}")
                
                # Get all tracks from this playlist
                tracks = sp.playlist_tracks(playlist['id'])
                
                while tracks:
                    for item in tracks['items']:
                        if item['track'] and item['track']['id']:
                            all_playlist_tracks.append({
                                'id': item['track']['id'],
                                'name': item['track']['name'],
                                'artist': ', '.join([artist['name'] for artist in item['track']['artists']])
                            })
                    
                    if tracks['next']:
                        tracks = sp.next(tracks)
                    else:
                        break
            
            if playlists['next']:
                playlists = sp.next(playlists)
            else:
                break
        
        # Remove duplicates (same song in multiple playlists)
        unique_tracks = {}
        for track in all_playlist_tracks:
            unique_tracks[track['id']] = track
        
        all_unique_tracks = list(unique_tracks.values())
        print(f"DEBUG - Found {len(all_unique_tracks)} unique songs across all playlists")
        
        # Get current liked songs to avoid duplicates
        print("DEBUG - Getting current liked songs")
        liked_track_ids = set()
        liked_songs = sp.current_user_saved_tracks(limit=50, offset=0)
        
        while liked_songs:
            for item in liked_songs['items']:
                if item['track']:
                    liked_track_ids.add(item['track']['id'])
            
            if liked_songs['next']:
                liked_songs = sp.next(liked_songs)
            else:
                break
        
        print(f"DEBUG - Found {len(liked_track_ids)} existing liked songs")
        
        # Find songs that need to be added
        songs_to_add = [track for track in all_unique_tracks if track['id'] not in liked_track_ids]
        print(f"DEBUG - Need to add {len(songs_to_add)} new songs to liked songs")
        
        # Add songs in batches (Spotify allows up to 50 tracks per request)
        added_count = 0
        batch_size = 50
        
        for i in range(0, len(songs_to_add), batch_size):
            batch = songs_to_add[i:i + batch_size]
            track_ids = [track['id'] for track in batch]
            
            try:
                sp.current_user_saved_tracks_add(tracks=track_ids)
                added_count += len(track_ids)
                print(f"DEBUG - Added batch of {len(track_ids)} songs. Total added: {added_count}")
                
                # Small delay to be nice to Spotify's servers
                time.sleep(0.1)
                
            except Exception as batch_error:
                print(f"ERROR - Failed to add batch: {batch_error}")
                continue
        
        print(f"DEBUG - Import complete. Added {added_count} songs to liked songs")
        
        return jsonify({
            'success': True,
            'total_playlist_songs': len(all_unique_tracks),
            'already_liked': len(liked_track_ids),
            'newly_added': added_count,
            'message': f'Successfully added {added_count} songs to your Liked Songs'
        })
        
    except Exception as e:
        print(f"ERROR - Import all songs failed: {str(e)}")
        import traceback
        print(f"ERROR - Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500
    

@app.route('/get-user-playlists')
def get_user_playlists():
    """Get user's playlists for the import modal"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        user_id = sp.current_user()['id']
        
        playlist_list = []
        playlists = sp.current_user_playlists(limit=50, offset=0)
        
        while playlists:
            for playlist in playlists['items']:
                # Only include playlists owned by the user
                if playlist['owner']['id'] == user_id:
                    playlist_info = {
                        'name': playlist['name'],
                        'track_count': playlist['tracks']['total'],
                        'id': playlist['id']
                    }
                    playlist_list.append(playlist_info)
            
            if playlists['next']:
                playlists = sp.next(playlists)
            else:
                break
        
        return jsonify({
            'success': True,
            'playlists': playlist_list
        })
        
    except Exception as e:
        print(f"ERROR - Get user playlists: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/import-selected-playlists-to-liked', methods=['POST'])
def import_selected_playlists_to_liked():
    """Import songs from selected playlists to Liked Songs"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        # Get selected playlist IDs from request
        data = request.get_json()
        selected_playlist_ids = data.get('playlist_ids', [])
        
        if not selected_playlist_ids:
            return jsonify({'error': 'No playlists selected'}), 400
        
        print(f"DEBUG - Starting import from {len(selected_playlist_ids)} selected playlists")
        
        # Get tracks from selected playlists only
        all_playlist_tracks = []
        
        for playlist_id in selected_playlist_ids:
            try:
                playlist = sp.playlist(playlist_id)
                print(f"DEBUG - Processing selected playlist: {playlist['name']}")
                
                tracks = sp.playlist_tracks(playlist_id)
                
                while tracks:
                    for item in tracks['items']:
                        if item['track'] and item['track']['id']:
                            all_playlist_tracks.append({
                                'id': item['track']['id'],
                                'name': item['track']['name'],
                                'artist': ', '.join([artist['name'] for artist in item['track']['artists']])
                            })
                    
                    if tracks['next']:
                        tracks = sp.next(tracks)
                    else:
                        break
                        
            except Exception as playlist_error:
                print(f"ERROR - Failed to process playlist {playlist_id}: {playlist_error}")
                continue
        
        # Remove duplicates
        unique_tracks = {}
        for track in all_playlist_tracks:
            unique_tracks[track['id']] = track
        
        all_unique_tracks = list(unique_tracks.values())
        print(f"DEBUG - Found {len(all_unique_tracks)} unique songs in selected playlists")
        
        # Get current liked songs to avoid duplicates
        print("DEBUG - Getting current liked songs")
        liked_track_ids = set()
        liked_songs = sp.current_user_saved_tracks(limit=50, offset=0)
        
        while liked_songs:
            for item in liked_songs['items']:
                if item['track']:
                    liked_track_ids.add(item['track']['id'])
            
            if liked_songs['next']:
                liked_songs = sp.next(liked_songs)
            else:
                break
        
        print(f"DEBUG - Found {len(liked_track_ids)} existing liked songs")
        
        # Find songs that need to be added
        songs_to_add = [track for track in all_unique_tracks if track['id'] not in liked_track_ids]
        print(f"DEBUG - Need to add {len(songs_to_add)} new songs to liked songs")
        
        # Add songs in batches
        added_count = 0
        batch_size = 50
        
        for i in range(0, len(songs_to_add), batch_size):
            batch = songs_to_add[i:i + batch_size]
            track_ids = [track['id'] for track in batch]
            
            try:
                sp.current_user_saved_tracks_add(tracks=track_ids)
                added_count += len(track_ids)
                print(f"DEBUG - Added batch of {len(track_ids)} songs. Total added: {added_count}")
                time.sleep(0.1)
                
            except Exception as batch_error:
                print(f"ERROR - Failed to add batch: {batch_error}")
                continue
        
        print(f"DEBUG - Import complete. Added {added_count} songs to liked songs")
        
        return jsonify({
            'success': True,
            'playlists_processed': len(selected_playlist_ids),
            'total_playlist_songs': len(all_unique_tracks),
            'already_liked': len(liked_track_ids),
            'newly_added': added_count,
            'message': f'Successfully added {added_count} songs from {len(selected_playlist_ids)} playlists'
        })
        
    except Exception as e:
        print(f"ERROR - Import selected playlists failed: {str(e)}")
        import traceback
        print(f"ERROR - Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500
    


    

@app.route('/create-filtered-playlist', methods=['POST'])
def create_filtered_playlist():
    """Create a Spotify playlist from filtered song results"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        playlist_name = data.get('playlist_name')
        description = data.get('description', '')
        
        # Validate inputs
        if not playlist_name:
            return jsonify({'error': 'Playlist name is required'}), 400
        
        print(f"DEBUG - Original description: '{description}'")
        print(f"DEBUG - Original description length: {len(description)}")
        
        # Clean and truncate description if needed
        # Remove any problematic characters that might cause Spotify API issues
        clean_description = description.replace('\n', ' ').replace('\r', ' ')
        # Remove any control characters
        clean_description = ''.join(char for char in clean_description if ord(char) >= 32 or char in '\n\r\t')
        
        # Truncate if too long
        if len(clean_description) > 250:  # Being more conservative
            print(f"DEBUG - Description too long ({len(clean_description)} chars), truncating")
            clean_description = clean_description[:247] + "..."
        
        print(f"DEBUG - Cleaned description: '{clean_description}'")
        print(f"DEBUG - Cleaned description length: {len(clean_description)}")
        
        # Get filter criteria
        tempo_min = data.get('tempo_min', 1)
        tempo_max = data.get('tempo_max', 5)
        energy_min = data.get('energy_min', 1)
        energy_max = data.get('energy_max', 5)
        mood_min = data.get('mood_min', 1)
        mood_max = data.get('mood_max', 5)
        include_tag_ids = data.get('include_tag_ids', [])
        exclude_tag_ids = data.get('exclude_tag_ids', [])
        
        print(f"DEBUG - Filters: T{tempo_min}-{tempo_max}, E{energy_min}-{energy_max}, M{mood_min}-{mood_max}")
        print(f"DEBUG - Include tags: {include_tag_ids}, Exclude tags: {exclude_tag_ids}")
        
        # Re-run the filtering logic to get matching songs
        cache_key = session.get('cache_key')
        if not cache_key or cache_key not in liked_songs_cache:
            return jsonify({'error': 'No cached songs available'}), 400
        
        with cache_lock:
            cached_songs = liked_songs_cache[cache_key]['songs']
        
        # Filter songs using same logic as filter endpoint
        matching_spotify_ids = []
        
        for cached_song in cached_songs:
            song = Song.query.get(cached_song['db_id'])
            if not song:
                continue
            
            # Check attribute ranges
            if song.tempo < tempo_min or song.tempo > tempo_max:
                continue
            if song.energy < energy_min or song.energy > energy_max:
                continue
            if song.mood < mood_min or song.mood > mood_max:
                continue
            
            # Get song's tag IDs
            song_tag_ids = {tag.id for tag in song.tags}
            
            # Check include tags
            if include_tag_ids:
                required_include_tag_ids = set(include_tag_ids)
                if not required_include_tag_ids.issubset(song_tag_ids):
                    continue
            
            # Check exclude tags
            if exclude_tag_ids:
                excluded_tag_ids = set(exclude_tag_ids)
                if excluded_tag_ids.intersection(song_tag_ids):
                    continue
            
            # Song matches - add its Spotify ID
            matching_spotify_ids.append(song.spotify_id)
        
        print(f"DEBUG - Found {len(matching_spotify_ids)} matching songs")
        
        if len(matching_spotify_ids) == 0:
            return jsonify({'error': 'No songs match your criteria'}), 400
        
        # Create Spotify client
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        user = sp.current_user()
        
        # Try to create the playlist with full description first
        playlist = None
        description_used = clean_description
        
        try:
            print(f"DEBUG - Attempting to create playlist with full description")
            playlist = sp.user_playlist_create(
                user=user['id'],
                name=playlist_name,
                public=True,
                description=clean_description
            )
            print(f"DEBUG - Successfully created playlist with full description: {playlist['id']}")
            
        except Exception as create_error:
            print(f"DEBUG - Failed to create with full description: {create_error}")
            print(f"DEBUG - Trying with minimal description")
            
            try:
                # Try with just the human-readable part (no encoded data)
                simple_description = clean_description.split('[ST:')[0].strip()
                print(f"DEBUG - Trying simple description: '{simple_description}'")
                
                playlist = sp.user_playlist_create(
                    user=user['id'],
                    name=playlist_name,
                    public=True,
                    description=simple_description
                )
                description_used = simple_description
                print(f"DEBUG - Successfully created playlist with simple description: {playlist['id']}")
                
            except Exception as simple_error:
                print(f"DEBUG - Even simple description failed: {simple_error}")
                print(f"DEBUG - Creating with minimal description")
                
                # Last resort - very basic description
                playlist = sp.user_playlist_create(
                    user=user['id'],
                    name=playlist_name,
                    public=True,
                    description="Created with SpotifyTagger filters"
                )
                description_used = "Created with SpotifyTagger filters"
                print(f"DEBUG - Created playlist with minimal description: {playlist['id']}")
        
        # Add songs to playlist in batches
        batch_size = 100
        songs_added = 0
        
        # Convert spotify IDs to URIs
        track_uris = [f"spotify:track:{spotify_id}" for spotify_id in matching_spotify_ids]
        
        try:
            for i in range(0, len(track_uris), batch_size):
                batch = track_uris[i:i + batch_size]
                sp.playlist_add_items(playlist['id'], batch)
                songs_added += len(batch)
                print(f"DEBUG - Added {len(batch)} songs to playlist. Total: {songs_added}")
        except Exception as add_error:
            print(f"ERROR - Failed to add songs: {add_error}")
            return jsonify({'error': f'Playlist created but failed to add songs: {str(add_error)}'}), 500
        
        print(f"DEBUG - Playlist creation successful!")
        print(f"DEBUG - Final description used: '{description_used}'")
        
        return jsonify({
            'success': True,
            'playlist_name': playlist_name,
            'playlist_id': playlist['id'],
            'playlist_url': playlist['external_urls']['spotify'],
            'songs_added': songs_added,
            'description': description_used
        })
        
    except Exception as e:
        print(f"ERROR - Failed to create playlist: {str(e)}")
        import traceback
        print(f"ERROR - Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500
    


@app.route('/get-fresh-playlists')
def get_fresh_playlists():
    """Get fresh playlist data for refreshing the sidebar"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        user_id = sp.current_user()['id']
        
        playlist_list = []
        playlists = sp.current_user_playlists(limit=50, offset=0)
        
        while playlists:
            for playlist in playlists['items']:
                # Only include playlists owned by the user
                if playlist['owner']['id'] == user_id:
                    playlist_info = {
                        'name': playlist['name'],
                        'track_count': playlist['tracks']['total'],
                        'id': playlist['id']
                    }
                    playlist_list.append(playlist_info)
            
            if playlists['next']:
                playlists = sp.next(playlists)
            else:
                break
        
        print(f"DEBUG - Fetched {len(playlist_list)} fresh playlists")
        return jsonify({
            'success': True,
            'playlists': playlist_list
        })
        
    except Exception as e:
        print(f"ERROR - Failed to fetch fresh playlists: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    

@app.route('/get-smart-playlists')
def get_smart_playlists():
    """Get all playlists created by this system (with embedded criteria)"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        user_id = sp.current_user()['id']
        
        print("DEBUG - Starting smart playlist scan")
        
        smart_playlists = []
        all_playlists = []
        
        # Get all user's playlists
        playlists = sp.current_user_playlists(limit=50, offset=0)
        
        while playlists:
            for playlist in playlists['items']:
                # Only check playlists owned by the user
                if playlist['owner']['id'] == user_id:
                    all_playlists.append(playlist)
                    
                    print(f"DEBUG - Checking playlist: {playlist['name']}")
                    
                    # Get full playlist details to access description
                    try:
                        full_playlist = sp.playlist(playlist['id'], fields='id,name,description,tracks.total,external_urls')
                        description = full_playlist.get('description', '')
                        
                        print(f"DEBUG - Description: '{description}'")
                        
                        # Check if this playlist has our marker
                        if description and '[ST:' in description:
                            print(f"DEBUG - Found smart playlist: {playlist['name']}")
                            
                            # Try to parse the criteria
                            try:
                                # Extract the encoded criteria
                                marker_match = description.split('[ST:')
                                if len(marker_match) > 1:
                                    encoded_part = marker_match[1].split(']')[0]
                                    
                                    # Decode the criteria
                                    import base64
                                    import json
                                    criteria_json = base64.b64decode(encoded_part).decode('utf-8')
                                    criteria = json.loads(criteria_json)
                                    
                                    print(f"DEBUG - Parsed criteria: {criteria}")
                                    
                                    # Get human-readable part of description
                                    human_description = marker_match[0].strip()
                                    
                                    smart_playlist = {
                                        'id': playlist['id'],
                                        'name': playlist['name'],
                                        'track_count': full_playlist['tracks']['total'],
                                        'url': full_playlist['external_urls']['spotify'],
                                        'human_description': human_description,
                                        'criteria': criteria,
                                        'full_description': description
                                    }
                                    
                                    smart_playlists.append(smart_playlist)
                                    
                            except Exception as parse_error:
                                print(f"ERROR - Failed to parse criteria for {playlist['name']}: {parse_error}")
                                # Still add it but mark as unparseable
                                smart_playlists.append({
                                    'id': playlist['id'],
                                    'name': playlist['name'],
                                    'track_count': full_playlist['tracks']['total'],
                                    'url': full_playlist['external_urls']['spotify'],
                                    'human_description': description.split('[ST:')[0].strip(),
                                    'criteria': None,
                                    'full_description': description,
                                    'parse_error': True
                                })
                    
                    except Exception as playlist_error:
                        print(f"ERROR - Failed to get details for playlist {playlist['name']}: {playlist_error}")
                        continue
            
            # Get next batch of playlists
            if playlists['next']:
                playlists = sp.next(playlists)
            else:
                break
        
        print(f"DEBUG - Scanned {len(all_playlists)} total playlists")
        print(f"DEBUG - Found {len(smart_playlists)} smart playlists")
        
        return jsonify({
            'success': True,
            'smart_playlists': smart_playlists,
            'total_playlists_scanned': len(all_playlists)
        })
        
    except Exception as e:
        print(f"ERROR - Failed to scan smart playlists: {str(e)}")
        import traceback
        print(f"ERROR - Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500
    

@app.route('/refresh-smart-playlist', methods=['POST'])
def refresh_smart_playlist():
    """Refresh a smart playlist by adding newly tagged songs that match its criteria"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        playlist_id = data.get('playlist_id')
        
        if not playlist_id:
            return jsonify({'error': 'Playlist ID required'}), 400
        
        print(f"DEBUG - Refreshing smart playlist: {playlist_id}")
        
        # Create Spotify client
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        # Get playlist details to extract criteria
        playlist = sp.playlist(playlist_id, fields='id,name,description,tracks.items(track(id))')
        description = playlist.get('description', '')
        
        print(f"DEBUG - Playlist: {playlist['name']}")
        print(f"DEBUG - Description: {description}")
        
        # Parse criteria from description
        if '[ST:' not in description:
            return jsonify({'error': 'This playlist was not created with filter criteria'}), 400
        
        try:
            # Extract and decode criteria
            marker_match = description.split('[ST:')
            encoded_part = marker_match[1].split(']')[0]
            
            import base64
            import json
            criteria_json = base64.b64decode(encoded_part).decode('utf-8')
            criteria = json.loads(criteria_json)
            
            print(f"DEBUG - Parsed criteria: {criteria}")
            
            # Extract filter parameters
            tempo_min, tempo_max = criteria.get('t', [1, 5])
            energy_min, energy_max = criteria.get('e', [1, 5])
            emotion_min, emotion_max = criteria.get('m', [1, 5])
            include_tag_ids = criteria.get('i', [])
            exclude_tag_ids = criteria.get('x', [])
            
            print(f"DEBUG - Filters: T{tempo_min}-{tempo_max}, E{energy_min}-{energy_max}, M{emotion_min}-{emotion_max}")
            print(f"DEBUG - Include tags: {include_tag_ids}, Exclude tags: {exclude_tag_ids}")
            
        except Exception as parse_error:
            print(f"ERROR - Failed to parse criteria: {parse_error}")
            return jsonify({'error': 'Could not parse playlist criteria'}), 400
        
        # Get current songs in the playlist
        current_track_ids = set()
        for item in playlist['tracks']['items']:
            if item['track'] and item['track']['id']:
                current_track_ids.add(item['track']['id'])
        
        print(f"DEBUG - Playlist currently has {len(current_track_ids)} songs")
        
        # Get cached liked songs
        cache_key = session.get('cache_key')
        if not cache_key or cache_key not in liked_songs_cache:
            return jsonify({'error': 'Liked songs cache not available. Please go to Tag Songs tab and search something to load cache.'}), 400
        
        with cache_lock:
            cached_songs = liked_songs_cache[cache_key]['songs']
        
        print(f"DEBUG - Checking against {len(cached_songs)} cached liked songs")
        
        # Find songs that match criteria but aren't in playlist
        matching_songs = []
        songs_checked = 0
        
        for cached_song in cached_songs:
            songs_checked += 1
            
            # Skip if already in playlist
            if cached_song['spotify_id'] in current_track_ids:
                continue
            
            # Get song from database to check attributes
            song = Song.query.get(cached_song['db_id'])
            if not song:
                continue
            
            # Check attribute ranges
            if song.tempo < tempo_min or song.tempo > tempo_max:
                continue
            if song.energy < energy_min or song.energy > energy_max:
                continue
            if song.mood < emotion_min or song.mood > emotion_max:
                continue
            
            # Get song's tag IDs
            song_tag_ids = {tag.id for tag in song.tags}
            
            # Check include tags - song must have ALL required tags
            if include_tag_ids:
                required_tag_ids = set(include_tag_ids)
                if not required_tag_ids.issubset(song_tag_ids):
                    continue
            
            # Check exclude tags - song must NOT have ANY excluded tags
            if exclude_tag_ids:
                excluded_tag_ids = set(exclude_tag_ids)
                if excluded_tag_ids.intersection(song_tag_ids):
                    continue
            
            # Song matches criteria and isn't in playlist - add it
            matching_songs.append({
                'spotify_id': cached_song['spotify_id'],
                'name': cached_song['name'],
                'artist': cached_song['artist']
            })
        
        print(f"DEBUG - Found {len(matching_songs)} new songs to add (checked {songs_checked} total)")
        
        # Add new songs to playlist
        if len(matching_songs) == 0:
            return jsonify({
                'success': True,
                'playlist_name': playlist['name'],
                'songs_added': 0,
                'new_songs': [],
                'message': 'Playlist is already up to date!'
            })
        
        # Add songs in batches
        track_uris = [f"spotify:track:{song['spotify_id']}" for song in matching_songs]
        batch_size = 100
        songs_added = 0
        
        for i in range(0, len(track_uris), batch_size):
            batch = track_uris[i:i + batch_size]
            try:
                sp.playlist_add_items(playlist_id, batch)
                songs_added += len(batch)
                print(f"DEBUG - Added batch of {len(batch)} songs. Total: {songs_added}")
            except Exception as add_error:
                print(f"ERROR - Failed to add batch: {add_error}")
                break
        
        print(f"DEBUG - Successfully added {songs_added} songs to playlist")
        
        return jsonify({
            'success': True,
            'playlist_name': playlist['name'],
            'songs_added': songs_added,
            'new_songs': matching_songs[:songs_added],  # Only return successfully added songs
            'message': f'Added {songs_added} new songs to "{playlist["name"]}"!'
        })
        
    except Exception as e:
        print(f"ERROR - Failed to refresh playlist: {str(e)}")
        import traceback
        print(f"ERROR - Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500
    

@app.route('/refresh-all-smart-playlists', methods=['POST'])
def refresh_all_smart_playlists():
    """Automatically scan for and refresh all smart playlists"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        print("DEBUG - Starting automatic refresh of all smart playlists")
        
        # Create Spotify client
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        user_id = sp.current_user()['id']
        
        # Get cached liked songs
        cache_key = session.get('cache_key')
        if not cache_key or cache_key not in liked_songs_cache:
            return jsonify({'error': 'Liked songs cache not available. Please go to Tag Songs tab and search something to load cache.'}), 400
        
        with cache_lock:
            cached_songs = liked_songs_cache[cache_key]['songs']
        
        print(f"DEBUG - Using {len(cached_songs)} cached liked songs for matching")
        
        # Step 1: Scan for smart playlists
        smart_playlists = []
        playlists_scanned = 0
        
        playlists = sp.current_user_playlists(limit=50, offset=0)
        
        while playlists:
            for playlist in playlists['items']:
                if playlist['owner']['id'] == user_id:
                    playlists_scanned += 1
                    
                    try:
                        # Get full playlist details
                        full_playlist = sp.playlist(playlist['id'], fields='id,name,description,tracks.items(track(id))')
                        description = full_playlist.get('description', '')
                        
                        # Check if this is a smart playlist
                        if description and '[ST:' in description:
                            # Parse criteria
                            try:
                                marker_match = description.split('[ST:')
                                encoded_part = marker_match[1].split(']')[0]
                                
                                import base64
                                import json
                                criteria_json = base64.b64decode(encoded_part).decode('utf-8')
                                criteria = json.loads(criteria_json)
                                
                                # Get current songs in playlist
                                current_track_ids = set()
                                for item in full_playlist['tracks']['items']:
                                    if item['track'] and item['track']['id']:
                                        current_track_ids.add(item['track']['id'])
                                
                                smart_playlists.append({
                                    'id': playlist['id'],
                                    'name': playlist['name'],
                                    'criteria': criteria,
                                    'current_tracks': current_track_ids,
                                    'original_count': len(current_track_ids)
                                })
                                
                                print(f"DEBUG - Found smart playlist: {playlist['name']} with {len(current_track_ids)} songs")
                                
                            except Exception as parse_error:
                                print(f"DEBUG - Could not parse criteria for {playlist['name']}: {parse_error}")
                                continue
                                
                    except Exception as playlist_error:
                        print(f"DEBUG - Could not check playlist {playlist['name']}: {playlist_error}")
                        continue
            
            if playlists['next']:
                playlists = sp.next(playlists)
            else:
                break
        
        print(f"DEBUG - Found {len(smart_playlists)} smart playlists out of {playlists_scanned} total playlists")
        
        if len(smart_playlists) == 0:
            return jsonify({
                'success': True,
                'playlists_updated': 0,
                'total_songs_added': 0,
                'results': [],
                'message': f'No smart playlists found (scanned {playlists_scanned} playlists)'
            })
        
        # Step 2: Refresh each smart playlist
        results = []
        total_songs_added = 0
        
        for playlist_info in smart_playlists:
            print(f"DEBUG - Processing playlist: {playlist_info['name']}")
            
            criteria = playlist_info['criteria']
            current_track_ids = playlist_info['current_tracks']
            
            # Extract filter parameters
            tempo_min, tempo_max = criteria.get('t', [1, 5])
            energy_min, energy_max = criteria.get('e', [1, 5])
            emotion_min, emotion_max = criteria.get('m', [1, 5])
            include_tag_ids = criteria.get('i', [])
            exclude_tag_ids = criteria.get('x', [])
            
            # Find matching songs not in playlist
            matching_songs = []
            
            for cached_song in cached_songs:
                # Skip if already in playlist
                if cached_song['spotify_id'] in current_track_ids:
                    continue
                
                # Get song from database to check attributes
                song = Song.query.get(cached_song['db_id'])
                if not song:
                    continue
                
                # Check attribute ranges
                if song.tempo < tempo_min or song.tempo > tempo_max:
                    continue
                if song.energy < energy_min or song.energy > energy_max:
                    continue
                if song.mood < emotion_min or song.mood > emotion_max:
                    continue
                
                # Get song's tag IDs
                song_tag_ids = {tag.id for tag in song.tags}
                
                # Check include tags
                if include_tag_ids:
                    required_tag_ids = set(include_tag_ids)
                    if not required_tag_ids.issubset(song_tag_ids):
                        continue
                
                # Check exclude tags
                if exclude_tag_ids:
                    excluded_tag_ids = set(exclude_tag_ids)
                    if excluded_tag_ids.intersection(song_tag_ids):
                        continue
                
                # Song matches criteria
                matching_songs.append({
                    'spotify_id': cached_song['spotify_id'],
                    'name': cached_song['name'],
                    'artist': cached_song['artist']
                })
            
            print(f"DEBUG - Found {len(matching_songs)} new songs for {playlist_info['name']}")
            
            # Add songs to playlist
            songs_added = 0
            if len(matching_songs) > 0:
                track_uris = [f"spotify:track:{song['spotify_id']}" for song in matching_songs]
                batch_size = 100
                
                for i in range(0, len(track_uris), batch_size):
                    batch = track_uris[i:i + batch_size]
                    try:
                        sp.playlist_add_items(playlist_info['id'], batch)
                        songs_added += len(batch)
                    except Exception as add_error:
                        print(f"ERROR - Failed to add batch to {playlist_info['name']}: {add_error}")
                        break
                
                print(f"DEBUG - Added {songs_added} songs to {playlist_info['name']}")
            
            # Record results
            results.append({
                'playlist_name': playlist_info['name'],
                'playlist_id': playlist_info['id'],
                'original_count': playlist_info['original_count'],
                'songs_added': songs_added,
                'new_songs': matching_songs[:songs_added],
                'new_total': playlist_info['original_count'] + songs_added
            })
            
            total_songs_added += songs_added
        
        print(f"DEBUG - Refresh complete. Added {total_songs_added} total songs across {len(smart_playlists)} playlists")
        
        return jsonify({
            'success': True,
            'playlists_updated': len(smart_playlists),
            'playlists_scanned': playlists_scanned,
            'total_songs_added': total_songs_added,
            'results': results,
            'message': f'Refreshed {len(smart_playlists)} smart playlists'
        })
        
    except Exception as e:
        print(f"ERROR - Failed to refresh all smart playlists: {str(e)}")
        import traceback
        print(f"ERROR - Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/get-playlist-count')
def get_playlist_count():
    """Get the total number of user playlists for progress calculation"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        user_id = sp.current_user()['id']
        
        total_playlists = 0
        playlists = sp.current_user_playlists(limit=50, offset=0)
        
        while playlists:
            # Count only playlists owned by the user
            for playlist in playlists['items']:
                if playlist['owner']['id'] == user_id:
                    total_playlists += 1
            
            if playlists['next']:
                playlists = sp.next(playlists)
            else:
                break
        
        return jsonify({
            'success': True,
            'total_playlists': total_playlists
        })
        
    except Exception as e:
        print(f"ERROR - Get playlist count failed: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/import-playlists-batch', methods=['POST'])
def import_playlists_batch():
    """Import songs from a batch of playlists with progress tracking"""
    if 'token_info' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        selected_playlist_ids = data.get('playlist_ids', [])
        batch_start = data.get('batch_start', 0)
        batch_size = data.get('batch_size', 5)
        
        if not selected_playlist_ids:
            return jsonify({'error': 'No playlists provided'}), 400
        
        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        # Process only the current batch
        batch_end = min(batch_start + batch_size, len(selected_playlist_ids))
        current_batch = selected_playlist_ids[batch_start:batch_end]
        
        print(f"DEBUG - Processing batch {batch_start}-{batch_end-1} of {len(selected_playlist_ids)} total playlists")
        
        # Get tracks from current batch of playlists
        batch_tracks = []
        playlists_processed = []
        
        for playlist_id in current_batch:
            try:
                playlist = sp.playlist(playlist_id)
                playlist_name = playlist['name']
                playlists_processed.append(playlist_name)
                
                print(f"DEBUG - Processing playlist: {playlist_name}")
                
                tracks = sp.playlist_tracks(playlist_id)
                playlist_track_count = 0
                
                while tracks:
                    for item in tracks['items']:
                        if item['track'] and item['track']['id']:
                            batch_tracks.append({
                                'id': item['track']['id'],
                                'name': item['track']['name'],
                                'artist': ', '.join([artist['name'] for artist in item['track']['artists']])
                            })
                            playlist_track_count += 1
                    
                    if tracks['next']:
                        tracks = sp.next(tracks)
                    else:
                        break
                
                print(f"DEBUG - Found {playlist_track_count} tracks in {playlist_name}")
                        
            except Exception as playlist_error:
                print(f"ERROR - Failed to process playlist {playlist_id}: {playlist_error}")
                continue
        
        # Remove duplicates within this batch
        unique_tracks = {}
        for track in batch_tracks:
            unique_tracks[track['id']] = track
        
        batch_unique_tracks = list(unique_tracks.values())
        print(f"DEBUG - Batch has {len(batch_unique_tracks)} unique tracks")
        
        # Check which of these tracks are already in liked songs (real-time check)
        if len(batch_unique_tracks) == 0:
            print("DEBUG - No tracks to check")
            return jsonify({
                'success': True,
                'batch_start': batch_start,
                'batch_end': batch_end,
                'total_playlists': len(selected_playlist_ids),
                'playlists_processed': playlists_processed,
                'batch_unique_tracks': 0,
                'batch_songs_already_liked': 0,
                'batch_songs_added': 0,
                'is_final_batch': batch_end >= len(selected_playlist_ids),
                'progress_percent': int((batch_end / len(selected_playlist_ids)) * 100)
            })
        
        # Get track IDs to check
        track_ids_to_check = [track['id'] for track in batch_unique_tracks]
        print(f"DEBUG - Checking if {len(track_ids_to_check)} tracks are already liked...")
        
        # Use Spotify's contains API to check which tracks are already liked
        # This API takes up to 50 track IDs at a time
        already_liked_flags = []
        check_batch_size = 50
        
        try:
            for i in range(0, len(track_ids_to_check), check_batch_size):
                check_batch = track_ids_to_check[i:i + check_batch_size]
                contains_result = sp.current_user_saved_tracks_contains(check_batch)
                already_liked_flags.extend(contains_result)
                print(f"DEBUG - Checked batch {i//check_batch_size + 1}: {sum(contains_result)} out of {len(check_batch)} already liked")
        
        except Exception as check_error:
            print(f"ERROR - Failed to check liked status: {check_error}")
            # If check fails, assume none are liked to avoid duplicates
            already_liked_flags = [False] * len(track_ids_to_check)
        
        # Separate tracks into already liked vs new
        songs_to_add = []
        songs_already_liked = []
        
        for i, track in enumerate(batch_unique_tracks):
            if i < len(already_liked_flags) and already_liked_flags[i]:
                songs_already_liked.append(track)
            else:
                songs_to_add.append(track)
        
        print(f"DEBUG - From this batch: {len(songs_already_liked)} songs already liked, {len(songs_to_add)} new songs to add")
        if len(songs_to_add) > 0:
            sample_new = [track['name'] + ' - ' + track['artist'] for track in songs_to_add[:3]]
            print(f"DEBUG - Sample new songs to add: {sample_new}")
        if len(songs_already_liked) > 0:
            sample_liked = [track['name'] + ' - ' + track['artist'] for track in songs_already_liked[:3]]
            print(f"DEBUG - Sample already liked songs: {sample_liked}")
        
        # Add only the new songs to Spotify
        actually_added_count = 0
        spotify_batch_size = 50
        
        if len(songs_to_add) > 0:
            for i in range(0, len(songs_to_add), spotify_batch_size):
                spotify_batch = songs_to_add[i:i + spotify_batch_size]
                track_ids = [track['id'] for track in spotify_batch]
                
                try:
                    sp.current_user_saved_tracks_add(tracks=track_ids)
                    actually_added_count += len(track_ids)
                    
                    print(f"DEBUG - Added Spotify batch of {len(track_ids)} songs. Total added so far: {actually_added_count}")
                    time.sleep(0.1)
                    
                except Exception as batch_error:
                    print(f"ERROR - Failed to add Spotify batch: {batch_error}")
                    continue
        else:
            print("DEBUG - No new songs to add in this batch")
        
        # Final batch check
        is_final_batch = batch_end >= len(selected_playlist_ids)
        if is_final_batch:
            print("DEBUG - Final batch completed")
        
        return jsonify({
            'success': True,
            'batch_start': batch_start,
            'batch_end': batch_end,
            'total_playlists': len(selected_playlist_ids),
            'playlists_processed': playlists_processed,
            'batch_unique_tracks': len(batch_unique_tracks),
            'batch_songs_already_liked': len(songs_already_liked),
            'batch_songs_added': actually_added_count,
            'is_final_batch': is_final_batch,
            'progress_percent': int((batch_end / len(selected_playlist_ids)) * 100)
        })
        
    except Exception as e:
        print(f"ERROR - Batch import failed: {str(e)}")
        import traceback
        print(f"ERROR - Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500
    


@app.route('/get-next-song-info')
def get_next_song_info():
    """Get information about the next song without redirecting"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    try:
        current_offset = int(request.args.get('offset', 0))
        untagged_only = request.args.get('untagged_only', 'false') == 'true'
        
        # Check if token needs refreshing
        if is_token_expired(session['token_info']):
            session['token_info'] = refresh_access_token(session['token_info'])

        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        # Find the next song
        search_offset = current_offset + 1
        max_search = 50  # Reduced search limit for background operation
        songs_checked = 0
        
        while songs_checked < max_search:
            results = sp.current_user_saved_tracks(limit=1, offset=search_offset)
            
            if not results['items']:
                # No more songs
                return {'has_next': False}
                
            item = results['items'][0]
            if item['track']:
                saved_song = save_song_to_db(item['track'])
                songs_checked += 1
                
                # If not filtering or song is untagged, this is our next song
                if not untagged_only or len(saved_song.tags) == 0:
                    return {
                        'has_next': True,
                        'next_offset': search_offset,
                        'song_id': saved_song.id,
                        'spotify_id': item['track']['id'],
                        'name': item['track']['name'],
                        'artist': ', '.join([artist['name'] for artist in item['track']['artists']])
                    }
            
            search_offset += 1
            
        # Searched too many songs without finding untagged one
        return {'has_next': False, 'searched_limit_reached': True}
        
    except Exception as e:
        print(f"Error getting next song info: {e}")
        return {'error': str(e)}, 500


@app.route('/get-song-data/<int:offset>')
def get_song_data(offset):
    """Get song data for AJAX navigation without full page reload"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    try:
        untagged_only = request.args.get('untagged_only', 'false') == 'true'
        
        # Check if token needs refreshing
        if is_token_expired(session['token_info']):
            session['token_info'] = refresh_access_token(session['token_info'])

        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        # Find the song at this offset (reuse existing logic)
        current_position = offset
        songs_examined = 0
        max_search = 200
        
        while songs_examined < max_search:
            results = sp.current_user_saved_tracks(limit=50, offset=current_position)
            
            if not results['items']:
                return {'error': 'No more songs'}, 404
            
            # Look through this batch
            for i, item in enumerate(results['items']):
                if item['track']:
                    actual_position = current_position + i
                    songs_examined += 1
                    
                    saved_song = save_song_to_db(item['track'])
                    
                    if untagged_only and len(saved_song.tags) > 0:
                        continue
                    
                    # Format the song data
                    duration_ms = item['track']['duration_ms']
                    minutes = duration_ms // 60000
                    seconds = (duration_ms % 60000) // 1000
                    duration_formatted = f"{minutes}:{seconds:02d}"
                    
                    return {
                        'success': True,
                        'song': {
                            'name': item['track']['name'],
                            'artist': ', '.join([artist['name'] for artist in item['track']['artists']]),
                            'album': item['track']['album']['name'],
                            'duration': duration_formatted,
                            'spotify_id': item['track']['id'],
                            'db_id': saved_song.id,
                            'actual_position': actual_position
                        }
                    }
            
            # No matching song in this batch, get next batch
            if results['next']:
                current_position += 50
            else:
                break
        
        return {'error': 'No matching songs found'}, 404
        
    except Exception as e:
        print(f"Error getting song data: {e}")
        return {'error': str(e)}, 500


@app.route('/get-song-attributes-with-status/<int:song_id>')
def get_song_attributes_with_status(song_id):
    """Get current attributes and whether they've been manually set"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    song = Song.query.get(song_id)
    if not song:
        return {'error': 'Song not found'}, 404
    
    # Debug the actual database values
    print(f"DEBUG - Song {song_id} database values: tempo={song.tempo} (type: {type(song.tempo)}), energy={song.energy} (type: {type(song.energy)}), mood={song.mood} (type: {type(song.mood)})")
    print(f"DEBUG - Song {song_id} tag count: {len(song.tags)}")
    
    # Now we can properly detect unset attributes (NULL values)
    is_unset = (song.tempo is None and song.energy is None and song.mood is None)
    
    print(f"DEBUG - Song {song_id} is_unset: {is_unset}")
    
    return {
        'tempo': song.tempo,
        'energy': song.energy,
        'mood': song.mood,
        'is_unset': is_unset
    }


@app.route('/get-next-untagged-offset')
def get_next_untagged_offset():
    """Find the next untagged song offset for AJAX navigation"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    try:
        current_offset = int(request.args.get('offset', 0))
        
        # Check if token needs refreshing
        if is_token_expired(session['token_info']):
            session['token_info'] = refresh_access_token(session['token_info'])

        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        # Search forward for next untagged song
        search_offset = current_offset + 1
        max_search = 200
        songs_checked = 0
        
        while songs_checked < max_search:
            results = sp.current_user_saved_tracks(limit=1, offset=search_offset)
            
            if not results['items']:
                return {'found': False, 'reason': 'end_of_songs'}
                
            item = results['items'][0]
            if item['track']:
                saved_song = save_song_to_db(item['track'])
                songs_checked += 1
                
                if len(saved_song.tags) == 0:
                    return {'found': True, 'offset': search_offset}
            
            search_offset += 1
            
        return {'found': False, 'reason': 'search_limit_reached'}
        
    except Exception as e:
        print(f"Error finding next untagged offset: {e}")
        return {'error': str(e)}, 500


@app.route('/get-prev-untagged-offset')
def get_prev_untagged_offset():
    """Find the previous untagged song offset for AJAX navigation"""
    if 'token_info' not in session:
        return {'error': 'Not authenticated'}, 401
    
    try:
        current_offset = int(request.args.get('offset', 0))
        
        if current_offset <= 0:
            return {'found': False, 'reason': 'at_beginning'}
        
        # Check if token needs refreshing
        if is_token_expired(session['token_info']):
            session['token_info'] = refresh_access_token(session['token_info'])

        sp = spotipy.Spotify(auth=session['token_info']['access_token'])
        
        # Search backward for previous untagged song
        search_offset = current_offset - 1
        
        while search_offset >= 0:
            results = sp.current_user_saved_tracks(limit=1, offset=search_offset)
            
            if not results['items']:
                search_offset -= 1
                continue
                
            item = results['items'][0]
            if item['track']:
                saved_song = save_song_to_db(item['track'])
                
                if len(saved_song.tags) == 0:
                    return {'found': True, 'offset': search_offset}
            
            search_offset -= 1
            
        return {'found': False, 'reason': 'no_previous_untagged'}
        
    except Exception as e:
        print(f"Error finding previous untagged offset: {e}")
        return {'error': str(e)}, 500
    

@app.route('/logout')
def logout():
    """Clear session and show logged out state"""
    print("DEBUG - User logging out, clearing session")
    session.clear()
    
    # Show a simple logged out page
    return '''
    <html>
    <head>
        <title>Spotify Tagger</title>
        <link rel="stylesheet" href="{}">
    </head>
    <body style="text-align: center; padding: 100px; font-family: Arial, sans-serif;">
        <h1>Logged out successfully</h1>
        <p>You have been logged out of Spotify Tagger.</p>
        <a href="/login" style="background-color: #1db954; color: white; padding: 15px 30px; text-decoration: none; border-radius: 5px; font-size: 16px; display: inline-block; margin-top: 20px;">Login with Spotify</a>
    </body>
    </html>
    '''.format(url_for('static', filename='style.css'))


# Create database tables if they don't exist
try:
    with app.app_context():
        # Ensure instance folder exists
        import os
        instance_dir = os.path.dirname(app.instance_path)
        if not os.path.exists(app.instance_path):
            os.makedirs(app.instance_path)
            print(f"DEBUG - Created instance directory: {app.instance_path}")
        
        # Print database file path
        db_path = os.path.join(app.instance_path, 'spotify_tags.db')
        print(f"DEBUG - Creating database at: {db_path}")
        
        db.create_all() 
        print("Database tables created successfully!")
        
except Exception as e:
    print(f"ERROR - Failed to create database: {e}")
    print(f"ERROR - App instance path: {app.instance_path}")
    print(f"ERROR - Current working directory: {os.getcwd()}")

print("DEBUG - App is fully initialized and ready to serve requests")

if __name__ == '__main__':
    # Only run this when testing locally, not with gunicorn
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
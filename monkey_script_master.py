#!/usr/bin/env python3
# Monkey MIDI Player - MASTER VERSION (Full Features + BLE BPM)
import sys
sys.path.insert(0, '/usr/local/lib/python3.11/dist-packages/st7789')
import os, time, threading, smbus, datetime, json, asyncio
from bleak import BleakClient

# --- 1.5 SYSTEM OPTIMIZATIONS ---
# CPU performance mode and RT scheduling already handled by RT kernel
print("[SYSTEM] RT kernel active - performance mode enabled")

# --- 2. HARDCODED PATHS ---
BASE_DIR = "/home/pi"
soundfont_folder = os.path.join(BASE_DIR, "sf2")
midi_file_folder = os.path.join(BASE_DIR, "midifiles")
mixer_file = os.path.join(BASE_DIR, "mixer_settings.json")

# --- 3. CONFIGURATION & STATE ---
LED_NAME = "ACT"  
SHUTTING_DOWN = False  
LOW_POWER_MODE = False
MESSAGE = ""
msg_start_time = 0
volume_level = 0.5 

# --- BLE GLOBALS (From BPM Detection Script) ---
MONKEY_BPM = 120
BLE_CONNECTED = False
MONKEY_ADDRESS = "EF:B5:72:34:E5:03"
DATA_UUID = "1a9f2b32-1c1a-4ef0-9fb2-6a5e26c03db9"
file_loop_enabled = False
file_loop_path = None
# MIDI Timing & UI State (Restored from Fixed Script)
midi_clock_enabled = True
midi_transport_state = "stopped"
DEBUG_MIDI = False  
sustain_state = {i: False for i in range(16)}
last_active_channel = None
last_activity_time = {}
last_midi_activity = 0  

# --- 4. BLEAK MONITOR ENGINE ---
class BleakMonitor:
    async def notification_handler(self, sender, data):
        global MONKEY_BPM
        
        # Check if this is pattern data (JSON format)
        # You'll need to determine the key value the Monkey uses for patterns
        # Example: if key == 200:  # Pattern data key (replace with actual key)
        try:
            # Try to decode as JSON pattern data
            pattern_str = data.decode('utf-8')
            if pattern_str.startswith('{') and '"events"' in pattern_str:
                # This looks like a Monkey pattern
                print(f"[BLE] Received Monkey pattern: {len(pattern_str)} bytes")
                play_monkey_pattern(pattern_str)
                return
        except:
            pass  # Not pattern data, continue with key-value parsing
        
        # Parse key-value data (BPM, etc)
        for i in range(0, len(data) - 3, 4):
            key = int.from_bytes(data[i:i+2], 'little')
            val = int.from_bytes(data[i+2:i+4], 'little')
            if key == 5:
                MONKEY_BPM = val
            elif key == 101 and val > 0:
                calc_bpm = round(60000 / val)
                if abs(calc_bpm - MONKEY_BPM) > 1:
                    MONKEY_BPM = calc_bpm
            # Add pattern trigger key here if Monkey sends a separate "play pattern X" command
            # elif key == ???:  # Pattern playback trigger
            #     play_stored_pattern(val)  # Where val is pattern number

    async def run(self):
        global BLE_CONNECTED
        while not SHUTTING_DOWN:
            try:
                async with BleakClient(MONKEY_ADDRESS, timeout=10.0) as client:
                    await client.write_gatt_char(DATA_UUID, bytes([0x01, 0x00, 0x01, 0x00]))
                    await client.write_gatt_char(DATA_UUID, bytes([0xc8, 0x00, 0x00, 0x00]))
                    await client.start_notify(DATA_UUID, self.notification_handler)
                    BLE_CONNECTED = True
                    while client.is_connected and not SHUTTING_DOWN:
                        await asyncio.sleep(1)
                BLE_CONNECTED = False
            except:
                BLE_CONNECTED = False
                if not SHUTTING_DOWN: await asyncio.sleep(5)

def start_ble_thread():
    monitor = BleakMonitor()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(monitor.run())

# --- 5. SYSTEM THREADS & PERSISTENCE ---
def note_cleanup_thread():
    global last_midi_activity
    while not SHUTTING_DOWN:
        try:
            time.sleep(10.0)
            now = time.time()
            if fs and (now - last_midi_activity) > 10.0:
                for ch in range(16):
                    if not sustain_state.get(ch, False):
                        fs.cc(ch, 123, 0)
        except: time.sleep(10.0)

threading.Thread(target=note_cleanup_thread, daemon=True).start()

def save_mixer():
    try:
        with open(mixer_file, 'w') as f:
            json.dump(channel_volumes, f)
    except: pass

def load_mixer():
    global channel_volumes
    if os.path.exists(mixer_file):
        try:
            with open(mixer_file, 'r') as f:
                data = json.load(f)
                channel_volumes = {int(k): v for k, v in data.items()}
        except: pass

# ---------------------- RECORDING ENGINE ----------------------
class MidiRecorder:
    def __init__(self):
        self.recording = False
        self.mid = None
        self.track = None
        self.start_time = 0
        self.last_event_time = 0

    def start(self):
        import mido
        self.mid = mido.MidiFile()
        self.track = mido.MidiTrack()
        self.mid.tracks.append(self.track)
        self.recording = True
        self.start_time = time.time()
        self.last_event_time = self.start_time
        
        # Capture current drum kit selection (channel 9)
        # This ensures playback uses the same drum kit
        global selected_drum_kit
        if selected_drum_kit is not None:
            # For percussion (channel 9), just send program change
            # The percussion bank is automatically selected for channel 9
            self.track.append(mido.Message('program_change', channel=9, program=selected_drum_kit, time=0))

    def stop(self, filename):
        if not self.recording: return
        self.recording = False
        self.mid.save(filename)

    def add_event(self, msg):
        import mido
        if self.recording:
            now = time.time()
            delta = int(mido.second2tick(now - self.last_event_time, self.mid.ticks_per_beat, 500000))
            msg.time = delta
            self.track.append(msg)
            self.last_event_time = now

recorder = MidiRecorder()

# ---------------------- METRONOME ENGINE ----------------------
metronome_on = False
bpm = 120
metro_vol = 80 
metro_adjusting = False

# ---------------------- LOOP RECORDING ----------------------
loop_length = 4  # Number of bars in loop (4, 8, 16, 32)
loop_recording = False  # True when recording a loop
loop_playback = False  # True when loop is playing back
loop_file_path = None  # Path to temporary loop file
loop_start_time = 0  # When loop recording started
loop_bar_count = 0  # Current bar being recorded/played
loop_midi_events = []  # Stored MIDI events for playback
loop_playback_thread_active = False  # Thread control

# ---------------------- UNDO STACK ----------------------
loop_undo_stack = []  # Stack of previous loop states for undo
MAX_UNDO_LEVELS = 5  # Keep last 5 overdubs

# ---------------------- FILE LOOP PLAYBACK ----------------------
file_loop_enabled = False  # True when looping a MIDI file
file_loop_path = None  # Path to file being looped
file_loop_events = []  # Events for file loop playback
file_loop_position = 0.0  # Current playback position in seconds
file_loop_duration = 0.0  # Total duration of file in seconds

# ---------------------- FILE NORMAL PLAYBACK ----------------------
file_play_enabled = False  # True when playing a MIDI file (non-loop)
file_play_path = None  # Path to file being played
file_play_events = []  # Events for normal file playback
file_play_position = 0.0  # Current playback position in seconds
file_play_duration = 0.0  # Total duration of file in seconds

# ============================================================
# PROFESSIONAL PLAYBACK CORE (NEW STABLE ENGINE)
# ============================================================

# PLAYBACK STATE MACHINE (replaces file_play_enabled/file_loop_enabled)
PLAYBACK_NONE = 0
PLAYBACK_FILE = 1
PLAYBACK_FILE_LOOP = 2
PLAYBACK_LIVE_LOOP = 3
PLAYBACK_MONKEY_PATTERN = 4

playback_mode = PLAYBACK_NONE
playback_lock = threading.Lock()
_playback_thread_started = False

# Active notes tracking
active_notes = {i: set() for i in range(16)}

# Live loop storage
recorded_loop = []

# ============================================================
# MONKEY BEAT PATTERN PLAYBACK
# ============================================================

# Storage for received Monkey patterns
monkey_pattern_events = []  # Converted events ready for playback
monkey_pattern_active = False
monkey_pattern_length_beats = 4

def convert_monkey_pattern_to_events(pattern_json):
    """
    Convert Monkey JSON pattern to playable events.
    
    Monkey uses 24 ticks per beat internally.
    Converts tick times to seconds based on current BPM.
    
    Args:
        pattern_json: Dict with 'length_beats' and 'events' list
    
    Returns:
        List of event dicts with 'time' in seconds
    """
    global monkey_pattern_events, monkey_pattern_length_beats
    
    MONKEY_TICKS_PER_BEAT = 24
    current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
    seconds_per_beat = 60.0 / current_bpm
    seconds_per_tick = seconds_per_beat / MONKEY_TICKS_PER_BEAT
    
    monkey_pattern_length_beats = pattern_json.get('length_beats', 4)
    pattern_events = pattern_json.get('events', [])
    
    # Convert to playable events
    converted_events = []
    
    for event in pattern_events:
        note = event['note']
        press_ticks = event['time_ticks_press']
        release_ticks = event['time_ticks_release']
        velocity = event['velocity']
        
        # Convert ticks to seconds
        press_time = press_ticks * seconds_per_tick
        release_time = release_ticks * seconds_per_tick
        
        # Note on
        converted_events.append({
            'time': press_time,
            'type': 'note_on',
            'channel': 9,  # Drums
            'note': note,
            'velocity': velocity
        })
        
        # Note off
        converted_events.append({
            'time': release_time,
            'type': 'note_off',
            'channel': 9,
            'note': note
        })
    
    # Sort by time
    converted_events.sort(key=lambda e: (e['time'], e['type'] == 'note_off'))
    
    monkey_pattern_events = converted_events
    
    print(f"[MONKEY] Converted pattern: {len(pattern_events)} notes, {monkey_pattern_length_beats} beats, {len(converted_events)} events")
    return converted_events

def play_monkey_pattern(pattern_json_string):
    """
    Receive Monkey pattern as JSON string and start playback.
    
    Args:
        pattern_json_string: JSON string with pattern data
    """
    global playback_mode, monkey_pattern_events
    
    try:
        import json
        pattern_data = json.loads(pattern_json_string)
        
        # Convert pattern to events
        convert_monkey_pattern_to_events(pattern_data)
        
        # Start playback
        if monkey_pattern_events:
            start_playback_thread_once()
            with playback_lock:
                playback_mode = PLAYBACK_MONKEY_PATTERN
            print(f"[MONKEY] Pattern playback started")
        else:
            print(f"[MONKEY] No events in pattern")
    except Exception as e:
        print(f"[MONKEY] Error playing pattern: {e}")

# ============================================================
# SAFE HELPERS
# ============================================================

def hard_stop_all_playback():
    """Atomic stop + guaranteed note kill."""
    global playback_mode, file_play_position

    with playback_lock:
        playback_mode = PLAYBACK_NONE
        file_play_position = 0.0

    # HARD note kill
    if fs:
        for ch in list(active_notes.keys()):
            for note in list(active_notes[ch]):
                try:
                    fs.noteoff(ch, note)
                except Exception:
                    pass
            active_notes[ch].clear()

def responsive_sleep(seconds, still_running_fn):
    """High-resolution sleep that exits instantly on STOP."""
    end = time.time() + seconds
    while time.time() < end:
        if not still_running_fn() or SHUTTING_DOWN:
            return False
        time.sleep(0.002)
    return True

def start_playback_thread_once():
    """Safe single-start guard."""
    global _playback_thread_started
    if _playback_thread_started:
        print("[START] Thread already started")
        return

    print("[START] Starting playback thread...")
    t = threading.Thread(target=loop_playback_thread, daemon=True)
    t.start()
    _playback_thread_started = True
    print("[START] Thread started successfully")

# ============================================================
# MAIN PLAYBACK ENGINE
# ============================================================

def loop_playback_thread():
    """Professional playback engine with instant stop response."""
    global playback_mode, file_loop_position, file_play_position
    global fs, sfid  # Need access to FluidSynth globals
    global file_play_events, file_loop_events, loop_midi_events  # Need access to event lists

    print("[THREAD] Playback thread started!")
    
    iteration = 0
    _live_loop_anchor = None   # Chained start time for drift-free looping
    _monkey_anchor = None
    _file_loop_anchor = None
    while not SHUTTING_DOWN:
        try:
            iteration += 1
            if iteration % 50 == 0:  # Print every 50 iterations
                print(f"[THREAD] Heartbeat {iteration}, mode={playback_mode}")
            
            # Skip if FluidSynth not available
            if not fs or not sfid:
                if iteration <= 5:  # Only print first 5 times
                    print(f"[THREAD] Waiting for FluidSynth... fs={fs is not None}, sfid={sfid is not None}")
                time.sleep(0.1)
                continue
                
            with playback_lock:
                mode = playback_mode
            
            # Debug - show what we have
            if mode != PLAYBACK_NONE:
                if mode == PLAYBACK_FILE:
                    print(f"[THREAD] mode={mode}, FILE={PLAYBACK_FILE}, events={len(file_play_events) if file_play_events else 0}, fs={fs is not None}")
                elif mode == PLAYBACK_FILE_LOOP:
                    print(f"[THREAD] mode={mode}, LOOP={PLAYBACK_FILE_LOOP}, events={len(file_loop_events) if file_loop_events else 0}, fs={fs is not None}")
                else:
                    print(f"[THREAD] mode={mode}, events=?, fs={fs is not None}")
            
            if mode == PLAYBACK_FILE:
                print(f"[THREAD] mode=FILE, have {len(file_play_events) if file_play_events else 0} events, fs={fs is not None}")
                if not file_play_events:
                    print("[THREAD] FILE mode but file_play_events is EMPTY!")
                if not fs:
                    print("[THREAD] FILE mode but fs is NONE!")

            # ==================================================
            # LIVE LOOP PLAYBACK (recorded MIDI)
            # ==================================================
            if mode == PLAYBACK_LIVE_LOOP and not loop_recording and loop_midi_events:
                current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
                beats_per_bar = 4
                seconds_per_beat = 60.0 / current_bpm
                seconds_per_bar = seconds_per_beat * beats_per_bar
                total_loop_seconds = seconds_per_bar * loop_length
                
                # Drift-free anchor: first pass uses loop_start_time (set at exact loop boundary)
                if _live_loop_anchor is None:
                    _live_loop_anchor = loop_start_time if loop_start_time > 0 else time.time()
                loop_start = _live_loop_anchor
                
                # Send program changes (first iteration only to avoid overhead)
                program_changes = {}
                for event in loop_midi_events:
                    if event['type'] == 'program_change':
                        program_changes[event['channel']] = event['program']
                
                for ch, prog in program_changes.items():
                    if ch == 9:
                        try:
                            fs.program_select(ch, sfid, 128, prog)
                        except:
                            fs.program_select(ch, sfid, 0, prog)
                    else:
                        fs.program_select(ch, sfid, 0, prog)
                    fs.program_change(ch, prog)
                
                stopped = False
                # Play events
                for event in loop_midi_events:
                    with playback_lock:
                        if playback_mode != PLAYBACK_LIVE_LOOP:
                            for ch in list(active_notes.keys()):
                                for note in list(active_notes[ch]):
                                    try: fs.noteoff(ch, note)
                                    except: pass
                                active_notes[ch].clear()
                            stopped = True
                            break
                    
                    target_time = loop_start + event['time']
                    now = time.time()
                    
                    if target_time > now:
                        if not responsive_sleep(target_time - now, lambda: playback_mode == PLAYBACK_LIVE_LOOP):
                            stopped = True
                            break
                    
                    try:
                        msg_type = event['type']
                        ch = event['channel']
                        if msg_type == 'note_on':
                            fs.noteon(ch, event['note'], event['velocity'])
                            active_notes[ch].add(event['note'])
                        elif msg_type == 'note_off':
                            fs.noteoff(ch, event['note'])
                            active_notes[ch].discard(event['note'])
                        elif msg_type == 'control_change':
                            fs.cc(ch, event['control'], event['value'])
                    except Exception as e:
                        print(f"[LIVE LOOP ERROR] {e}")
                
                if stopped:
                    _live_loop_anchor = None  # Reset so next play starts clean
                else:
                    # Wait for remainder then advance anchor by exactly one loop length
                    elapsed = time.time() - loop_start
                    if elapsed < total_loop_seconds:
                        responsive_sleep(total_loop_seconds - elapsed, lambda: playback_mode == PLAYBACK_LIVE_LOOP)
                    _live_loop_anchor = loop_start + total_loop_seconds  # Chain: no drift accumulation

            # ==================================================
            # MONKEY PATTERN PLAYBACK
            # ==================================================
            elif mode == PLAYBACK_MONKEY_PATTERN and monkey_pattern_events and fs:
                print(f"[THREAD] Starting Monkey pattern playback, {len(monkey_pattern_events)} events, {monkey_pattern_length_beats} beats")
                
                current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
                seconds_per_beat = 60.0 / current_bpm
                total_pattern_seconds = seconds_per_beat * monkey_pattern_length_beats
                
                # Drift-free anchor
                if _monkey_anchor is None:
                    _monkey_anchor = time.time()
                loop_start = _monkey_anchor
                
                stopped = False
                for event in monkey_pattern_events:
                    with playback_lock:
                        if playback_mode != PLAYBACK_MONKEY_PATTERN:
                            for ch in list(active_notes.keys()):
                                for note in list(active_notes[ch]):
                                    try: fs.noteoff(ch, note)
                                    except: pass
                                active_notes[ch].clear()
                            stopped = True
                            break
                    
                    target_time = loop_start + event['time']
                    now = time.time()
                    
                    if target_time > now:
                        if not responsive_sleep(target_time - now, lambda: playback_mode == PLAYBACK_MONKEY_PATTERN):
                            stopped = True
                            break
                    
                    try:
                        msg_type = event['type']
                        ch = event['channel']
                        if msg_type == 'note_on':
                            fs.noteon(ch, event['note'], event['velocity'])
                            active_notes[ch].add(event['note'])
                        elif msg_type == 'note_off':
                            fs.noteoff(ch, event['note'])
                            active_notes[ch].discard(event['note'])
                    except Exception as e:
                        print(f"[MONKEY PATTERN ERROR] {e}")
                
                if stopped:
                    _monkey_anchor = None
                else:
                    elapsed = time.time() - loop_start
                    if elapsed < total_pattern_seconds:
                        responsive_sleep(total_pattern_seconds - elapsed, lambda: playback_mode == PLAYBACK_MONKEY_PATTERN)
                    _monkey_anchor = loop_start + total_pattern_seconds  # Chain: no drift

            # ==================================================
            # FILE LOOP PLAYBACK
            # ==================================================
            elif mode == PLAYBACK_FILE_LOOP and file_loop_events and fs:
                print(f"[THREAD] Starting file LOOP playback, {len(file_loop_events)} events")
                
                # Drift-free anchor
                if _file_loop_anchor is None:
                    _file_loop_anchor = time.time()
                loop_start = _file_loop_anchor
                
                # Send program changes
                program_changes = {}
                for event in file_loop_events:
                    if event['type'] == 'program_change':
                        program_changes[event['channel']] = event['program']
                
                print(f"[THREAD] Sending {len(program_changes)} program changes for loop")
                
                for ch, prog in program_changes.items():
                    if ch == 9:
                        try:
                            fs.program_select(ch, sfid, 128, prog)
                        except:
                            fs.program_select(ch, sfid, 0, prog)
                    else:
                        fs.program_select(ch, sfid, 0, prog)
                    fs.program_change(ch, prog)
                
                stopped = False
                # Play events
                for event in file_loop_events:
                    with playback_lock:
                        if playback_mode != PLAYBACK_FILE_LOOP:
                            for ch in list(active_notes.keys()):
                                for note in list(active_notes[ch]):
                                    try: fs.noteoff(ch, note)
                                    except: pass
                                active_notes[ch].clear()
                            stopped = True
                            break
                    
                    file_loop_position = event['time']
                    
                    target_time = loop_start + event['time']
                    now = time.time()
                    
                    if target_time > now:
                        if not responsive_sleep(target_time - now, lambda: playback_mode == PLAYBACK_FILE_LOOP):
                            stopped = True
                            break
                    
                    try:
                        msg_type = event['type']
                        ch = event['channel']
                        if msg_type == 'note_on':
                            fs.noteon(ch, event['note'], event['velocity'])
                            active_notes[ch].add(event['note'])
                        elif msg_type == 'note_off':
                            fs.noteoff(ch, event['note'])
                            active_notes[ch].discard(event['note'])
                        elif msg_type == 'control_change':
                            fs.cc(ch, event['control'], event['value'])
                    except Exception as e:
                        print(f"[FILE LOOP ERROR] {e}")
                
                file_loop_position = 0.0
                if stopped:
                    _file_loop_anchor = None
                else:
                    # Chain anchor to exact intended end — no drift accumulation
                    _file_loop_anchor = loop_start + file_loop_duration
                    print(f"[THREAD] File loop iteration complete, chaining anchor")
                with playback_lock:
                    current_mode = playback_mode
                    if current_mode != PLAYBACK_FILE_LOOP:
                        _file_loop_anchor = None
                        print(f"[THREAD] Mode changed from LOOP")

            # ==================================================
            # NORMAL FILE PLAYBACK
            # ==================================================
            elif mode == PLAYBACK_FILE and file_play_events and fs:
                print(f"[THREAD] Starting file playback, {len(file_play_events)} events")
                loop_start = time.time()
                
                # Send program changes
                program_changes = {}
                for event in file_play_events:
                    if event['type'] == 'program_change':
                        program_changes[event['channel']] = event['program']
                
                print(f"[THREAD] Sending {len(program_changes)} program changes")
                for ch, prog in program_changes.items():
                    if ch == 9:
                        try:
                            fs.program_select(ch, sfid, 128, prog)
                        except:
                            fs.program_select(ch, sfid, 0, prog)
                    else:
                        fs.program_select(ch, sfid, 0, prog)
                    fs.program_change(ch, prog)
                
                time.sleep(0.1)
                
                # Play events
                notes_played = 0
                for event in file_play_events:
                    with playback_lock:
                        if playback_mode != PLAYBACK_FILE:
                            print("[THREAD] Playback stopped")
                            # Stop pressed - kill active notes
                            for ch in list(active_notes.keys()):
                                for note in list(active_notes[ch]):
                                    try:
                                        fs.noteoff(ch, note)
                                    except:
                                        pass
                                active_notes[ch].clear()
                            break
                    
                    file_play_position = event['time']
                    
                    target_time = loop_start + event['time']
                    now = time.time()
                    
                    if target_time > now:
                        if not responsive_sleep(target_time - now, lambda: playback_mode == PLAYBACK_FILE):
                            # print("[THREAD] Responsive sleep interrupted")
                            break
                    
                    try:
                        msg_type = event['type']
                        ch = event['channel']
                        
                        if msg_type == 'note_on':
                            fs.noteon(ch, event['note'], event['velocity'])
                            active_notes[ch].add(event['note'])
                            notes_played += 1
                            # Removed debug print for performance
                        elif msg_type == 'note_off':
                            fs.noteoff(ch, event['note'])
                            active_notes[ch].discard(event['note'])
                        elif msg_type == 'control_change':
                            fs.cc(ch, event['control'], event['value'])
                    except Exception as e:
                        print(f"[FILE PLAY ERROR] {e}")
                
                # Clean finish
                # print(f"[THREAD] File playback complete, played {notes_played} notes")
                
                # Clean up any remaining active notes
                for ch in list(active_notes.keys()):
                    for note in list(active_notes[ch]):
                        try:
                            fs.noteoff(ch, note)
                        except:
                            pass
                    active_notes[ch].clear()
                
                # Reset mode so we're ready for next play
                with playback_lock:
                    playback_mode = PLAYBACK_NONE
                file_play_position = 0.0

            else:
                time.sleep(0.01)

        except Exception as e:
            import traceback
            print(f"[PLAYBACK THREAD ERROR] {e}")
            traceback.print_exc()
            time.sleep(0.1)

# Don't auto-start - will be started on first play
# threading.Thread(target=loop_playback_thread, daemon=True).start()

def loop_monitor_thread():
    """Monitor loop recording progress based on time"""
    global loop_bar_count, loop_recording, loop_playback, loop_file_path, loop_start_time, metronome_on
    global loop_midi_events, playback_mode
    
    while not SHUTTING_DOWN:
        try:
            if loop_recording:
                current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
                beats_per_bar = 4
                seconds_per_beat = 60.0 / current_bpm
                seconds_per_bar = seconds_per_beat * beats_per_bar
                total_loop_seconds = seconds_per_bar * loop_length
                
                # Calculate elapsed time
                elapsed = time.time() - loop_start_time
                
                # Calculate current bar
                loop_bar_count = int(elapsed / seconds_per_bar)
                
                # Check if loop is complete
                if elapsed >= total_loop_seconds:
                    # Capture the intended loop boundary time BEFORE any processing delay
                    intended_loop_end = loop_start_time + total_loop_seconds

                    # Loop complete - stop recording and start playback
                    global metronome_on
                    loop_recording = False
                    metronome_on = False  # Turn off metronome during loop playback
                    
                    if recorder.recording:
                        temp_path = os.path.join(midi_file_folder, "_loop_temp.mid")
                        recorder.stop(temp_path)
                        
                        # Load MIDI events from file
                        try:
                            import mido
                            mid = mido.MidiFile(temp_path)
                            loop_midi_events = []
                            current_time = 0
                            
                            for track in mid.tracks:
                                for msg in track:
                                    current_time += mido.tick2second(msg.time, mid.ticks_per_beat, 500000)
                                    
                                    if msg.type == 'note_on' and msg.velocity > 0:
                                        loop_midi_events.append({
                                            'time': current_time,
                                            'type': 'note_on',
                                            'channel': msg.channel,
                                            'note': msg.note,
                                            'velocity': msg.velocity
                                        })
                                    elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                                        loop_midi_events.append({
                                            'time': current_time,
                                            'type': 'note_off',
                                            'channel': msg.channel,
                                            'note': msg.note
                                        })
                                    elif msg.type == 'control_change':
                                        loop_midi_events.append({
                                            'time': current_time,
                                            'type': 'control_change',
                                            'channel': msg.channel,
                                            'control': msg.control,
                                            'value': msg.value
                                        })
                                    elif msg.type == 'program_change':
                                        loop_midi_events.append({
                                            'time': current_time,
                                            'type': 'program_change',
                                            'channel': msg.channel,
                                            'program': msg.program
                                        })
                        except Exception as e:
                            print(f"Error loading loop: {e}")
                        
                        loop_file_path = temp_path
                        loop_playback = True
                        loop_bar_count = 0
                        # Use intended boundary time not time.time() - eliminates file loading delay offset
                        loop_start_time = intended_loop_end
                        
                        # Start playback using new system
                        start_playback_thread_once()
                        with playback_lock:
                            playback_mode = PLAYBACK_LIVE_LOOP
                        
                        # Start overdub recording
                        recorder.start()
                
                time.sleep(0.1)  # Check 10 times per second
            elif loop_playback:
                current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
                beats_per_bar = 4
                seconds_per_beat = 60.0 / current_bpm
                seconds_per_bar = seconds_per_beat * beats_per_bar
                total_loop_seconds = seconds_per_bar * loop_length
                
                # Calculate elapsed time since loop started playing
                elapsed = time.time() - loop_start_time
                
                # Calculate current bar
                loop_bar_count = int(elapsed / seconds_per_bar)
                
                # Check if we need to merge overdub
                if elapsed >= total_loop_seconds:
                    loop_bar_count = 0
                    loop_start_time = time.time()
                    
                    # If currently overdubbing, merge new events
                    if recorder.recording:
                        # Save current overdub
                        overdub_path = os.path.join(midi_file_folder, "_overdub_temp.mid")
                        recorder.stop(overdub_path)
                        
                        # Load and merge overdub events
                        try:
                            import mido, copy
                            overdub_mid = mido.MidiFile(overdub_path)
                            current_time = 0
                            new_events = []

                            for track in overdub_mid.tracks:
                                for msg in track:
                                    current_time += mido.tick2second(msg.time, overdub_mid.ticks_per_beat, 500000)
                                    
                                    if msg.type == 'note_on' and msg.velocity > 0:
                                        new_events.append({
                                            'time': current_time,
                                            'type': 'note_on',
                                            'channel': msg.channel,
                                            'note': msg.note,
                                            'velocity': msg.velocity
                                        })
                                    elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                                        new_events.append({
                                            'time': current_time,
                                            'type': 'note_off',
                                            'channel': msg.channel,
                                            'note': msg.note
                                        })
                                    elif msg.type == 'control_change':
                                        new_events.append({
                                            'time': current_time,
                                            'type': 'control_change',
                                            'channel': msg.channel,
                                            'control': msg.control,
                                            'value': msg.value
                                        })
                                    elif msg.type == 'program_change':
                                        new_events.append({
                                            'time': current_time,
                                            'type': 'program_change',
                                            'channel': msg.channel,
                                            'program': msg.program
                                        })
                            
                            # Only save undo state and merge if new notes were actually recorded
                            has_notes = any(e['type'] == 'note_on' for e in new_events)
                            if has_notes:
                                loop_undo_stack.append(copy.deepcopy(loop_midi_events))
                                if len(loop_undo_stack) > MAX_UNDO_LEVELS:
                                    loop_undo_stack.pop(0)
                                print(f"[UNDO] Saved state (stack size: {len(loop_undo_stack)})")
                                loop_midi_events.extend(new_events)
                                loop_midi_events.sort(key=lambda x: x['time'])
                            else:
                                print("[UNDO] No new notes recorded, skipping undo save")
                            
                            # Clean up overdub temp
                            os.remove(overdub_path)
                        except Exception as e:
                            print(f"Error merging overdub: {e}")
                        
                        # Restart overdub recording
                        recorder.start()
                
                time.sleep(0.1)
            else:
                time.sleep(0.2)
        except:
            time.sleep(0.2)

threading.Thread(target=loop_monitor_thread, daemon=True).start()

def metronome_worker():
    while True:
        # Use Bluetooth BPM if available, otherwise manual BPM
        current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
        
        if metronome_on and fs:
            try:
                fs.noteon(9, 76, 110) 
                time.sleep(0.05)
                fs.noteoff(9, 76)
                time.sleep((60.0 / current_bpm) - 0.05)
            except: 
                time.sleep(0.1)
        else:
            time.sleep(0.2)

threading.Thread(target=metronome_worker, daemon=True).start()

# --- 7. UPS MONITOR ---
class UPS_C:
    def __init__(self, addr=0x43):
        self.bus = None; self.addr = addr; self.readings = []
        try: self.bus = smbus.SMBus(1)
        except: pass
    def get_voltage(self):
        if not self.bus: return 0.0
        try:
            for attempt in range(3):
                read = self.bus.read_word_data(self.addr, 0x02)
                swapped = ((read << 8) & 0xFF00) | ((read >> 8) & 0x00FF)
                v = (swapped >> 3) * 0.004
                if 2.5 <= v <= 5.0:
                    self.readings.append(v)
                    if len(self.readings) > 20: self.readings.pop(0)
                    return sorted(self.readings)[len(self.readings)//2]
            return sorted(self.readings)[len(self.readings)//2] if self.readings else 0.0
        except: return 0.0
    def get_capacity_percent(self):
        """Convert voltage to % using realistic Li-ion discharge curve"""
        v = self.get_voltage()
        if v == 0.0: return 0
        curve = [
            (4.20, 100), (4.15, 98), (4.10, 95), (4.05, 91),
            (4.00, 86),  (3.95, 81), (3.90, 76), (3.85, 70),
            (3.80, 63),  (3.75, 56), (3.70, 48), (3.65, 41),
            (3.60, 34),  (3.55, 27), (3.50, 21), (3.45, 15),
            (3.40, 10),  (3.30, 6),  (3.20, 3),  (3.10, 1),
            (3.00, 0)
        ]
        if v >= curve[0][0]: return 100
        if v <= curve[-1][0]: return 0
        for i in range(len(curve) - 1):
            v_high, p_high = curve[i]
            v_low, p_low = curve[i + 1]
            if v_low <= v <= v_high:
                ratio = (v - v_low) / (v_high - v_low)
                return int(p_low + ratio * (p_high - p_low))
        return 0

    def get_time_left(self):
        v = self.get_voltage()
        if v == 0.0: return "N/A"
        p = self.get_capacity_percent()
        total_minutes = (p / 100) * (160 if LOW_POWER_MODE else 130)
        return f"{int(total_minutes // 60)}h{int(total_minutes % 60):02d}m"

ups = UPS_C()

# --- 8. UI STATE ---
MAIN_MENU = ["MIDI KEYBOARD", "SOUND FONT", "MIDI FILE", "MIXER", "DRUM KIT", "RECORD", "STOP LOOP", "UNDO OVERDUB", "LOOP LENGTH", "METRONOME", "VOLUME", "POWER", "SHUTDOWN"]
files = MAIN_MENU.copy()
pathes = MAIN_MENU.copy()
selectedindex = 0
operation_mode = "main screen"
selected_file_path = ""
rename_string = ""

rename_chars = [" ", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", 
                "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", 
                "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "_", "-", "←", "→", "✓"]  # ← = move left, → = move right, ✓ = save

rename_char_idx = 0
rename_cursor_pos = 0  # Position in the string being edited
rename_edit_mode = False  # False = change char, True = move cursor
rename_scroll_count = 0  # Track consecutive scrolls for acceleration
last_rename_scroll_time = 0  # Track when last scroll happened
channel_volumes = {i: 100 for i in range(16)}
load_mixer() # Load settings on start
mixer_selected_ch = 0
mixer_adjusting = False
channel_presets = {}
last_active_channel = None  # Track which channel last received a note

# BACK button press tracking for cancel recording (3 presses)
back_press_count = 0
back_press_last_time = 0
BACK_PRESS_TIMEOUT = 2.0  # Reset counter if more than 2 seconds between presses
last_active_time = 0  # Timestamp of last activity
DEBOUNCE_MS = 0.15  # 150ms debounce window for all buttons
_last_button_time = {"up": 0, "down": 0, "select": 0, "back": 0}
selected_drum_kit = 0  # Currently selected drum kit program (0-127)
available_drum_kits = []  # List of (program_number, name) tuples for available drums
drum_kit_index = 0  # Index in available_drum_kits list

# --- 9. HARDWARE INITIALIZATION ---
rtmidi = fluidsynth = st7789 = None
Image = ImageDraw = ImageFont = None
fs = None; sfid = None; loaded_sf2_path = None; disp = None
img = draw = font = font_tiny = None
_last_display_time = 0.0
soundfont_paths, soundfont_names = [], []; midi_paths, midi_names = [], []
init_complete = False  # Blocks update_display until background_init finishes

def lazy_imports():
    global rtmidi, fluidsynth, st7789, Image, ImageDraw, ImageFont
    import rtmidi, fluidsynth, st7789
    from PIL import Image, ImageDraw, ImageFont

def init_buttons():
    global button_up, button_down, button_select, button_back
    from gpiozero import Button
    # Adding pull_up=True is essential for Pirate Audio buttons
    button_up = Button(16, pull_up=True)
    button_down = Button(24, pull_up=True)
    button_select = Button(5, pull_up=True)
    button_back = Button(6, pull_up=True)

def init_display():
    global disp, img, draw, font, font_tiny
    if disp is not None:
        return  # Already initialised by early splash
    try:
        import st7789 as st_lib
        disp = st_lib.ST7789(width=240, height=240, rotation=90, port=0, cs=st_lib.BG_SPI_CS_FRONT, dc=9, backlight=13, spi_speed_hz=24_000_000)
        disp.begin()
        img = Image.new("RGB", (240, 240), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        try: 
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
            font_tiny = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except: 
            font = ImageFont.load_default(); font_tiny = ImageFont.load_default()
    except: pass

def init_fluidsynth_lazy():
    global fs
    if fs is None:
        try:
            import fluidsynth as fs_lib
            fs = fs_lib.Synth()

            # ==============================
            # AUDIO BUFFER (OPTIMIZED FOR PI ZERO 2W)
            # ==============================
            # Pi Zero 2W needs larger buffer than Pi 4/5 to avoid dropouts
            fs.setting('audio.period-size', 128)   # 64 may cause xruns on Zero 2W
            fs.setting('audio.periods', 3)         # 3 periods for stability
            fs.setting('audio.sample-format', 'float')
            fs.setting('audio.realtime-prio', 70)  # Lower priority for Zero 2W

            # ==============================
            # SYNTH CORE (LIGHTER FOR ZERO 2W)
            # ==============================
            fs.setting('synth.gain', volume_level)
            fs.setting('synth.polyphony', 48 if LOW_POWER_MODE else 64)  # Lower for Zero
            fs.setting('synth.cpu-cores', 4)       # Zero 2W has 4 cores
            fs.setting('synth.dynamic-sample-loading', 1)

            # ==============================
            # EFFECTS OFF (CRITICAL ON ZERO 2W)
            # ==============================
            fs.setting('synth.reverb.active', 0)
            fs.setting('synth.chorus.active', 0)
            fs.setting('synth.ladspa.active', 0)   # Disable LADSPA effects

            # ==============================
            # MIDI
            # ==============================
            #fs.setting('midi.autoconnect', 0)

            # ==============================
            # START
            # ==============================
            fs.start(driver="alsa", device="hw:0,0")

            print("[AUDIO] FluidSynth low-latency mode active")

        except Exception as e:
            print(f"[AUDIO] FluidSynth init error: {e}")

# --- 10. POWER MANAGEMENT ---
def undo_last_overdub():
    """Undo the last overdub by restoring previous loop state"""
    global loop_midi_events, loop_undo_stack, MESSAGE, msg_start_time
    global loop_playback, loop_recording, playback_mode

    if not loop_undo_stack:
        MESSAGE = "Nothing to undo"
        msg_start_time = time.time()
        print("[UNDO] No undo history available")
        return

    was_playing = loop_playback

    # Pause playback engine so it stops iterating old events
    with playback_lock:
        playback_mode = PLAYBACK_NONE

    # Restore previous state
    loop_midi_events = loop_undo_stack.pop()
    loop_recording = False

    # If loop was playing, restart it with the restored events
    if was_playing:
        with playback_lock:
            playback_mode = PLAYBACK_LIVE_LOOP
        print(f"[UNDO] Restarted playback with restored state")

    MESSAGE = f"Undone! ({len(loop_undo_stack)} left)"
    msg_start_time = time.time()
    print(f"[UNDO] Restored previous state ({len(loop_undo_stack)} undo levels remaining)")

def toggle_power_mode():
    global LOW_POWER_MODE, MESSAGE, msg_start_time
    LOW_POWER_MODE = not LOW_POWER_MODE
    if LOW_POWER_MODE:
        os.system("sudo tvservice -o > /dev/null 2>&1")
        os.system("sudo rfkill block wifi")
        os.system("echo powersave | sudo tee /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor > /dev/null")
        os.system(f"echo none | sudo tee /sys/class/leds/{LED_NAME}/trigger > /dev/null")
        os.system(f"echo 0 | sudo tee /sys/class/leds/{LED_NAME}/brightness > /dev/null")
        if fs: fs.setting('synth.polyphony', 48)
        MESSAGE = "Lean: ON (ECO)"
    else:
        os.system("sudo tvservice -p > /dev/null 2>&1")
        os.system("sudo rfkill unblock wifi")
        os.system("echo ondemand | sudo tee /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor > /dev/null")
        os.system(f"echo mmc0 | sudo tee /sys/class/leds/{LED_NAME}/trigger > /dev/null")
        os.system(f"echo 1 | sudo tee /sys/class/leds/{LED_NAME}/brightness > /dev/null")
        if fs: fs.setting('synth.polyphony', 96)
        MESSAGE = "Lean: OFF (MAX)"
    msg_start_time = time.time()

# --- 11. MIDI ENGINE & PRESETS ---
def build_sf2_preset_map(path):
    mapping = {}
    try:
        from sf2utils.sf2parse import Sf2File
        with open(path, "rb") as f:
            sf2 = Sf2File(f)
            for p in sf2.presets:
                b = getattr(p, 'bank', 0); pr = getattr(p, 'preset', None)
                if pr is not None: mapping[(int(b), int(pr))] = str(getattr(p, 'name', f"P{pr}"))
        return mapping, True
    except: return {}, False

def scan_drum_kits():
    """Scan loaded soundfont for available drum kits (bank 128)"""
    global available_drum_kits
    available_drum_kits = []
    
    if loaded_sf2_path is None:
        return
    
    try:
        from sf2utils.sf2parse import Sf2File
        with open(loaded_sf2_path, "rb") as f:
            sf2 = Sf2File(f)
            for p in sf2.presets:
                bank = getattr(p, 'bank', 0)
                prog = getattr(p, 'preset', None)
                name = str(getattr(p, 'name', f"Kit {prog}"))
                
                # Bank 128 = percussion/drums
                if bank == 128 and prog is not None:
                    available_drum_kits.append((int(prog), name))
        
        # Sort by program number
        available_drum_kits.sort(key=lambda x: x[0])
        
        # If no drum kits found, add default
        if not available_drum_kits:
            available_drum_kits = [(0, "Standard Kit")]
    except:
        # Fallback if sf2utils not available or error
        available_drum_kits = [
            (0, "Standard Kit"),
            (8, "Room Kit"),
            (16, "Power Kit"),
            (24, "Electronic Kit"),
            (25, "TR-808 Kit"),
            (32, "Jazz Kit"),
            (40, "Brush Kit"),
            (48, "Orchestra Kit")
        ]

def get_internal_channel(monkey_ch):
    if monkey_ch == 0: return 9
    elif monkey_ch <= 9: return monkey_ch - 1
    else: return monkey_ch

def select_first_presets_for_monkey():
    global channel_presets
    if sfid is None or fs is None: return
    mapping, ok = build_sf2_preset_map(loaded_sf2_path)
    for i in range(16): fs.cc(i, 7, channel_volumes.get(i, 100))
    fs.cc(9, 7, metro_vol)
    try: fs.program_select(9, sfid, 128, 0)
    except: fs.program_select(9, sfid, 0, 0)
    fs.program_change(9, 0)
    channel_presets[9] = mapping.get((128, 0), "Drums") if ok else "Drums"
    for m_ch in range(1, 10):
        f_ch = get_internal_channel(m_ch); prog = m_ch - 1
        fs.program_select(f_ch, sfid, 0, prog); fs.program_change(f_ch, prog)
        channel_presets[f_ch] = mapping.get((0, prog), f"Preset {prog}") if ok else f"Preset {prog}"
    for midi_ch in range(10, 16):
        fs.program_select(midi_ch, sfid, 0, midi_ch); fs.program_change(midi_ch, midi_ch)
        channel_presets[midi_ch] = mapping.get((0, midi_ch), f"Preset {midi_ch}") if ok else f"Preset {midi_ch}"

class MultiMidiIn:
    """MIDI manager that supports multiple simultaneous input connections"""
    def __init__(self):
        import rtmidi as rt_lib
        self.rt_lib = rt_lib
        self.active_ports = {}  # {port_name: MidiIn object}
        self.callback = None
    
    def set_callback(self, cb):
        """Set callback for all current and future MIDI inputs"""
        self.callback = cb
        for port_name, midiin in self.active_ports.items():
            if midiin.is_port_open():
                midiin.set_callback(self._make_callback(port_name))
    
    def _make_callback(self, port_name):
        """Create a callback wrapper that identifies which port the message came from"""
        def _cb(msg, ts):
            if self.callback:
                self.callback(msg, ts)
        return _cb
    
    def toggle_port_by_name(self, name):
        """Toggle a MIDI port on/off"""
        if name in self.active_ports:
            # Disconnect this port
            self.disconnect_port(name)
            return False  # Now disconnected
        else:
            # Connect this port
            self.connect_port(name)
            return True  # Now connected
    
    def connect_port(self, name):
        """Connect to a MIDI port"""
        def connect_thread():
            global MESSAGE, msg_start_time
            try:
                # Get all available ports
                temp_midi = self.rt_lib.MidiIn()
                ports = temp_midi.get_ports()
                
                if name in ports:
                    # Create new MidiIn for this port
                    midiin = self.rt_lib.MidiIn()
                    midiin.open_port(ports.index(name))
                    
                    # Set callback
                    if self.callback:
                        midiin.set_callback(self._make_callback(name))
                    
                    # Store in active ports
                    self.active_ports[name] = midiin
                    
                    MESSAGE = f"Conn: {name[:16]}"
                    msg_start_time = time.time()
            except Exception as e:
                MESSAGE = f"Error: {str(e)[:15]}"
                msg_start_time = time.time()
        
        threading.Thread(target=connect_thread, daemon=True).start()
    
    def disconnect_port(self, name):
        """Disconnect a MIDI port"""
        global MESSAGE, msg_start_time
        if name in self.active_ports:
            try:
                self.active_ports[name].close_port()
                del self.active_ports[name]
                
                MESSAGE = f"Disconn: {name[:13]}"
                msg_start_time = time.time()
            except:
                pass
    
    def list_ports(self):
        """List all available MIDI ports"""
        temp_midi = self.rt_lib.MidiIn()
        return temp_midi.get_ports()
    
    def get_connected_ports(self):
        """Get list of currently connected port names"""
        return list(self.active_ports.keys())
    
    def is_port_connected(self, name):
        """Check if a port is currently connected"""
        return name in self.active_ports
    
    # Legacy compatibility methods
    def open_port_by_name_async(self, name):
        """Legacy method - toggles port instead"""
        self.toggle_port_by_name(name)

def midi_callback(message_data, timestamp):
    global midi_transport_state, last_active_channel, last_activity_time, last_midi_activity
    import mido
    import time
    message, _ = message_data
    
    # Track MIDI activity for cleanup thread
    last_midi_activity = time.time()
    
    if len(message) == 0:
        return
    
    if DEBUG_MIDI:
        print(f"MIDI: {' '.join(f'{b:02X}' for b in message)}")
    
    # Handle system messages (0xF0 - 0xFF)
    if message[0] >= 0xF0:
        system_status = message[0]
        
        if system_status == 0xFA:
            midi_transport_state = "playing"
            if DEBUG_MIDI:
                print("MIDI: Transport START")
        elif system_status == 0xFC:
            midi_transport_state = "stopped"
            if DEBUG_MIDI:
                print("MIDI: Transport STOP")
        elif system_status == 0xFB:
            midi_transport_state = "playing"
            if DEBUG_MIDI:
                print("MIDI: Transport CONTINUE")
        elif system_status == 0xFF:
            # System Reset
            if fs:
                for ch in range(16):
                    fs.cc(ch, 123, 0)  # All Notes Off
                    fs.cc(ch, 64, 0)   # Release sustain
            if DEBUG_MIDI:
                print("MIDI: System RESET")
        
        return  # Don't process system messages further
    
    # Standard MIDI channel messages
    status, ch = message[0] & 0xF0, message[0] & 0x0F
    n1 = message[1] if len(message) > 1 else 0
    n2 = message[2] if len(message) > 2 else 0
    
    # Recording
    if recorder.recording:
        if status == 0x90:
            recorder.add_event(mido.Message('note_on', channel=ch, note=n1, velocity=n2))
            if DEBUG_MIDI:
                print(f"[RECORD] Note ON ch={ch} note={n1} vel={n2}")
        elif status == 0x80:
            recorder.add_event(mido.Message('note_off', channel=ch, note=n1, velocity=n2))
        elif status == 0xB0:
            recorder.add_event(mido.Message('control_change', channel=ch, control=n1, value=n2))
        elif status == 0xC0:
            recorder.add_event(mido.Message('program_change', channel=ch, program=n1))
    
    # Playback to FluidSynth
    if fs:
        if status == 0x90:  # Note on
            if n2 == 0:
                fs.noteoff(ch, n1)
            else:
                # Mark this channel as active
                last_active_channel = ch
                last_activity_time[ch] = time.time()
                fs.noteon(ch, n1, n2)
        elif status == 0x80:  # Note off
            fs.noteoff(ch, n1)
        elif status == 0xB0:  # Control change
            # Track sustain for cleanup
            if n1 == 64:
                sustain_state[ch] = n2 >= 64
            fs.cc(ch, n1, n2)
        elif status == 0xC0:  # Program change
            fs.program_change(ch, n1)
        elif status == 0xE0:  # Pitch bend
            fs.pitch_bend(ch, (n2 << 7) + n1 - 8192)

# --- 12. FILE SCANS ---
def scan_soundfonts():
    global soundfont_paths, soundfont_names
    p, l = [], []
    if os.path.isdir(soundfont_folder):
        for f in os.listdir(soundfont_folder):
            if f.endswith('.sf2'): p.append(os.path.join(soundfont_folder, f)); l.append(f.replace('.sf2', ''))
    soundfont_paths, soundfont_names = p, l

def scan_midifiles():
    global midi_paths, midi_names
    p, l = [], []
    if os.path.isdir(midi_file_folder):
        for f in sorted(os.listdir(midi_file_folder)):
            if f.endswith('.mid'): p.append(os.path.join(midi_file_folder, f)); l.append(f.replace('.mid', ''))
    midi_paths, midi_names = p, l

# --- 13. BUTTON HANDLERS ---
def handle_up():
    if SHUTTING_DOWN: return
    now = time.time()
    if now - _last_button_time["up"] < DEBOUNCE_MS: return
    _last_button_time["up"] = now
    global selectedindex, volume_level, rename_char_idx, channel_volumes, mixer_selected_ch, bpm, metro_vol, metro_adjusting, drum_kit_index, selected_drum_kit, loop_length
    global rename_scroll_count, last_rename_scroll_time, rename_cursor_pos
    if operation_mode == "VOLUME":
        volume_level = min(1.0, volume_level + 0.05)
        if fs: fs.setting('synth.gain', volume_level)
    elif operation_mode == "DRUM KIT":
        # Navigate to previous drum kit
        drum_kit_index = max(0, drum_kit_index - 1)
        if available_drum_kits:
            selected_drum_kit = available_drum_kits[drum_kit_index][0]
            # Apply immediately
            if fs and sfid:
                try:
                    fs.program_select(9, sfid, 128, selected_drum_kit)
                    fs.program_change(9, selected_drum_kit)
                    mapping, ok = build_sf2_preset_map(loaded_sf2_path)
                    channel_presets[9] = mapping.get((128, selected_drum_kit), f"Drums {selected_drum_kit}") if ok else f"Drums {selected_drum_kit}"
                except: pass
    elif operation_mode == "RENAME":
        # Fast scroll with acceleration
        now = time.time()
        if now - last_rename_scroll_time < 0.3:  # If scrolling rapidly (< 300ms between presses)
            rename_scroll_count = min(10, rename_scroll_count + 1)  # Accelerate up to 10x
        else:
            rename_scroll_count = 1  # Reset to normal speed
        last_rename_scroll_time = now
        
        rename_char_idx = (rename_char_idx - rename_scroll_count) % len(rename_chars)
    elif operation_mode == "MIXER":
        if mixer_adjusting:
            f_ch = get_internal_channel(mixer_selected_ch)
            channel_volumes[f_ch] = min(127, channel_volumes[f_ch] + 5)
            if fs: fs.cc(f_ch, 7, channel_volumes[f_ch])
        else: mixer_selected_ch = max(0, mixer_selected_ch - 1)
    elif operation_mode == "LOOP LENGTH":
        # Cycle through loop lengths: 4, 8, 16, 32
        global loop_length
        if loop_length == 4:
            loop_length = 8
        elif loop_length == 8:
            loop_length = 16
        elif loop_length == 16:
            loop_length = 32
        else:
            loop_length = 4
    elif operation_mode == "METRONOME":
        if metro_adjusting:
            if selectedindex == 1: bpm = min(240, bpm + 5)
            elif selectedindex == 2: 
                metro_vol = min(127, metro_vol + 5)
                if fs: fs.cc(9, 7, metro_vol)
        else: selectedindex = max(0, selectedindex - 1)
    else: selectedindex = max(0, selectedindex - 1)

def handle_down():
    if SHUTTING_DOWN: return
    now = time.time()
    if now - _last_button_time["down"] < DEBOUNCE_MS: return
    _last_button_time["down"] = now
    global selectedindex, volume_level, rename_char_idx, channel_volumes, mixer_selected_ch, bpm, metro_vol, metro_adjusting, drum_kit_index, selected_drum_kit, loop_length
    global rename_scroll_count, last_rename_scroll_time, rename_cursor_pos
    if operation_mode == "VOLUME":
        volume_level = max(0.0, volume_level - 0.05)
        if fs: fs.setting('synth.gain', volume_level)
    elif operation_mode == "DRUM KIT":
        # Navigate to next drum kit
        if available_drum_kits:
            drum_kit_index = min(len(available_drum_kits) - 1, drum_kit_index + 1)
            selected_drum_kit = available_drum_kits[drum_kit_index][0]
            # Apply immediately
            if fs and sfid:
                try:
                    fs.program_select(9, sfid, 128, selected_drum_kit)
                    fs.program_change(9, selected_drum_kit)
                    mapping, ok = build_sf2_preset_map(loaded_sf2_path)
                    channel_presets[9] = mapping.get((128, selected_drum_kit), f"Drums {selected_drum_kit}") if ok else f"Drums {selected_drum_kit}"
                except: pass
    elif operation_mode == "RENAME":
        # Fast scroll with acceleration
        now = time.time()
        if now - last_rename_scroll_time < 0.3:  # If scrolling rapidly (< 300ms between presses)
            rename_scroll_count = min(10, rename_scroll_count + 1)  # Accelerate up to 10x
        else:
            rename_scroll_count = 1  # Reset to normal speed
        last_rename_scroll_time = now
        
        rename_char_idx = (rename_char_idx + rename_scroll_count) % len(rename_chars)
    elif operation_mode == "MIXER":
        if mixer_adjusting:
            f_ch = get_internal_channel(mixer_selected_ch)
            channel_volumes[f_ch] = max(0, channel_volumes[f_ch] - 5)
            if fs: fs.cc(f_ch, 7, channel_volumes[f_ch])
        else: mixer_selected_ch = min(15, mixer_selected_ch + 1)  # Changed from 9 to 15 for 16 channels
    elif operation_mode == "LOOP LENGTH":
        # Cycle through loop lengths: 4, 8, 16, 32
        global loop_length
        if loop_length == 32:
            loop_length = 16
        elif loop_length == 16:
            loop_length = 8
        elif loop_length == 8:
            loop_length = 4
        else:
            loop_length = 32
    elif operation_mode == "METRONOME":
        if metro_adjusting:
            if selectedindex == 1: bpm = max(40, bpm - 5)
            elif selectedindex == 2: 
                metro_vol = max(0, metro_vol - 5)
                if fs: fs.cc(9, 7, metro_vol)
        else: selectedindex = min(2, selectedindex + 1)
    else: selectedindex = min(len(files) - 1, selectedindex + 1)

def handle_back():
    if SHUTTING_DOWN: return
    now = time.time()
    if now - _last_button_time["back"] < DEBOUNCE_MS: return
    _last_button_time["back"] = now
    global operation_mode, files, pathes, selectedindex, rename_string, mixer_adjusting, metro_adjusting
    global MESSAGE, msg_start_time, recorder, loop_midi_events, loop_undo_stack
    global loop_recording, loop_playback, metronome_on
    global back_press_count, back_press_last_time
    
    current_time = time.time()
    
    # PRIORITY 1: If overdubbing (loop exists + recording), single BACK = undo current overdub
    if recorder.recording and loop_midi_events:  # Overdubbing
        # Reset 3x cancel counter if too much time passed
        if current_time - back_press_last_time > BACK_PRESS_TIMEOUT:
            back_press_count = 0
        
        back_press_count += 1
        back_press_last_time = current_time
        
        if back_press_count == 1:
            # First press during overdub = UNDO current overdub, restart recording
            print("[UNDO] Cancelling current overdub, restarting")
            
            # Discard current overdub recording
            try:
                import os
                temp_path = os.path.join(midi_file_folder, "_temp_discard.mid")
                recorder.stop(temp_path)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except:
                pass
            
            # Restore previous loop state from undo stack
            if loop_undo_stack:
                import copy
                loop_midi_events = copy.deepcopy(loop_undo_stack[-1])
                MESSAGE = "Undone! Rec restarted"
                print(f"[UNDO] Restored to previous state, {len(loop_undo_stack)} undo levels available")
            else:
                MESSAGE = "Overdub cancelled - restarting"
                print("[UNDO] No undo history, just cancelling current overdub")
            
            msg_start_time = current_time
            
            # Restart overdub recording immediately
            recorder.start()
            return
            
        elif back_press_count == 2:
            MESSAGE = "Press BACK 1 more time to cancel"
            msg_start_time = current_time
            print("[CANCEL] BACK pressed 2/3 times")
            return
            
        elif back_press_count >= 3:
            # CANCEL THE OVERDUB!
            print("[CANCEL] Overdub cancelled by user (3x BACK)")
            
            try:
                import os
                temp_path = os.path.join(midi_file_folder, "_cancelled.mid")
                recorder.stop(temp_path)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except:
                pass
            
            # Stop loop playback AND recording
            loop_recording = False
            loop_playback = False
            metronome_on = False
            back_press_count = 0
            
            # Clear loop events AND undo stack
            loop_midi_events.clear()
            loop_undo_stack.clear()
            
            MESSAGE = "Overdub cancelled"
            msg_start_time = current_time
            operation_mode = "main screen"
            files[:] = MAIN_MENU.copy()
            print("[CANCEL] Cleared loop and undo stack")
            return
    
    # PRIORITY 2: If recording initial loop (no loop exists yet), 3x BACK = cancel
    elif recorder.recording and not loop_midi_events:  # Initial recording
        # Reset counter if too much time passed
        if current_time - back_press_last_time > BACK_PRESS_TIMEOUT:
            back_press_count = 0
        
        back_press_count += 1
        back_press_last_time = current_time
        
        if back_press_count == 1:
            MESSAGE = "Press BACK 2 more times to cancel"
            msg_start_time = current_time
            print("[CANCEL] BACK pressed 1/3 times")
        elif back_press_count == 2:
            MESSAGE = "Press BACK 1 more time to cancel"
            msg_start_time = current_time
            print("[CANCEL] BACK pressed 2/3 times")
        elif back_press_count >= 3:
            # CANCEL THE RECORDING!
            print("[CANCEL] Recording cancelled by user (3x BACK)")
            
            try:
                import os
                temp_path = os.path.join(midi_file_folder, "_cancelled.mid")
                recorder.stop(temp_path)
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except:
                pass
            
            # Stop everything and clear loop
            loop_recording = False
            loop_playback = False
            metronome_on = False
            back_press_count = 0
            
            # Clear loop events to stop playback
            loop_midi_events.clear()
            loop_undo_stack.clear()
            
            MESSAGE = "Recording cancelled"
            msg_start_time = current_time
            operation_mode = "main screen"
            files[:] = MAIN_MENU.copy()
            return
        
        return  # Don't continue to normal BACK behavior while recording
    
    # Reset counter when not recording
    back_press_count = 0
    
    # Normal BACK button behavior (not recording)
    if operation_mode == "MIXER":
        if mixer_adjusting: mixer_adjusting = False; return
        else: save_mixer() 
    if operation_mode == "METRONOME" and metro_adjusting: metro_adjusting = False; return
    if operation_mode == "RENAME":
        global rename_cursor_pos
        if rename_cursor_pos > 0:
            rename_string = rename_string[:rename_cursor_pos-1] + rename_string[rename_cursor_pos:]
            rename_cursor_pos -= 1
    elif operation_mode == "FILE ACTION":
        operation_mode = "MIDI FILE"; scan_midifiles(); files, pathes = midi_names.copy(), midi_paths.copy()
    else:
        operation_mode = "main screen"; files = MAIN_MENU.copy()
    selectedindex = 0


def handle_select():
    # ALL globals MUST be at the very top to avoid SyntaxErrors
    global operation_mode, files, pathes, selectedindex, MESSAGE, msg_start_time
    global fs, sfid, SHUTTING_DOWN, rename_string, rename_char_idx, rename_cursor_pos
    global mixer_adjusting, metronome_on, selected_file_path, loaded_sf2_path
    global metro_adjusting, loop_recording, loop_playback, loop_file_path
    global loop_bar_count, loop_midi_events, loop_start_time, countdown_value
    global countdown_start, drum_kit_index, loop_length, bpm
    global file_loop_duration, file_loop_start_time # <--- CRITICAL
    global operation_mode, files, pathes, selectedindex, MESSAGE, msg_start_time, fs, sfid, SHUTTING_DOWN
    global rename_string, rename_char_idx, rename_cursor_pos, mixer_adjusting, metronome_on, selected_file_path, loaded_sf2_path, metro_adjusting
    global playback_mode  # NEW - needed for playback system
    global file_play_events, file_loop_events, file_play_path, file_loop_path  # NEW - for file playback
    global file_play_duration, file_loop_duration, file_play_position, file_loop_position  # NEW - for position tracking
    global monkey_pattern_events, monkey_pattern_active  # NEW - for Monkey pattern playback
    global loop_recording, loop_playback, loop_file_path, loop_bar_count, loop_midi_events

    if SHUTTING_DOWN: return
    now = time.time()
    if now - _last_button_time["select"] < DEBOUNCE_MS: return
    _last_button_time["select"] = now
    
    # Normal select button behavior
    
    if operation_mode == "MIXER": mixer_adjusting = not mixer_adjusting; return
    if operation_mode == "METRONOME":
        if selectedindex == 0: metronome_on = not metronome_on
        else: metro_adjusting = not metro_adjusting
        return
        
    if not files and operation_mode != "RENAME": return
    if operation_mode != "RENAME": sel = files[selectedindex]

    if operation_mode == "main screen":
        
        if sel == "MIXER": operation_mode = "MIXER"; return
        if sel == "DRUM KIT":
            # Scan for available drum kits in the loaded soundfont
            scan_drum_kits()
            # Find current drum kit in the list
            global drum_kit_index
            drum_kit_index = 0
            for i, (prog, name) in enumerate(available_drum_kits):
                if prog == selected_drum_kit:
                    drum_kit_index = i
                    break
            operation_mode = "DRUM KIT"
            return
        if sel == "METRONOME": operation_mode = "METRONOME"; selectedindex = 0; return
        if sel == "STOP LOOP" or sel == "PLAY LOOP":
            # Toggle loop playback
            if playback_mode == PLAYBACK_LIVE_LOOP:
                # Stop loop playback
                playback_mode = PLAYBACK_NONE
                MESSAGE = "Loop stopped"
                msg_start_time = time.time()
                print("[LOOP] Loop playback stopped")
            elif loop_midi_events:
                # Start loop playback
                playback_mode = PLAYBACK_LIVE_LOOP
                MESSAGE = "Loop playing"
                msg_start_time = time.time()
                print("[LOOP] Loop playback started")
            else:
                MESSAGE = "No loop to play"
                msg_start_time = time.time()
            return
        if sel == "UNDO OVERDUB":
            undo_last_overdub()
            return
        if sel == "RECORD":
            
            if not recorder.recording:
                # 1. Capture current timing
                current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
                # 2. Calculate how long one beat lasts
                seconds_per_beat = 60.0 / current_bpm
                
                # If no loop exists, clear undo stack (starting fresh)
                if not loop_midi_events:
                    loop_undo_stack.clear()
                    print("[RECORD] Starting fresh recording, cleared undo stack")
                
                countdown_start = time.time()
                operation_mode = "countdown"
                metronome_on = True
                time.sleep(0.1)  # Brief delay to ensure metronome starts
                
                # Start loop recording
                recorder.start()
                loop_recording = True
                loop_bar_count = 0
                loop_start_time = time.time()  # Record start time for accurate timing
                print(f"[RECORD] Started recording, recorder.recording={recorder.recording}, loop_recording={loop_recording}")
                MESSAGE = f"Recording {loop_length} bars"
            else:
                # Stop loop recording and save merged loop
                ts = datetime.datetime.now().strftime("%H%M%S")
                path = os.path.join(midi_file_folder, f"loop_{ts}.mid")
                
                # Stop current overdub recording
                recorder.stop(path)
                
                # If we have loop events in memory, save those instead (they include all overdubs)
                if loop_midi_events:
                    try:
                        import mido
                        # Create new MIDI file from events
                        mid = mido.MidiFile()
                        track = mido.MidiTrack()
                        mid.tracks.append(track)
                        
                        # Add drum kit program change if selected
                        if selected_drum_kit is not None:
                            track.append(mido.Message('program_change', channel=9, program=selected_drum_kit, time=0))
                        
                        # Convert events back to MIDI messages with delta times
                        last_time = 0
                        for event in loop_midi_events:
                            delta = event['time'] - last_time
                            delta_ticks = mido.second2tick(delta, mid.ticks_per_beat, 500000)
                            
                            if event['type'] == 'note_on':
                                track.append(mido.Message('note_on', 
                                    channel=event['channel'], 
                                    note=event['note'], 
                                    velocity=event['velocity'], 
                                    time=int(delta_ticks)))
                            elif event['type'] == 'note_off':
                                track.append(mido.Message('note_off', 
                                    channel=event['channel'], 
                                    note=event['note'], 
                                    velocity=0,
                                    time=int(delta_ticks)))
                            elif event['type'] == 'control_change':
                                track.append(mido.Message('control_change',
                                    channel=event['channel'],
                                    control=event['control'],
                                    value=event['value'],
                                    time=int(delta_ticks)))
                            elif event['type'] == 'program_change':
                                track.append(mido.Message('program_change',
                                    channel=event['channel'],
                                    program=event['program'],
                                    time=int(delta_ticks)))
                            
                            last_time = event['time']
                        
                        # Save the file
                        mid.save(path)
                    except Exception as e:
                        print(f"Error saving merged loop: {e}")
                
                loop_recording = False
                loop_playback = False
                loop_bar_count = 0
                loop_midi_events = []  # Clear events
                metronome_on = False  # Turn off metronome when saving
                
                # Stop playback immediately
                hard_stop_all_playback()
                
                # Clean up temp file
                if loop_file_path and os.path.exists(loop_file_path):
                    try:
                        os.remove(loop_file_path)
                    except:
                        pass
                loop_file_path = None
                MESSAGE = "Loop Saved"
                scan_midifiles()
            msg_start_time = time.time()
            return
        
        if sel == "LOOP LENGTH":
            operation_mode = "LOOP LENGTH"
            selectedindex = 0
            return
        
        if sel == "VOLUME": operation_mode = "VOLUME"; return
        if sel == "POWER": toggle_power_mode(); return
        
        if sel == "SHUTDOWN":
            SHUTTING_DOWN = True 
            time.sleep(0.2)  # Increased delay to prevent double-press
            draw.rectangle((0, 0, 240, 240), fill=(0, 0, 0))
            draw.text((45, 100), "SYSTEM HALT", font=font, fill=(255, 0, 0))
            draw.text((35, 140), "SAFE TO UNPLUG", font=font_tiny, fill=(255, 255, 255))
            disp.display(img)
            if fs: fs.delete()
            time.sleep(1.0)
            os.system("sudo /sbin/poweroff")
            return

        operation_mode = sel
        if sel == "SOUND FONT": scan_soundfonts(); files, pathes = soundfont_names.copy(), soundfont_paths.copy()
        elif sel == "MIDI FILE": scan_midifiles(); files, pathes = midi_names.copy(), midi_paths.copy()
        elif sel == "MIDI KEYBOARD": files = pathes = midi_manager.list_ports()
        selectedindex = 0
    elif operation_mode == "MIDI FILE":
        selected_file_path = pathes[selectedindex]; operation_mode = "FILE ACTION"
        files = ["PLAY", "LOOP", "STOP", "RENAME", "DELETE", "BACK"]; selectedindex = 0
    elif operation_mode == "FILE ACTION":
        if sel == "PLAY":
            if sfid is None:
                MESSAGE = "LOAD SF2 FIRST"
            else:
                # Load MIDI file for playback
                try:
                    import mido
                    init_fluidsynth_lazy()
                    
                    mid = mido.MidiFile(selected_file_path)
                    file_play_events = []
                    current_time = 0
                    
                    for track in mid.tracks:
                        for msg in track:
                            current_time += mido.tick2second(msg.time, mid.ticks_per_beat, 500000)
                            
                            if msg.type == 'note_on' and msg.velocity > 0:
                                file_play_events.append({
                                    'time': current_time,
                                    'type': 'note_on',
                                    'channel': msg.channel,
                                    'note': msg.note,
                                    'velocity': msg.velocity
                                })
                            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                                file_play_events.append({
                                    'time': current_time,
                                    'type': 'note_off',
                                    'channel': msg.channel,
                                    'note': msg.note
                                })
                            elif msg.type == 'control_change':
                                file_play_events.append({
                                    'time': current_time,
                                    'type': 'control_change',
                                    'channel': msg.channel,
                                    'control': msg.control,
                                    'value': msg.value
                                })
                            elif msg.type == 'program_change':
                                file_play_events.append({
                                    'time': current_time,
                                    'type': 'program_change',
                                    'channel': msg.channel,
                                    'program': msg.program
                                })
                    
                    # Calculate duration
                    file_play_duration = max([e['time'] for e in file_play_events]) if file_play_events else 0
                    file_play_position = 0.0
                    file_play_path = selected_file_path
                    
                    # Debug
                    print(f"[PLAY] Loaded {len(file_play_events)} events, duration: {file_play_duration:.2f}s")
                    
                    # Use new playback system
                    start_playback_thread_once()
                    with playback_lock:
                        playback_mode = PLAYBACK_FILE
                    
                    print(f"[PLAY] Playback mode set to {playback_mode}, thread started")
                    
                    MESSAGE = "Playing"
                except Exception as e:
                    MESSAGE = "Play Error"
                    print(f"Error loading file play: {e}")
            msg_start_time = time.time()
        elif sel == "LOOP":
            if sfid is None:
                MESSAGE = "LOAD SF2 FIRST"
            else:
                # Load MIDI file and prepare for looping
                try:
                    import mido
                    init_fluidsynth_lazy()
                    
                    mid = mido.MidiFile(selected_file_path)
                    file_loop_events = []
                    current_time = 0
                    
                    for track in mid.tracks:
                        for msg in track:
                            current_time += mido.tick2second(msg.time, mid.ticks_per_beat, 500000)
                            
                            if msg.type == 'note_on' and msg.velocity > 0:
                                file_loop_events.append({
                                    'time': current_time,
                                    'type': 'note_on',
                                    'channel': msg.channel,
                                    'note': msg.note,
                                    'velocity': msg.velocity
                                })
                            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                                file_loop_events.append({
                                    'time': current_time,
                                    'type': 'note_off',
                                    'channel': msg.channel,
                                    'note': msg.note
                                })
                            elif msg.type == 'control_change':
                                file_loop_events.append({
                                    'time': current_time,
                                    'type': 'control_change',
                                    'channel': msg.channel,
                                    'control': msg.control,
                                    'value': msg.value
                                })
                            elif msg.type == 'program_change':
                                file_loop_events.append({
                                    'time': current_time,
                                    'type': 'program_change',
                                    'channel': msg.channel,
                                    'program': msg.program
                                })
                    
                    # Calculate duration from last event time
                    file_loop_duration = max([e['time'] for e in file_loop_events]) if file_loop_events else 0
                    file_loop_position = 0.0
                    file_loop_path = selected_file_path
                    
                    # Debug
                    print(f"[LOOP] Loaded {len(file_loop_events)} events, duration: {file_loop_duration:.2f}s")
                    
                    # Use new playback system
                    start_playback_thread_once()
                    with playback_lock:
                        playback_mode = PLAYBACK_FILE_LOOP
                    
                    print(f"[LOOP] Playback mode set to {playback_mode}, thread started")
                    
                    MESSAGE = "Looping"
                except Exception as e:
                    MESSAGE = "Loop Error"
                    print(f"Error loading file loop: {e}")
            msg_start_time = time.time()

        elif sel == "STOP":
            # Immediate atomic stop
            hard_stop_all_playback()
            
            # Clear events
            file_loop_events = []
            file_loop_path = None
            file_play_events = []
            file_play_path = None
            monkey_pattern_events = []  # Clear Monkey patterns too
            
            MESSAGE = "Stopped"
            msg_start_time = time.time()
        elif sel == "RENAME": 
            operation_mode = "RENAME"
            rename_string = os.path.basename(selected_file_path).replace(".mid", "")
            rename_cursor_pos = len(rename_string)  # Start cursor at end
            rename_char_idx = 0  # Start at space
        elif sel == "DELETE":
            try: os.remove(selected_file_path); MESSAGE = "Deleted"; scan_midifiles(); handle_back()
            except: MESSAGE = "Error"
            msg_start_time = time.time()
        elif sel == "BACK": handle_back()
    elif operation_mode == "RENAME":
        # SELECT inserts/replaces character at cursor position
        char = rename_chars[rename_char_idx]
        
        if char == "←":
            # Move cursor left
            rename_cursor_pos = max(0, rename_cursor_pos - 1)
        elif char == "→":
            # Move cursor right (or append position)
            rename_cursor_pos = min(len(rename_string), rename_cursor_pos + 1)
        elif char == "✓":
            # Save and exit
            new_path = os.path.join(midi_file_folder, rename_string.strip() + ".mid")
            try: 
                os.rename(selected_file_path, new_path)
                MESSAGE = "Renamed"
            except: 
                MESSAGE = "Error"
            msg_start_time = time.time()
            operation_mode = "FILE ACTION"
            files = ["PLAY", "STOP", "RENAME", "DELETE", "BACK"]
        elif char == " ":
            # Space just moves cursor right (skip insertion)
            rename_cursor_pos = min(len(rename_string), rename_cursor_pos + 1)
        else:
            # Insert/replace character at cursor position
            if rename_cursor_pos >= len(rename_string):
                rename_string += char  # Append
            else:
                # Replace character at cursor
                rename_string = rename_string[:rename_cursor_pos] + char + rename_string[rename_cursor_pos+1:]
            
            # Move cursor forward
            rename_cursor_pos = min(len(rename_string), rename_cursor_pos + 1)
        
        rename_char_idx = 0  # Reset to space after action
    else:
        if operation_mode == "SOUND FONT":
            loaded_sf2_path = pathes[selectedindex]; init_fluidsynth_lazy()
            sfid = fs.sfload(loaded_sf2_path, True)
            select_first_presets_for_monkey()
            scan_drum_kits()  # Scan for available drum kits
            MESSAGE = "SF2 Loaded"
            msg_start_time = time.time()
            # Don't call handle_back() - stay in SOUND FONT menu so user can load more
            # They can press BACK manually when done
        elif operation_mode == "MIDI KEYBOARD":
            midi_manager.open_port_by_name_async(pathes[selectedindex])
            msg_start_time = time.time()
            # Don't call handle_back() - MIDI connection stays active
            # User can press BACK manually when done

# --- 14. DISPLAY ENGINE ---
def update_display():
    # 1. Add 'recorder' and 'mixer_selected_ch' to the globals list
    global _last_display_time, draw, img, MESSAGE, operation_mode, SHUTTING_DOWN
    global loop_recording, loop_playback, countdown_start, loop_start_time, msg_start_time
    global bpm, MONKEY_BPM, BLE_CONNECTED, loop_length, recorder, mixer_selected_ch, mixer_adjusting
    
    if not init_complete: return  # Hold splash until background_init finishes
    if SHUTTING_DOWN or draw is None: return
    
    now = time.time()

    # --- BPM SYNCED COUNTDOWN ---
    if operation_mode == "countdown":
        current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
        # Ensure BPM is at least 1 to avoid division by zero
        seconds_per_beat = 60.0 / max(current_bpm, 1) 
        
        elapsed = now - countdown_start
        beats_passed = int(elapsed / seconds_per_beat)
        
        if beats_passed < 3:
            current_count = 3 - beats_passed
            draw.rectangle((0, 0, 240, 240), fill=(0, 0, 0))
            
            # Draw the count
            draw.text((95, 70), str(current_count), font=font, fill=(255, 0, 0))
            draw.text((60, 130), "GET READY", font=font, fill=(255, 255, 0))
            
            disp.display(img)
            return
        else:
            # TRIGGER RECORDING ON THE 4th BEAT (No 'import' needed now)
            operation_mode = "main screen"
            recorder.start() # Using the global recorder object
            loop_recording = True
            loop_start_time = time.time()
            MESSAGE = ""
            msg_start_time = time.time()


    # 2. NORMAL DISPLAY REFRESH RATE
    if now - _last_display_time < (0.15 if LOW_POWER_MODE else 0.06): return
    _last_display_time = now
    
    accent = (255, 255, 0) if LOW_POWER_MODE else (255, 255, 255)
    
    # --- Start Drawing Standard UI ---
    draw.rectangle((0, 0, 240, 240), fill=(0, 0, 0))
    
    # Update main menu dynamically based on state
    if operation_mode == "main screen" and files == MAIN_MENU:
        # Create dynamic menu
        dynamic_menu = MAIN_MENU.copy()
        # Update STOP/PLAY LOOP text based on current state
        for i, item in enumerate(dynamic_menu):
            if item == "STOP LOOP" or item == "PLAY LOOP":
                if playback_mode == PLAYBACK_LIVE_LOOP:
                    dynamic_menu[i] = "STOP LOOP"
                else:
                    dynamic_menu[i] = "PLAY LOOP"
                break
        files[:] = dynamic_menu
    
    # Header with LOW BATTERY warning
    draw.rectangle((0, 0, 240, 26), fill=(30, 30, 30))
    
    # Check battery voltage and add warning
    current_voltage = ups.get_voltage()
    time_left = ups.get_time_left()
    battery_percent = ups.get_capacity_percent()
    battery_low = current_voltage > 0 and battery_percent < 20
    battery_critical = current_voltage > 0 and battery_percent < 8
    
    # Flash warning if battery low
    flash_on = (int(now * 2) % 2) == 0  # Flash at 2Hz
    
    if battery_critical and flash_on:
        # Critical: Flash red background
        draw.rectangle((0, 0, 120, 26), fill=(80, 0, 0))
        draw.text((10, 4), f"⚠ BATT! {time_left}", font=font_tiny, fill=(255, 0, 0))
    elif battery_low:
        # Low: Yellow text
        draw.text((10, 4), f"⚠ LOW: {time_left}", font=font_tiny, fill=(255, 200, 0))
    else:
        # Normal: Show time remaining
        draw.text((10, 4), f"TIME: {time_left}", font=font_tiny, fill=accent)
    
    current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
    transport = "►" if midi_transport_state == "playing" else "■"
    draw.text((130, 4), f"{transport} {current_bpm}{' [BLE]' if BLE_CONNECTED else ''}", font=font_tiny, fill=(0, 255, 0) if BLE_CONNECTED else accent)
    
    draw.rectangle((0, 26, 240, 56), fill=(50, 50, 50))
    draw.text((10, 31), operation_mode.upper(), font=font, fill=accent)
    if operation_mode == "RENAME":
        # Show filename with cursor
        before_cursor = rename_string[:rename_cursor_pos]
        at_cursor = rename_string[rename_cursor_pos] if rename_cursor_pos < len(rename_string) else " "
        after_cursor = rename_string[rename_cursor_pos+1:] if rename_cursor_pos < len(rename_string) - 1 else ""
        
        # Display filename with cursor highlight
        draw.text((10, 65), before_cursor, font=font, fill=(0, 255, 0))
        cursor_x = 10 + len(before_cursor) * 12  # Approximate char width
        draw.rectangle((cursor_x, 63, cursor_x + 12, 83), fill=(0, 255, 0))
        draw.text((cursor_x, 65), at_cursor, font=font, fill=(0, 0, 0))
        draw.text((cursor_x + 12, 65), after_cursor, font=font, fill=(0, 255, 0))
        
        # Show current character selector
        draw.text((10, 105), "SELECT CHAR:", font=font_tiny, fill=accent)
        char_curr = rename_chars[rename_char_idx]
        draw.rectangle((75, 125, 115, 160), fill=accent)
        draw.text((88, 130), char_curr, font=font, fill=(0,0,0))
        
        # Instructions - compact
        draw.text((10, 175), "UP/DN: Change", font=font_tiny, fill=(180, 180, 180))
        draw.text((10, 190), "SEL: Insert", font=font_tiny, fill=(180, 180, 180))
        draw.text((10, 205), "BACK: Delete", font=font_tiny, fill=(180, 180, 180))
        draw.text((10, 220), "Use ←→✓", font=font_tiny, fill=(180, 180, 180))
    elif operation_mode == "MIXER":
        # Show all 16 channels (changed from 10)
        view_size = 10
        
        # Auto-scroll to show the last active channel (currently playing instrument)
        if last_active_channel is not None and not mixer_adjusting:
            # Find the Monkey channel number for this MIDI channel
            monkey_ch = None
            for m_ch in range(16):
                if get_internal_channel(m_ch) == last_active_channel:
                    monkey_ch = m_ch
                    break
            
            # If we found it, center it in the view
            if monkey_ch is not None:
                current_start = max(0, min(mixer_selected_ch - 4, 16 - view_size))
                current_end = current_start + view_size
                
                # If active channel is outside the visible range, scroll to show it
                if monkey_ch < current_start or monkey_ch >= current_end:
                    # Center the active channel in the view
                    start_idx = max(0, min(monkey_ch - 4, 16 - view_size))
                    # Also update selected channel to follow
                    mixer_selected_ch = monkey_ch
                else:
                    # Use normal scrolling based on selection
                    start_idx = current_start
            else:
                start_idx = max(0, min(mixer_selected_ch - 4, 16 - view_size))
        else:
            # No active channel or adjusting volume, always use manual selection
            start_idx = max(0, min(mixer_selected_ch - 4, 16 - view_size))
        
        for i in range(start_idx, min(start_idx + view_size, 16)):
            y = 60 + ((i - start_idx) * 18)
            f_ch = get_internal_channel(i)
            color = accent if i == mixer_selected_ch else (200, 200, 200)
            if i == mixer_selected_ch and mixer_adjusting: 
                draw.rectangle((5, y, 235, y+16), outline=(0, 255, 0))
            
            # Check if this channel is currently active (played recently)
            now = time.time()
            is_active = f_ch in last_activity_time and (now - last_activity_time[f_ch]) < 0.5
            
            # Show activity indicator (green dot)
            if is_active:
                draw.ellipse((5, y+5, 11, y+11), fill=(0, 255, 0))
            
            # Show Monkey channel number
            preset_name = channel_presets.get(f_ch, f'CH {i+1}')[:8]  # Max 8 chars
            label = f"M{i}: {preset_name}"
            draw.text((15, y), label, font=font_tiny, fill=color)
            draw.rectangle((150, y+4, 150 + int(channel_volumes.get(f_ch, 100)/1.6), y+12), fill=color)
    elif operation_mode == "METRONOME":
        opts = [f"STATUS: {'ON' if metronome_on else 'OFF'}", f"SPEED: {bpm} BPM", f"VOL: {metro_vol}"]
        for i, opt in enumerate(opts):
            y = 80 + (i * 40); color = accent if i == selectedindex else (200, 200, 200)
            if i == selectedindex: draw.rectangle([10, y-5, 230, y+25], outline=(0, 255, 0) if metro_adjusting else color)
            draw.text((20, y), opt, font=font, fill=color)

    elif operation_mode == "LOOP LENGTH":
        draw.text((20, 80), "LOOP LENGTH:", font=font, fill=accent)
        draw.rectangle((15, 115, 225, 160), fill=(50, 50, 50))
        draw.text((70, 125), f"{loop_length} BARS", font=font, fill=(0, 255, 0))
        draw.text((30, 180), "UP/DN: Change length", font=font_tiny, fill=(180, 180, 180))
        draw.text((30, 200), "BACK: Return to menu", font=font_tiny, fill=(180, 180, 180))

    elif operation_mode == "MIDI KEYBOARD":
        view_size = 5; start_idx = max(0, min(selectedindex - 2, len(files) - view_size))
        for i, line in enumerate(files[start_idx:start_idx+view_size], start=start_idx):
            y = 62 + (i-start_idx)*28; color = (0,0,0) if i == selectedindex else accent
            if i == selectedindex: draw.rectangle([10, y, 230, y+26], fill=accent)
            is_conn = midi_manager.is_port_connected(line)
            draw.rectangle([15, y+6, 29, y+20], outline=color, width=2)
            if is_conn: draw.line([18, y+13, 21, y+17, 26, y+9], fill=(0,255,0), width=2)
            draw.text((35, y+2), line[:18], font=font, fill=color)
    elif operation_mode == "DRUM KIT":
        # Display current drum kit selection
            if not available_drum_kits:
                draw.text((20, 80), "Load SF2 first", font=font, fill=accent)
            else:
                prog, name = available_drum_kits[drum_kit_index]
            
            # Show current selection highlighted
                draw.text((20, 70), "SELECT DRUM KIT:", font=font_tiny, fill=accent)
            
            # Show current kit with highlight
                draw.rectangle((15, 95, 225, 130), fill=(50, 50, 50))
                draw.text((25, 102), name[:18], font=font, fill=(0, 255, 0))
            
            # Show navigation info
                draw.text((30, 145), f"Kit {drum_kit_index + 1}/{len(available_drum_kits)}", font=font_tiny, fill=accent)
                draw.text((30, 165), f"Program: {prog}", font=font_tiny, fill=(180, 180, 180))
                draw.text((25, 190), "UP/DN: Browse", font=font_tiny, fill=accent)
                draw.text((25, 205), "BACK: Return", font=font_tiny, fill=accent)
    elif operation_mode == "VOLUME":
            draw.text((30, 90), "MASTER GAIN", font=font, fill=accent)
            draw.rectangle((20, 120, 220, 150), outline=accent, width=2)
            fill_w = int(196 * volume_level)
            draw.rectangle((22, 122, 22 + fill_w, 148), fill=(0, 255, 0))
            draw.text((100, 160), f"{int(volume_level * 100)}%", font=font, fill=accent)
    
    
    else:
        view_size = 5; start_idx = max(0, min(selectedindex - 2, len(files) - view_size))
        for i, line in enumerate(files[start_idx:start_idx+view_size], start=start_idx):
            y = 62 + (i-start_idx)*28; color = (0,0,0) if i == selectedindex else accent
            if i == selectedindex: draw.rectangle([10, y, 230, y+26], fill=accent)
            draw.text((15, y+2), line[:22], font=font, fill=color)
    
    # Loop recording progress display - larger and clearer
    if loop_recording or loop_playback:
        current_bpm = MONKEY_BPM if BLE_CONNECTED else bpm
        beats_per_bar = 4
        seconds_per_beat = 60.0 / current_bpm
        seconds_per_bar = seconds_per_beat * beats_per_bar
        
        # Calculate progress
        bar_text = f"Bar {loop_bar_count + 1}/{loop_length}"
        status_text = "● RECORDING" if loop_recording else "▶ LOOP PLAY"
        
        # Semi-transparent dark overlay for better visibility
        draw.rectangle((10, 75, 230, 155), fill=(20, 20, 20))
        draw.rectangle((10, 75, 230, 155), outline=(0, 255, 0), width=2)
        
        # Status text - large and prominent
        draw.text((30, 85), status_text, font=font, fill=(255, 255, 0) if loop_recording else (0, 255, 0))
        
        # Progress bar - bigger
        draw.rectangle((20, 115, 220, 140), outline=(0, 255, 0), width=2)
        progress = (loop_bar_count / loop_length) if loop_length > 0 else 0
        fill_width = int(196 * progress)
        if fill_width > 0:
            draw.rectangle((22, 117, 22 + fill_width, 138), fill=(0, 255, 0))
        
        # Bar count centered in progress bar
        draw.text((90, 122), bar_text, font=font_tiny, fill=(255, 255, 255))
    
    # File playback indicators - compact to not obscure menu buttons
    elif playback_mode == PLAYBACK_FILE and file_play_path:
        # Compact progress bar at bottom of screen
        if file_play_duration > 0:
            progress = file_play_position / file_play_duration
            progress = max(0.0, min(1.0, progress))
            
            # Progress bar at very bottom
            draw.rectangle((10, 215, 230, 235), fill=(20, 20, 20))
            draw.rectangle((10, 215, 230, 235), outline=(0, 255, 0), width=1)
            
            # Filled portion
            fill_width = int(216 * progress)
            if fill_width > 0:
                draw.rectangle((12, 217, 12 + fill_width, 233), fill=(0, 200, 0))
            
            # Time display centered
            mins = int(file_play_position // 60)
            secs = int(file_play_position % 60)
            total_mins = int(file_play_duration // 60)
            total_secs = int(file_play_duration % 60)
            time_text = f"▶ {mins}:{secs:02d}/{total_mins}:{total_secs:02d}"
            draw.text((70, 220), time_text, font=font_tiny, fill=(255, 255, 255))
    
    elif playback_mode == PLAYBACK_FILE_LOOP and file_loop_path:
        # Compact loop indicator at bottom
        if file_loop_duration > 0:
            progress = file_loop_position / file_loop_duration
            progress = max(0.0, min(1.0, progress))
            
            # Progress bar at very bottom
            draw.rectangle((10, 215, 230, 235), fill=(20, 20, 20))
            draw.rectangle((10, 215, 230, 235), outline=(0, 200, 255), width=1)
            
            # Filled portion
            fill_width = int(216 * progress)
            if fill_width > 0:
                draw.rectangle((12, 217, 12 + fill_width, 233), fill=(0, 150, 200))
            
            # Time display centered
            mins = int(file_loop_position // 60)
            secs = int(file_loop_position % 60)
            total_mins = int(file_loop_duration // 60)
            total_secs = int(file_loop_duration % 60)
            time_text = f"⟲ {mins}:{secs:02d}/{total_mins}:{total_secs:02d}"
            draw.text((70, 220), time_text, font=font_tiny, fill=(255, 255, 255))
    
    # Message overlay - always shows on top of any other display state
    if MESSAGE and now - msg_start_time < 2.0:
        draw.rectangle((20, 160, 220, 200), fill=(200, 0, 0))
        draw.rectangle((20, 160, 220, 200), outline=(255, 255, 0), width=2)
        draw.text((30, 170), MESSAGE[:22], font=font_tiny, fill=(255, 255, 255))
    
    disp.display(img)

# --- 15. BOOT SEQUENCE ---
def background_init():
    try:
        lazy_imports()
        init_buttons()
        init_display()
        threading.Thread(target=scan_soundfonts, daemon=True).start()
        threading.Thread(target=scan_midifiles, daemon=True).start()
        threading.Thread(target=start_ble_thread, daemon=True).start()
        button_up.when_pressed = handle_up; button_down.when_pressed = handle_down
        button_select.when_pressed = handle_select; button_back.when_pressed = handle_back
        global midi_manager, init_complete
        midi_manager = MultiMidiIn(); midi_manager.set_callback(midi_callback)
        init_complete = True
    except Exception as e:
        print(f"[INIT ERROR] {e}")
        init_complete = True

def main():
    # Add file_loop_duration and file_loop_start_time to globals
    global file_loop_enabled, file_loop_path, fs, file_loop_duration, file_loop_start_time
    file_loop_duration = 0
    file_loop_start_time = 0
    
    load_mixer()
    threading.Thread(target=background_init, daemon=True).start()
    
    while True:
        if not SHUTTING_DOWN:
            # --- MIDI FILE LOOP CHECK ---
            if file_loop_enabled and fs and file_loop_duration > 0:
                elapsed = time.time() - file_loop_start_time
                
                # If the song time is up, restart it
                if elapsed >= file_loop_duration:
                    fs.play_midi_file(file_loop_path)
                    file_loop_start_time = time.time() # Reset the clock
            
            update_display()
            
        time.sleep(0.05)

if __name__ == '__main__':
    main()
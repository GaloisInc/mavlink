#!/usr/bin/env python
'''
mavproxy - a MAVLink proxy program

Copyright Andrew Tridgell 2011
Released under the GNU GPL version 3 or later

'''

import sys, os, struct, math, time, socket
import fnmatch, errno, threading
import serial, Queue, select
import logging

# find the mavlink.py module
for d in [ 'pymavlink',
           os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'pymavlink'),
           os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'mavlink', 'pymavlink') ]:
    if os.path.exists(d):
        sys.path.insert(0, d)
        if os.name == 'nt':
            try:
                # broken python compilation of mavlink.py on windows!
                os.unlink(os.path.join(d, 'mavlink.pyc'))
            except:
                pass

# add modules path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), 'modules'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), 'modules', 'lib'))

import select, textconsole

class MPSettings(object):
    def __init__(self):
        self.vars = [ ('link', int),
                      ('altreadout', int),
                      ('distreadout', int),
                      ('battreadout', int),
                      ('heartbeat', int),
                      ('numcells', int),
                      ('speech', int),
                      ('mavfwd', int),
                      ('mavfwd_rate', int),
                      ('streamrate', int),
                      ('streamrate2', int),
                      ('heartbeatreport', int),
                      ('radiosetup', int),
                      ('paramretry', int),
                      ('moddebug', int),
                      ('rc1mul', int),
                      ('rc2mul', int),
                      ('rc4mul', int)]
        self.link = 1
        self.altreadout = 10
        self.distreadout = 200
        self.battreadout = 0
        self.basealtitude = -1
        self.heartbeat = 1
        self.numcells = 0
        self.mavfwd = 1
        self.mavfwd_rate = 0
        self.speech = 0
        self.streamrate = 4
        self.streamrate2 = 4
        self.radiosetup = 0
        self.heartbeatreport = 1
        self.paramretry = 10
        self.rc1mul = 1
        self.rc2mul = 1
        self.rc4mul = 1
        self.moddebug = 2

    def set(self, vname, value):
        '''set a setting'''
        for (v,t) in sorted(self.vars):
            if v == vname:
                try:
                    value = t(value)
                except:
                    print("Unable to convert %s to type %s" % (value, t))
                    return
                setattr(self, vname, value)
                return

    def show(self, v):
        '''show settings'''
        print("%20s %s" % (v, getattr(self, v)))

    def show_all(self):
        '''show all settings'''
        for (v,t) in sorted(self.vars):
            self.show(v)

class MPStatus(object):
    '''hold status information about the mavproxy'''
    def __init__(self):
        if opts.quadcopter:
            self.rc_throttle = [ 0.0, 0.0, 0.0, 0.0 ]
        else:
            self.rc_aileron  = 0
            self.rc_elevator = 0
            self.rc_throttle = 0
            self.rc_rudder   = 0
        self.gps	 = None
        self.msgs = {}
        self.msg_count = {}
        self.counters = {'MasterIn' : [], 'MasterOut' : 0, 'FGearIn' : 0, 'FGearOut' : 0, 'Slave' : 0}
        self.setup_mode = opts.setup
        self.wp_op = None
        self.wp_save_filename = None
        self.wploader = mavwp.MAVWPLoader()
        self.fenceloader = mavwp.MAVFenceLoader()
        self.loading_waypoints = False
        self.loading_waypoint_lasttime = time.time()
        self.mav_error = 0
        self.target_system = 1
        self.target_component = 1
        self.speech = None
        self.altitude = 0
        self.last_altitude_announce = 0.0
        self.last_distance_announce = 0.0
        self.last_battery_announce = 0
        self.last_avionics_battery_announce = 0
        self.battery_level = -1
        self.avionics_battery_level = -1
        self.last_waypoint = 0
        self.exit = False
        self.joystick_inputs = False # Has joystick input been provided recently?
        self.override = [ 0 ] * 8
        self.last_override = [ 0 ] * 8
        self.override_counter = 0
        self.flightmode = 'MAV'
        self.logdir = None
        self.last_heartbeat = 0
        self.last_heartbeat_clk = 0
        self.heartbeat_error = False
        self.last_apm_msg = None
        self.highest_usec = 0
        self.fence_enabled = False
        self.last_fence_breach = 0
        self.last_fence_status = 0
        self.have_gps_lock = False
        self.lost_gps_lock = False
        self.watch = None
        self.last_streamrate1 = -1
        self.last_streamrate2 = -1
        self.last_paramretry = 0
        self.last_seq = 0
        self.heading = 0

    def show(self, f, pattern=None):
        '''write status to status.txt'''
        if pattern is None:
            f.write('Counters: ')
            for c in self.counters:
                f.write('%s:%s ' % (c, self.counters[c]))
            f.write('\n')
            f.write('MAV Errors: %u\n' % self.mav_error)
            f.write(str(self.gps)+'\n')
        for m in sorted(self.msgs.keys()):
            if pattern is not None and not fnmatch.fnmatch(str(m).upper(), pattern.upper()):
                continue
            f.write("%u: %s\n" % (self.msg_count[m], str(self.msgs[m])))

    def write(self):
        '''write status to status.txt'''
        f = open('status.txt', mode='w')
        self.show(f)
        f.close()

class MAVFunctions(object):
    pass

class MPState(object):
    '''holds state of mavproxy'''
    def __init__(self):
        self.console = textconsole.SimpleConsole()
        self.map = None
        self.settings = MPSettings()
        self.status = MPStatus()

        # master mavlink device
        self.mav_master = None

        # mavlink outputs
        self.mav_outputs = []

        # SITL output
        self.sitl_output = None

        self.mav_param = {}
        self.modules = []
        self.functions = MAVFunctions()
        self.functions.say = say
        self.functions.process_stdin = process_stdin
        self.select_extra = {}
        self.msg_queue = Queue.Queue()

        self.smaccm_nav = SmaccmNav()

    def queue_message(self, message):
        logging.info('Queueing message %s', message)
        self.msg_queue.put(message)

    def master(self):
        '''return the currently chosen mavlink master object'''
        if self.settings.link > len(self.mav_master):
            self.settings.link = 1

        # try to use one with no link error
        if not self.mav_master[self.settings.link-1].linkerror:
            return self.mav_master[self.settings.link-1]
        for m in self.mav_master:
            if not m.linkerror:
                return m
        return self.mav_master[self.settings.link-1]

class EdgeTriggered:
    def __init__(self, timeout=333):
        self.last = False
        self.time = 0
        self.timeout = timeout * 1000 # ms to usec
    def sample(self,s):
        if s and not self.last:
            t = get_usec()
            if (t - self.time) > self.timeout:
                self.time = t
                self.last = s
                return True
        if not s:
            self.last = s
        return False

class SmaccmNav:
    def __init__(self):
        self._u = EdgeTriggered()
        self._d = EdgeTriggered()
        self._l = EdgeTriggered()
        self._r = EdgeTriggered()
        self._stop = EdgeTriggered()
        self._clear = EdgeTriggered()
        self.alt_setpt = None
        self.head_setpt = None
        self.period = mavutil.periodic_event(3)
    def udlr(self,u,d,l,r):
        s = False
        if self._u.sample(u):
            self.new_altitude(0.5)
            s = True
        if self._d.sample(d):
            self.new_altitude(-0.5)
            s = True
        if self._l.sample(l):
            self.new_heading(-60)
            s = True
        if self._r.sample(r):
            self.new_heading(60)
            s = True
        if s:
            self.send()
    def stop(self,v):
        if self._stop.sample(v):
            self.new_heading(0)
            self.new_altitude(0)
            self.send()
    def clear(self,v):
        if self._clear.sample(v):
            self.clear_alt_heading()
            self.send()

    def new_altitude(self,offset):
        if 'VFR_HUD' not in mpstate.status.msgs:
            return
        current_alt = mpstate.status.msgs['VFR_HUD'].alt # float in m
        newalt_mm = int((current_alt + offset) * 1000)
        mpstate.console.writeln('current alt %f, changing by %f to %f'
            %(current_alt, offset, float(newalt_mm) / 1000.0))
        self.alt_setpt = newalt_mm

    def new_heading(self,offset):

        if 'VFR_HUD' not in mpstate.status.msgs:
            return
        current_heading = mpstate.status.msgs['VFR_HUD'].heading
        newhead_sum  = math.radians(current_heading) + math.radians(offset)
        newhead_wrapped = math.degrees(zero2pidomain(newhead_sum))
        newhead = int(newhead_wrapped * 100)
        mpstate.console.writeln('current heading %f, rotating by %f degrees to %f'
            %(current_heading, offset, newhead_wrapped))
        self.head_setpt = newhead

    def clear_alt_heading(self):
        mpstate.console.writeln('clearing nav controller setpoints')
        self.alt_setpt = None
        self.head_setpt = None

    def send(self):
        mpstate.master().mav.smaccmpilot_nav_cmd_send(
            -1,     # autoland_active
            -1,     # autoland_complete
            self.alt_setpt if self.alt_setpt else 0,     # alt_set
            1500,     # alt_rate_set
            1 if self.alt_setpt else -1,     # alt_set_valid
            self.head_setpt if self.head_setpt else 0,     # heading_set
            1 if self.head_setpt else -1,     # heading_set_valid
            0,     # lat_set
            0,     # lon_set
            -1,     # lat_lon_set_valid
            0,     # vel_x_set
            0,     # vel_y_set
            -1)     # vel_set_valid

def get_usec():
    '''time since 1970 in microseconds'''
    return int(time.time() * 1.0e6)

class rline(object):
    '''async readline abstraction'''
    def __init__(self, prompt):
        import threading
        self.prompt = prompt
        self.line = None
        try:
            import readline
        except Exception:
            pass

    def set_prompt(self, prompt):
        if prompt != self.prompt:
            self.prompt = prompt
            sys.stdout.write(prompt)
            
def say(text, priority='important'):
    '''speak some text'''
    ''' http://cvs.freebsoft.org/doc/speechd/ssip.html see 4.3.1 for priorities'''
    mpstate.console.writeln(text)
    if mpstate.settings.speech:
        import speechd
        mpstate.status.speech = speechd.SSIPClient('MAVProxy%u' % os.getpid())
        mpstate.status.speech.set_output_module('festival')
        mpstate.status.speech.set_language('en')
        mpstate.status.speech.set_priority(priority)
        mpstate.status.speech.set_punctuation(speechd.PunctuationMode.SOME)
        mpstate.status.speech.speak(text)
        mpstate.status.speech.close()

def get_mav_param(param, default=None):
    '''return a EEPROM parameter value'''
    if not param in mpstate.mav_param:
        return default
    return mpstate.mav_param[param]


def send_rc_override():
    '''send RC override packet'''
    if mpstate.sitl_output:
        buf = struct.pack('<HHHHHHHH',
                          *mpstate.status.override)
        mpstate.sitl_output.write(buf)
    else:
        mpstate.master().mav.rc_channels_override_send(mpstate.status.target_system,
                                                         mpstate.status.target_component,
                                                         *mpstate.status.override)

def cmd_trim(args):
    '''trim aileron, elevator and rudder to current values'''
    if not 'RC_CHANNELS_RAW' in mpstate.status.msgs:
        print("No RC_CHANNELS_RAW to trim with")
        return
    m = mpstate.status.msgs['RC_CHANNELS_RAW']

    mpstate.master().param_set_send('ROLL_TRIM',  m.chan1_raw)
    mpstate.master().param_set_send('PITCH_TRIM', m.chan2_raw)
    mpstate.master().param_set_send('YAW_TRIM',   m.chan4_raw)
    print("Trimmed to aileron=%u elevator=%u rudder=%u" % (
        m.chan1_raw, m.chan2_raw, m.chan4_raw))

def cmd_loiter(args):
    '''set LOITER mode'''
    mpstate.master().set_mode_loiter()

def cmd_auto(args):
    '''set AUTO mode'''
    mpstate.master().set_mode_auto()

def cmd_ground(args):
    '''do a ground start mode'''
    mpstate.master().calibrate_imu()

def cmd_level(args):
    '''do a ground start mode'''
    mpstate.master().calibrate_level()

def cmd_rtl(args):
    '''set RTL mode'''
    mpstate.master().set_mode_rtl()

def cmd_manual(args):
    '''set MANUAL mode'''
    MAV_ACTION_SET_MANUAL = 12
    mpstate.master().mav.action_send(mpstate.status.target_system, mpstate.status.target_component, MAV_ACTION_SET_MANUAL)

def cmd_mode(args):
    '''set SMACCMPilot flight mode'''
    if len(args) != 1:
        print("Usage: mode MODE")
        return
    try:
        mode = int(args[0])
    except:
        print("Invalid mode.")
        return
    # XXX should use the flag from the enum here
    mpstate.master().mav.set_mode_send(mpstate.status.target_system, 1, mode)

def cmd_joystick(args):
    '''joystick load'''
    if len(args) != 0:
        print("usage: joystick")
        return
    else: module_load("mavproxy_joystick")

def cmd_arm(args):
    '''arm motors'''
    mpstate.master().arducopter_arm()

def cmd_disarm(args):
    '''disarm motors'''
    mpstate.master().arducopter_disarm()

def cmd_attack(args):
    '''cmd attack'''
    mpstate.master().set_attack()

def cmd_stream_rate(args):
    if len(args) == 1 and int(args[0]) >= 0 and int(args[0]) <= 100:
        mpstate.settings.streamrate = int(args[0])
        set_stream_rates()
    else:
        print("invalid stream rate setting, provide an integer argument "
                + " between 0 and 100")

def cmd_left(args):
    if len(args) == 0:
        mpstate.smaccm_nav.new_heading(-90)
    elif len(args) == 1:
        try:
            i = int(args[0])
        except:
            print("invalid argument, must be integer number in degrees")
        mpstate.smaccm_nav.new_heading(-1*i)
    else:
        print("invalid arguments, expects one integer arg")

def cmd_right(args):
    if len(args) == 0:
        mpstate.smaccm_nav.new_heading(90)
    elif len(args) == 1:
        try:
            i = int(args[0])
        except:
            print("invalid argument, must be integer number in degrees")
        mpstate.smaccm_nav.new_heading(i)
    else:
        print("invalid arguments, expects one integer arg")

def cmd_up(args):
    if len(args) == 0:
        mpstate.smaccm_nav.new_altitude(1)
    elif len(args) == 1:
        try:
            i = float(args[0])
        except:
            print("invalid argument, must be a float in degrees")
        mpstate.smaccm_nav.new_altitude(i)
    else:
        print("invalid arguments, expects one integer arg")

def cmd_down(args):
    if len(args) == 0:
        mpstate.smaccm_nav.new_altitude(-1)
    elif len(args) == 1:
        try:
            i = float(args[0])
        except:
            print("invalid argument, must be a float in degrees")
        mpstate.smaccm_nav.new_altitude(-1*i)
    else:
        print("invalid arguments, expects one integer arg")

def cmd_nonav(args):
    smaccm_nav_clear();

def process_waypoint_request(m, master):
    '''process a waypoint request from the master'''
    if (not mpstate.status.loading_waypoints or
        time.time() > mpstate.status.loading_waypoint_lasttime + 10.0):
        mpstate.status.loading_waypoints = False
        mpstate.console.error("not loading waypoints")
        return
    if m.seq >= mpstate.status.wploader.count():
        mpstate.console.error("Request for bad waypoint %u (max %u)" % (m.seq, mpstate.status.wploader.count()))
        return
    master.mav.send(mpstate.status.wploader.wp(m.seq))
    mpstate.status.loading_waypoint_lasttime = time.time()
    mpstate.console.writeln("Sent waypoint %u : %s" % (m.seq, mpstate.status.wploader.wp(m.seq)))
    if m.seq == mpstate.status.wploader.count() - 1:
        mpstate.status.loading_waypoints = False
        mpstate.console.writeln("Sent all %u waypoints" % mpstate.status.wploader.count())

def load_waypoints(filename):
    '''load waypoints from a file'''
    mpstate.status.wploader.target_system = mpstate.status.target_system
    mpstate.status.wploader.target_component = mpstate.status.target_component
    try:
        mpstate.status.wploader.load(filename)
    except Exception, msg:
        print("Unable to load %s - %s" % (filename, msg))
        return
    print("Loaded %u waypoints from %s" % (mpstate.status.wploader.count(), filename))

    mpstate.master().waypoint_clear_all_send()
    if mpstate.status.wploader.count() == 0:
        return

    mpstate.status.loading_waypoints = True
    mpstate.status.loading_waypoint_lasttime = time.time()
    mpstate.master().waypoint_count_send(mpstate.status.wploader.count())

def save_waypoints(filename):
    '''save waypoints to a file'''
    try:
        mpstate.status.wploader.save(filename)
    except Exception, msg:
        print("Failed to save %s - %s" % (filename, msg))
        return
    print("Saved %u waypoints to %s" % (mpstate.status.wploader.count(), filename))
             

def cmd_wp(args):
    '''waypoint commands'''
    if len(args) < 1:
        print("usage: wp <list|load|save|set|clear>")
        return

    if args[0] == "load":
        if len(args) != 2:
            print("usage: wp load <filename>")
            return
        load_waypoints(args[1])
    elif args[0] == "list":
        mpstate.status.wp_op = "list"
        mpstate.master().waypoint_request_list_send()
    elif args[0] == "save":
        if len(args) != 2:
            print("usage: wp save <filename>")
            return
        mpstate.status.wp_save_filename = args[1]
        mpstate.status.wp_op = "save"
        mpstate.master().waypoint_request_list_send()
    elif args[0] == "set":
        if len(args) != 2:
            print("usage: wp set <wpindex>")
            return
        mpstate.master().waypoint_set_current_send(int(args[1]))
    elif args[0] == "clear":
        mpstate.master().waypoint_clear_all_send()
    else:
        print("Usage: wp <list|load|save|set|clear>")


def fetch_fence_point(i):
    '''fetch one fence point'''
    mpstate.master().mav.fence_fetch_point_send(mpstate.status.target_system,
                                                mpstate.status.target_component, i)
    tstart = time.time()
    p = None
    while time.time() - tstart < 1:
        p = mpstate.master().recv_match(type='FENCE_POINT', blocking=False)
        if p is not None:
            break
        time.sleep(0.1)
        continue
    if p is None:
        mpstate.console.error("Failed to fetch point %u" % i)
        return None
    return p



def load_fence(filename):
    '''load fence points from a file'''
    try:
        mpstate.status.fenceloader.target_system = mpstate.status.target_system
        mpstate.status.fenceloader.target_component = mpstate.status.target_component
        mpstate.status.fenceloader.load(filename)
    except Exception, msg:
        print("Unable to load %s - %s" % (filename, msg))
        return
    print("Loaded %u geo-fence points from %s" % (mpstate.status.fenceloader.count(), filename))

    # must disable geo-fencing when loading
    action = get_mav_param('FENCE_ACTION', mavutil.mavlink.FENCE_ACTION_NONE)
    param_set('FENCE_ACTION', mavutil.mavlink.FENCE_ACTION_NONE)
    param_set('FENCE_TOTAL', mpstate.status.fenceloader.count())
    for i in range(mpstate.status.fenceloader.count()):
        p = mpstate.status.fenceloader.point(i)
        mpstate.master().mav.send(p)
        p2 = fetch_fence_point(i)
        if p2 is None:
            param_set('FENCE_ACTION', action)
            return
        if (p.idx != p2.idx or
            abs(p.lat - p2.lat) >= 0.00001 or
            abs(p.lng - p2.lng) >= 0.00001):
            print("Failed to send fence point %u" % i)
            param_set('FENCE_ACTION', action)
            return
    param_set('FENCE_ACTION', action)


def list_fence(filename):
    '''list fence points, optionally saving to a file'''

    mpstate.status.fenceloader.clear()
    count = get_mav_param('FENCE_TOTAL', 0)
    if count == 0:
        print("No geo-fence points")
        return
    for i in range(int(count)):
        p = fetch_fence_point(i)
        if p is None:
            return
        mpstate.status.fenceloader.add(p)

    if filename is not None:
        try:
            mpstate.status.fenceloader.save(filename)
        except Exception, msg:
            print("Unable to save %s - %s" % (filename, msg))
            return
        print("Saved %u geo-fence points to %s" % (mpstate.status.fenceloader.count(), filename))
    else:
        for i in range(mpstate.status.fenceloader.count()):
            p = mpstate.status.fenceloader.point(i)
            mpstate.console.writeln("lat=%f lng=%f" % (p.lat, p.lng))


def cmd_fence(args):
    '''geo-fence commands'''
    if len(args) < 1:
        print("usage: fence <list|load|save|clear>")
        return

    if args[0] == "load":
        if len(args) != 2:
            print("usage: fence load <filename>")
            return
        load_fence(args[1])
    elif args[0] == "list":
        list_fence(None)
    elif args[0] == "save":
        if len(args) != 2:
            print("usage: fence save <filename>")
            return
        list_fence(args[1])
    elif args[0] == "clear":
        param_set('FENCE_TOTAL', 0)
    else:
        print("Usage: fence <list|load|save|clear>")


def param_set(name, value, retries=3):
    '''set a parameter'''
    got_ack = False
    while retries > 0 and not got_ack:
        retries -= 1
        mpstate.master().param_set_send(name.upper(), float(value))
        tstart = time.time()
        while time.time() - tstart < 1:
            ack = mpstate.master().recv_match(type='PARAM_VALUE', blocking=False)
            if ack == None:
                time.sleep(0.1)
                continue
            if str(name).upper() == str(ack.param_id).upper():
                got_ack = True
                break
    if not got_ack:
        print("timeout setting %s to %f" % (name, float(value)))
        return False
    return True


def param_save(filename, wildcard):
    '''save parameters to a file'''
    f = open(filename, mode='w')
    k = mpstate.mav_param.keys()
    k.sort()
    count = 0
    for p in k:
        if p and fnmatch.fnmatch(str(p).upper(), wildcard.upper()):
            f.write("%-15.15s %f\n" % (p, mpstate.mav_param[p]))
            count += 1
    f.close()
    print("Saved %u parameters to %s" % (count, filename))


def param_load_file(filename, wildcard):
    '''load parameters from a file'''
    try:
        f = open(filename, mode='r')
    except:
        print("Failed to open file '%s'" % filename)
        return
    count = 0
    changed = 0
    for line in f:
        line = line.strip()
        if not line or line[0] == "#":
            continue
        a = line.split()
        if len(a) != 2:
            print("Invalid line: %s" % line)
            continue
        # some parameters should not be loaded from file
        if a[0] in ['SYSID_SW_MREV', 'SYS_NUM_RESETS', 'ARSPD_OFFSET', 'GND_ABS_PRESS',
                    'GND_TEMP', 'CMD_TOTAL', 'CMD_INDEX', 'LOG_LASTFILE', 'FENCE_TOTAL',
                    'FORMAT_VERSION' ]:
            continue
        if not fnmatch.fnmatch(a[0].upper(), wildcard.upper()):
            continue
        if a[0] not in mpstate.mav_param:
            print("Unknown parameter %s" % a[0])
            continue
        old_value = mpstate.mav_param[a[0]]
        if math.fabs(old_value - float(a[1])) > 0.000001:
            if param_set(a[0], a[1]):
                print("changed %s from %f to %f" % (a[0], old_value, float(a[1])))
            changed += 1
        count += 1
    f.close()
    print("Loaded %u parameters from %s (changed %u)" % (count, filename, changed))
    

param_wildcard = "*"

def cmd_param(args):
    '''control parameters'''
    if len(args) < 1:
        print("usage: param <fetch|edit|set|show|load [paramfile]>")
        return
    if args[0] == "fetch":
        mpstate.master().param_fetch_all()
        print("Requested parameter list")
    elif args[0] == "save":
        if len(args) < 2:
            print("usage: param save <filename> [wildcard]")
            return
        if len(args) > 2:
            param_wildcard = args[2]
        else:
            param_wildcard = "*"
        param_save(args[1], param_wildcard)
    elif args[0] == "set":
        if len(args) != 3:
            print("Usage: param set PARMNAME VALUE")
            return
        param = args[1]
        value = args[2]
        if not param.upper() in mpstate.mav_param:
            print("Unable to find parameter '%s'" % param)
            return
        param_set(param, value)
    elif args[0] == "load":
        if len(args) < 2:
            print("Usage: param load <filename> [wildcard]")
            return
        if len(args) > 2:
            param_wildcard = args[2]
        else:
            param_wildcard = "*"
        param_load_file(args[1], param_wildcard)
    elif args[0] == "show":
        if len(args) > 1:
            pattern = args[1]
        else:
            pattern = "*"
        k = sorted(mpstate.mav_param.keys())
        for p in k:
            if fnmatch.fnmatch(str(p).upper(), pattern.upper()):
                print("%-15.15s %f" % (str(p), mpstate.mav_param[p]))
    else:
        print("Unknown subcommand '%s' (try 'fetch', 'save', 'set', 'show', 'load')" % args[0])

def cmd_set(args):
    '''control mavproxy options'''
    if len(args) == 0:
        mpstate.settings.show_all()
        return

    if getattr(mpstate.settings, args[0], None) is None:
        print("Unknown setting '%s'" % args[0])
        return
    if len(args) == 1:
        mpstate.settings.show(args[0])
    else:
        mpstate.settings.set(args[0], args[1])

def cmd_status(args):
    '''show status'''
    if len(args) == 0:
        mpstate.status.show(sys.stdout, pattern=None)
    else:
        for pattern in args:
            mpstate.status.show(sys.stdout, pattern=pattern)

def cmd_bat(args):
    '''show battery levels'''
    print("Flight battery:   %u%%" % mpstate.status.battery_level)
    print("Avionics battery: %u%%" % mpstate.status.avionics_battery_level)

def cmd_alt(args):
    '''show altitude'''
    print("Altitude:  %.1f" % mpstate.status.altitude)


def cmd_up_deprecated_lol(args):
    '''adjust TRIM_PITCH_CD up by 5 degrees'''
    if len(args) == 0:
        adjust = 5.0
    else:
        adjust = float(args[0])
    old_trim = get_mav_param('TRIM_PITCH_CD', None)
    if old_trim is None:
        print("Existing trim value unknown!")
        return
    new_trim = int(old_trim + (adjust*100))
    if math.fabs(new_trim - old_trim) > 1000:
        print("Adjustment by %d too large (from %d to %d)" % (adjust*100, old_trim, new_trim))
        return
    print("Adjusting TRIM_PITCH_CD from %d to %d" % (old_trim, new_trim))
    param_set('TRIM_PITCH_CD', new_trim)


def cmd_setup(args):
    mpstate.status.setup_mode = True
    mpstate.rl.set_prompt("")


def cmd_reset(args):
    print("Resetting master")
    mpstate.master().reset()

def cmd_link(args):
    for master in mpstate.mav_master:
        linkdelay = (mpstate.status.highest_usec - master.highest_usec)*1e-6
        if master.linkerror:
            print("link %u down" % (master.linknum+1))
        elif master.link_delayed:
            print("link %u delayed by %.2f seconds" % (master.linknum+1, linkdelay))
        else:
            print("link %u OK (%u packets, %.2fs delay, %u lost, %.1f%% loss)" % (master.linknum+1,
                                                                                  mpstate.status.counters['MasterIn'][master.linknum],
                                                                                  linkdelay,
                                                                                  master.mav_loss,
                                                                                  master.packet_loss()))

def cmd_watch(args):
    '''watch a mavlink packet pattern'''
    if len(args) == 0:
        mpstate.status.watch = None
        return
    mpstate.status.watch = args[0]
    print("Watching %s" % mpstate.status.watch)

def cmd_module(args):
    '''module commands'''
    usage = "usage: module <list|load|reload>"
    if len(args) < 1:
        print(usage)
        return
    if args[0] == "list":
        for m in mpstate.modules:
            print("%s: %s" % (m.name(), m.description()))
    elif args[0] == "load":
        if len(args) < 2:
            print("usage: module load <name>")
            return
        module_load(args[1])
    elif args[0] == "reload":
        if len(args) < 2:
            print("usage: module reload <name>")
            return
        modname = os.path.basename(args[1])
        for m in mpstate.modules:
            if m.name() == modname:
                try:
                    m.unload()
                except Exception:
                    pass
                reload(m)
                m.init(mpstate)
                print("Reloaded module %s" % modname)
                return
        print("Unable to find module %s" % modname)
    else:
        print(usage)

def module_load(mod):
    '''module load'''
    try:
        directory = os.path.dirname(mod)
        modname   = os.path.basename(mod)
        if directory:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                            'modules', directory))
        m = __import__(modname)
        # if m in mpstate.modules:
        #     m.unload()
            # raise RuntimeError("module already loaded")
        m.init(mpstate)
        mpstate.modules.append(m)
        print("Loaded module %s" % modname)
    except Exception, msg:
        print("Unable to load module %s: %s" % (modname, msg))
        raise

command_map = {
    'wp'       : (cmd_wp,       'waypoint management'),
    'fence'    : (cmd_fence,    'geo-fence management'),
    'param'    : (cmd_param,    'manage APM parameters'),
    'setup'    : (cmd_setup,    'go into setup mode'),
    'reset'    : (cmd_reset,    'reopen the connection to the MAVLink master'),
    'status'   : (cmd_status,   'show status'),
    'trim'     : (cmd_trim,     'trim aileron, elevator and rudder to current values'),
    'auto'     : (cmd_auto,     'set AUTO mode'),
    'ground'   : (cmd_ground,   'do a ground start'),
    'level'    : (cmd_level,    'set level on a multicopter'),
    'loiter'   : (cmd_loiter,   'set LOITER mode'),
    'rtl'      : (cmd_rtl,      'set RTL mode'),
    'manual'   : (cmd_manual,   'set MANUAL mode'),
    'set'      : (cmd_set,      'mavproxy settings'),
    'bat'      : (cmd_bat,      'show battery levels'),
    'alt'      : (cmd_alt,      'show relative altitude'),
    'link'     : (cmd_link,     'show link status'),
    'watch'    : (cmd_watch,    'watch a MAVLink pattern'),
    'module'   : (cmd_module,   'module commands'),
    'attack'   : (cmd_attack,   'flood data-link'),
    'stream'   : (cmd_stream_rate, 'set stream rate'),
    'mode'     : (cmd_mode,     'set SMACCMPilot flight mode'),
    'arm'      : (cmd_arm,      'arm motors'),
    'disarm'   : (cmd_disarm,   'disarm motors'),
    'joystick' : (cmd_joystick, 'load joystick'),
    'left'     : (cmd_left,     'rotate vehicle left'),
    'right'    : (cmd_right,    'rotate vehicle right'),
    'up'       : (cmd_up,       'change alt upwards'),
    'down'     : (cmd_down,     'change alt downwards'),
    'nonav'    : (cmd_nonav,    'cancel vehicle alt and roatate commands')
    }

def process_stdin(line):
    '''handle commands from user'''
    if line is None:
        sys.exit(0)
    line = line.strip()

    if mpstate.status.setup_mode:
        # in setup mode we send strings straight to the master
        if line == '.':
            mpstate.status.setup_mode = False
            mpstate.status.flightmode = "MAV"
            mpstate.rl.set_prompt("MAV> ")
            return
        if line == '+++':
            mpstate.master().write(line)
        else:
            mpstate.master().write(line + '\r')
        return

    if not line:
        return

    args = line.split()
    cmd = args[0]
    if cmd == 'help':
        k = command_map.keys()
        k.sort()
        for cmd in k:
            (fn, help) = command_map[cmd]
            print("%-15s : %s" % (cmd, help))
        return
    if not cmd in command_map:
        print("Unknown command '%s'" % line)
        return
    (fn, help) = command_map[cmd]
    try:
        fn(args[1:])
    except Exception as e:
        print("ERROR in command: %s" % str(e))


def scale_rc(servo, min, max, param):
    '''scale a PWM value'''
    # default to servo range of 1000 to 2000
    min_pwm  = get_mav_param('%s_MIN'  % param, 0)
    max_pwm  = get_mav_param('%s_MAX'  % param, 0)
    if min_pwm == 0 or max_pwm == 0:
        return 0
    if max_pwm == min_pwm:
        p = 0.0
    else:
        p = (servo-min_pwm) / float(max_pwm-min_pwm)
    v = min + p*(max-min)
    if v < min:
        v = min
    if v > max:
        v = max
    return v


def system_check():
    '''check that the system is ready to fly'''
    ok = True

    if mavutil.mavlink.WIRE_PROTOCOL_VERSION == '1.0':
        if not 'GPS_RAW_INT' in mpstate.status.msgs:
            say("WARNING no GPS status")
            return
        if mpstate.status.msgs['GPS_RAW_INT'].fix_type != 3:
            say("WARNING no GPS lock")
            ok = False
    else:
        if not 'GPS_RAW' in mpstate.status.msgs and not 'GPS_RAW_INT' in mpstate.status.msgs:
            say("WARNING no GPS status")
            return
        if mpstate.status.msgs['GPS_RAW'].fix_type != 2:
            say("WARNING no GPS lock")
            ok = False

    if not 'PITCH_MIN' in mpstate.mav_param:
        say("WARNING no pitch parameter available")
        return
        
    if int(mpstate.mav_param['PITCH_MIN']) > 1300:
        say("WARNING PITCH MINIMUM not set")
        ok = False

    if not 'ATTITUDE' in mpstate.status.msgs:
        say("WARNING no attitude recorded")
        return

    if math.fabs(mpstate.status.msgs['ATTITUDE'].pitch) > math.radians(5):
        say("WARNING pitch is %u degrees" % math.degrees(mpstate.status.msgs['ATTITUDE'].pitch))
        ok = False

    if math.fabs(mpstate.status.msgs['ATTITUDE'].roll) > math.radians(5):
        say("WARNING roll is %u degrees" % math.degrees(mpstate.status.msgs['ATTITUDE'].roll))
        ok = False

    if ok:
        say("All OK SYSTEM READY TO FLY")


def beep():
    f = open("/dev/tty", mode="w")
    f.write(chr(7))
    f.close()

def vcell_to_battery_percent(vcell):
    '''convert a cell voltage to a percentage battery level'''
    if vcell > 4.1:
        # above 4.1 is 100% battery
        return 100.0
    elif vcell > 3.81:
        # 3.81 is 17% remaining, from flight logs
        return 17.0 + 83.0 * (vcell - 3.81) / (4.1 - 3.81)
    elif vcell > 3.81:
        # below 3.2 it degrades fast. It's dead at 3.2
        return 0.0 + 17.0 * (vcell - 3.20) / (3.81 - 3.20)
    # it's dead or disconnected
    return 0.0


def battery_update(SYS_STATUS):
    '''update battery level'''

    # main flight battery
    mpstate.status.battery_level = SYS_STATUS.battery_remaining/10.0

    # avionics battery
    if not 'AP_ADC' in mpstate.status.msgs:
        return
    rawvalue = float(mpstate.status.msgs['AP_ADC'].adc2)
    INPUT_VOLTAGE = 4.68
    VOLT_DIV_RATIO = 3.56
    voltage = rawvalue*(INPUT_VOLTAGE/1024.0)*VOLT_DIV_RATIO
    vcell = voltage / mpstate.settings.numcells

    avionics_battery_level = vcell_to_battery_percent(vcell)

    if mpstate.status.avionics_battery_level == -1 or abs(avionics_battery_level-mpstate.status.avionics_battery_level) > 70:
        mpstate.status.avionics_battery_level = avionics_battery_level
    else:
        mpstate.status.avionics_battery_level = (95*mpstate.status.avionics_battery_level + 5*avionics_battery_level)/100



def battery_report():
    '''report battery level'''
    if int(mpstate.settings.battreadout) == 0:
        return

    rbattery_level = int((mpstate.status.battery_level+5)/10)*10

    if rbattery_level != mpstate.status.last_battery_announce:
        say("Flight battery %u percent" % rbattery_level,priority='notification')
        mpstate.status.last_battery_announce = rbattery_level
    if rbattery_level <= 20:
        say("Flight battery warning")

    # avionics battery reporting disabled for now
    return
    avionics_rbattery_level = int((mpstate.status.avionics_battery_level+5)/10)*10

    if avionics_rbattery_level != mpstate.status.last_avionics_battery_announce:
        say("Avionics Battery %u percent" % avionics_rbattery_level,priority='notification')
        mpstate.status.last_avionics_battery_announce = avionics_rbattery_level
    if avionics_rbattery_level <= 20:
        say("Avionics battery warning")


foo_now = time.time()

def handle_usec_timestamp(m, master):
    '''special handling for MAVLink packets with a usec field'''
    usec = m.time_usec
    if usec + 6.0e7 < master.highest_usec:
        say('Time has wrapped')
        mpstate.console.writeln("usec %u highest_usec %u" % (usec, master.highest_usec))
        mpstate.status.highest_usec = usec
        for mm in mpstate.mav_master:
            mm.link_delayed = False
            mm.highest_usec = usec
        return

    # we want to detect when a link has significant buffering, causing us to
    # receive old packets. If we get packets that are more than 1 second old,
    # then mark the link as being delayed. We will not act on packets from this
    # link until it has caught up
    master.highest_usec = usec
    if usec > mpstate.status.highest_usec:
        mpstate.status.highest_usec = usec
    if usec + 1e6 < mpstate.status.highest_usec and not master.link_delayed and len(mpstate.mav_master) > 1:
        master.link_delayed = True
        mpstate.console.writeln("link %u delayed" % (master.linknum+1))
    elif usec + 0.5e6 > mpstate.status.highest_usec and master.link_delayed:
        master.link_delayed = False
        mpstate.console.writeln("link %u OK" % (master.linknum+1))

def report_altitude(altitude):
    '''possibly report a new altitude'''
    mpstate.status.altitude = altitude
    if (int(mpstate.settings.altreadout) > 0 and
        math.fabs(mpstate.status.altitude - mpstate.status.last_altitude_announce) >= int(mpstate.settings.altreadout)):
        mpstate.status.last_altitude_announce = mpstate.status.altitude
        rounded_alt = int(mpstate.settings.altreadout) * ((5+int(mpstate.status.altitude - mpstate.settings.basealtitude)) / int(mpstate.settings.altreadout))
        say("height %u" % rounded_alt, priority='notification')

def master_callback(m, master):
    '''process mavlink message m on master, sending any messages to recipients'''
    if getattr(m, '_timestamp', None) is None:
        master.post_message(m)
    mpstate.status.counters['MasterIn'][master.linknum] += 1

    if not m.get_type().startswith('GPS_RAW') and getattr(m, 'time_usec', None) is not None:
        # update link_delayed attribute
        handle_usec_timestamp(m, master)

    mtype = m.get_type()

    # and log them
    if mtype != 'BAD_DATA' and mpstate.logqueue:
        # XXX
        # print("type: %s" % mtype)
        # put link number in bottom 2 bits, so we can analyse packet
        # delay in saved logs
        usec = get_usec()
        usec = (usec & ~3) | master.linknum
        mpstate.logqueue.put(str(struct.pack('>Q', usec) + m.get_msgbuf()))

    if master.link_delayed:
        # don't process delayed packets 
        return
    
    if mtype == 'HEARTBEAT':
        if (mpstate.status.target_system != m.get_srcSystem() or
            mpstate.status.target_component != m.get_srcComponent()):
            mpstate.status.target_system = m.get_srcSystem()
            mpstate.status.target_component = m.get_srcComponent()
            say("online system %u component %u" % (mpstate.status.target_system, mpstate.status.target_component),'message')
        if mpstate.status.heartbeat_error:
            mpstate.status.heartbeat_error = False
            say("heartbeat OK")
        if master.linkerror:
            master.linkerror = False
            say("link %u OK" % (master.linknum+1))

        # Time between heartbeats sent by AP.  Set by SmaccmPilot
        # print("HEARTBEAT_DIFF: %f sec" % (time.time() - mpstate.status.last_heartbeat))
        mpstate.status.last_heartbeat = time.time()
        master.last_heartbeat = mpstate.status.last_heartbeat

    # XXX report messages dropped.
    elif mtype == "DATA16" and opts.smaccm_report_data:
        print("Data packet:")
        arr = m.data16
        dropped = 0
        for i in reversed([0,1,2,3]):
            dropped += ord(arr[i]) * pow(256, i)
        print("dropped: %d" % dropped)

        lastSucc = 0
        for i in reversed([4,5,6,7]):
            lastSucc += ord(arr[i]) * pow(256, i-4)
        print("lastSucc: %d" % lastSucc)

    elif mtype == 'STATUSTEXT':
        if m.text != mpstate.status.last_apm_msg:
            mpstate.console.writeln("APM: %s" % m.text, bg='red')
            mpstate.status.last_apm_msg = m.text
    elif mtype == 'PARAM_VALUE':
        mpstate.mav_param[str(m.param_id)] = m.param_value
        if m.param_index+1 == m.param_count:
            mpstate.console.writeln("Received %u parameters" % m.param_count)
            if mpstate.status.logdir != None:
                param_save(os.path.join(mpstate.status.logdir, 'mav.parm'), '*')

    elif mtype == 'SERVO_OUTPUT_RAW':
        if opts.quadcopter:
            mpstate.status.rc_throttle[0] = scale_rc(m.servo1_raw, 0.0, 1.0, param='RC3')
            mpstate.status.rc_throttle[1] = scale_rc(m.servo2_raw, 0.0, 1.0, param='RC3')
            mpstate.status.rc_throttle[2] = scale_rc(m.servo3_raw, 0.0, 1.0, param='RC3')
            mpstate.status.rc_throttle[3] = scale_rc(m.servo4_raw, 0.0, 1.0, param='RC3')
        else:
            mpstate.status.rc_aileron  = scale_rc(m.servo1_raw, -1.0, 1.0, param='RC1') * mpstate.settings.rc1mul
            mpstate.status.rc_elevator = scale_rc(m.servo2_raw, -1.0, 1.0, param='RC2') * mpstate.settings.rc2mul
            mpstate.status.rc_throttle = scale_rc(m.servo3_raw, 0.0, 1.0, param='RC3')
            mpstate.status.rc_rudder   = scale_rc(m.servo4_raw, -1.0, 1.0, param='RC4') * mpstate.settings.rc4mul
            if mpstate.status.rc_throttle < 0.1:
                mpstate.status.rc_throttle = 0

    elif mtype in ['WAYPOINT_COUNT','MISSION_COUNT']:
        if mpstate.status.wp_op is None:
            mpstate.console.error("No waypoint load started")
        else:
            mpstate.status.wploader.clear()
            mpstate.status.wploader.expected_count = m.count
            mpstate.console.writeln("Requesting %u waypoints t=%s now=%s" % (m.count,
                                                                             time.asctime(time.localtime(m._timestamp)),
                                                                             time.asctime()))
            master.waypoint_request_send(0)

    elif mtype in ['WAYPOINT', 'MISSION_ITEM'] and mpstate.status.wp_op != None:
        if m.seq > mpstate.status.wploader.count():
            mpstate.console.writeln("Unexpected waypoint number %u - expected %u" % (m.seq, mpstate.status.wploader.count()))
        elif m.seq < mpstate.status.wploader.count():
            # a duplicate
            pass
        else:
            mpstate.status.wploader.add(m)
        if m.seq+1 < mpstate.status.wploader.expected_count:
            master.waypoint_request_send(m.seq+1)
        else:
            if mpstate.status.wp_op == 'list':
                for i in range(mpstate.status.wploader.count()):
                    w = mpstate.status.wploader.wp(i)
                    print("%u %u %.10f %.10f %f p1=%.1f p2=%.1f p3=%.1f p4=%.1f cur=%u auto=%u" % (
                        w.command, w.frame, w.x, w.y, w.z,
                        w.param1, w.param2, w.param3, w.param4,
                        w.current, w.autocontinue))
            elif mpstate.status.wp_op == "save":
                save_waypoints(mpstate.status.wp_save_filename)
            mpstate.status.wp_op = None

    elif mtype in ["WAYPOINT_REQUEST", "MISSION_REQUEST"]:
        process_waypoint_request(m, master)

    elif mtype in ["WAYPOINT_CURRENT", "MISSION_CURRENT"]:
        if m.seq != mpstate.status.last_waypoint:
            mpstate.status.last_waypoint = m.seq
            say("waypoint %u" % m.seq,priority='message')

    elif mtype == "SYS_STATUS":
        battery_update(m)
        if master.flightmode != mpstate.status.flightmode:
            mpstate.status.flightmode = master.flightmode
            mpstate.rl.set_prompt(mpstate.status.flightmode + "> ")
            say("Mode " + mpstate.status.flightmode)

    elif mtype == "VFR_HUD":
        have_gps_fix = False
        if 'GPS_RAW' in mpstate.status.msgs and mpstate.status.msgs['GPS_RAW'].fix_type == 2:
            have_gps_fix = True
        if 'GPS_RAW_INT' in mpstate.status.msgs and mpstate.status.msgs['GPS_RAW_INT'].fix_type == 3:
            have_gps_fix = True
        if have_gps_fix and not mpstate.status.have_gps_lock:
                mpstate.status.last_altitude_announce = 0.0
                say("GPS lock at %u meters" % m.alt, priority='notification')
                # re-fetch to get the home pressure and temperature
                mpstate.master().param_fetch_all()
                mpstate.status.have_gps_lock = True
        ground_press = get_mav_param('GND_ABS_PRESS', None)
        if opts.quadcopter or ground_press is None:
            # we're on a ArduCopter which uses relative altitude in VFR_HUD
            report_altitude(m.alt)
        if opts.smaccm_report_data:
            # XXX Print incoming VFR info and keep track of time.
            global foo_now
            print("Got VFR: %f, heading: %f" % (time.time() - foo_now, m.heading))
            foo_now = time.time()

    elif mtype == "GPS_RAW":
        if mpstate.status.have_gps_lock:
            if m.fix_type != 2 and not mpstate.status.lost_gps_lock:
                say("GPS fix lost")
                mpstate.status.lost_gps_lock = True
            if m.fix_type == 2 and mpstate.status.lost_gps_lock:
                say("GPS OK")
                mpstate.status.lost_gps_lock = False

    elif mtype == "GPS_RAW_INT":
        if mpstate.status.have_gps_lock:
            if m.fix_type != 3 and not mpstate.status.lost_gps_lock:
                say("GPS fix lost")
                mpstate.status.lost_gps_lock = True
            if m.fix_type == 3 and mpstate.status.lost_gps_lock:
                say("GPS OK")
                mpstate.status.lost_gps_lock = False

    elif mtype == "RC_CHANNELS_RAW":
#        if (m.chan7_raw > 1700 and mpstate.status.flightmode == "MANUAL"):
#            system_check()
        if mpstate.settings.radiosetup:
            for i in range(1,9):
                v = getattr(m, 'chan%u_raw' % i)
                rcmin = get_mav_param('RC%u_MIN' % i, 0)
                if rcmin > v:
                    if param_set('RC%u_MIN' % i, v):
                        mpstate.console.writeln("Set RC%u_MIN=%u" % (i, v))
                rcmax = get_mav_param('RC%u_MAX' % i, 0)
                if rcmax < v:
                    if param_set('RC%u_MAX' % i, v):
                        mpstate.console.writeln("Set RC%u_MAX=%u" % (i, v))

    elif mtype == "NAV_CONTROLLER_OUTPUT" and mpstate.status.flightmode == "AUTO" and mpstate.settings.distreadout:
        rounded_dist = int(m.wp_dist/mpstate.settings.distreadout)*mpstate.settings.distreadout
        if math.fabs(rounded_dist - mpstate.status.last_distance_announce) >= mpstate.settings.distreadout:
            if rounded_dist != 0:
                say("%u" % rounded_dist, priority="progress")
            mpstate.status.last_distance_announce = rounded_dist

    elif mtype == "FENCE_STATUS":
        if not mpstate.status.fence_enabled:
            mpstate.status.fence_enabled = True
            say("fence enabled")
        if mpstate.status.last_fence_breach != m.breach_time:
            say("fence breach")
        if mpstate.status.last_fence_status != m.breach_status:
            if m.breach_status == mavutil.mavlink.FENCE_BREACH_NONE:
                say("fence OK")
        mpstate.status.last_fence_breach = m.breach_time
        mpstate.status.last_fence_status = m.breach_status

    elif mtype == "SCALED_PRESSURE":
        ground_press = get_mav_param('GND_ABS_PRESS', None)
        ground_temperature = get_mav_param('GND_TEMP', None)
        if ground_press is not None and ground_temperature is not None:
            altitude = None
            try:
                scaling = ground_press / (m.press_abs*100)
                temp = ground_temperature + 273.15
                altitude = math.log(scaling) * temp * 29271.267 * 0.001
            except ValueError:
                pass
            except ZeroDivisionError:
                pass
            if altitude is not None:
                report_altitude(altitude)

    elif mtype == "BAD_DATA":
        # print("Bad data! ")
        if mavutil.all_printable(m.data):
            mpstate.console.write(str(m.data), bg='red')
    elif mtype in [ 'HEARTBEAT', 'GLOBAL_POSITION', 'RC_CHANNELS_SCALED',
                    'ATTITUDE', 'RC_CHANNELS_RAW', 'GPS_STATUS', 'WAYPOINT_CURRENT',
                    'SERVO_OUTPUT_RAW', 'VFR_HUD',
                    'GLOBAL_POSITION_INT', 'RAW_PRESSURE', 'RAW_IMU',
                    'WAYPOINT_ACK', 'MISSION_ACK',
                    'NAV_CONTROLLER_OUTPUT', 'GPS_RAW', 'GPS_RAW_INT', 'WAYPOINT',
                    'SCALED_PRESSURE', 'SENSOR_OFFSETS', 'MEMINFO', 'AP_ADC',
                    'FENCE_POINT', 'FENCE_STATUS', 'DCM', 'RADIO', 'AHRS', 'HWSTATUS', 'SIMSTATE', 'PPP' ]:
        pass
    else:
        pass

    if mpstate.status.watch is not None:
        if fnmatch.fnmatch(m.get_type().upper(), mpstate.status.watch.upper()):
            mpstate.console.writeln(m)

    # keep the last message of each type around
    mpstate.status.msgs[m.get_type()] = m
    if not m.get_type() in mpstate.status.msg_count:
        mpstate.status.msg_count[m.get_type()] = 0
    mpstate.status.msg_count[m.get_type()] += 1

    # don't pass along bad data
    if mtype != "BAD_DATA":
        # pass messages along to listeners, except for REQUEST_DATA_STREAM, which
        # would lead a conflict in stream rate setting between mavproxy and the other
        # GCS
        if mpstate.settings.mavfwd_rate or mtype != 'REQUEST_DATA_STREAM':
            for r in mpstate.mav_outputs:
                r.write(m.get_msgbuf())

        # pass to modules
        for mod in mpstate.modules:
            try:
                mod.mavlink_packet(m)
            except Exception, msg:
                if mpstate.settings.moddebug == 1:
                    print(msg)
                elif mpstate.settings.moddebug > 1:
                    import traceback
                    exc_type, exc_value, exc_traceback = sys.exc_info()
                    traceback.print_exception(exc_type, exc_value, exc_traceback,
                                              limit=2, file=sys.stdout)

def process_master(m):
    '''process packets from the MAVLink master'''
    try:
        s = m.recv()
    except Exception:
        return
    if mpstate.logqueue_raw:
        mpstate.logqueue_raw.put(str(s))

    if mpstate.status.setup_mode:
        sys.stdout.write(str(s))
        sys.stdout.flush()
        return

    if m.first_byte:
        m.auto_mavlink_version(s)
    msgs = m.mav.parse_buffer(s)
    if msgs:
        for msg in msgs:
            if getattr(m, '_timestamp', None) is None:
                m.post_message(msg)
            if msg.get_type() == "BAD_DATA":
                if opts.show_errors:
                    mpstate.console.writeln("MAV error: %s" % msg)
                mpstate.status.mav_error += 1

    

def process_mavlink(slave):
    '''process packets from MAVLink slaves, forwarding to the master'''
    try:
        buf = slave.recv()
    except socket.error:
        return
    try:
        if slave.first_byte:
            slave.auto_mavlink_version(buf)
        m = slave.mav.decode(buf)
    except mavutil.mavlink.MAVError as e:
        mpstate.console.error("Bad MAVLink slave message from %s: %s" % (slave.address, e.message))
        return
    if mpstate.settings.mavfwd and not mpstate.status.setup_mode:
        mpstate.master().write(m.get_msgbuf())
    mpstate.status.counters['Slave'] += 1


def mkdir_p(dir):
    '''like mkdir -p'''
    if not dir:
        return
    if dir.endswith("/"):
        mkdir_p(dir[:-1])
        return
    if os.path.isdir(dir):
        return
    mkdir_p(os.path.dirname(dir))
    os.mkdir(dir)

def log_writer():
    '''log writing thread'''
    while True:
        mpstate.logfile_raw.write(mpstate.logqueue_raw.get())
        while not mpstate.logqueue_raw.empty():
            mpstate.logfile_raw.write(mpstate.logqueue_raw.get())
        while not mpstate.logqueue.empty():
            mpstate.logfile.write(mpstate.logqueue.get())
        mpstate.logfile.flush()
        mpstate.logfile_raw.flush()
        if status_period.trigger():
            mpstate.status.write()

def open_logs():
    '''open log files'''
    if opts.append_log:
        mode = 'a'
    else:
        mode = 'w'
    logfile = opts.logfile
    if opts.aircraft is not None:
        dirname = "%s/logs/%s" % (opts.aircraft, time.strftime("%Y-%m-%d"))
        mkdir_p(dirname)
        for i in range(1, 10000):
            fdir = os.path.join(dirname, 'flight%u' % i)
            if not os.path.exists(fdir):
                break
        if os.path.exists(fdir):
            print("Flight logs full")
            sys.exit(1)
        mkdir_p(fdir)
        print(fdir)
        logfile = os.path.join(fdir, 'flight.log')
        mpstate.status.logdir = fdir
    mpstate.logfile_name = logfile
    mpstate.logfile = open(logfile, mode=mode)
    mpstate.logfile_raw = open(logfile+'.raw', mode=mode)
    print("Logging to %s" % logfile)

    # queues for logging
    mpstate.logqueue = Queue.Queue()
    mpstate.logqueue_raw = Queue.Queue()

    # use a separate thread for writing to the logfile to prevent
    # delays during disk writes (important as delays can be long if camera
    # app is running)
    t = threading.Thread(target=log_writer)
    t.daemon = True
    t.start()


def set_stream_rates():
    '''set mavlink stream rates'''
    if (not msg_period.trigger() and
        mpstate.status.last_streamrate1 == mpstate.settings.streamrate and
        mpstate.status.last_streamrate2 == mpstate.settings.streamrate2):
        return
    mpstate.status.last_streamrate1 = mpstate.settings.streamrate
    mpstate.status.last_streamrate2 = mpstate.settings.streamrate2

    for master in mpstate.mav_master:
        if master.linknum == 0:
            rate = mpstate.settings.streamrate
        else:
            rate = mpstate.settings.streamrate2
        # print("Sending stream rate req: %f" % rate)
        master.mav.request_data_stream_send(mpstate.status.target_system, mpstate.status.target_component,
                                            mavutil.mavlink.MAV_DATA_STREAM_ALL,
                                            rate, 1)

def check_link_status():
    '''check status of master links'''
    tnow = time.time()
    if mpstate.status.last_heartbeat != 0 and tnow > mpstate.status.last_heartbeat + 5:
        say("no heartbeat")
        mpstate.status.heartbeat_error = True
    for master in mpstate.mav_master:
        if not master.linkerror and tnow > master.last_heartbeat + 5:
            say("link %u down" % (master.linknum+1))
            master.linkerror = True

def periodic_tasks():
    '''run periodic checks'''
    if mpstate.status.setup_mode:
        return

    while not mpstate.msg_queue.empty():
        # Shouldn't block here if the queue isn't empty, but be defensive.
        message = mpstate.msg_queue.get(block=False)
        logging.info('Sending queued message %s', message)
        mpstate.master().mav.send(message)


    if heartbeat_period.trigger() and mpstate.settings.heartbeat != 0:
        mpstate.status.counters['MasterOut'] += 1
        for master in mpstate.mav_master:
            if master.mavlink10():
                master.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                                          0, 0, 0)
            else:
                MAV_GROUND = 5
                MAV_AUTOPILOT_NONE = 4
                master.mav.heartbeat_send(MAV_GROUND, MAV_AUTOPILOT_NONE)

    if heartbeat_check_period.trigger():
        check_link_status()

    set_stream_rates()

    for master in mpstate.mav_master:
        if (not master.param_fetch_complete and
            mpstate.settings.paramretry != 0 and
            time.time() - mpstate.status.last_paramretry > mpstate.settings.paramretry and
            master.time_since('PARAM_VALUE') > 10 and
            'HEARTBEAT' in master.messages):
            mpstate.status.last_paramretry = time.time()
            mpstate.console.writeln("fetching parameters")
            master.param_fetch_all()

    if battery_period.trigger():
        battery_report()

    if mpstate.smaccm_nav.period.trigger():
        mpstate.smaccm_nav.send()

    if mpstate.override_period.trigger(): # Period for override messages
        if (# Not an empty override message
            mpstate.status.override != [ 0 ] * 8 and
            # Deadman button on
            mpstate.status.override[4] == 2000):
           send_rc_override()
         # Recently sent messages?  Then notify the deadman is off a few times
        # so it gets through the radio.
        elif mpstate.status.joystick_inputs:
            mpstate.status.override = [ 0 ] * 8
            for i in range(10):
                send_rc_override()
            mpstate.status.joystick_inputs = False

    # call optional module idle tasks. These are called at several hundred Hz.
    # This includes the joystick
    for m in mpstate.modules:
        if hasattr(m, 'idle_task'):
            try:
                m.idle_task()
            except Exception, msg:
                if mpstate.settings.moddebug == 1:
                    print(msg)
                elif mpstate.settings.moddebug > 1:
                    import traceback
                    exc_type, exc_value, exc_traceback = sys.exc_info()
                    traceback.print_exception(exc_type, exc_value, exc_traceback,
                                              limit=2, file=sys.stdout)




def main_loop():
    '''main processing loop'''
    if not mpstate.status.setup_mode and not opts.nowait:
        for master in mpstate.mav_master:
            master.wait_heartbeat()
            master.param_fetch_all()
        set_stream_rates()

    while True:
        if mpstate is None or mpstate.status.exit:
            return
        if mpstate.rl.line is not None:
            cmds = mpstate.rl.line.split(';')
            for c in cmds:
                process_stdin(c)
            mpstate.rl.line = None

        for master in mpstate.mav_master:
            if master.fd is None:
                if master.port.inWaiting() > 0:
                    process_master(master)

        periodic_tasks()

        rin = []
        for master in mpstate.mav_master:
            if master.fd is not None:
                rin.append(master.fd)
        for m in mpstate.mav_outputs:
            rin.append(m.fd)
        if rin == []:
            time.sleep(0.001)
            continue

        for fd in mpstate.select_extra:
            rin.append(fd)
        try:
            (rin, win, xin) = select.select(rin, [], [], 0.001)
        except select.error:
            continue

        if mpstate is None:
            return

        for fd in rin:
            for master in mpstate.mav_master:
                if fd == master.fd:
                    process_master(master)
                    continue
            for m in mpstate.mav_outputs:
                if fd == m.fd:
                    process_mavlink(m)
                    continue

            # this allow modules to register their own file descriptors
            # for the main select loop
            if fd in mpstate.select_extra:
                try:
                    # call the registered read function
                    (fn, args) = mpstate.select_extra[fd]
                    fn(args)
                except Exception, msg:
                    if mpstate.settings.moddebug == 1:
                        print(msg)
                    # on an exception, remove it from the select list
                    mpstate.select_extra.pop(fd)



def input_loop():
    '''wait for user input'''
    while True:
        while mpstate.rl.line is not None:
            time.sleep(0.01)
        try:
            line = raw_input(mpstate.rl.prompt)
        except EOFError:
            mpstate.status.exit = True
            sys.exit(1)
        mpstate.rl.line = line
            

def run_script(scriptfile):
    '''run a script file'''
    try:
        f = open(scriptfile, mode='r')
    except Exception:
        return
    mpstate.console.writeln("Running script %s" % scriptfile)
    for line in f:
        line = line.strip()
        if line == "" or line.startswith('#'):
            continue
        mpstate.console.writeln("-> %s" % line)
        process_stdin(line)
    f.close()


LOGGING_LEVELS = {
  'DEBUG': logging.DEBUG,
  'INFO': logging.INFO,
  'WARNING': logging.WARNING,
  'ERROR': logging.ERROR,
  'CRITICAL': logging.CRITICAL
  }


def get_logging_level_by_name(name):
  return LOGGING_LEVELS[name]

def zero2pidomain(angle):
    twopi = 2*math.pi
    wrapped = math.fmod(math.fabs(angle),twopi)
    if angle > 0:
        return wrapped
    else:
        return twopi - wrapped


if __name__ == '__main__':

    from optparse import OptionParser
    parser = OptionParser("mavproxy.py [options]")

    parser.add_option('--logging-level', dest='logging_level', default='INFO',
                      choices=LOGGING_LEVELS.keys(),
                      help='The logging level to use.')
    parser.add_option("--master",dest="master", action='append', help="MAVLink master port", default=[])
    parser.add_option("--baudrate", dest="baudrate", type='int',
                      help="master port baud rate", default=115200)
    parser.add_option("--out",   dest="output", help="MAVLink output port",
                      action='append', default=[])
    parser.add_option("--sitl", dest="sitl",  default=None, help="SITL output port")
    parser.add_option("--streamrate",dest="streamrate", default=4, type='int',
                      help="MAVLink stream rate")
    parser.add_option("--source-system", dest='SOURCE_SYSTEM', type='int',
                      default=255, help='MAVLink source system for this GCS')
    parser.add_option("--target-system", dest='TARGET_SYSTEM', type='int',
                      default=1, help='MAVLink target master system')
    parser.add_option("--target-component", dest='TARGET_COMPONENT', type='int',
                      default=1, help='MAVLink target master component')
    parser.add_option("--logfile", dest="logfile", help="MAVLink master logfile",
                      default='mav.log')
    parser.add_option("-a", "--append-log", dest="append_log", help="Append to log files",
                      action='store_true', default=False)
    parser.add_option("--quadcopter", dest="quadcopter", help="use quadcopter controls",
                      action='store_true', default=False)
    parser.add_option("--setup", dest="setup", help="start in setup mode",
                      action='store_true', default=False)
    parser.add_option("--nodtr", dest="nodtr", help="disable DTR drop on close",
                      action='store_true', default=False)
    parser.add_option("--show-errors", dest="show_errors", help="show MAVLink error packets",
                      action='store_true', default=False)
    parser.add_option("--speech", dest="speech", help="use text to speach",
                      action='store_true', default=False)
    parser.add_option("--num-cells", dest="num_cells", help="number of LiPo battery cells",
                      type='int', default=0)
    parser.add_option("--aircraft", dest="aircraft", help="aircraft name", default=None)
    parser.add_option("--cmd", dest="cmd", help="initial commands", default=None)
    parser.add_option("--console", action='store_true', help="use GUI console")
    parser.add_option("--map", action='store_true', help="load map module")
    parser.add_option(
        '--module', action='append',
        help='Load the specified module.  Can be used multiple times.')
    parser.add_option("--mav09", action='store_true', default=False, help="Use MAVLink protocol 0.9")
    parser.add_option("--nowait", action='store_true', default=False, help="don't wait for HEARTBEAT on startup")
    parser.add_option("--smaccm-report-data", action='store_true',
            default=False, help="smaccm specific data16 reporting")
    
    (opts, args) = parser.parse_args()

    if opts.mav09:
        os.environ['MAVLINK09'] = '1'
    import mavutil, mavwp

    # global mavproxy state
    mpstate = MPState()
    mpstate.status.exit = False
    mpstate.command_map = command_map

    if opts.speech:
        # start the speech-dispatcher early, so it doesn't inherit any ports from
        # modules/mavutil
        say('Startup')

    if not opts.master:
        serial_list = mavutil.auto_detect_serial(preferred_list=['*FTDI*',"*Arduino_Mega_2560*", "*USB_to_UART*"])
        if len(serial_list) == 1:
            opts.master = [serial_list[0].device]
        else:
            print('''
Please choose a MAVLink master with --master
For example:
    --master=com14
    --master=/dev/ttyUSB0
    --master=127.0.0.1:14550

Auto-detected serial ports are:
''')
            for port in serial_list:
                print("%s" % port)
            sys.exit(1)

    # container for status information
    mpstate.status.target_system = opts.TARGET_SYSTEM
    mpstate.status.target_component = opts.TARGET_COMPONENT

    mpstate.mav_master = []
    
    # open master link
    for mdev in opts.master:
        if mdev.startswith('tcp:'):
            m = mavutil.mavtcp(mdev[4:])
        elif mdev.find(':') != -1:
            m = mavutil.mavudp(mdev, input=True)
        else:
            m = mavutil.mavserial(mdev, baud=opts.baudrate, autoreconnect=True)
        m.mav.set_callback(master_callback, m)
        m.linknum = len(mpstate.mav_master)
        m.linkerror = False
        m.link_delayed = False
        m.last_heartbeat = 0
        m.highest_usec = 0
        mpstate.mav_master.append(m)
        mpstate.status.counters['MasterIn'].append(0)

    # log all packets from the master, for later replay
    open_logs()

    # open any mavlink UDP ports
    for p in opts.output:
        mpstate.mav_outputs.append(mavutil.mavudp(p, input=False))

    if opts.sitl:
        mpstate.sitl_output = mavutil.mavudp(opts.sitl, input=False)

    mpstate.settings.numcells = opts.num_cells
    mpstate.settings.speech = opts.speech
    mpstate.settings.streamrate = opts.streamrate
    mpstate.settings.streamrate2 = opts.streamrate

    status_period = mavutil.periodic_event(1.0)
    # XXX crank up the rate for sending stream rate data (specifically request_data_stream)
    msg_period = mavutil.periodic_event(1)
    heartbeat_period = mavutil.periodic_event(1)
    battery_period = mavutil.periodic_event(0.1)
    if mpstate.sitl_output:
        mpstate.override_period = mavutil.periodic_event(20)
    else:
        mpstate.override_period = mavutil.periodic_event(20)
    heartbeat_check_period = mavutil.periodic_event(0.33)

    mpstate.rl = rline("MAV> ")
    if opts.setup:
        mpstate.rl.set_prompt("")

    if opts.aircraft is not None:
        start_script = os.path.join(opts.aircraft, "mavinit.scr")
        if os.path.exists(start_script):
            run_script(start_script)

    if opts.console:
        process_stdin('module load console')

    if opts.map:
        process_stdin('module load map')

    if opts.module:
        for module in opts.module:
            process_stdin('module load %s' % (module,))

    if opts.cmd is not None:
        cmds = opts.cmd.split(';')
        for c in cmds:
            process_stdin(c)

    # run main loop as a thread
    mpstate.status.thread = threading.Thread(target=main_loop)
    mpstate.status.thread.daemon = True
    mpstate.status.thread.start()

    # use main program for input. This ensures the terminal cleans
    # up on exit
    try:
        input_loop()
    except KeyboardInterrupt:
        print("exiting")
        mpstate.status.exit = True
        sys.exit(1)

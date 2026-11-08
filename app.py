from flask import Flask, render_template, request, jsonify, send_from_directory, g
from pathlib import Path
from contextlib import closing
from tempfile import NamedTemporaryFile
import hashlib, json, re, socket, threading, math, os, time
app=Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
ROOT=Path(__file__).resolve().parent
CFG=ROOT/'config.json'
NGINX_SITE = Path('/etc/nginx/sites-enabled/hp6033a')
lock=threading.Lock()
DEFAULT={'instrument_name':'HP 6033A','voltage':0.0,'current':1.0,'output':False,'front_panel_lockout_requested':False,'voltage_presets':[3.3,5,9,12,13.8,18], 'current_presets':[0.5,1,2,3,5,10]}
CLASSIC_LABEL_DEFAULTS = {
    'model': '6033A',
    'name': 'SYSTEM POWER SUPPLY',
    'voltage': '0-20V',
    'current': '0-30A',
    'power': '200W',
}


def valid_classic_label(value):
    return isinstance(value, str) and 1 <= len(value.strip()) <= 40 and value.isprintable()


def load():
    try:
        data=json.loads(CFG.read_text()); return {**DEFAULT,**data}
    except (OSError,ValueError):return DEFAULT.copy()
state=load()
configured_labels = state.get('classic_labels')
if not isinstance(configured_labels, dict):
    configured_labels = {}
state['classic_labels'] = {
    key: configured_labels[key].strip() if valid_classic_label(configured_labels.get(key)) else default
    for key, default in CLASSIC_LABEL_DEFAULTS.items()
}
state["output"]=False  # Simulation defaults OFF on every process restart
def save():
    temp=CFG.with_suffix('.tmp');temp.write_text(json.dumps(state,indent=2));temp.replace(CFG)


def proxy_control_auth_mode():
    # Only trust the header overwritten by the Nginx Unix-socket proxy.
    if request.environ.get('REMOTE_ADDR') != '':
        return None
    mode = request.headers.get('X-HP6033A-Password-Free')
    allowed_modes = ('1', '2') if request.is_secure else ('3',)
    if mode not in allowed_modes:
        return None
    # Older sites forward unknown client headers. Require an installed site
    # that explicitly overwrites the header in both proxy locations.
    try:
        directive = f'proxy_set_header X-HP6033A-Password-Free {mode};'
        lines = NGINX_SITE.read_text().splitlines()
        if sum(line.strip() == directive for line in lines) == 2:
            return mode
    except (OSError, UnicodeError):
        pass
    return None


def control_password_required():
    mode = proxy_control_auth_mode()
    if mode == '2':
        try:
            return (ROOT / '.control_auth_mode').read_text().strip() != 'password-free' or (
                not (ROOT / '.control_htpasswd').is_file()
            )
        except (OSError, UnicodeError):
            return True
    return mode not in ('1', '3')


@app.get('/')
def home():
    return render_template(
        'index.html', password_required=control_password_required(),
        auth_mode_editable=proxy_control_auth_mode() == "2",
        http_only=proxy_control_auth_mode() == "3",
        limits=dict(voltage=MAX_REMOTE_VOLTAGE, current=MAX_REMOTE_CURRENT)
    )

@app.get('/classic-dual')
def classic_dual():
    with lock:
        labels = dict(state['classic_labels'])
    return render_template(
        'classic_dual_preview.html', classic_labels=labels,
        password_required=control_password_required(),
        auth_mode_editable=proxy_control_auth_mode() == "2",
        http_only=proxy_control_auth_mode() == "3",
        limits=dict(voltage=MAX_REMOTE_VOLTAGE, current=MAX_REMOTE_CURRENT)
    )

UPDATE_SOCKET = "/run/hp6033a-update.sock"


def installed_software():
    try:
        version = json.loads((ROOT / ".hp6033a-version.json").read_text())
        tag, commit = version["release"], version["commit"]
        if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) and re.fullmatch(r"[a-f0-9]{40}", commit):
            return {"tag": tag, "display": f"{tag} ({commit[:12]})"}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    fingerprint = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    return {"tag": None, "display": f"Unversioned ({fingerprint})"}


def software_update_request(payload):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(UPDATE_SOCKET)
            connection.sendall(json.dumps(payload).encode() + b"\n")
            with connection.makefile("rb") as stream:
                response = json.loads(stream.readline(8192))
        result, code = response["body"], response["code"]
    except (OSError, ValueError, KeyError, TypeError):
        result, code = {"error": "Software update helper is unavailable."}, 503
    if payload == {"action": "status"}:
        result["installed"] = installed_software()
    return jsonify(result), code, {"Cache-Control": "no-store"}


@app.route('/api/software-updates', methods=['GET', 'POST'])
def software_updates():
    if not request.is_secure:
        return jsonify(error="Use the controller HTTPS address."), 403
    if request.method == "GET":
        return software_update_request({"action": "status"})
    if request.headers.get("Origin") != f"https://{request.host}":
        return jsonify(error="Update request must come from the controller page."), 403
    if not request.is_json or request.content_length is None or request.content_length > 4096:
        return jsonify(error="Expected a small JSON update request."), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("action") not in ("check", "authorize", "install"):
        return jsonify(error="Unknown software update request."), 400
    return software_update_request(payload)


@app.get('/docs/')
def docs():return send_from_directory(ROOT/'docs','index.html')
# READ_ONLY_GPIB_V1: live readback, no instrument writes
GPIB_RESOURCE = os.environ.get(
    'HP6033A_GPIB_RESOURCE',
    load().get('gpib_resource', '')
)
gpib_lock = threading.Lock()

# Synchronizes remote authorization with hardware commands.
control_lock = threading.RLock()


# LIVE_CONTROL_SAFETY_V1
MAX_REMOTE_VOLTAGE = 20.0
MAX_REMOTE_CURRENT = 30.0

# Never persist remote-control authorization.
# Every application restart begins with hardware writes disabled.
remote_control_enabled = False

# Hardware-changing commands must acquire gpib_lock.
# Never apply saved configuration values automatically.

def read_gpib():
    import pyvisa
    with gpib_lock:
        rm = pyvisa.ResourceManager('@py')
        ps = None
        try:
            ps = rm.open_resource(GPIB_RESOURCE)
            ps.timeout = 3000
            ps.write_termination = '\n'
            ps.read_termination = '\n'
            readings = {}
            for key in ('VSET', 'ISET', 'VOUT', 'IOUT', 'OUT', 'STS'):
                response = ps.query(key+'?').strip()
                parts = response.split()
                if len(parts) != 2 or parts[0].upper() != key:
                    raise ValueError('Unexpected '+key+' response: '+repr(response))
                value = float(parts[1])
                if not math.isfinite(value):
                    raise ValueError('Non-finite '+key+' reading')
                readings[key] = value
            return readings
        finally:
            if ps is not None: ps.close()
            rm.close()


# GUARDED_GPIB_WRITE_V1
def write_gpib_setting(key, value):
    with control_lock:
        import pyvisa

        # Output OFF is permitted even when remote programming is disabled.
        # All other hardware changes require authorization.
        if not remote_control_enabled and not (
            key == 'output' and value is False
        ):
            raise PermissionError("Remote control is disabled")

        commands = {
            'voltage': ('VSET', 'VSET?', MAX_REMOTE_VOLTAGE),
            'current': ('ISET', 'ISET?', MAX_REMOTE_CURRENT),
        }

        if key in commands:
            command, query, maximum = commands[key]
            if isinstance(value, bool):
                raise ValueError("Invalid numeric setting")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError("Invalid numeric setting")

            if not math.isfinite(number) or not 0 <= number <= maximum:
                raise ValueError("Setting exceeds remote-control safety limit")

            instruction = f"{command} {number:.3f}"
            expected = number

        elif key == 'output':
            if not isinstance(value, bool):
                raise ValueError("Output must be true or false")

            instruction = 'OUT ON' if value else 'OUT OFF'
            query = 'OUT?'
            expected = int(value)

        else:
            raise ValueError("Unsupported hardware command")

        with gpib_lock:
            rm = pyvisa.ResourceManager('@py')
            ps = None
            try:
                ps = rm.open_resource(GPIB_RESOURCE)
                ps.timeout = 5000
                ps.write_termination = '\n'
                ps.read_termination = '\n'

                # Verify output is OFF before changing voltage/current.
                if key in ('voltage', 'current'):
                    output_response = ps.query('OUT?').strip().split()
                    if output_response != ['OUT', '0']:
                        raise RuntimeError(
                            'Voltage/current programming blocked: '
                            'output is not confirmed OFF'
                        )

                # Verify hardware settings before enabling output.
                if key == 'output' and value is True:
                    for setting, maximum in (
                        ('VSET', MAX_REMOTE_VOLTAGE),
                        ('ISET', MAX_REMOTE_CURRENT),
                    ):
                        reply = ps.query(setting + '?').strip().split()

                        if len(reply) != 2 or reply[0].upper() != setting:
                            raise RuntimeError(
                                'Cannot verify ' + setting
                            )

                        actual_setting = float(reply[1])

                        minimum = 0.001 if setting == 'ISET' else 0.0
                        if not math.isfinite(actual_setting) or not (
                            minimum <= actual_setting <= maximum
                        ):
                            raise RuntimeError(
                                setting + ' is outside permitted output-on limits'
                            )

                    output_reply = ps.query('OUT?').strip().split()
                    if output_reply != ['OUT', '0']:
                        raise RuntimeError(
                            'Output must be confirmed OFF before enabling'
                        )

                ps.write(instruction)
                response = ps.query(query).strip().split()

                expected_name = query.rstrip('?')
                if len(response) != 2 or response[0].upper() != expected_name:
                    raise RuntimeError("Unexpected hardware readback")

                actual = float(response[1])
                if not math.isfinite(actual) or abs(actual - expected) > 0.005:
                    raise RuntimeError(
                        f"Hardware readback mismatch: expected {expected}, got {actual}"
                    )

                return actual
            finally:
                if ps is not None:
                    ps.close()
                rm.close()


    # REMOTE_AUTH_API_V1

def valid_control_credentials():
    import bcrypt
    import hmac

    credentials = request.authorization
    if not credentials or credentials.type.lower() != 'basic':
        return False
    try:
        entry = (ROOT / '.control_htpasswd').read_text().strip()
        username, password_hash = entry.split(':', 1)
        valid_user = hmac.compare_digest(
            (credentials.username or '').encode('utf-8'), username.encode('utf-8')
        )
        valid_password = bcrypt.checkpw(
            (credentials.password or '').encode('utf-8'), password_hash.encode('ascii')
        )
        return valid_user and valid_password
    except (OSError, ValueError, UnicodeError):
        return False


@app.before_request
def authenticate_control_request():
    if not request.path.startswith('/api/control/'):
        return None

    # Serialize authorization and instrument operations with mode changes.
    control_lock.acquire()
    g.control_request_locked = True
    if control_password_required() and not valid_control_credentials():
        return jsonify(error='Invalid or missing credentials'), 401
    return None


@app.teardown_request
def release_control_request_lock(error):
    if g.pop('control_request_locked', False):
        control_lock.release()


@app.post('/api/admin/control-auth')
def set_dashboard_control_auth():
    global remote_control_enabled

    if proxy_control_auth_mode() != '2':
        return jsonify(error='Dashboard authentication settings are unavailable'), 403
    if request.headers.get('Origin') != f'https://{request.host}':
        return jsonify(error='Request origin did not match the controller'), 403
    if not valid_control_credentials():
        return jsonify(error='Invalid or missing credentials'), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get('password_required')) is not bool:
        return jsonify(error='Expected a password requirement setting'), 400

    required = data['password_required']
    with control_lock:
        if required != control_password_required():
            temporary = None
            try:
                with NamedTemporaryFile(mode='w', dir=ROOT, delete=False) as config:
                    temporary = Path(config.name)
                    config.write('protected\n' if required else 'password-free\n')
                temporary.replace(ROOT / '.control_auth_mode')
            except OSError:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                return jsonify(error='Could not save the authentication setting'), 500
            remote_control_enabled = False
    return jsonify(password_required=required)


@app.post('/api/control/enable')
def enable_remote_control():
    global remote_control_enabled

    with control_lock:
        remote_control_enabled = False
        try:
            r = read_gpib()

            if r['OUT'] != 0:
                return jsonify(error='Power supply output must be OFF'), 409

            if not (0 <= r['VSET'] <= MAX_REMOTE_VOLTAGE):
                return jsonify(error='Existing voltage exceeds safety limit'), 409

            if not (0 <= r['ISET'] <= MAX_REMOTE_CURRENT):
                return jsonify(error='Existing current exceeds safety limit'), 409

            remote_control_enabled = True
            return jsonify(
                enabled=True,
                message='Remote programming authorized',
                voltage=r['VSET'],
                current=r['ISET'],
                output=False
            )

        except Exception as exc:
            remote_control_enabled = False
            return jsonify(error=str(exc)[:180]), 503


@app.post('/api/control/output-off')
def output_off_only():
    global remote_control_enabled

    with control_lock:
        try:
            import pyvisa

            with gpib_lock, closing(pyvisa.ResourceManager('@py')) as rm:
                with closing(rm.open_resource(GPIB_RESOURCE)) as ps:
                    ps.timeout = 5000
                    ps.write_termination = '\n'
                    ps.read_termination = '\n'
                    ps.write('OUT OFF')
                    if ps.query('OUT?').strip().split() != ['OUT', '0']:
                        raise RuntimeError('Output OFF could not be verified')

        except Exception as exc:
            remote_control_enabled = False
            return jsonify(error=str(exc)[:180]), 503

        return jsonify(
            success=True,
            output=False,
            message='Power supply output confirmed OFF'
        )


@app.post('/api/control/disable')
def disable_remote_control():
    global remote_control_enabled

    with control_lock:
        remote_control_enabled = False

    return jsonify(
        enabled=False,
        message='Remote programming disabled'
    )


# AUTHENTICATED_HARDWARE_CONTROL_V1
@app.post('/api/control/set')
def set_remote_control():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(error='Expected JSON object'), 400

    if set(data) != {'key', 'value'}:
        return jsonify(error='Expected key and value only'), 400

    key = data['key']
    if not isinstance(key, str) or key not in ('voltage', 'current', 'output'):
        return jsonify(error='Unsupported control'), 400

    try:
        actual = write_gpib_setting(key, data['value'])
        return jsonify(success=True, key=key, actual=actual)

    except PermissionError as exc:
        return jsonify(error=str(exc)), 403

    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400

    except Exception:
        app.logger.exception('GPIB control failed')
        return jsonify(error='Hardware command failed; verify instrument state'), 503


@app.get('/api/status')
def status():
    with lock:
        snapshot = dict(state)
    result = {**snapshot,
              'remote_control_enabled': remote_control_enabled,
              'mode': 'LIVE CONTROL' if remote_control_enabled else 'LIVE READ ONLY',
              'gpib_connected':False,
              'gpib_status':'NOT CONNECTED', 'regulation':'UNKNOWN',
              'front_panel_lockout_active':None,
              'front_panel_lockout_status':'NOT APPLIED - READ ONLY',
              'output':None, 'measured_voltage':0.0, 'measured_current':0.0,
              'measured_power':0.0, 'sample_time':time.time()}
    try:
        r = read_gpib()
        result.update(voltage=r['VSET'], current=r['ISET'],
                      measured_voltage=r['VOUT'], measured_current=r['IOUT'],
                      measured_power=r['VOUT']*r['IOUT'],
                      gpib_connected=True, gpib_status='CONNECTED',
                      output=bool(int(r['OUT'])),
                      regulation='UNKNOWN')
    except Exception as exc:
        result['gpib_status']='ERROR: '+str(exc)[:140]
    return jsonify(result)

# Arduino health is independent of the HP 6033A's GPIB connection.
# Never send instrument commands from this diagnostics endpoint.
SERIAL_PORT=os.environ.get('HP6033A_ARDUINO_PORT','/dev/ttyACM0')
SERIAL_BAUD=115200
serial_lock=threading.Lock()

def arduino_probe():
    try:
        import serial
    except ImportError:
        return {'connected':False,'port':SERIAL_PORT,'firmware':None,'response_ms':None,'error':'pyserial not installed'}
    try:
        # Only one caller can open the port at a time; no other process should own it.
        with serial_lock:
            start=time.monotonic()
            with serial.Serial(SERIAL_PORT,SERIAL_BAUD,timeout=1.2,write_timeout=1.0) as dev:
                # The Micro uses native USB CDC; opening this port normally does not reset it.
                dev.reset_input_buffer()
                def ask(cmd):
                    dev.write((cmd+'\n').encode('ascii'))
                    dev.flush()
                    return dev.readline().decode('ascii',errors='replace').strip()
                pong=ask('PING')
                if pong!='PONG':
                    raise RuntimeError('Unexpected PING reply: '+repr(pong))
                elapsed=round((time.monotonic()-start)*1000,1)
                version=ask('VERSION?')
                if not version.startswith('HP6033A-GPIB '):
                    raise RuntimeError('Unexpected firmware reply: '+repr(version))
            return {'connected':True,'port':SERIAL_PORT,'firmware':version,'response_ms':elapsed,'error':None}
    except (OSError,Exception) as exc:
        return {'connected':False,'port':SERIAL_PORT,'firmware':None,'response_ms':None,'error':str(exc)[:180]}

probe_cache={'at':0.0,'value':None}
probe_cache_lock=threading.Lock()
@app.get('/api/arduino/status')
def arduino_status():
    # Cache routine polling; force=1 performs a new diagnostic on button press.
    force=request.args.get('force')=='1'
    with probe_cache_lock:
        now=time.monotonic()
        if force or probe_cache['value'] is None or now-probe_cache['at']>=5:
            probe_cache['value']=arduino_probe()
            probe_cache['at']=time.monotonic()
        return jsonify(probe_cache['value'])

@app.post('/api/settings')
def settings():
    data=request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error='Expected a JSON object'),400
    if 'classic_labels' in data:
        labels = data['classic_labels']
        if not isinstance(labels, dict) or not labels or any(
            key not in CLASSIC_LABEL_DEFAULTS or not valid_classic_label(value)
            for key, value in labels.items()
        ):
            return jsonify(error='Each Classic label must be 1–40 printable characters'),400
    if any(k in data for k in ('voltage','current','output','front_panel_lockout_requested')):
        return jsonify(error='Read-only mode: instrument controls are disabled'),403
    if 'instrument_name' in data:
        name = data['instrument_name']
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            return jsonify(error='Instrument name must be 1–60 characters'),400
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            return jsonify(error='Instrument name contains invalid characters'),400
        data['instrument_name'] = name.strip()
    with lock:
        for key,maximum in [('voltage',20),('current',30)]:
            if key in data:
                try:value=float(data[key])
                except (TypeError,ValueError):return jsonify(error=f'Invalid {key}'),400
                if not math.isfinite(value) or not 0<=value<=maximum:return jsonify(error=f'{key} out of range'),400
                state[key]=round(value,2)
        if 'output' in data:
            if not isinstance(data['output'],bool):return jsonify(error='Invalid output'),400
            state['output']=data['output']
        if 'front_panel_lockout_requested' in data:
            if not isinstance(data['front_panel_lockout_requested'],bool):return jsonify(error='Invalid lockout request'),400
            state['front_panel_lockout_requested']=data['front_panel_lockout_requested']
        for key,maximum in [('voltage_presets',20),('current_presets',30)]:
            if key in data:
                try:values=[float(x) for x in data[key]]
                except (TypeError,ValueError):return jsonify(error='Invalid presets'),400
                if len(values)!=6 or any(not math.isfinite(v) or not 0<=v<=maximum for v in values):return jsonify(error='Invalid presets'),400
                state[key]=values
        if 'instrument_name' in data:
            state['instrument_name'] = data['instrument_name']
        if 'classic_labels' in data:
            state['classic_labels'] = {
                **state['classic_labels'],
                **{key: value.strip() for key, value in data['classic_labels'].items()},
            }
        save()
    return status()
if __name__=='__main__':app.run(host='127.0.0.1',port=8000)

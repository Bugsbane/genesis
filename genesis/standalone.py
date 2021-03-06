import sys
import os
import logging
import syslog

from genesis.api import ComponentManager
from genesis.config import Config
from genesis.core import Application, AppDispatcher
from genesis.plugmgr import PluginLoader
from genesis import version
from genesis import deployed
import genesis.utils

try:
    from gevent.pywsgi import WSGIServer
    import gevent.pool
    http_server = 'gevent'
except ImportError:
    from wsgiref.simple_server import make_server
    WSGIServer = lambda adr,**kw : make_server(adr[0], adr[1], kw['application'])
    http_server = 'wsgiref'

from datetime import datetime


class DebugHandler (logging.StreamHandler):
    def __init__(self):
        self.capturing = False
        self.buffer = ''

    def start(self):
        self.capturing = True

    def stop(self):
        self.capturing = False

    def handle(self, record):
        if self.capturing:
            self.buffer += self.formatter.format(record) + '\n'

class ConsoleHandler (logging.StreamHandler):
    def __init__(self, stream, debug):
        self.debug = debug
        logging.StreamHandler.__init__(self, stream)

    def handle(self, record):
        if not self.stream.isatty():
            return logging.StreamHandler.handle(self, record)

        s = ''
        d = datetime.fromtimestamp(record.created)
        s += d.strftime("\033[37m%d.%m.%Y %H:%M \033[0m")
        if self.debug:
            s += ('%s:%s'%(record.filename,record.lineno)).ljust(30)
        l = ''
        if record.levelname == 'DEBUG':
            l = '\033[37mDEBUG\033[0m '
        if record.levelname == 'INFO':
            l = '\033[32mINFO\033[0m  '
        if record.levelname == 'WARNING':
            l = '\033[33mWARN\033[0m  '
        if record.levelname == 'ERROR':
            l = '\033[31mERROR\033[0m '
        s += l.ljust(9)
        s += record.msg
        s += '\n'
        self.stream.write(s)


def make_log(debug=False, log_level=logging.INFO):
    log = logging.getLogger('genesis')
    log.setLevel(logging.DEBUG)

    stdout = ConsoleHandler(sys.stdout, debug)
    stdout.setLevel(log_level)

    log.blackbox = DebugHandler()
    log.blackbox.setLevel(logging.DEBUG)
    dformatter = logging.Formatter('%(asctime)s [%(levelname)s] %(module)s: %(message)s')
    log.blackbox.setFormatter(dformatter)
    stdout.setFormatter(dformatter)
    log.addHandler(log.blackbox)

    log.addHandler(stdout)

    return log


def run_server(log_level=logging.INFO, config_file=''):
    log = make_log(debug=log_level==logging.DEBUG, log_level=log_level)

    # For the debugging purposes
    log.info('Genesis %s' % version())

    # We need this early
    genesis.utils.logger = log

    # Read config
    config = Config()
    if config_file:
        log.info('Using config file %s'%config_file)
        config.load(config_file)
    else:
        log.info('Using default settings')

    # Handle first-launch reconfiguration
    deployed.reconfigure(config)
    
    # Add log handler to config, so all plugins could access it
    config.set('log_facility',log)

    # Start recording log for the bug reports
    log.blackbox.start()

    arch = genesis.utils.detect_architecture()
    log.info('Detected architecture/hardware: %s, %s'%(arch[0],arch[1]))

    platform = genesis.utils.detect_platform()
    log.info('Detected platform: %s'%platform)

    # Load external plugins
    PluginLoader.initialize(log, config.get('genesis', 'plugins'), arch[0], platform)
    PluginLoader.load_plugins()

    # Start components
    app = Application(config)
    PluginLoader.register_mgr(app) # Register permanent app
    ComponentManager.create(app)

    # Check system time
    log.info('Verifying system time...')
    os = 0
    try:
        os = genesis.utils.SystemTime.get_offset()
    except Exception, e:
        log.error('System time could not be retrieved. Error: %s' % str(e))
    if os < -3600 or os > 3600:
        log.info('System time was off by %s secs - updating' % str(os))
        try:
            genesis.utils.SystemTime.set_datetime()
        except Exception, e:
            log.error('System time could not be set. Error: %s' % str(e))

    # Make sure correct kernel modules are enabled
    genesis.utils.shell('modprobe ip_tables')
    genesis.utils.shell('modprobe loop')
    # Load and verify security rules
    log.info('Starting security plugin...')
    genesis.apis.networkcontrol(app).session_start()

    # Start server
    host = config.get('genesis','bind_host')
    port = config.getint('genesis','bind_port')
    log.info('Listening on %s:%d'%(host, port))

    # SSL params
    ssl = {}
    if config.getint('genesis', 'ssl') == 1:
        ssl = {
    	    'keyfile':  config.get('genesis','cert_key'),
    	    'certfile': config.get('genesis','cert_file'),
    	}

    log.info('Using HTTP server: %s'%http_server)

    server = WSGIServer(
        (host, port),
        application=AppDispatcher(config).dispatcher,
        **ssl
    )

    config.set('server', server)

    try:
        syslog.openlog(
            ident='genesis',
            facility=syslog.LOG_AUTH,
        )
    except:
        syslog.openlog('genesis')

    log.info('Starting server')

    server.serve_forever()

    ComponentManager.get().stop()

    if hasattr(server, 'restart_marker'):
        log.info('Restarting by request')

        fd = 20 # Close all descriptors. Creepy thing
        while fd > 2:
            try:
                os.close(fd)
                log.debug('Closed descriptor #%i'%fd)
            except:
                pass
            fd -= 1

        import os
        os.execv(sys.argv[0], sys.argv)
    else:
        log.info('Stopped by request')

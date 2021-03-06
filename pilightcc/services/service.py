""" Service module. """

# Multiprocessing
from threading import Thread, Lock, Event

# Delay
import time

# Communication
import zmq

# Initialization
from argparse import ArgumentParser


class BaseService(object):
    """ BaseService class.
    Subclasses should implement _run_service.
    Implementations for _on_shutdown and _handle_message are optional.
    State codes 0-5 are reserved for BaseService.
    """

    # The delay interval for shutdown monitoring, safe delays.
    __SAFE_DELAY_INCREMENT = 0.5

    __HOST_ADDRESS = "tcp://127.0.0.1"

    def __init__(self, port, require_settings=False):
        """ Constructor
        - port      : the 0mq communication port
        """
        # Setup the 0mq channel.
        self.__context = zmq.Context()
        self.__socket = self.__context.socket(zmq.PAIR)
        print "{}: (pyzmq version: {}) started on: tcp://127.0.0.1:{}"\
            .format(self.__class__.__name__, zmq.pyzmq_version(), port)
        self.__socket.connect("{}:{}".format(
            BaseService.__HOST_ADDRESS, port))

        # Initialize state.
        self.__enabled = False
        self.__shutting_down = False
        self._state = None
        self._update_state()

        # Setup service if possible.
        if not require_settings:
            self._setup()
            self.__initialized = True
        else:
            self.__initialized = False

        # Setup setting handling.
        self.__setting_store = SettingsStore()

    def __del__(self):
        self.__socket.close()
        self.__context.destroy()

    def __load_settings(self, settings):
        self.__setting_store.update(settings)

    def __service_setup(self):
        self._setup()
        self.__initialized = True
        # Enable after initialization if already set to be enabled.
        if self.__enabled:
            self.__service_enable(self.__enabled)

    def __service_enable(self, enable):
        self.__enabled = enable
        # Only enable/disable if initialized first.
        if self.__initialized:
            self._enable(enable)

    def __service_on_shutdown(self):
        self.__shutting_down = True
        self._enable(False)
        self._on_shutdown()

    def __service_handle_message(self, msg):
        """ Handle standard message types.
        - msg   : the message (None is allowed)
        """
        if msg is not None:
            print("{}: Message received: {} - {}"
                  .format(self.__class__.__name__, msg.type, msg.data))
            if msg.type == ServiceMessage.Type.ENABLE:
                # Enable/Disable if state changed.
                if self.__enabled != msg.data:
                    self.__service_enable(msg.data)
                    self._update_state()

            elif msg.type == ServiceMessage.Type.KILL:
                if not self.__shutting_down:
                    self.__service_on_shutdown()
                    self._update_state()

            elif msg.type == ServiceMessage.Type.SETTINGS:
                self.__load_settings(msg.data)
                # Do first time setup if waiting for settings.
                if not self.__initialized:
                    self.__service_setup()

            else:
                self._handle_message(msg)

    def _register_settings_unit(self, keys, callback=None):
        self.__setting_store.add_unit(keys, callback)

    def _get_setting(self, key):
        return self.__setting_store.get_setting(key)

    def _get_settings(self):
        return self.__setting_store.get_settings()

    def _send_message(self, msg):
        msg.send(self.__socket)

    def _update_state(self, value=None, msg=None):
        # Use previous value if none was given.
        try:
            value = self._state.get_value() if value is None else value
        except AttributeError:
            pass
        # Only update if new state is different.
        new_state = ServiceState(self.__enabled, self.__shutting_down, value,
                                 msg)
        print("{}: State updated: {}"
              .format(self.__class__.__name__, new_state))
        if self._state != new_state:
            self._state = new_state
            self._send_message(ServiceMessage(ServiceMessage.Type.STATE,
                                              self._state.to_data()))

    def _safe_delay(self, delay):
        while delay > BaseService.__SAFE_DELAY_INCREMENT:
            # Delay for ony a small increment.
            time.sleep(BaseService.__SAFE_DELAY_INCREMENT)
            delay -= BaseService.__SAFE_DELAY_INCREMENT

            # Check and handle any messages.
            msg = ServiceMessage.check_for_message(self.__socket)
            self.__service_handle_message(msg)
            if self.__shutting_down:
                return
        # Sleep for any remaining delay.
        time.sleep(delay)

    def run(self):
        """ Service execution method.
        Should not be overridden.
        """
        # Main loop, exit on leave.
        while not self.__shutting_down:
            # Check for any incoming messages, wait if disabled.
            if self.__enabled and self.__initialized:
                # Run service.
                self._run_service()
                msg = ServiceMessage.check_for_message(self.__socket)
            else:
                msg = ServiceMessage.wait_for_message(self.__socket)

            self.__service_handle_message(msg)

    def _enable(self, enable):
        """ Can be implemented by subclass.
        Called if the service is signaled to enable/disable.
            :param enable: enable/disable
            :type enable: bool
        """
        pass

    def _setup(self):
        """ Can be implemented by subclass.
        Called when the service is first enabled or after
        settings are first set (if required=True).
        """
        pass

    def _on_shutdown(self):
        """ Can be implemented by subclass.
        Called if the service is signaled to shutdown.
        """
        pass

    def _handle_message(self, msg):
        """ Can be implemented by subclass.
        Called when a message of unknown type is received.
            :param msg: the received message
            :type msg: str
        """
        pass

    def _run_service(self):
        """ To be implemented by subclass.
        Called periodically by the process, with settings updated
        and enable flag checked before every run.
        """
        raise NotImplementedError("Please implement this method")


class SettingsStore(object):
    def __init__(self):
        self.__settings = {}
        self.__units = []

    def add_unit(self, keys, callback=None):
        self.__units.append((keys, callback))
        for key in keys:
            self.__settings[key] = None

    def get_setting(self, key):
        return self.__settings[key]

    def get_settings(self):
        return self.__settings

    def update(self, settings):
        for key, value in settings.iteritems():
            if key in self.__settings and self.__settings[key] != value:
                # Update the value.
                self.__settings[key] = value

                # Check units if callback is needed for the changed setting.
                for keys, callback in self.__units:
                    if key in keys and callback is not None:
                        callback()


class ServiceLauncher(object):
    """ Service launcher class.
    """

    @staticmethod
    def parse_args_and_execute(name, service):
        """ Parses arguments. """
        parser = ArgumentParser(description="The " + name + " service.")
        parser.add_argument('--port', type=int, required=True,
                            help="communication port")
        args = parser.parse_args()

        print("Service: Started: {}".format(name))
        service(args.port).run()
        print("Service: Terminated: {}".format(name))


class ServiceConnector(object):
    """ Service connector class.
    """

    __HOST_ADDRESS = "tcp://127.0.0.1"

    def __init__(self, spawn_monitor=False):
        # Setup the 0mq channel to the started service.
        self.__context = zmq.Context()
        self.__socket = self.__context.socket(zmq.PAIR)
        self.__port = self.__socket.bind_to_random_port(
            ServiceConnector.__HOST_ADDRESS)
        print "Manager: Connector bound to port " + str(self.__port)

        # Setup state access.
        self.__state_lock = Lock()
        self.__state_update_event = Event()
        self.__state = ServiceState(False, False)

        # Spawn a monitor thread.
        if spawn_monitor:
            thread = Thread(target=self.__monitor_state)
            thread.daemon = True
            thread.start()

    def __del__(self):
        self.__socket.close()
        self.__context.destroy()

    def __monitor_state(self):
        while True:
            msg = ServiceMessage.wait_for_message(self.__socket,
                                                  ServiceMessage.Type.STATE)
            self.__update_state(msg.data)

    def __update_state(self, data):
        with self.__state_lock:
            self.__state = ServiceState.from_data(data)
            self.__state_update_event.set()
            print "Manager: State received: {0}".format(self.__state)

    def wait_for_update(self, timeout=None):
        if self.__state_update_event.wait(timeout):
            self.__state_update_event.clear()
            return True
        else:
            return False

    def get_state(self):
        with self.__state_lock:
            return self.__state

    def get_port(self):
        """ Return the bound port. """
        return self.__port

    def shutdown(self):
        """ Signal the service to shutdown.
        Can be called from any process. Awaits confirmation from service.
        """
        ServiceMessage(ServiceMessage.Type.KILL).send(self.__socket)
        while not self.__state.is_shutting_down() and self.wait_for_update():
            pass  # TODO timeout unresponsive procs.

    def enable(self, enable):
        """ Signal the service to be enabled/disabled.
        Can be called from any process.
        - enable    : true to enable / false to disable
        """
        ServiceMessage(ServiceMessage.Type.ENABLE, enable).send(self.__socket)

    def update_settings(self, settings):
        """ Signal the service to update its settings.
        Can be called from any process.
        - settings  : the updated settings dictionary
        """
        ServiceMessage(ServiceMessage.Type.SETTINGS, settings).send(
            self.__socket)


class ServiceMessage(object):
    """ Service message class.
    """

    class Type(object):
        ENABLE = 0
        KILL = 1
        SETTINGS = 2
        STATE = 3

    def __init__(self, _type, data=None):
        self.type = _type
        self.data = data

    def __to_msg(self):
        return {'type': self.type, 'data': self.data}

    def send(self, zmq_socket):
        zmq_socket.send_json(self.__to_msg())

    @classmethod
    def from_message(cls, msg):
        return cls(msg['type'], msg['data'])

    @classmethod
    def wait_for_message(cls, zmq_socket, _type=None):
        while True:
            service_message = cls.from_message(zmq_socket.recv_json())
            # Return message if the _type isn't requested or matches.
            if _type is None or service_message.type == _type:
                return service_message

    @classmethod
    def check_for_message(cls, zmq_socket):
        try:
            return cls.from_message(zmq_socket.recv_json(zmq.NOBLOCK))
        except zmq.ZMQError:
            return None


class ServiceState(object):
    """ Service state class.
    """

    def __init__(self, enable, shutdown, value=None, msg=None):
        """ Constructor """
        self.__enable = enable
        self.__shutdown = shutdown
        self.__value = value
        self.__msg = msg

    def __eq__(self, other):
        if isinstance(other, ServiceState):
            return self.__enable == other.__enable and \
                   self.__shutdown == other.__shutdown and \
                   self.__value == other.__value and \
                   self.__msg == other.__msg
        return NotImplemented

    def __ne__(self, other):
        return not self == other

    def __str__(self):
        state_str = " State: Not set"
        if self.__value is not None:
            state_str = " State: value={0}".format(self.__value)
            if self.__msg is not None:
                state_str += " msg={0}".format(self.__msg)
        return "Service [enable={0} shutdown={1}{2}]".format(self.__enable,
                                                             self.__shutdown,
                                                             state_str)

    def is_enabled(self):
        """ Getter for service enable state. """
        return self.__enable

    def is_shutting_down(self):
        """ Getter for service shutdown state. """
        return self.__shutdown

    def get_value(self):
        """ Getter for service state value. """
        return self.__value

    def get_message(self):
        """ Getter for service state message. """
        return self.__msg

    def to_data(self):
        return {
            'service': {'enable': self.__enable, 'shutdown': self.__shutdown},
            'value': self.__value, 'msg': self.__msg}

    @classmethod
    def from_data(cls, data):
        return cls(data['service']['enable'], data['service']['shutdown'],
                   data['value'], data['msg'])


class DelayTimer(object):
    """ DelayTimer class.
    Provides a real time delay that depends on the time of the last delay.
    """

    def __init__(self, delay=0):
        """ Constructor
        - delay : delay between calls in seconds
        """
        self.__delay = delay
        self.__last_time = 0

    def set_delay(self, delay):
        """ Setter for the delay. """
        self.__delay = delay

    def start(self):
        """ Set the start of the execution of the caller process.
        """
        self.__last_time = time.clock()

    def delay(self):
        """ Delay the calling process for the time left of the delay.
        """
        delta = self.__delay - (time.clock() - self.__last_time)
        if delta > 0:
            time.sleep(delta)

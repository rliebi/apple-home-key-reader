import functools
import logging

import threading
import paho.mqtt.client as mqtt
from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_DOOR_LOCK

from service import Service

log = logging.getLogger()


# Lock class performs no logic, forwarding requests to Service class
class Lock(Accessory):
    category = CATEGORY_DOOR_LOCK

    def __init__(self, *args, service: Service, lock_state_at_startup=1, mqtt_client:dict=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_client_public_keys = None

        self._lock_target_state = lock_state_at_startup
        self._lock_current_state = 1

        self.service = service
        self.service.on_endpoint_authenticated = self.on_endpoint_authenticated
        self.add_lock_service()
        self.add_nfc_access_service()
        self.mqtt_settings = mqtt_client
        self.start_mqtt_listener()
        self.add_unpair_hook()

    def start_mqtt_listener(self):
        def on_connect(mqtt_client, userdata, flags, rc):

            log.info(f"Connected to MQTT broker with result code {rc}")
            mqtt_client.subscribe(self.mqtt_settings["topic"])

        def on_message(mqtt_client, userdata, msg):
            log.info(f"MQTT message received: {msg.topic} {msg.payload}")
            if self._lock_current_state == 0:
                return
            # Add logic to handle the message and trigger Shelly
            if msg.payload.decode() == "trigger" and self.service:  # Replace with actual condition
                self.service.trigger_webhook()

        client = mqtt.Client()
        client.username_pw_set(self.mqtt_settings["username"], self.mqtt_settings["password"])
        client.on_connect = on_connect
        client.on_message = on_message

        client.connect(self.mqtt_settings["host"], self.mqtt_settings["port"], 60)

        thread = threading.Thread(target=client.loop_forever, daemon=True)
        thread.start()
    def on_endpoint_authenticated(self, endpoint):
        # self._lock_target_state = 0
        log.info(
            f"Toggling lock state due to endpoint authentication event {self._lock_target_state} -> {self._lock_current_state} {endpoint}"
        )
        # self.lock_target_state.set_value(self._lock_target_state, should_notify=True)
        # self._lock_current_state = self._lock_target_state
        # self.lock_current_state.set_value(self._lock_current_state, should_notify=True)
        if self.service:
            self.service.trigger_webhook()

    def add_unpair_hook(self):
        unpair = self.driver.unpair

        @functools.wraps(unpair)
        def patched_unpair(client_uuid):
            unpair(client_uuid)
            self.on_unpair(client_uuid)

        self.driver.unpair = patched_unpair

    def add_preload_service(self, service, chars=None, unique_id=None):
        """Create a service with the given name and add it to this acc."""
        if isinstance(service, str):
            service = self.driver.loader.get_service(service)
        if unique_id is not None:
            service.unique_id = unique_id
        if chars:
            chars = chars if isinstance(chars, list) else [chars]
            for char_name in chars:
                if isinstance(char_name, str):
                    char = self.driver.loader.get_char(char_name)
                    service.add_characteristic(char)
                else:
                    service.add_characteristic(char_name)
        self.add_service(service)
        return service

    def add_info_service(self):
        serv_info = self.driver.loader.get_service("AccessoryInformation")
        serv_info.configure_char("Name", value=self.display_name)
        serv_info.configure_char("SerialNumber", value="default")
        serv_info.add_characteristic(self.driver.loader.get_char("HardwareFinish"))
        serv_info.configure_char(
            "HardwareFinish", getter_callback=self.get_hardware_finish
        )
        self.add_service(serv_info)

    def add_lock_service(self):
        self.service_lock_mechanism = self.add_preload_service("LockMechanism")

        self.lock_current_state = self.service_lock_mechanism.configure_char(
            "LockCurrentState", getter_callback=self.get_lock_current_state, value=0
        )

        self.lock_target_state = self.service_lock_mechanism.configure_char(
            "LockTargetState",
            getter_callback=self.get_lock_target_state,
            setter_callback=self.set_lock_target_state,
            value=0,
        )

        self.service_lock_management = self.add_preload_service("LockManagement")

        self.lock_control_point = self.service_lock_management.configure_char(
            "LockControlPoint",
            setter_callback=self.set_lock_control_point,
        )

        self.lock_version = self.service_lock_management.configure_char(
            "Version",
            getter_callback=self.get_lock_version,
        )

    def add_nfc_access_service(self):
        self.service_nfc = self.add_preload_service("NFCAccess")

        self.char_nfc_access_supported_configuration = self.service_nfc.configure_char(
            "NFCAccessSupportedConfiguration",
            getter_callback=self.get_nfc_access_supported_configuration,
        )

        self.char_nfc_access_control_point = self.service_nfc.configure_char(
            "NFCAccessControlPoint",
            getter_callback=self.get_nfc_access_control_point,
            setter_callback=self.set_nfc_access_control_point,
        )

        self.configuration_state = self.service_nfc.configure_char(
            "ConfigurationState", getter_callback=self.get_configuration_state
        )

    def _update_hap_pairings(self):
        client_public_keys = set(self.clients.values())
        if self._last_client_public_keys == client_public_keys:
            return
        self._last_client_public_keys = client_public_keys
        self.service.update_hap_pairings(client_public_keys)

    def get_lock_current_state(self):
        log.info(f"get_lock_current_state {self._lock_current_state}")
        if self.service.is_door_closed() is not None:
            return self.service.is_door_closed()
        return self._lock_current_state

    def get_lock_target_state(self):
        log.info(f"get_lock_target_state {self._lock_target_state}")
        return self._lock_target_state

    def set_lock_target_state(self, value):
        # value = 1 if (self.service.is_door_closed()) else 0
        log.info(f"set_lock_target_state {value}")
        self._lock_target_state = self._lock_current_state = value
        self.lock_current_state.set_value(self._lock_current_state, should_notify=True)
        return self._lock_target_state

    def get_lock_version(self):
        log.info("get_lock_version")
        return "1.0.0"

    def set_lock_control_point(self, value):
        log.info(f"set_lock_control_point: {value}")

    # All methods down here are forwarded to Service
    def get_hardware_finish(self):
        self._update_hap_pairings()
        log.info("get_hardware_finish")
        return self.service.get_hardware_finish()

    def get_nfc_access_supported_configuration(self):
        self._update_hap_pairings()
        log.info("get_nfc_access_supported_configuration")
        return self.service.get_nfc_access_supported_configuration()

    def get_nfc_access_control_point(self):
        self._update_hap_pairings()
        log.info("get_nfc_access_control_point")
        return self.service.get_nfc_access_control_point()

    def set_nfc_access_control_point(self, value):
        self._update_hap_pairings()
        log.info(f"set_nfc_access_control_point {value}")
        return self.service.set_nfc_access_control_point(value)

    def get_configuration_state(self):
        self._update_hap_pairings()
        log.info("get_configuration_state")
        return self.service.get_configuration_state()

    @property
    def clients(self):
        return self.driver.state.paired_clients

    def on_unpair(self, client_id):
        log.info(f"on_unpair {client_id}")
        self._update_hap_pairings()

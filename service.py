import base64
import logging
import time
import os
from operator import attrgetter
from requests.auth import HTTPBasicAuth
import requests
from entity import (
    Issuer,
    Operation,
    ReaderKeyResponse,
    ReaderKeyRequest,
    HardwareFinishResponse,
    HardwareFinishColor,
    DeviceCredentialRequest,
    DeviceCredentialResponse,
    Endpoint,
    Enrollments,
    Enrollment,
    OperationStatus,
    SupportedConfigurationResponse,
    ControlPointRequest,
    ControlPointResponse,
)
from homekey import read_homekey, ProtocolError
from repository import Repository
from util.bfclf import (
    BroadcastFrameContactlessFrontend,
    RemoteTarget,
    activate,
    ISODEPTag,
)
from util.digital_key import DigitalKeyFlow, DigitalKeyTransactionType
from util.ecp import ECP
from util.iso7816 import ISO7816Tag
from util.threads import create_runner
from util.structable import pack_into_base64_string, unpack_from_base64_string

log = logging.getLogger()


class Service:
    def __init__(
        self,
        clf: BroadcastFrameContactlessFrontend,
        repository: Repository,
        express: bool = True,
        finish: str = "silver",
        flow: str = "fast",
        webhook_config=None,
        door_status_config=None,
        throttle_polling = 0.1
    ) -> None:
        self.repository = repository
        self.clf = clf
        self.throttle_polling = throttle_polling
        self.express = express in (True, "True", "true", "1")
        self.webhook_config = webhook_config
        self.door_status_config = door_status_config
        try:
            self.hardware_finish_color = HardwareFinishColor[finish.upper()]
        except KeyError:
            self.hardware_finish_color = HardwareFinishColor.BLACK
            log.warning(
                f"HardwareFinish {finish} is not supported. Falling back to {self.hardware_finish_color}"
            )
        try:
            self.flow = DigitalKeyFlow[flow.upper()]
        except KeyError:
            self.flow = DigitalKeyFlow.FAST
            log.warning(
                f"Digital Key flow {flow} is not supported. Falling back to {self.flow}"
            )

        self._run_flag = True
        self._runner = None

    def on_endpoint_authenticated(self, endpoint):
        """This method will be called when an endpoint is authenticated"""
        # Currently overwritten by accessory.py

    def fetch_door_status(self):
        if not self.door_status_config:
            log.warning("Door status URL not configured")
            return None

        url = self.door_status_config["url"]
        json_path = self.door_status_config.get("path", [])
        closed_value = self.door_status_config.get("closed_value", 1)
        auth_config = self.door_status_config.get("auth", {})

        headers = {}
        auth = None
        if auth_config.get("type") == "Bearer":
            headers["Authorization"] = f"Bearer {auth_config['token']}"
        elif auth_config.get("type") == "Basic":
            auth = HTTPBasicAuth(auth_config["basic_username"], auth_config["basic_password"])

        try:
            response = requests.get(url, headers=headers, auth=auth)
            response.raise_for_status()
            status = response.json()
            log.info(f"Fetched door status: {status}")

            # Traverse the JSON path to get the door status
            for key in json_path:
                if isinstance(status, list):
                    try:
                        key = int(key)
                        status = status[key]
                    except (ValueError, IndexError) as e:
                        log.error(f"Error accessing list at path {json_path} with key {key}: {e}")
                        return None
                elif isinstance(status, dict):
                    status = status.get(key)
                    if status is None:
                        log.error(f"Invalid path in door status JSON: {json_path}")
                        return None
                else:
                    log.error(f"Unexpected data type at path {json_path} with key {key}")
                    return None

            log.info(f"Door status at path {json_path}: {status}")
            return status == closed_value
        except requests.RequestException as e:
            log.error(f"Failed to fetch door status: {e}")
            return None

    def is_door_closed(self):
        door_status = self.fetch_door_status()
        return door_status

    def trigger_webhook(self, data=None):
        if data is None:
            data = {}
        if not self.webhook_config:
            log.warning(f"Webhook not configured")
            return

        url = self.webhook_config["url"]
        method = self.webhook_config.get("method", "POST")
        auth_config = self.webhook_config.get("auth", {})

        headers = {}
        auth = None
        if auth_config.get("type") == "Bearer":
            headers["Authorization"] = f"Bearer {auth_config['token']}"
        elif auth_config.get("type") == "Basic":
            auth = HTTPBasicAuth(auth_config["basic_username"], auth_config["basic_password"])

        try:
            if method == "POST":
                response = requests.post(url, json=data, headers=headers, auth=auth)
            else:
                response = requests.get(url, params=data, headers=headers, auth=auth)

            response.raise_for_status()
        except requests.RequestException as e:
            print(f"Webhook trigger failed: {e}")
            log.warning(
               f"Webhook trigger failed: {e}"
            )

    def start(self):
        self._runner = create_runner(
            name="homekey",
            target=self.run,
            flag=attrgetter("_run_flag"),
            delay=0,
            exception_delay=5,
            start=True,
        )

    def stop(self):
        self._run_flag = False
        if self._runner is not None:
            self._runner.join()

    def update_hap_pairings(self, issuer_public_keys):
        issuers = {
            issuer.public_key: issuer for issuer in self.repository.get_all_issuers()
        }
        for issuer in issuers.values():
            if issuer.public_key in issuer_public_keys:
                continue
            log.info(f"Removing issuer {issuer} as their pairing has been removed")
            self.repository.remove_issuer(issuer)

        for issuer_public_key in issuer_public_keys:
            if issuer_public_key in issuers:
                continue
            issuer = Issuer(public_key=issuer_public_key, endpoints=[])
            log.info(f"Adding issuer {issuer} based on paired clients")
            self.repository.upsert_issuer(issuer)

    def _read_homekey(self):
        start = time.monotonic()

        remote_target = self.clf.sense(
            RemoteTarget("106A"),
            broadcast=ECP.home(
                identifier=self.repository.get_reader_group_identifier(),
                flag_2=self.express,
            ).pack(),
        )

        if remote_target is None:
            # Throttle polling attempts to prevent overheating & RF performance degradation
            time.sleep(max(0, self.throttle_polling - time.monotonic() + start))
            return

        target = activate(self.clf, remote_target)
        if target is None:
            return

        if not isinstance(target, ISODEPTag):
            log.info(
                f"Found non-ISODEP Tag with UID: {target.identifier.hex().upper()}"
            )
            while self.clf.sense(RemoteTarget("106A")) is not None:
                log.info("Waiting for target to leave the field...")
                time.sleep(0.5)
            return

        log.info(f"Got NFC tag {target}")

        tag = ISO7816Tag(target)
        try:
            result_flow, new_issuers_state, endpoint = read_homekey(
                tag,
                issuers=self.repository.get_all_issuers(),
                preferred_versions=[b"\x02\x00"],
                flow=self.flow,
                transaction_code=DigitalKeyTransactionType.UNLOCK,
                reader_identifier=self.repository.get_reader_group_identifier()
                + self.repository.get_reader_identifier(),
                reader_private_key=self.repository.get_reader_private_key(),
                key_size=16,
            )

            if new_issuers_state is not None and len(new_issuers_state):
                self.repository.upsert_issuers(new_issuers_state)

            log.info(f"Authenticated endpoint via {result_flow!r}: {endpoint}")

            end = time.monotonic()
            log.info(f"Transaction took {(end - start) * 1000} ms")

            if endpoint is not None:
                self.on_endpoint_authenticated(endpoint)
        except ProtocolError as e:
            log.info(f'Could not authenticate device due to protocol error "{e}"')

        # Let device cool down, wait for ISODEP to drop to consider comms finished
        while target.is_present:
            log.info("Waiting for device to leave the field...")
            time.sleep(0.5)
        log.info("Device left the field. Continuing in 2 seconds...")
        time.sleep(2)
        log.info("Waiting for next device...")

    def run(self):
        if (self.clf is None) or (self.repository is None):
            return
        if self.repository.get_reader_private_key() in (None, b""):
            raise Exception("Device is not configured via HAP. NFC inactive")

        log.info("Connecting to the NFC reader...")

        self.clf.device = None
        self.clf.open(self.clf.path)
        if self.clf.device is None:
            raise Exception(
                f"Could not connect to NFC device {self.clf} at {self.clf.path}"
            )

        while self._run_flag:
            self._read_homekey()

    def get_reader_key(self, request: ReaderKeyRequest) -> ReaderKeyResponse:
        response = ReaderKeyResponse(
            key_identifier=self.repository.get_reader_group_identifier(),
        )
        return response

    def add_reader_key(self, request: ReaderKeyRequest) -> ReaderKeyResponse:
        changed = False
        if self.repository.get_reader_private_key() != request.reader_private_key:
            changed = True
            self.repository.set_reader_private_key(request.reader_private_key)
        if self.repository.get_reader_identifier() != request.unique_reader_identifier:
            changed = True
            self.repository.set_reader_identifier(request.unique_reader_identifier)
        response = ReaderKeyResponse(
            status=OperationStatus.SUCCESS if changed else OperationStatus.DUPLICATE
        )
        return response

    def remove_reader_key(self, request: ReaderKeyRequest) -> ReaderKeyResponse:
        if request.key_identifier == self.repository.get_reader_group_identifier():
            self.repository.set_reader_private_key(bytes.fromhex("00" * 32))
        response = ReaderKeyResponse(
            status=OperationStatus.SUCCESS
            if request.key_identifier == self.repository.get_reader_group_identifier()
            else OperationStatus.DOES_NOT_EXIST
        )
        return response

    def get_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        log.info(f"*** get_device_credential request={request}")

    def add_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        endpoint = self.repository.get_endpoint_by_public_key(
            b"\x04" + request.credential_public_key
        )
        log.info(f"*** add_device_credential endpoint={endpoint}")

        if endpoint is not None:
            if endpoint.enrollments.hap is None:
                issuer = self.repository.get_issuer_by_id(request.issuer_key_identifier)
                endpoint.enrollments.hap = Enrollment(
                    at=int(time.time()),
                    payload=base64.b64encode(request.pack()).decode(),
                )
                self.repository.upsert_endpoint(issuer.id, endpoint)
            return DeviceCredentialResponse(
                key_identifier=self.repository.get_reader_group_identifier(),
                status=OperationStatus.DUPLICATE,
            )

        issuer = self.repository.get_issuer_by_id(request.issuer_key_identifier)
        log.info(f"*** add_device_credential issuer={issuer}")

        if issuer is None:
            return DeviceCredentialResponse(
                key_identifier=self.repository.get_reader_group_identifier(),
                status=OperationStatus.DOES_NOT_EXIST,
            )

        self.repository.upsert_endpoint(
            issuer.id,
            Endpoint(
                last_used_at=0,
                counter=0,
                key_type=request.key_type,
                public_key=b"\x04" + request.credential_public_key,
                persistent_key=os.urandom(32),
                enrollments=Enrollments(
                    hap=Enrollment(
                        at=int(time.time()),
                        payload=base64.b64encode(request.pack()).decode(),
                    ),
                    attestation=None,
                ),
            ),
        )
        return DeviceCredentialResponse(
            issuer_key_identifier=issuer.id, status=OperationStatus.DUPLICATE
        )

    def remove_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        log.info(f"*** remove_device_credential request={request}")

    def get_hardware_finish(self):
        result = pack_into_base64_string(
            HardwareFinishResponse(color=self.hardware_finish_color)
        )
        log.info(f"get_hardware_finish={result}")
        return result

    def get_nfc_access_supported_configuration(self):
        result = pack_into_base64_string(
            SupportedConfigurationResponse(
                number_of_issuer_keys=16, number_of_inactive_credentials=16
            )
        )
        log.info(f"TODO get_nfc_access_supported_configuration={result}")
        return result

    def get_nfc_access_control_point(self):
        log.info("get_nfc_access_control_point")
        return ""

    def set_nfc_access_control_point(self, value):
        log.info(f"<-- (B64) {value}")
        request_packed_tlv = unpack_from_base64_string(value)
        log.info(f"<-- (TLV) {request_packed_tlv.hex()}")
        request: ControlPointRequest = ControlPointRequest.unpack(request_packed_tlv)
        log.info(f"<-- (OBJ) {request}")
        operation = request.operation
        response = ControlPointResponse()

        if request.device_credential_request is not None:
            subrequest: DeviceCredentialRequest = request.device_credential_request
            response.device_credential_response = (
                self.get_device_credential(subrequest)
                if operation == Operation.GET
                else self.add_device_credential(subrequest)
                if operation == Operation.ADD
                else self.remove_device_credential(subrequest)
                if operation == Operation.REMOVE
                else None
            )
        elif request.reader_key_request is not None:
            subrequest: ReaderKeyRequest = request.reader_key_request
            response.reader_key_response = (
                self.get_reader_key(subrequest)
                if operation == Operation.GET
                else self.add_reader_key(subrequest)
                if operation == Operation.ADD
                else self.remove_reader_key(subrequest)
                if operation == Operation.REMOVE
                else None
            )
        log.info(f"--> (OBJ) {response}")
        packed_tlv_response = response.pack()
        log.info(f"--> (TLV) {packed_tlv_response.hex()}")
        response = pack_into_base64_string(packed_tlv_response)
        log.info(f"--> (B64) {response}")
        return response

    def get_configuration_state(self):
        log.info("get_configuration_state")
        return 0

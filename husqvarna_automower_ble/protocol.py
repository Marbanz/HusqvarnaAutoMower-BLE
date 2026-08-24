import binascii
from .helpers import crc
from enum import IntEnum
import asyncio
import logging
import json
from importlib.resources import files
from bleak.exc import BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bleak import BleakClient

logger = logging.getLogger(__name__)

WRITE_CHAR = "98bd0002-0b0e-421a-84e5-ddbf75dc6de4"
READ_CHAR = "98bd0003-0b0e-421a-84e5-ddbf75dc6de4"
PROTOCOL_DESCRIPTOR_CHAR = "98bd0004-0b0e-421a-84e5-ddbf75dc6de4"
GATT_AUTH_ERROR_TEXT = (
    "Insufficient authentication",
    "Insufficient authorization",
    "Insufficient encryption",
)


class ModeOfOperation(IntEnum):
    # ProtocolTypes$IMowerAppMowerMode, used in modeOfOperation: 4586, 1
    # Comments from: https://developer.husqvarnagroup.cloud/apis/Automower+Connect+API?tab=status%20description%20and%20error%20codes#user-content-mode
    AUTO = 0
    MANUAL = 1
    HOME = 2  # Mower goes home and parks forever. Week schedule is not used. Cannot be overridden with forced mowing.
    DEMO = 3  # Same as main area, but shorter times. No blade operation
    POI = 4


class MowerState(IntEnum):
    # ProtocolTypes$IMowerAppState, used in mowerState: 4586, 2
    # Comments from: https://developer.husqvarnagroup.cloud/apis/Automower+Connect+API?tab=status%20description%20and%20error%20codes#user-content-state
    OFF = 0  # Mower is turned off.
    WAIT_FOR_SAFETYPIN = 1
    STOPPED = 2  # Mower is stopped requires manual action.
    FATAL_ERROR = 3
    PENDING_START = 4
    PAUSED = 5  # Mower has been paused by user.
    IN_OPERATION = 6  # See value in activity for status.
    RESTRICTED = (
        7  # Mower can currently not mow due to week calender, or override park.
    )
    ERROR = 8  # An error has occurred. Check errorCode. Mower requires manual action.


class MowerActivity(IntEnum):
    # ProtocolTypes$IMowerAppActivity, used in mowerActivity: 4586, 3
    # Comments from: https://developer.husqvarnagroup.cloud/apis/Automower+Connect+API?tab=status%20description%20and%20error%20codes#user-content-activity
    NONE = 0
    CHARGING = 1  # Mower is charging in station due to low battery.
    GOING_OUT = 2
    MOWING = 3  # Mower is mowing lawn. If in demo mode the blades are not in operation.
    GOING_HOME = 4  # Mower is going home to the charging station.
    PARKED = 5
    STOPPED_IN_GARDEN = 6  # Mower has stopped. Needs manual action to resume


class OverrideAction(IntEnum):
    NONE = 0
    FORCEDPARK = 1
    FORCEDMOW = 2


class ResponseResult(IntEnum):
    OK = 0
    UNKNOWN_ERROR = 1
    INVALID_VALUE = 2
    OUT_OF_RANGE = 3
    NOT_AVAILABLE = 4
    NOT_ALLOWED = 5
    INVALID_GROUP = 6
    INVALID_ID = 7
    DEVICE_BUSY = 8
    INVALID_PIN = 9
    MOWER_BLOCKED = 10


def _response_result_label(value: int) -> str:
    try:
        result = ResponseResult(value)
    except ValueError:
        return f"UNKNOWN_RESULT({value})"
    return f"{result.name}({value})"


def _is_gatt_auth_error(err: Exception) -> bool:
    return any(text in str(err) for text in GATT_AUTH_ERROR_TEXT)


class TaskInformation:
    def __init__(
        self,
        start_time_in_minutes,
        duration_in_minutes,
        on_monday,
        on_tuesday,
        on_wednesday,
        on_thursday,
        on_friday,
        on_saturday,
        on_sunday,
    ):
        self.start_time_in_minutes = start_time_in_minutes
        self.duration_in_minutes = duration_in_minutes
        self.on_monday = on_monday
        self.on_tuesday = on_tuesday
        self.on_wednesday = on_wednesday
        self.on_thursday = on_thursday
        self.on_friday = on_friday
        self.on_saturday = on_saturday
        self.on_sunday = on_sunday


class Command:
    def __init__(self, channel_id: int, parameter: dict):
        self.channel_id = channel_id

        self.major = parameter["major"]
        self.minor = parameter["minor"]

        self.request_data_type = parameter.get("requestType")

        if "responseType" not in parameter:
            parameter["responseType"] = "no_response"

        if not isinstance(parameter["responseType"], dict):  # Always wrap in list
            self.response_data_type = {"response": parameter["responseType"]}
        else:
            self.response_data_type = parameter["responseType"]
        self.request_data = bytearray()

    def generate_request(self, **kwargs) -> bytearray:
        self.request_data = bytearray(18)
        self.request_data[0] = 0x02  # Hard coded value (start of packet)
        self.request_data[1] = 0xFD  # 0xFD = LINKED_PACKET_TYPE
        self.request_data[2] = 0x00  # Length, low byte, updated later
        self.request_data[3] = 0x00  # Length, high byte, updated later

        # ChannelID
        self.request_data[4:8] = self.channel_id.to_bytes(4, byteorder="little")

        self.request_data[8] = 0x01  # is_linked (usually 0x01)

        self.request_data[9] = 0x00  # CRC, Updated later
        self.request_data[10] = (
            0x00  # Packet type (0x00 = request, 0x01 = response, 0x02 = event)
        )
        self.request_data[11] = 0xAF  # Hard coded value

        major_bytes = self.major.to_bytes(2, byteorder="little")

        self.request_data[12] = major_bytes[0]  # low byte of 'module'
        self.request_data[13] = major_bytes[1]  # high byte of 'module'
        self.request_data[14] = self.minor  # low byte of 'command'
        self.request_data[15] = 0x00  # high byte of 'command'

        # Byte 16 represents length of request data type
        request_length = 0
        request_data = bytearray()
        if self.request_data_type is not None:
            for request_name, request_type in self.request_data_type.items():
                if request_name not in kwargs:
                    raise ValueError(
                        "Missing request parameter: "
                        + request_name
                        + " for command ("
                        + str(self.major)
                        + ", "
                        + str(self.minor)
                        + ")"
                    )

                if request_type == "uint32":
                    request_length += 4
                    request_data += kwargs[request_name].to_bytes(4, byteorder="little")
                elif request_type == "uint16":
                    request_length += 2
                    request_data += kwargs[request_name].to_bytes(2, byteorder="little")
                elif request_type == "uint8":
                    request_length += 1
                    request_data += kwargs[request_name].to_bytes(1, byteorder="little")
                elif request_type == "bool":
                    request_length += 1
                    request_data += (1 if kwargs[request_name] else 0).to_bytes(
                        1, byteorder="little"
                    )
                else:
                    raise ValueError("Unknown request type: " + request_type)
        self.request_data[16] = request_length

        self.request_data[17] = 0x00  # high byte of request_length
        if request_length > 0:
            self.request_data += request_data

        self.request_data[2] = len(self.request_data) - 2  # Length

        self.request_data[9] = crc(self.request_data, 1, 8)  # CRC

        # Two last bytes are crc and 0x03
        self.request_data.append(crc(self.request_data, 1, len(self.request_data) - 1))
        self.request_data.append(0x03)  # Hard coded value

        return self.request_data

    def parse_response(self, response_data: bytearray) -> dict[str, int | str] | None:
        response_length = response_data[17]
        data = response_data[19 : 19 + response_length]
        response: dict[str, int | str] = {}
        dpos = 0  # data position
        for name, dtype in self.response_data_type.items():
            if dtype == "no_response":
                return None
            if (dtype == "tUnixTime") or (dtype == "uint32"):
                response[name] = int.from_bytes(
                    data[dpos : dpos + 4], byteorder="little"
                )
                dpos += 4
            elif dtype == "uint16":
                response[name] = int.from_bytes(
                    data[dpos : dpos + 2], byteorder="little"
                )
                dpos += 2
            elif dtype == "sint16":
                response[name] = int.from_bytes(
                    data[dpos : dpos + 2], byteorder="little", signed=True
                )
                dpos += 2
            elif dtype == "remaining_uint":
                response[name] = int.from_bytes(data[dpos:], byteorder="little")
                dpos = len(data)
            elif (dtype == "uint8") or (dtype == "bool"):
                response[name] = data[dpos]
                dpos += 1
            elif dtype == "ascii":
                if len(self.response_data_type) != 1:
                    raise ValueError(
                        "ASCII response type can currently only be used when there is only one response type"
                    )
                response[name] = data.decode("ascii").rstrip(
                    "\x00"
                )  # Remove trailing null bytes
                dpos += len(data)
            elif dtype == "utf16":
                if len(self.response_data_type) != 1:
                    raise ValueError(
                        "UTF-16 response type can currently only be used when there is only one response type"
                    )
                try:
                    response[name] = data.decode("utf-16-le").rstrip("\x00")
                except UnicodeDecodeError as err:
                    raise ValueError("Unable to decode UTF-16 response") from err
                dpos += len(data)
            else:
                raise ValueError("Unknown data type: " + dtype)
        if dpos != len(data):
            raise ValueError(f"Data length mismatch. Read {dpos} bytes of {len(data)}")
        return response

    def validate_command_response(self, response_data: bytearray) -> bool:
        if response_data[0] != 0x02:
            return False

        if response_data[1] != 0xFD:
            return False

        if response_data[3] != 0x00:  # high byte of length
            return False

        if response_data[4:8] != self.channel_id.to_bytes(4, byteorder="little"):
            return False

        if response_data[8] != 0x01:
            # This is a valid config, but we don't support it
            # return m1656b(decodeState, c10786f);
            return False

        if response_data[9] != crc(response_data, 1, 8):
            return False

        if response_data[10] != 0x01:  # packet type is not 0x01 = response
            return False

        if response_data[11] != 0xAF:
            return False

        major_bytes = self.major.to_bytes(4, byteorder="little")
        if response_data[12] != major_bytes[0]:
            return False
        if response_data[13] != major_bytes[1]:
            return False
        if response_data[14] != self.minor:
            return False

        if response_data[15] != 0x00:  # high byte of 'command' (self.minor)
            return False

        if (
            response_data[16] != 0x00
        ):  # result: OK(0), UNKNOWN_ERROR(1), INVALID_VALUE(2), OUT_OF_RANGE(3), NOT_AVAILABLE(4), NOT_ALLOWED(5), INVALID_GROUP(6), INVALID_ID(7), DEVICE_BUSY(8), INVALID_PIN(9), MOWER_BLOCKED(10);
            logger.warning(
                "Command %d/%d returned %s",
                self.major,
                self.minor,
                _response_result_label(response_data[16]),
            )

        return True


class BLEClient:
    def __init__(self, channel_id: int, address, pin=None):
        self.channel_id = channel_id
        self.address = address
        self.pin = pin
        self.MTU_SIZE = 20

        self.lock = asyncio.Lock()
        self.queue: asyncio.Queue[bytearray | None] = asyncio.Queue()

        self.client: BleakClient | None = None
        self.protocol = None
        self.write_char: BleakGATTCharacteristic | None = None
        self.read_char: BleakGATTCharacteristic | None = None
        self._notify_started = False

    async def get_protocol(self):
        if self.protocol is None:

            def read_protocol_file():
                protocol_file = files(__package__).joinpath("protocol.json")
                logger.debug("Loading protocol from %s", protocol_file)
                with protocol_file.open("r") as f:
                    return json.load(f)

            self.protocol = await asyncio.get_running_loop().run_in_executor(
                None, read_protocol_file
            )
        return self.protocol

    async def _get_response(self) -> bytearray | None:
        try:
            data = await asyncio.wait_for(self.queue.get(), timeout=5)

        except TimeoutError:
            logger.warning("Unable to get response from device: '%s'", self.address)
            return None

        return data

    async def _get_response_silent(self) -> bytearray | None:
        """Get response with debug-level logging (for retry scenarios)"""
        try:
            data = await asyncio.wait_for(self.queue.get(), timeout=5)

        except TimeoutError:
            logger.debug(
                "Unable to get response from device (retry attempt): '%s'", self.address
            )
            return None

        return data

    async def _write_data(self, data):
        logger.debug("Writing: %s", str(binascii.hexlify(data)))

        if self.client is None or self.write_char is None:
            raise RuntimeError("BLE client is not connected")

        chunk_size = self.MTU_SIZE - 3
        for chunk in (
            data[i : i + chunk_size] for i in range(0, len(data), chunk_size)
        ):
            await self.client.write_gatt_char(self.write_char, chunk, response=False)

        logger.debug("Finished writing")

    async def _read_data(self):
        data = await self._get_response()

        if data is None:
            return None

        while data and data[0] != 0x02:
            packet_start = data.find(b"\x02")
            if packet_start >= 0:
                logger.debug(
                    "Discarding stale response prefix: %s",
                    binascii.hexlify(data[:packet_start]),
                )
                data = data[packet_start:]
                break

            logger.debug(
                "Discarding stale response fragment: %s", binascii.hexlify(data)
            )
            data = await self._get_response()
            if data is None:
                return None

        while len(data) < 3:
            # We got such a small amount of data, let's try again.
            chunk = await self._get_response()
            if chunk is None:
                return None
            data += chunk

        length = data[2] + 4

        logger.debug("Waiting for %d bytes", length)

        while len(data) < length:
            try:
                chunk = await asyncio.wait_for(self.queue.get(), timeout=5)
                if chunk is None:
                    return None
                data += chunk
            except TimeoutError:
                logger.error(
                    "Unable to get full response from device '%s', currently have %s",
                    self.address,
                    str(binascii.hexlify(data)),
                )
                logger.error("Expecting %d bytes, only have %d", length, len(data))
                return None

        logger.debug("Final response: %s", str(binascii.hexlify(data)))

        return data

    async def _read_data_silent(self):
        """Read data with debug-level logging (for retry scenarios)"""
        data = await self._get_response_silent()

        if data is None:
            return None

        while data and data[0] != 0x02:
            packet_start = data.find(b"\x02")
            if packet_start >= 0:
                logger.debug(
                    "Discarding stale response prefix: %s",
                    binascii.hexlify(data[:packet_start]),
                )
                data = data[packet_start:]
                break

            logger.debug(
                "Discarding stale response fragment: %s", binascii.hexlify(data)
            )
            data = await self._get_response_silent()
            if data is None:
                return None

        while len(data) < 3:
            # We got such a small amount of data, let's try again.
            chunk = await self._get_response_silent()
            if chunk is None:
                return None
            data += chunk

        length = data[2] + 4

        logger.debug("Waiting for %d bytes", length)

        while len(data) < length:
            try:
                chunk = await asyncio.wait_for(self.queue.get(), timeout=5)
                if chunk is None:
                    return None
                data += chunk
            except TimeoutError:
                logger.debug(
                    "Unable to get full response from device (retry attempt): '%s', currently have %s",
                    self.address,
                    str(binascii.hexlify(data)),
                )
                logger.debug("Expecting %d bytes, only have %d", length, len(data))
                return None

        logger.debug("Final response: %s", str(binascii.hexlify(data)))

        return data

    async def _request_response(self, request_data):
        async with self.lock:
            try:
                # If there are previous responses, flush them out
                while not self.queue.empty():
                    await self.queue.get()

                await self._write_data(request_data)

                response_data = await self._read_data()
                if response_data is None:
                    logger.warning(
                        "Unable to communicate with device: '%s'", self.address
                    )
                    if self.is_connected():
                        await self.disconnect()
                    return None

            except asyncio.exceptions.CancelledError:
                logger.debug("Received CancelledError")
                if self.is_connected():
                    await self.disconnect()
                return None
            except BleakError as err:
                logger.warning("BLE communication failed: %s", err)
                if self.is_connected():
                    await self.disconnect()
                raise

        return response_data

    async def _request_response_with_retry(self, request_data):
        """
        Send request with automatic retry (up to 3 times).
        Does not log errors on failed attempts until the final attempt.
        Useful for handling device deep sleep scenarios.
        """
        max_attempts = 3

        async with self.lock:
            for attempt in range(max_attempts):
                try:
                    logger.debug("Retry attempt %d/%d", attempt + 1, max_attempts)

                    # If there are previous responses, flush them out
                    while not self.queue.empty():
                        await self.queue.get()

                    await self._write_data(request_data)

                    # Use silent read on retry attempts, normal read on last attempt
                    if attempt < max_attempts - 1:
                        response_data = await self._read_data_silent()
                    else:
                        response_data = await self._read_data()

                    if response_data is not None:
                        logger.debug("Got response on attempt %d", attempt + 1)
                        return response_data

                    logger.debug("No response on attempt %d", attempt + 1)

                except asyncio.exceptions.CancelledError:
                    logger.debug("Received CancelledError")
                    if self.is_connected():
                        await self.disconnect()
                    return None
                except BleakError as err:
                    logger.warning("BLE communication failed: %s", err)
                    if self.is_connected():
                        await self.disconnect()
                    raise

                # Wait before retrying (except on last attempt)
                if attempt < max_attempts - 1:
                    logger.debug("Waiting 1 seconds before retry...")
                    await asyncio.sleep(1)

        logger.warning("Unable to communicate with device: '%s'", self.address)
        if self.is_connected():
            await self.disconnect()
        return None

    async def connect(self, device) -> ResponseResult:
        """
        Connect to a device and setup the channel

        Returns a ResponseResult
        """
        if self.is_connected():
            logger.debug("Already connected")
            return ResponseResult.OK

        logger.info("starting scan...")

        if device is None:
            logger.warning("Could not find device with address '%s'", self.address)
            return ResponseResult.UNKNOWN_ERROR

        self.write_char = None
        self.read_char = None
        self._notify_started = False

        logger.info("connecting to device...")
        self.client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or "Unknown Device",
        )
        logger.info("connected")

        logger.info("pairing device...")
        try:
            await self.client.pair()
            logger.info("paired")
        except BleakError as err:
            logger.info("Pairing failed, continuing with protocol handshake: %s", err)
            if not self.client.is_connected:
                return ResponseResult.UNKNOWN_ERROR

        # This is not safe, _mtu_size is not defined in BaseBleakClient but may
        # be defined in subclasses.
        self.client._backend._mtu_size = self.MTU_SIZE  # type: ignore[attr-defined]

        for service in self.client.services:
            logger.debug("[Service] %s", service)

            for char in service.characteristics:
                if char.uuid == WRITE_CHAR:
                    self.write_char = char

                if char.uuid == READ_CHAR:
                    self.read_char = char

                if char.uuid in (WRITE_CHAR, READ_CHAR):
                    logger.debug(
                        "  [Characteristic] %s (%s)",
                        char,
                        ",".join(char.properties),
                    )
                    continue

                if "read" in char.properties:
                    try:
                        value = await self.client.read_gatt_char(char.uuid)
                        logger.debug(
                            "  [Characteristic] %s (%s), Value: %r",
                            char,
                            ",".join(char.properties),
                            value,
                        )
                    except Exception as e:
                        logger.debug(
                            "  [Characteristic] %s (%s), Error: %s",
                            char,
                            ",".join(char.properties),
                            e,
                        )
                else:
                    logger.debug(
                        "  [Characteristic] %s (%s)", char, ",".join(char.properties)
                    )

        async def notification_handler(
            characteristic: BleakGATTCharacteristic, data: bytearray
        ):
            logger.debug("Received: %s", str(binascii.hexlify(data)))
            await self.queue.put(data)

        if self.write_char is None or self.read_char is None:
            logger.error("Could not find required write/read BLE characteristics")
            if self.is_connected():
                await self.disconnect()
            return ResponseResult.NOT_AVAILABLE

        try:
            await self.client.start_notify(self.read_char, notification_handler)
            self._notify_started = True
        except BleakError as err:
            if _is_gatt_auth_error(err):
                logger.info(
                    "Notification subscription needs BLE authentication; "
                    "retrying pairing once"
                )
                try:
                    await self.client.pair()
                    await asyncio.sleep(1.0)
                    await self.client.start_notify(self.read_char, notification_handler)
                    self._notify_started = True
                except BleakError as retry_err:
                    logger.warning(
                        "Unable to subscribe to mower notifications after "
                        "pairing retry: %s",
                        retry_err,
                    )
                    if self.is_connected():
                        await self.disconnect()
                    return ResponseResult.NOT_ALLOWED
            else:
                logger.warning("Unable to subscribe to mower notifications: %s", err)
                if self.is_connected():
                    await self.disconnect()
                return ResponseResult.NOT_ALLOWED

        await asyncio.sleep(3.0)

        logger.debug("Setting channel ID")
        request = self.generate_request_setup_channel_id()
        response = await self._request_response_with_retry(request)
        if response is None:
            return ResponseResult.UNKNOWN_ERROR

        logger.debug("Generating request handshake")
        request = self.generate_request_handshake()
        response = await self._request_response(request)
        if response is None:
            return ResponseResult.UNKNOWN_ERROR

        if self.pin is not None:
            logger.debug("Entering operator PIN")
            command = Command(
                self.channel_id, (await self.get_protocol())["EnterOperatorPin"]
            )
            request = command.generate_request(code=self.pin)
            response = await self._request_response(request)
            if response is None:
                return ResponseResult.UNKNOWN_ERROR
            if command.validate_command_response(response) is False:
                logger.warning("PIN response failed validation")
                return ResponseResult.UNKNOWN_ERROR
            result = self.get_response_result(response)
            # If the result is UNKNOWN_ERROR, assume the pin was invalid
            if result == ResponseResult.UNKNOWN_ERROR:
                return ResponseResult.INVALID_PIN

            return result

        return ResponseResult.OK

    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def probe_gatts(self, device):
        logger.info("connecting to device...")
        if device is None:
            raise BleakError(f"Could not find device with address '{self.address}'")

        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or "Unknown Device",
        )
        logger.info("connected")

        manufacture = None
        model = None
        device_type = None

        try:
            for service in client.services:
                logger.debug("[Service] %s", service)

                if service.uuid == "98bd0001-0b0e-421a-84e5-ddbf75dc6de4":
                    manufacture = service.description

                for char in service.characteristics:
                    properties = ",".join(char.properties)
                    should_read = char.uuid in (
                        "00002a00-0000-1000-8000-00805f9b34fb",
                        PROTOCOL_DESCRIPTOR_CHAR,
                    )

                    if char.uuid in (WRITE_CHAR, READ_CHAR):
                        logger.debug("  [Characteristic] %s (%s)", char, properties)
                        continue

                    if "read" in char.properties and should_read:
                        try:
                            value = await client.read_gatt_char(char.uuid)
                            logger.debug(
                                "  [Characteristic] %s (%s), Value: %r",
                                char,
                                properties,
                                value,
                            )
                        except Exception as e:
                            logger.debug(
                                "  [Characteristic] %s (%s), Error: %s",
                                char,
                                properties,
                                e,
                            )
                            continue

                        if char.uuid == "00002a00-0000-1000-8000-00805f9b34fb":
                            model = value.decode()

                        if char.uuid == PROTOCOL_DESCRIPTOR_CHAR:
                            device_type = value.rstrip(b"\x00").decode()

                    elif "read" in char.properties:
                        logger.debug(
                            "  [Characteristic] %s (%s), Value skipped during probe",
                            char,
                            properties,
                        )
                    else:
                        logger.debug("  [Characteristic] %s (%s)", char, properties)
        finally:
            await client.disconnect()

        return (manufacture, device_type, model)

    async def disconnect(self):
        """
        Disconnect from the mower, this should be called after every
        `connect()` before the Python script exits
        """

        if self.client is None or self.read_char is None:
            return

        logger.info("disconnecting...")
        await self.client.disconnect()
        logger.info("disconnected")
        self.client = None
        self.write_char = None
        self.read_char = None
        self._notify_started = False

        await self.queue.put(None)

    def generate_request_setup_channel_id(self) -> bytearray:
        """
        Setup the channelID with an Automower, this is the first
        command that should be sent
        """
        data = bytearray.fromhex("02fd160000000000002e1400000000000000004d61696e00")

        # New ChannelID
        data[11:15] = self.channel_id.to_bytes(4, byteorder="little")

        # CRC and end byte
        data[9] = crc(data, 1, 8)
        data.append(crc(data, 1, len(data) - 1))
        data.append(0x03)

        return data

    def generate_request_handshake(self) -> bytearray:
        """
        Generate a request handshake. This should be called after
        the channel id is set up but before other commands
        """
        data = bytearray.fromhex("02fd0a000000000000d00801")

        data[4:8] = self.channel_id.to_bytes(4, byteorder="little")

        # CRCs and end byte
        data[9] = crc(data, 1, 8)
        data.append(crc(data, 1, len(data) - 1))
        data.append(0x03)

        return data

    def get_response_result(self, response_data: bytearray) -> ResponseResult:
        result_code = response_data[16]
        try:
            return ResponseResult(result_code)
        except ValueError:
            logger.debug("Unknown response result code: %d", result_code)
            return ResponseResult.UNKNOWN_ERROR

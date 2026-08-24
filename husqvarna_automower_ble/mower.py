"""
The top level script to connect and communicate with the mower
This sends requests and decodes responses. This is an example of
how the request and response classes can be used.
"""

import argparse
import asyncio
import logging
import datetime as dt

from husqvarna_automower_ble.protocol import (
    BLEClient,
    Command,
    MowerState,
    MowerActivity,
    ModeOfOperation,
    OverrideAction,
    ResponseResult,
)
from husqvarna_automower_ble.models import MowerModels
from husqvarna_automower_ble.error_codes import ErrorCodes

from bleak import BleakScanner

logger = logging.getLogger(__name__)

type ResponseScalar = int | str
type ResponseDict = dict[str, ResponseScalar]
type CommandResponse = ResponseScalar | ResponseDict | None
type CommandResult = tuple[CommandResponse, bool]


class Mower(BLEClient):
    def __init__(self, channel_id: int, address: str, pin: int | None = None):
        super().__init__(channel_id, address, pin)
        self.command_lock = asyncio.Lock()

    @staticmethod
    def _command_succeeded(command_name: str, result: ResponseResult) -> bool:
        # The StartTrigger command can return UNKNOWN_ERROR even when it succeeds
        return result == ResponseResult.OK or (
            command_name == "StartTrigger" and result == ResponseResult.UNKNOWN_ERROR
        )

    async def command(self, command_name: str, **kwargs) -> CommandResult:
        async with self.command_lock:
            return await self._command(command_name, **kwargs)

    async def _command(self, command_name: str, **kwargs) -> CommandResult:
        """
        This function is used to simplify the communication of the mower using the commands found in protocol.json.
        It will send a request to the mower and then wait for a response. The response will be parsed and returned to the caller.
        """
        protocol = await self.get_protocol()
        command_definition = protocol.get(command_name)
        if command_definition is None:
            logger.error("Unknown command: %s", command_name)
            return (None, False)

        command = Command(self.channel_id, command_definition)
        request = command.generate_request(**kwargs)
        response = await self._request_response(request)
        if response is None:
            return (None, False)

        if not command.validate_command_response(response):
            logger.warning("Response failed validation for command: %s", command_name)
            return (None, False)

        result = self.get_response_result(response)
        if not self._command_succeeded(command_name, result):
            logger.warning("Command failed: %s (%s)", command_name, result.name)
            return (None, False)

        response_dict = command.parse_response(response)
        if (
            response_dict is not None and len(response_dict) == 1
        ):  # If there is only one key in the response, return the value
            return (response_dict["response"], True)
        return (response_dict, True)

    async def get_manufacturer(self) -> str | None:
        """Get the mower manufacturer"""
        model, ok = await self.command("GetModel")
        if not ok or not isinstance(model, dict):
            return None

        device_type = model.get("deviceType")
        device_variant = model.get("deviceVariant")
        if not isinstance(device_type, int) or not isinstance(device_variant, int):
            return None

        model_information = MowerModels.get((device_type, device_variant))
        if model_information is None:
            return f"Unknown Manufacturer ({device_type}, {device_variant})"

        return model_information.manufacturer

    async def get_model(self) -> str | None:
        """Get the mower model."""
        model, ok = await self.command("GetModel")
        if not ok or not isinstance(model, dict):
            return None

        device_type = model.get("deviceType")
        device_variant = model.get("deviceVariant")
        if not isinstance(device_type, int) or not isinstance(device_variant, int):
            return None

        model_information = MowerModels.get((device_type, device_variant))
        if model_information is None:
            return f"Unknown Model ({device_type}, {device_variant})"

        return model_information.model

    async def get_serial_number(self) -> str | None:
        """Get the mower serial number."""
        serial_number, ok = await self.command("GetSerialNumber")
        if not ok or serial_number is None:
            return None
        if isinstance(serial_number, int):
            return str(serial_number)
        if isinstance(serial_number, str):
            return serial_number
        return None

    async def mower_name(self) -> str | None:
        """Query the mower name."""
        name, ok = await self.command("GetUserMowerNameAsAsciiString")
        return name if ok and isinstance(name, str) else None

    async def battery_level(self) -> int | None:
        """Query the mower battery level."""
        battery, ok = await self.command("GetBatteryLevel")
        return battery if ok and isinstance(battery, int) else None

    async def is_charging(self) -> bool:
        """Check if the mower is charging."""
        response, ok = await self.command("IsCharging")
        return bool(response) if ok and response is not None else False

    async def mower_mode(self) -> ModeOfOperation | None:
        """Query the mower mode"""
        mode, ok = await self.command("GetMode")
        if not ok or not isinstance(mode, int):
            return None
        return ModeOfOperation(mode)

    async def mower_state(self) -> MowerState | None:
        """Query the mower state"""
        state, ok = await self.command("GetState")
        if not ok or not isinstance(state, int):
            return None
        return MowerState(state)

    async def mower_activity(self) -> MowerActivity | None:
        """Query the mower activity"""
        activity, ok = await self.command("GetActivity")
        if not ok or not isinstance(activity, int):
            return None
        return MowerActivity(activity)

    async def mower_override_status(self) -> OverrideAction | None:
        """Query the mower override status"""
        override, ok = await self.command("GetOverride")
        if not ok or not isinstance(override, dict):
            return None

        action = override.get("action")
        if not isinstance(action, int):
            return None

        try:
            return OverrideAction(action)
        except ValueError:
            return None

    async def mower_error(self) -> ErrorCodes | None:
        """Query the mower error"""
        error, ok = await self.command("GetError")
        if not ok or not isinstance(error, int):
            return None
        return ErrorCodes(error)

    async def mower_next_start_time(self) -> dt.datetime | None:
        """Query the mower next start time"""
        next_start_time, ok = await self.command("GetNextStartTime")
        if not ok or not isinstance(next_start_time, int) or next_start_time == 0:
            return None
        return dt.datetime.fromtimestamp(next_start_time, dt.UTC).replace(tzinfo=None)

    async def mower_statistics(self) -> dict | None:
        """Query the mower statistics"""
        # Retrieve each statistic and set to None when the command failed
        async with self.command_lock:
            running, ok = await self._command("GetTotalRunningTime")
            cutting, ok2 = await self._command("GetTotalCuttingTime")
            charging, ok3 = await self._command("GetTotalChargingTime")
            searching, ok4 = await self._command("GetTotalSearchingTime")
            collisions, ok5 = await self._command("GetNumberOfCollisions")
            cycles, ok6 = await self._command("GetNumberOfChargingCycles")

        stats = {
            "totalRunningTime": running if ok else None,
            "totalCuttingTime": cutting if ok2 else None,
            "totalChargingTime": charging if ok3 else None,
            "totalSearchingTime": searching if ok4 else None,
            "numberOfCollisions": collisions if ok5 else None,
            "numberOfChargingCycles": cycles if ok6 else None,
        }

        # Check if all statistics are retrieved successfully
        if all(value is None for value in stats.values()):
            return None

        return stats

    async def mower_override(self, duration_hours: float = 3.0) -> bool:
        """
        Force the mower to run for the specified duration in hours.
        """
        if duration_hours <= 0:
            raise ValueError("Duration must be greater than 0")

        async with self.command_lock:
            _, ok1 = await self._command("SetMode", mode=ModeOfOperation.AUTO)
            _, ok2 = await self._command(
                "SetOverrideMow", duration=int(duration_hours * 3600)
            )
            _, ok3 = await self._command("StartTrigger")
        return ok1 and ok2 and ok3

    async def mower_pause(self) -> bool:
        """Pause the mower's current operation

        Returns True if the command succeeded.
        """
        _, ok = await self.command("Pause")
        return ok

    async def mower_resume(self) -> bool:
        """Resume the mower's operation

        Returns True if the command succeeded.
        """
        _, ok = await self.command("StartTrigger")
        return ok

    async def mower_park(self) -> bool:
        """Park the mower until the next scheduled start

        Returns True if the commands succeeded.
        """
        async with self.command_lock:
            _, ok1 = await self._command("SetOverrideParkUntilNextStart")
            _, ok2 = await self._command("StartTrigger")
        return ok1 and ok2

    async def mower_park_indefinitely(self) -> bool:
        """Park the mower indefinitely

        Returns True if the commands succeeded.
        """
        async with self.command_lock:
            _, ok1 = await self._command("ClearOverride")
            _, ok2 = await self._command("SetMode", mode=ModeOfOperation.HOME)
            _, ok3 = await self._command("StartTrigger")
        return ok1 and ok2 and ok3

    async def mower_auto(self) -> bool:
        """Set the mower to automatic operation

        Returns True if the commands succeeded.
        """
        async with self.command_lock:
            _, ok1 = await self._command("ClearOverride")
            _, ok2 = await self._command("SetMode", mode=ModeOfOperation.AUTO)
            _, ok3 = await self._command("StartTrigger")
        return ok1 and ok2 and ok3


async def main(mower: Mower, args: argparse.Namespace):
    device = await BleakScanner.find_device_by_address(mower.address, timeout=30)

    if device is None:
        print(f"Unable to connect to device address: {mower.address}")
        print(
            "Please make sure the device address is correct, the device is powered on, and nearby."
        )
        return

    try:
        connection_result = await mower.connect(device)

        if connection_result != ResponseResult.OK:
            print("Error connecting to device")
            print(f"Connection result: {connection_result.name}")
            return

        manufacturer = await mower.get_manufacturer()
        print(f"Mower manufacturer: {manufacturer or 'Unknown'}")

        model = await mower.get_model()
        print(f"Mower model: {model or 'Unknown'}")

        serial_number = await mower.get_serial_number()
        print(f"Mower serial number: {serial_number or 'Unknown'}")

        name = await mower.mower_name()
        print(f"Mower name: {name or 'Unknown'}")

        battery_level = await mower.battery_level()
        print(f"Battery is: {battery_level}%")

        charging = await mower.is_charging()
        print("Mower is charging" if charging else "Mower is not charging")

        mode = await mower.mower_mode()
        print(f"Mower mode: {mode.name if mode is not None else 'Unknown'}")

        state = await mower.mower_state()
        print(f"Mower state: {state.name if state is not None else 'Unknown'}")

        activity = await mower.mower_activity()
        print(f"Mower activity: {activity.name if activity is not None else 'Unknown'}")

        override_status = await mower.mower_override_status()
        print(
            f"Mower override status: {override_status.name if override_status is not None else 'Unknown'}"
        )

        error = await mower.mower_error()
        print(f"Mower error: {error.name if error is not None else 'Unknown'}")

        next_start_time = await mower.mower_next_start_time()
        if next_start_time:
            print(f"Next start time: {next_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("No next start time")

        statistics = await mower.mower_statistics()
        if statistics:
            print("Mower statistics:")
            for key, value in statistics.items():
                print(f"  {key}: {value}")
        else:
            print("No statistics available")

        if args.command:
            print(f"Sending command to control mower ({args.command})")
            match args.command:
                case "park":
                    print("command=park")
                    cmd_result = await mower.mower_park()
                case "pause":
                    print("command=pause")
                    cmd_result = await mower.mower_pause()
                case "resume":
                    print("command=resume")
                    cmd_result = await mower.mower_resume()
                case "override":
                    print("command=override")
                    cmd_result = await mower.mower_override()
                case _:
                    print(f"command=??? (Unknown command: {args.command})")
                    cmd_result = False
            print(f"command result = {cmd_result}")

    finally:
        await mower.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    device_group = parser.add_mutually_exclusive_group(required=True)

    device_group.add_argument(
        "--address",
        metavar="<address>",
        help="The Bluetooth address of the Automower device to connect to.",
    )

    parser.add_argument(
        "--pin",
        metavar="<code>",
        type=int,
        default=None,
        help="Send PIN to authenticate. This feature is experimental and might not work.",
    )

    parser.add_argument(
        "--command",
        metavar="<command>",
        default=None,
        help="Send command to control mower (one of resume, pause, park, or override).",
    )

    args = parser.parse_args()

    mower = Mower(1197489078, args.address, args.pin)

    log_level = logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)-15s %(name)-8s %(levelname)s: %(message)s",
    )

    asyncio.run(main(mower, args))

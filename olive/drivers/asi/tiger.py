from functools import lru_cache, partial
import logging
from typing import Union

import trio
from serial import Serial
from serial.tools import list_ports

from olive.core import Driver, DeviceInfo
from olive.devices import MotionController
from olive.devices.errors import (
    UnsupportedDeviceError,
    OutOfRangeError,
    UnknownCommandError,
)
from olive.devices.motion import Axis

from olive.drivers.asi import errors

__all__ = ["ASI", "Tiger"]

logger = logging.getLogger(__name__)


class ASIAxis(Axis):
    def __init__(self, parent, axis, *args, **kwargs):
        super().__init__(parent.driver, *args, parent, **kwargs)
        self._axis = axis

        self._info = None

    ##

    async def test_open(self):
        try:
            await self.open()
            if not await self.get_property("motor_control"):
                raise UnsupportedDeviceError("axis not connected")
            logger.info(f".. {self.info}")
        finally:
            await self.close()

    async def _open(self):
        self._info = DeviceInfo(vendor="ASI", model=self.axis)

    async def _close(self):
        self._info = None

    ##

    async def enumerate_properties(self):
        return ("motor_control",)

    async def _get_motor_control(self):
        status = self.parent.send_cmd("MC", f"{self.axis}?")
        return status == "1"

    ##

    async def home(self, blocking=True):
        self.parent.send_cmd("!", self.axis)
        if blocking:
            await self.wait()

    async def get_position(self):
        pass

    async def set_absolute_position(self, pos, blocking=True):
        # 0.1 micron per unit step
        pos *= 1000 * 10
        self.parent.send_cmd("M", **{self.axis: pos})
        if blocking:
            await self.wait()

    async def set_relative_position(self, pos, blocking=True):
        # 0.1 micron per unit step
        pos *= 1000 * 10
        self.parent.send_cmd("R", **{self.axis: pos})
        if blocking:
            print('start blocking')
            await self.wait()

    ##

    async def get_velocity(self):
        pass

    async def set_velocity(self, vel):
        self.parent.send_cmd("S", vel)

    ##

    async def get_acceleration(self):
        pass

    async def set_acceleration(self, acc):
        pass

    ##

    async def set_origin(self):
        self.parent.send_cmd("HM", f"{self.axis}+")

    async def get_limits(self):
        pass

    async def set_limits(self):
        pass

    ##

    async def calibrate(self):
        pass

    async def stop(self, emergency=False):
        self.parent.send_cmd("\\")

    async def wait(self):
        print("start waiting")
        while self.is_busy:
            await trio.sleep(1)

    ##

    @property
    def axis(self):
        return self._axis

    @property
    def info(self):
        return self._info

    @property
    def is_busy(self):
        return self.parent.is_busy

    @property
    def is_opened(self):
        return self.info is not None


class ASISerialCommandController(MotionController):
    def __init__(self, driver, port, *args, baudrate=115200, **kwargs):
        super().__init__(driver, *args, **kwargs)

        ser = Serial()
        ser.port = port
        ser.baudrate = baudrate
        self._handle, self._lock = ser, trio.StrictFIFOLock()

        self._info = None

    ##

    async def _open(self):
        await trio.to_thread.run_sync(self.handle.open)

        # create info
        model = self.send_cmd("BU")
        version = self.send_cmd("V")
        self._info = DeviceInfo(vendor="ASI", model=model, version=version)

    async def _close(self):
        self._info = None
        await trio.to_thread.run_sync(self.handle.close)

    ##

    async def enumerate_properties(self):
        return tuple()

    ##

    @property
    def handle(self):
        return self._handle

    @property
    def info(self):
        return self._info

    @property
    def is_busy(self):
        """
        STATUS is handles quickly in the ASI command parser. The official way to rapid poll.
        """
        self.handle.write(b"/\r")
        response = self.handle.read_until(b"\r\n")
        return response[0] == ord("B")

    @property
    def is_opened(self):
        return self.handle.is_open

    @property
    def lock(self):
        return self._lock

    ##

    def send_cmd(self, *args, **kwargs):
        # 1) compact
        args = [str(arg) for arg in args]
        kwargs = [f"{str(k)}={v}" for k, v in kwargs.items()]
        # 2) join
        cmd = " ".join(args + kwargs)
        # 3) response format
        cmd = f"{cmd}\r".encode()
        logger.debug(f"SEND {cmd}")
        # 4) send
        self.handle.write(cmd)
        return self._check_error()

    def _check_error(self):
        response = self.handle.read_until(b"\r\n")
        response = response.replace(b"\r", b"\n")
        response = response.decode("ascii").rstrip()
        logger.debug(f"RECV {response}")
        if response.startswith(":N"):
            errno = int(response[3:])
            ASISerialCommandController.interpret_error(errno)
        elif response.startswith(":A"):
            return response[3:]
        return response

    @staticmethod
    def interpret_error(errno):
        klass, msg = {
            1: (UnknownCommandError, ""),
            2: (errors.UnrecognizedAxisError, ""),
            3: (errors.MissingParameterError, ""),
            4: (OutOfRangeError, ""),
            5: (RuntimeError, "operation failed"),
            6: (RuntimeError, "undefined error"),
            7: (errors.InvalidCardAddressError, ""),
            21: (errors.HaltError, ""),
        }.get(errno, (errors.UnknownError, f"errno={errno}"))
        raise klass(msg)


class Tiger(ASISerialCommandController):
    async def test_open(self):
        try:
            await self.open()

            # test controller string
            if self.info.model != "TIGER_COMM":
                raise UnsupportedDeviceError
            logger.info(f".. {self.info}")
        finally:
            await self.close()

    ##

    async def enumerate_properties(self):
        return ("cards",) + await super().enumerate_properties()

    async def _get_cards(self):
        response = self.send_cmd("N")
        cards = []
        for line in response.split("\n"):
            # strip card address
            address, line = line.split(":", maxsplit=1)
            address = int(address[3:])

            # split options
            line = line.strip()
            function, version, character, *options = line.split(" ")

            cards.append(
                {
                    "address": address,
                    "character": character,
                    "version": version,
                    "function": function,
                }
            )
        return tuple(cards)

    ##

    async def enumerate_axes(self) -> Union[ASIAxis]:
        cards = await self.get_property("cards")

        print(">>>")
        import json  # noqa

        print(json.dumps(cards, indent=4))
        print("<<<")

        axes = []
        for card in cards:
            if card["character"] not in ("SCAN_XY_LED", "STD_ZF"):
                continue
            # parse axes identifier
            motors = card["function"].split(",")
            for motor in motors:
                axes.append(motor.split(":", maxsplit=1)[0])

        valid_axes = []
        logger.debug("TESTING VALID AXES")
        for axis in axes:
            try:
                axis = ASIAxis(self, axis)
                await axis.test_open()
                valid_axes.append(axis)
            except UnsupportedDeviceError:
                pass
        return tuple(valid_axes)


class ASI(Driver):
    def __init__(self):
        super().__init__()

    ##

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def enumerate_devices(self) -> Union[Tiger]:
        pass

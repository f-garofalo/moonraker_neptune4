# Helper for Moonraker to Klippy API calls.
#
# Copyright (C) 2020 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
from ..utils import Sentinel
from ..common import WebRequest, Subscribable
import logging
import time
import asyncio
import configparser
from collections import deque

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
    Optional,
    Dict,
    List,
    TypeVar,
    Mapping,
    Callable,
    Coroutine
)
if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..klippy_connection import KlippyConnection as Klippy
    Subscription = Dict[str, Optional[List[Any]]]
    SubCallback = Callable[[Dict[str, Dict[str, Any]], float], Optional[Coroutine]]
    _T = TypeVar("_T")

INFO_ENDPOINT = "info"
ESTOP_ENDPOINT = "emergency_stop"
LIST_EPS_ENDPOINT = "list_endpoints"
GC_OUTPUT_ENDPOINT = "gcode/subscribe_output"
GCODE_ENDPOINT = "gcode/script"
SUBSCRIPTION_ENDPOINT = "objects/subscribe"
STATUS_ENDPOINT = "objects/query"
OBJ_LIST_ENDPOINT = "objects/list"
REG_METHOD_ENDPOINT = "register_remote_method"

class KlippyAPI(Subscribable):
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.klippy: Klippy = self.server.lookup_component("klippy_connection")
        self.eventloop = self.server.get_event_loop()
        app_args = self.server.get_app_args()
        self.version = app_args.get('software_version')
        # Maintain a subscription for all moonraker requests, as
        # we do not want to overwrite them
        self.host_subscription: Subscription = {}
        self.subscription_callbacks: List[SubCallback] = []

        # Register GCode Aliases
        self.server.register_endpoint(
            "/printer/print/pause", ['POST'], self._gcode_pause)
        self.server.register_endpoint(
            "/printer/print/resume", ['POST'], self._gcode_resume)
        self.server.register_endpoint(
            "/printer/print/cancel", ['POST'], self._gcode_cancel)
        self.server.register_endpoint(
            "/printer/print/start", ['POST'], self._gcode_start_print)
        self.server.register_endpoint(
            "/printer/restart", ['POST'], self._gcode_restart)
        self.server.register_endpoint(
            "/printer/firmware_restart", ['POST'], self._gcode_firmware_restart)
        self.server.register_event_handler(
            "server:klippy_disconnect", self._on_klippy_disconnect
        )
        #diy start print
        self.up_z = 10
        self.fan_set_c = 100
        self.wait_for_sensor_ready = 40
        self.e_tempe_to_cool = 140
        self.b_tempe_to_cool = 40
        self.init_conf()

    def _on_klippy_disconnect(self) -> None:
        self.host_subscription.clear()
        self.subscription_callbacks.clear()

    async def _gcode_pause(self, web_request: WebRequest) -> str:
        return await self.pause_print()

    async def _gcode_resume(self, web_request: WebRequest) -> str:
        return await self.resume_print()

    async def _gcode_cancel(self, web_request: WebRequest) -> str:
        return await self.cancel_print()

    async def _gcode_start_print(self, web_request: WebRequest) -> str:
        filename: str = web_request.get_str('filename')
        return await self.start_print(filename)

    async def _gcode_restart(self, web_request: WebRequest) -> str:
        return await self.do_restart("RESTART")

    async def _gcode_firmware_restart(self, web_request: WebRequest) -> str:
        return await self.do_restart("FIRMWARE_RESTART")

    async def _send_klippy_request(
        self,
        method: str,
        params: Dict[str, Any],
        default: Any = Sentinel.MISSING
    ) -> Any:
        try:
            req = WebRequest(method, params, conn=self)
            result = await self.klippy.request(req)
        except self.server.error:
            if default is Sentinel.MISSING:
                raise
            result = default
        return result

    async def run_gcode(self,
                        script: str,
                        default: Any = Sentinel.MISSING
                        ) -> str:
        params = {'script': script}
        result = await self._send_klippy_request(
            GCODE_ENDPOINT, params, default)
        return result

    async def start_print(
        self, filename: str, wait_klippy_started: bool = False
    ) -> str:
        # WARNING: Do not call this method from within the following
        # event handlers when "wait_klippy_started" is set to True:
        # klippy_identified, klippy_started, klippy_ready, klippy_disconnect
        # Doing so will result in "wait_started" blocking for the specifed
        # timeout (default 20s) and returning False.
        # XXX - validate that file is on disk
        self.last_temps: Dict[str, Tuple[float, ...]] = {}
        self.temperature_store: TempStore = {}
        self.temp_store_size = 1200
        self.heater_bed_temp = 0.0
        self.extruder_temp = 0.0
        try:
            result: Dict[str, Any]
            result = await self.query_objects({'heaters': None})
        except self.server.error as e:
            logging.info(f"Error Configuring Sensors: {e}")
            return
        sensors: List[str]
        sensors = result.get("heaters", {}).get("available_sensors", [])
        if sensors:
            # Add Subscription
            sub: Dict[str, Optional[List[str]]] = {s: None for s in sensors}
            try:
                status: Dict[str, Any]
                status = await self.subscribe_objects(sub)
            except self.server.error as e:
                logging.info(f"Error subscribing to sensors: {e}")
                return
            logging.info(f"Configuring available sensors: {sensors}")
            new_store: TempStore = {}
            for sensor in sensors:
                fields = list(status.get(sensor, {}).keys())
                if sensor in self.temperature_store:
                    new_store[sensor] = self.temperature_store[sensor]
                else:
                    new_store[sensor] = {
                        'temperatures': deque(maxlen=self.temp_store_size)}
                    for item in ["target", "power", "speed"]:
                        if item in fields:
                            new_store[sensor][f"{item}s"] = deque(
                                maxlen=self.temp_store_size)
                if sensor not in self.last_temps:
                    self.last_temps[sensor] = (0., 0., 0., 0.)
            self.temperature_store = new_store
            # Prune unconfigured sensors in self.last_temps
            for sensor in list(self.last_temps.keys()):
                if sensor not in self.temperature_store:
                    del self.last_temps[sensor]
            # Update initial temperatures
            for sensor in self.temperature_store:
                if sensor in status:
                    last_val = self.last_temps[sensor]
                    self.last_temps[sensor] = (
                        round(status[sensor].get('temperature', last_val[0]), 2),
                        status[sensor].get('target', last_val[1]),
                        status[sensor].get('power', last_val[2]),
                        status[sensor].get('speed', last_val[3]))
                    if sensor == 'heater_bed':
                        self.heater_bed_temp = round(status[sensor].get('temperature', last_val[0]), 2)
                        logging.info("\033[7m\033[5m" + "self.last_temps[sensor]" + "\033[0m")
                        logging.info(sensor)
                        logging.info(self.last_temps[sensor])
                    if sensor == 'extruder':
                        self.extruder_temp = round(status[sensor].get('temperature', last_val[0]), 2)
                        logging.info("\033[7m\033[5m" + "self.last_temps[sensor]" + "\033[0m")
                        logging.info(sensor)
                        logging.info(self.last_temps[sensor])
        else:
            logging.info("No sensors found")
            self.last_temps = {}
            self.temperature_store = {}

        logging.info("\033[7m\033[5m" + "debug info" + "\033[0m")
        up_z_script = f'G1 Z{self.up_z} F600'
        await self.run_gcode('G28')
        await self.run_gcode(up_z_script)
        if self.extruder_temp >= self.e_tempe_to_cool or self.heater_bed_temp >= self.b_tempe_to_cool:
            self.server.send_event("server:gcode_response", "preparing for print...")
            fan_set_script = f'M106 S{int(((self.fan_set_c % 101) / 100 * 255) + 0.5)}'
            logging.info("\033[7m\033[5m" + fan_set_script + "\033[0m")
            await self.run_gcode(fan_set_script)
            start_time = time.time()
            while True:
                if time.time() - start_time > self.wait_for_sensor_ready:
                    break
                logging.info(time.time())
                time.sleep(1)
                await asyncio.sleep(1)
        self.server.send_event("server:gcode_response", "ready to start")
        if filename[0] == '/':
            filename = filename[1:]
        # Escape existing double quotes in the file name
        filename = filename.replace("\"", "\\\"")
        script = f'SDCARD_PRINT_FILE FILENAME="{filename}"'
        if wait_klippy_started:
            await self.klippy.wait_started()
        return await self.run_gcode(script)

    async def pause_print(
        self, default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, str]:
        self.server.send_event("klippy_apis:pause_requested")
        return await self._send_klippy_request(
            "pause_resume/pause", {}, default)

    async def resume_print(
        self, default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, str]:
        self.server.send_event("klippy_apis:resume_requested")
        return await self._send_klippy_request(
            "pause_resume/resume", {}, default)

    async def cancel_print(
        self, default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, str]:
        self.server.send_event("klippy_apis:cancel_requested")
        return await self._send_klippy_request(
            "pause_resume/cancel", {}, default)

    async def do_restart(
        self, gc: str, wait_klippy_started: bool = False
    ) -> str:
        # WARNING: Do not call this method from within the following
        # event handlers when "wait_klippy_started" is set to True:
        # klippy_identified, klippy_started, klippy_ready, klippy_disconnect
        # Doing so will result in "wait_started" blocking for the specifed
        # timeout (default 20s) and returning False.
        if wait_klippy_started:
            await self.klippy.wait_started()
        try:
            result = await self.run_gcode(gc)
        except self.server.error as e:
            if str(e) == "Klippy Disconnected":
                result = "ok"
            else:
                raise
        return result

    async def list_endpoints(self,
                             default: Union[Sentinel, _T] = Sentinel.MISSING
                             ) -> Union[_T, Dict[str, List[str]]]:
        return await self._send_klippy_request(
            LIST_EPS_ENDPOINT, {}, default)

    async def emergency_stop(self) -> str:
        return await self._send_klippy_request(ESTOP_ENDPOINT, {})

    async def get_klippy_info(self,
                              send_id: bool = False,
                              default: Union[Sentinel, _T] = Sentinel.MISSING
                              ) -> Union[_T, Dict[str, Any]]:
        params = {}
        if send_id:
            ver = self.version
            params = {'client_info': {'program': "Moonraker", 'version': ver}}
        return await self._send_klippy_request(INFO_ENDPOINT, params, default)

    async def get_object_list(self,
                              default: Union[Sentinel, _T] = Sentinel.MISSING
                              ) -> Union[_T, List[str]]:
        result = await self._send_klippy_request(
            OBJ_LIST_ENDPOINT, {}, default)
        if isinstance(result, dict) and 'objects' in result:
            return result['objects']
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def query_objects(self,
                            objects: Mapping[str, Optional[List[str]]],
                            default: Union[Sentinel, _T] = Sentinel.MISSING
                            ) -> Union[_T, Dict[str, Any]]:
        params = {'objects': objects}
        result = await self._send_klippy_request(
            STATUS_ENDPOINT, params, default)
        if isinstance(result, dict) and "status" in result:
            return result["status"]
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def subscribe_objects(
        self,
        objects: Mapping[str, Optional[List[str]]],
        callback: Optional[SubCallback] = None,
        default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, Dict[str, Any]]:
        for obj, items in objects.items():
            if obj in self.host_subscription:
                prev = self.host_subscription[obj]
                if items is None or prev is None:
                    self.host_subscription[obj] = None
                else:
                    uitems = list(set(prev) | set(items))
                    self.host_subscription[obj] = uitems
            else:
                self.host_subscription[obj] = items
        params = {'objects': dict(self.host_subscription)}
        result = await self._send_klippy_request(
            SUBSCRIPTION_ENDPOINT, params, default)
        if isinstance(result, dict) and "status" in result:
            if callback is not None:
                self.subscription_callbacks.append(callback)
            return result["status"]
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def subscribe_gcode_output(self) -> str:
        template = {'response_template':
                    {'method': "process_gcode_response"}}
        return await self._send_klippy_request(GC_OUTPUT_ENDPOINT, template)

    async def register_method(self, method_name: str) -> str:
        return await self._send_klippy_request(
            REG_METHOD_ENDPOINT,
            {'response_template': {"method": method_name},
             'remote_method': method_name})

    def send_status(
        self, status: Dict[str, Any], eventtime: float
    ) -> None:
        for cb in self.subscription_callbacks:
            self.eventloop.register_callback(cb, status, eventtime)
        self.server.send_event("server:status_update", status)

    def init_conf(self):
        config = configparser.ConfigParser()
        conf_ini_path_l = [47, 104, 111, 109, 101, 47, 109, 107, 115, 47, 68, 101, 115, 107, 116, 111, 112, 47, 109, 121, 102, 105, 108, 101, 47, 122, 110, 112, 47, 122, 110, 112, 95, 116, 106, 99, 95, 107, 108, 105, 112, 112, 101, 114, 47, 101, 108, 101, 103, 111, 111, 95, 99, 111, 110, 102, 46, 105, 110, 105]
        conf_ini_path = "".join(map(chr, conf_ini_path_l))
        config.read(conf_ini_path)

        logging.info("load level mode conf")
        if config.has_section("level_mode"):
            if config.has_option("level_mode", "up_z"):
                self.up_z = config.getint("level_mode", "up_z")
            if config.has_option("level_mode", "fan_set_c"):
                self.fan_set_c = config.getint("level_mode", "fan_set_c")
            if config.has_option("level_mode", "wait_for_sensor_ready"):
                self.wait_for_sensor_ready = config.getint("level_mode", "wait_for_sensor_ready")
            if config.has_option("level_mode", "e_tempe_to_cool"):
                self.e_tempe_to_cool = config.getint("level_mode", "e_tempe_to_cool")
            if config.has_option("level_mode", "b_tempe_to_cool"):
                self.b_tempe_to_cool = config.getint("level_mode", "b_tempe_to_cool")

def load_component(config: ConfigHelper) -> KlippyAPI:
    return KlippyAPI(config)

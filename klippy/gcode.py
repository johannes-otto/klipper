# Parse gcode commands
#
# Copyright (C) 2016  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, re, logging, collections
import homing, extruder

# Parse out incoming GCode and find and translate head movements
class GCodeParser:
    RETRY_TIME = 0.100
    def __init__(self, printer, fd, is_fileinput=False):
        self.printer = printer
        self.fd = fd
        self.is_fileinput = is_fileinput
        # Input handling
        self.reactor = printer.reactor
        self.is_processing_data = False
        self.fd_handle = None
        if not is_fileinput:
            self.fd_handle = self.reactor.register_fd(self.fd, self.process_data)
        self.partial_input = ""
        self.bytes_read = 0
        self.input_log = collections.deque([], 50)
        # Command handling
        self.gcode_handlers = self.build_handlers(False)
        self.is_printer_ready = False
        self.need_ack = False
        self.toolhead = self.fan = self.extruder = None
        self.heaters = []
        self.speed = 25.0
        self.absolutecoord = self.absoluteextrude = True
        self.base_position = [0.0, 0.0, 0.0, 0.0]
        self.last_position = [0.0, 0.0, 0.0, 0.0]
        self.homing_add = [0.0, 0.0, 0.0, 0.0]
        self.axis2pos = {'X': 0, 'Y': 1, 'Z': 2, 'E': 3}
    def build_handlers(self, is_ready):
        handlers = self.all_handlers
        if not is_ready:
            handlers = [h for h in handlers
                        if getattr(self, 'cmd_'+h+'_when_not_ready', False)]
        gcode_handlers = { h: getattr(self, 'cmd_'+h) for h in handlers }
        for h, f in list(gcode_handlers.items()):
            aliases = getattr(self, 'cmd_'+h+'_aliases', [])
            gcode_handlers.update({ a: f for a in aliases })
        return gcode_handlers
    def stats(self, eventtime):
        return "gcodein=%d" % (self.bytes_read,)
    def set_printer_ready(self, is_ready):
        if self.is_printer_ready == is_ready:
            return
        self.is_printer_ready = is_ready
        self.gcode_handlers = self.build_handlers(is_ready)
        if not is_ready:
            # Printer is shutdown (could be running in a background thread)
            return
        # Lookup printer components
        self.toolhead = self.printer.objects.get('toolhead')
        extruders = extruder.get_printer_extruders(self.printer)
        if extruders:
            self.extruder = extruders[0]
            self.toolhead.set_extruder(self.extruder)
        self.heaters = [ e.get_heater() for e in extruders ]
        self.heaters.append(self.printer.objects.get('heater_bed'))
        self.fan = self.printer.objects.get('fan')
        
        for i in range(99):
            extruder_fan_name='extruder_auto_fan%d' % (i,)
            extruder_fan=self.printer.objects.get(extruder_fan_name)
            if not i and not extruder_fan:
                extruder_fan=self.printer.objects.get('extruder_auto_fan')
            if extruder_fan:
                extruder_fan.printer_ready=True
                
        if self.is_fileinput and self.fd_handle is None:
            self.fd_handle = self.reactor.register_fd(self.fd, self.process_data)
    def motor_heater_off(self):
        if self.toolhead is None:
            return
        self.toolhead.motor_off()
        print_time = self.toolhead.get_last_move_time()
        for heater in self.heaters:
            if heater is not None:
                heater.set_temp(print_time, 0.)
        if self.fan is not None:
            self.fan.set_speed(print_time, 0.)
    def dump_debug(self):
        logging.info("Dumping gcode input %d blocks" % (
            len(self.input_log),))
        for eventtime, data in self.input_log:
            logging.info("Read %f: %s" % (eventtime, repr(data)))
    # Parse input into commands
    args_r = re.compile('([a-zA-Z_]+|[a-zA-Z*])')
    def process_commands(self, commands, need_ack=True):
        prev_need_ack = self.need_ack
        for line in commands:
            # Ignore comments and leading/trailing spaces
            line = origline = line.strip()
            cpos = line.find(';')
            if cpos >= 0:
                line = line[:cpos]
            # Break command into parts
            parts = self.args_r.split(line)[1:]
            params = { parts[i].upper(): parts[i+1].strip()
                       for i in range(0, len(parts), 2) }
            params['#original'] = origline
            if parts and parts[0].upper() == 'N':
                # Skip line number at start of command
                del parts[:2]
            if not parts:
                self.cmd_default(params)
                continue
            params['#command'] = cmd = parts[0].upper() + parts[1].strip()
            # Invoke handler for command
            self.need_ack = need_ack
            handler = self.gcode_handlers.get(cmd, self.cmd_default)
            try:
                handler(params)
            except error as e:
                self.respond_error(str(e))
            except:
                logging.exception("Exception in command handler")
                self.toolhead.force_shutdown()
                self.respond_error('Internal error on command:"%s"' % (cmd,))
                if self.is_fileinput:
                    self.printer.request_exit('exit_eof')
                    break
            self.ack()
        self.need_ack = prev_need_ack
    def process_data(self, eventtime):
        data = os.read(self.fd, 4096)
        self.input_log.append((eventtime, data))
        self.bytes_read += len(data)
        lines = data.split('\n')
        lines[0] = self.partial_input + lines[0]
        self.partial_input = lines.pop()
        if self.is_processing_data:
            if not lines:
                return
            if not self.is_fileinput and lines[0].strip().upper() == 'M112':
                self.cmd_M112({})
            self.reactor.unregister_fd(self.fd_handle)
            self.fd_handle = None
            while self.is_processing_data:
                eventtime = self.reactor.pause(eventtime + 0.100)
        self.is_processing_data = True
        self.process_commands(lines)
        self.is_processing_data = False
        if self.fd_handle is None:
            self.fd_handle = self.reactor.register_fd(self.fd, self.process_data)
        if not data and self.is_fileinput:
            self.motor_heater_off()
            self.printer.request_exit('exit_eof')
    # Response handling
    def ack(self, msg=None):
        if not self.need_ack or self.is_fileinput:
            return
        if msg:
            os.write(self.fd, "ok %s\n" % (msg,))
        else:
            os.write(self.fd, "ok\n")
        self.need_ack = False
    def respond(self, msg):
        logging.debug(msg)
        if self.is_fileinput:
            return
        os.write(self.fd, msg+"\n")
    def respond_info(self, msg):
        lines = [l.strip() for l in msg.strip().split('\n')]
        self.respond("// " + "\n// ".join(lines))
    def respond_error(self, msg):
        lines = msg.strip().split('\n')
        if len(lines) > 1:
            self.respond_info("\n".join(lines[:-1]))
        self.respond('!! %s' % (lines[-1].strip(),))
    # Parameter parsing helpers
    def get_int(self, name, params, default=None):
        if name in params:
            try:
                return int(params[name])
            except ValueError:
                raise error("Error on '%s': unable to parse %s" % (
                    params['#original'], params[name]))
        if default is not None:
            return default
        raise error("Error on '%s': missing %s" % (params['#original'], name))
    def get_float(self, name, params, default=None):
        if name in params:
            try:
                return float(params[name])
            except ValueError:
                raise error("Error on '%s': unable to parse %s" % (
                    params['#original'], params[name]))
        if default is not None:
            return default
        raise error("Error on '%s': missing %s" % (params['#original'], name))
    # Temperature wrappers
    def get_temp(self):
        if not self.is_printer_ready:
            return "T:0"
        # Tn:XXX /YYY B:XXX /YYY
        out = []
        for i, heater in enumerate(self.heaters):
            if heater is not None:
                cur, target = heater.get_temp()
                name = "B"
                if i < len(self.heaters) - 1:
                    name = "T%d" % (i,)
                out.append("%s:%.1f /%.1f" % (name, cur, target))
        return " ".join(out)
    def bg_temp(self, heater):
        if self.is_fileinput:
            return
        eventtime = self.reactor.monotonic()
        while self.is_printer_ready and heater.check_busy(eventtime):
            print_time = self.toolhead.get_last_move_time()
            self.respond(self.get_temp())
            eventtime = self.reactor.pause(eventtime + 1.)
    def set_temp(self, params, is_bed=False, wait=False):
        temp = self.get_float('S', params, 0.)
        heater = None
        if is_bed:
            heater = self.heaters[-1]
        elif 'T' in params:
            heater_index = self.get_int('T', params)
            if heater_index >= 0 and heater_index < len(self.heaters) - 1:
                heater = self.heaters[heater_index]
        elif self.extruder is not None:
            heater = self.extruder.get_heater()
        if heater is None:
            if temp > 0.:
                self.respond_error("Heater not configured")
            return
        print_time = self.toolhead.get_last_move_time()
        try:
            heater.set_temp(print_time, temp)
        except heater.error as e:
            self.respond_error(str(e))
            return
        if wait:
            self.bg_temp(heater)
    def set_fan_speed(self, speed):
        if self.fan is None:
            if speed:
                self.respond_info("Fan not configured")
            return
        print_time = self.toolhead.get_last_move_time()
        self.fan.set_speed(print_time, speed)
    # Individual command handlers
    def cmd_default(self, params):
        if not self.is_printer_ready:
            self.respond_error(self.printer.get_state_message())
            return
        cmd = params.get('#command')
        if not cmd:
            logging.debug(params['#original'])
            return
        if cmd[0] == 'T' and len(cmd) > 1 and cmd[1].isdigit():
            # Tn command has to be handled specially
            self.cmd_Tn(params)
            return
        self.respond('echo:Unknown command:"%s"' % (cmd,))
    def cmd_Tn(self, params):
        # Select Tool
        index = self.get_int('T', params)
        extruders = extruder.get_printer_extruders(self.printer)
        if self.extruder is None or index < 0 or index >= len(extruders):
            self.respond_error("Extruder %d not configured" % (index,))
            return
        e = extruders[index]
        if self.extruder is e:
            return
        deactivate_gcode = self.extruder.get_activate_gcode(False)
        self.process_commands(deactivate_gcode.split('\n'), need_ack=False)
        try:
            self.toolhead.set_extruder(e)
        except homing.EndstopError as e:
            self.respond_error(str(e))
            return
        self.extruder = e
        self.last_position = self.toolhead.get_position()
        activate_gcode = self.extruder.get_activate_gcode(True)
        self.process_commands(activate_gcode.split('\n'), need_ack=False)
    all_handlers = [
        'G1', 'G4', 'G20', 'G28', 'G90', 'G91', 'G92',
        'M82', 'M83', 'M18', 'M105', 'M104', 'M109', 'M112', 'M114', 'M115',
        'M140', 'M190', 'M106', 'M107', 'M206', 'M400',
        'IGNORE', 'QUERY_ENDSTOPS', 'PID_TUNE', 'RESTART', 'FIRMWARE_RESTART',
        'STATUS', 'HELP']
    cmd_G1_aliases = ['G0']
    def cmd_G1(self, params):
        # Move
        try:
            for a, p in self.axis2pos.items():
                if a in params:
                    v = float(params[a])
                    if (not self.absolutecoord
                        or (p>2 and not self.absoluteextrude)):
                        # value relative to position of last move
                        self.last_position[p] += v
                    else:
                        # value relative to base coordinate position
                        self.last_position[p] = v + self.base_position[p]
            if 'F' in params:
                speed = float(params['F']) / 60.
                if speed <= 0.:
                    raise ValueError()
                self.speed = speed
        except ValueError as e:
            self.last_position = self.toolhead.get_position()
            raise error("Unable to parse move '%s'" % (params['#original'],))
        try:
            self.toolhead.move(self.last_position, self.speed)
        except homing.EndstopError as e:
            self.respond_error(str(e))
            self.last_position = self.toolhead.get_position()
    def cmd_G4(self, params):
        # Dwell
        if 'S' in params:
            delay = self.get_float('S', params)
        else:
            delay = self.get_float('P', params, 0.) / 1000.
        self.toolhead.dwell(delay)
    def cmd_G20(self, params):
        # Set units to inches
        self.respond_error('Machine does not support G20 (inches) command')
    def cmd_G28(self, params):
        # Move to origin
        axes = []
        for axis in 'XYZ':
            if axis in params:
                axes.append(self.axis2pos[axis])
        if not axes:
            axes = [0, 1, 2]
        homing_state = homing.Homing(self.toolhead, axes)
        if self.is_fileinput:
            homing_state.set_no_verify_retract()
        try:
            self.toolhead.home(homing_state)
        except homing.EndstopError as e:
            self.toolhead.motor_off()
            self.respond_error(str(e))
            return
        newpos = self.toolhead.get_position()
        for axis in homing_state.get_axes():
            self.last_position[axis] = newpos[axis]
            self.base_position[axis] = -self.homing_add[axis]
    def cmd_G90(self, params):
        # Use absolute coordinates
        self.absolutecoord = True
    def cmd_G91(self, params):
        # Use relative coordinates
        self.absolutecoord = False
    def cmd_G92(self, params):
        # Set position
        offsets = { p: self.get_float(a, params)
                    for a, p in self.axis2pos.items() if a in params }
        for p, offset in offsets.items():
            self.base_position[p] = self.last_position[p] - offset
        if not offsets:
            self.base_position = list(self.last_position)
    def cmd_M82(self, params):
        # Use absolute distances for extrusion
        self.absoluteextrude = True
    def cmd_M83(self, params):
        # Use relative distances for extrusion
        self.absoluteextrude = False
    cmd_M18_aliases = ["M84"]
    def cmd_M18(self, params):
        # Turn off motors
        self.toolhead.motor_off()
    cmd_M105_when_not_ready = True
    def cmd_M105(self, params):
        # Get Extruder Temperature
        self.ack(self.get_temp())
    def cmd_M104(self, params):
        # Set Extruder Temperature
        self.set_temp(params)
    def cmd_M109(self, params):
        # Set Extruder Temperature and Wait
        self.set_temp(params, wait=True)
    def cmd_M112(self, params):
        # Emergency Stop
        self.toolhead.force_shutdown()
    cmd_M114_when_not_ready = True
    def cmd_M114(self, params):
        # Get Current Position
        if self.toolhead is None:
            self.cmd_default(params)
            return
        kinpos = self.toolhead.get_position()
        self.respond("X:%.3f Y:%.3f Z:%.3f E:%.3f Count X:%.3f Y:%.3f Z:%.3f" % (
            self.last_position[0], self.last_position[1],
            self.last_position[2], self.last_position[3],
            kinpos[0], kinpos[1], kinpos[2]))
    cmd_M115_when_not_ready = True
    def cmd_M115(self, params):
        # Get Firmware Version and Capabilities
        kw = {"FIRMWARE_NAME": "Klipper"
              , "FIRMWARE_VERSION": self.printer.software_version}
        self.ack(" ".join(["%s:%s" % (k, v) for k, v in kw.items()]))
    def cmd_M140(self, params):
        # Set Bed Temperature
        self.set_temp(params, is_bed=True)
    def cmd_M190(self, params):
        # Set Bed Temperature and Wait
        self.set_temp(params, is_bed=True, wait=True)
    def cmd_M106(self, params):
        # Set fan speed
        self.set_fan_speed(self.get_float('S', params, 255.) / 255.)
    def cmd_M107(self, params):
        # Turn fan off
        self.set_fan_speed(0.)
    def cmd_M206(self, params):
        # Set home offset
        offsets = { p: self.get_float(a, params)
                    for a, p in self.axis2pos.items() if a in params }
        for p, offset in offsets.items():
            self.base_position[p] += self.homing_add[p] - offset
            self.homing_add[p] = offset
    def cmd_M400(self, params):
        # Wait for current moves to finish
        self.toolhead.wait_moves()
    cmd_IGNORE_when_not_ready = True
    cmd_IGNORE_aliases = ["G21", "M110", "M21"]
    def cmd_IGNORE(self, params):
        # Commands that are just silently accepted
        pass
    cmd_QUERY_ENDSTOPS_help = "Report on the status of each endstop"
    cmd_QUERY_ENDSTOPS_aliases = ["M119"]
    def cmd_QUERY_ENDSTOPS(self, params):
        # Get Endstop Status
        if self.is_fileinput:
            return
        try:
            res = self.toolhead.query_endstops()
        except self.printer.mcu.error as e:
            self.respond_error(str(e))
            return
        self.respond(" ".join(["%s:%s" % (name, ["open", "TRIGGERED"][not not t])
                               for name, t in res]))
    cmd_PID_TUNE_help = "Run PID Tuning"
    cmd_PID_TUNE_aliases = ["M303"]
    def cmd_PID_TUNE(self, params):
        # Run PID tuning
        heater_index = self.get_int('E', params, 0)
        if (heater_index < -1 or heater_index >= len(self.heaters) - 1
            or self.heaters[heater_index] is None):
            self.respond_error("Heater not configured")
        heater = self.heaters[heater_index]
        temp = self.get_float('S', params)
        heater.start_auto_tune(temp)
        self.bg_temp(heater)
    def prep_restart(self):
        if self.is_printer_ready:
            self.respond_info("Preparing to restart...")
            self.motor_heater_off()
            self.toolhead.dwell(0.500)
            self.toolhead.wait_moves()
    cmd_RESTART_when_not_ready = True
    cmd_RESTART_help = "Reload config file and restart host software"
    def cmd_RESTART(self, params):
        self.prep_restart()
        self.printer.request_exit('restart')
    cmd_FIRMWARE_RESTART_when_not_ready = True
    cmd_FIRMWARE_RESTART_help = "Restart firmware, host, and reload config"
    def cmd_FIRMWARE_RESTART(self, params):
        self.prep_restart()
        self.printer.request_exit('firmware_restart')
    cmd_STATUS_when_not_ready = True
    cmd_STATUS_help = "Report the printer status"
    def cmd_STATUS(self, params):
        msg = self.printer.get_state_message()
        if self.is_printer_ready:
            self.respond_info(msg)
        else:
            self.respond_error(msg)
    cmd_HELP_when_not_ready = True
    def cmd_HELP(self, params):
        cmdhelp = []
        if not self.is_printer_ready:
            cmdhelp.append("Printer is not ready - not all commands available.")
        cmdhelp.append("Available extended commands:")
        for cmd in sorted(self.gcode_handlers):
            desc = getattr(self, 'cmd_'+cmd+'_help', None)
            if desc is not None:
                cmdhelp.append("%-10s: %s" % (cmd, desc))
        self.respond_info("\n".join(cmdhelp))

class error(Exception):
    pass

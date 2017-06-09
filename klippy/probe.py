# Z-Probe support
#
# Copyright (C) 2017  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import homing

class PrinterProbe:
    def __init__(self, printer, config):
        self.printer = printer
        self.speed = config.getfloat('speed', 5.0)
        self.z_distance = config.getfloat('max_distance', 20.0)
        self.mcu_probe = printer.mcu.create_endstop(config.get('pin'))
        toolhead = printer.objects['toolhead']
        z_steppers = toolhead.get_z_steppers()
        for s in z_steppers:
            self.mcu_probe.add_stepper(s.mcu_stepper)
        self.min_step_dist = min(s.step_dist for s in z_steppers)
    # External commands
    def probe_height(self):
        toolhead = self.printer.objects['toolhead']
        pos = toolhead.get_position()
        pos[2] -= self.z_distance
        # Start homing and issue move
        print_time = toolhead.get_last_move_time()
        mcu_time = self.mcu_probe.print_to_mcu_time(print_time)
        self.mcu_probe.home_start(mcu_time, self.min_step_dist / self.speed)
        toolhead.move(pos, self.speed)
        move_end_print_time = toolhead.get_last_move_time()
        move_end_mcu_time = self.mcu_probe.print_to_mcu_time(move_end_print_time)
        toolhead.reset_print_time()
        self.mcu_probe.home_finalize(move_end_mcu_time)
        # Wait for probe to trigger
        try:
            self.mcu_probe.home_wait()
        except self.mcu_probe.error as e:
            raise homing.EndstopError("Failed to probe: %s" % (str(e),))
        # Update with new position
        toolhead.reset_position()

def add_printer_objects(printer, config):
    if config.has_section('probe'):
        printer.add_object('probe', PrinterProbe(
            printer, config.getsection('probe')))

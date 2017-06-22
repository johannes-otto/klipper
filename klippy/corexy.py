# Code for handling the kinematics of corexy robots
#
# Copyright (C) 2017  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import stepper, homing

StepList = (0, 1, 2)

class CoreXYKinematics:
    def __init__(self, printer, config):
        self.steppers = [stepper.PrinterStepper(
            printer, config.getsection('stepper_' + n), n)
                         for n in ['x', 'y', 'z']]
        self.steppers[0].mcu_endstop.add_stepper(self.steppers[1].mcu_stepper)
        self.steppers[1].mcu_endstop.add_stepper(self.steppers[0].mcu_stepper)
        self.max_z_velocity = config.getfloat(
            'max_z_velocity', 9999999.9, above=0.)
        self.max_z_accel = config.getfloat(
            'max_z_accel', 9999999.9, above=0.)
        self.need_motor_enable = True
        self.limits = [(1.0, -1.0)] * 3
    def set_max_jerk(self, max_xy_halt_velocity, max_velocity, max_accel):
        self.steppers[0].set_max_jerk(max_xy_halt_velocity, max_accel)
        self.steppers[1].set_max_jerk(max_xy_halt_velocity, max_accel)
        self.steppers[2].set_max_jerk(0., self.max_z_accel)
    def get_position(self):
        pos = [s.mcu_stepper.get_commanded_position() for s in self.steppers]
        return [0.5 * (pos[0] + pos[1]), 0.5 * (pos[0] - pos[1]), pos[2]]
    def set_position(self, newpos):
        pos = (newpos[0] + newpos[1], newpos[0] - newpos[1], newpos[2])
        for i in StepList:
            self.steppers[i].mcu_stepper.set_position(pos[i])
    def home(self, homing_state):
        # Each axis is homed independently and in order
        for axis in homing_state.get_axes():
            s = self.steppers[axis]
            self.limits[axis] = (s.position_min, s.position_max)
            # Determine moves
            if s.homing_positive_dir:
                pos = s.position_endstop - 1.5*(
                    s.position_endstop - s.position_min)
                rpos = s.position_endstop - s.homing_retract_dist
                r2pos = rpos - s.homing_retract_dist
            else:
                pos = s.position_endstop + 1.5*(
                    s.position_max - s.position_endstop)
                rpos = s.position_endstop + s.homing_retract_dist
                r2pos = rpos + s.homing_retract_dist
            # Initial homing
            homepos = [None, None, None, None]
            homepos[axis] = s.position_endstop
            coord = [None, None, None, None]
            coord[axis] = pos
            homing_state.home(list(coord), homepos, [s], s.homing_speed)
            # Retract
            coord[axis] = rpos
            homing_state.retract(list(coord), s.homing_speed)
            # Home again
            coord[axis] = r2pos
            homing_state.home(
                list(coord), homepos, [s], s.homing_speed/2.0, second_home=True)
            if axis == 2:
                # Support endstop phase detection on Z axis
                coord[axis] = s.position_endstop + s.get_homed_offset()
                homing_state.set_homed_position(coord)
    def motor_off(self, move_time):
        self.limits = [(1.0, -1.0)] * 3
        for stepper in self.steppers:
            stepper.motor_enable(move_time, 0)
        self.need_motor_enable = True
    def _check_motor_enable(self, move_time, move):
        if move.axes_d[0] or move.axes_d[1]:
            self.steppers[0].motor_enable(move_time, 1)
            self.steppers[1].motor_enable(move_time, 1)
        if move.axes_d[2]:
            self.steppers[2].motor_enable(move_time, 1)
        need_motor_enable = False
        for i in StepList:
            need_motor_enable |= self.steppers[i].need_motor_enable
        self.need_motor_enable = need_motor_enable
    def query_endstops(self, print_time):
        endstops = [(s, s.query_endstop(print_time)) for s in self.steppers]
        return [(s.name, es.query_endstop_wait()) for s, es in endstops]
    def get_z_steppers(self):
        return [self.steppers[2]]
    def _check_endstops(self, move):
        end_pos = move.end_pos
        for i in StepList:
            if (move.axes_d[i]
                and (end_pos[i] < self.limits[i][0]
                     or end_pos[i] > self.limits[i][1])):
                if self.limits[i][0] > self.limits[i][1]:
                    raise homing.EndstopMoveError(
                        end_pos, "Must home axis first")
                raise homing.EndstopMoveError(end_pos)
    def check_move(self, move):
        limits = self.limits
        xpos, ypos = move.end_pos[:2]
        if (xpos < limits[0][0] or xpos > limits[0][1]
            or ypos < limits[1][0] or ypos > limits[1][1]):
            self._check_endstops(move)
        if not move.axes_d[2]:
            # Normal XY move - use defaults
            return
        # Move with Z - update velocity and accel for slower Z axis
        self._check_endstops(move)
        z_ratio = move.move_d / abs(move.axes_d[2])
        move.limit_speed(
            self.max_z_velocity * z_ratio, self.max_z_accel * z_ratio)
    def move(self, move_time, move):
        if self.need_motor_enable:
            self._check_motor_enable(move_time, move)
        sxp = move.start_pos[0]
        syp = move.start_pos[1]
        move_start_pos = (sxp + syp, sxp - syp, move.start_pos[2])
        exp = move.end_pos[0]
        eyp = move.end_pos[1]
        axes_d = ((exp + eyp) - move_start_pos[0],
                  (exp - eyp) - move_start_pos[1], move.axes_d[2])
        for i in StepList:
            axis_d = axes_d[i]
            if not axis_d:
                continue
            mcu_stepper = self.steppers[i].mcu_stepper
            mcu_time = mcu_stepper.print_to_mcu_time(move_time)
            start_pos = move_start_pos[i]
            axis_r = abs(axis_d) / move.move_d
            accel = move.accel * axis_r
            cruise_v = move.cruise_v * axis_r

            # Acceleration steps
            if move.accel_r:
                accel_d = move.accel_r * axis_d
                mcu_stepper.step_const(
                    mcu_time, start_pos, accel_d, move.start_v * axis_r, accel)
                start_pos += accel_d
                mcu_time += move.accel_t
            # Cruising steps
            if move.cruise_r:
                cruise_d = move.cruise_r * axis_d
                mcu_stepper.step_const(
                    mcu_time, start_pos, cruise_d, cruise_v, 0.)
                start_pos += cruise_d
                mcu_time += move.cruise_t
            # Deceleration steps
            if move.decel_r:
                decel_d = move.decel_r * axis_d
                mcu_stepper.step_const(
                    mcu_time, start_pos, decel_d, cruise_v, -accel)

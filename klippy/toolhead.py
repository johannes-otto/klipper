# Code for coordinating events on the printer toolhead
#
# Copyright (C) 2016  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging
import cartesian, corexy, delta, extruder

# Common suffixes: _d is distance (in mm), _v is velocity (in
#   mm/second), _v2 is velocity squared (mm^2/s^2), _t is time (in
#   seconds), _r is ratio (scalar between 0.0 and 1.0)

# Class to track each move request
class Move:
    def __init__(self, toolhead, start_pos, end_pos, speed):
        self.toolhead = toolhead
        self.start_pos = tuple(start_pos)
        self.end_pos = tuple(end_pos)
        self.accel = toolhead.max_accel
        self.is_kinematic_move = True
        self.axes_d = axes_d = [end_pos[i] - start_pos[i] for i in (0, 1, 2, 3)]
        self.move_d = move_d = math.sqrt(sum([d*d for d in axes_d[:3]]))
        if not move_d:
            # Extrude only move
            self.move_d = move_d = abs(axes_d[3])
            self.is_kinematic_move = False
        self.min_move_t = move_d / speed
        # Junction speeds are tracked in velocity squared.  The
        # delta_v2 is the maximum amount of this squared-velocity that
        # can change in this move.
        self.max_start_v2 = 0.
        self.max_cruise_v2 = speed**2
        self.delta_v2 = 2.0 * move_d * self.accel
        self.max_smoothed_v2 = 0.
        self.smooth_delta_v2 = 2.0 * move_d * toolhead.max_accel_to_decel
    def limit_speed(self, speed, accel):
        speed2 = speed**2
        if speed2 < self.max_cruise_v2:
            self.max_cruise_v2 = speed2
            self.min_move_t = self.move_d / speed
        self.accel = min(self.accel, accel)
        self.delta_v2 = 2.0 * self.move_d * self.accel
        self.smooth_delta_v2 = min(self.smooth_delta_v2, self.delta_v2)
    def calc_junction(self, prev_move):
        axes_d = self.axes_d
        prev_axes_d = prev_move.axes_d
        if (axes_d[2] or prev_axes_d[2] or self.accel != prev_move.accel
            or not self.is_kinematic_move or not prev_move.is_kinematic_move):
            return
        # Allow extruder to calculate its maximum junction
        extruder_v2 = self.toolhead.extruder.calc_junction(prev_move, self)
        # Find max velocity using approximated centripetal velocity as
        # described at:
        # https://onehossshay.wordpress.com/2011/09/24/improving_grbl_cornering_algorithm/
        junction_cos_theta = -((axes_d[0] * prev_axes_d[0]
                                + axes_d[1] * prev_axes_d[1])
                               / (self.move_d * prev_move.move_d))
        if junction_cos_theta > 0.999999:
            return
        junction_cos_theta = max(junction_cos_theta, -0.999999)
        sin_theta_d2 = math.sqrt(0.5*(1.0-junction_cos_theta))
        R = self.toolhead.junction_deviation * sin_theta_d2 / (1. - sin_theta_d2)
        self.max_start_v2 = min(
            R * self.accel, self.max_cruise_v2, prev_move.max_cruise_v2
            , extruder_v2, prev_move.max_start_v2 + prev_move.delta_v2)
        self.max_smoothed_v2 = min(
            self.max_start_v2
            , prev_move.max_smoothed_v2 + prev_move.smooth_delta_v2)
    def set_junction(self, start_v2, cruise_v2, end_v2):
        # Determine accel, cruise, and decel portions of the move distance
        inv_delta_v2 = 1. / self.delta_v2
        self.accel_r = accel_r = (cruise_v2 - start_v2) * inv_delta_v2
        self.decel_r = decel_r = (cruise_v2 - end_v2) * inv_delta_v2
        self.cruise_r = cruise_r = 1. - accel_r - decel_r
        # Determine move velocities
        self.start_v = start_v = math.sqrt(start_v2)
        self.cruise_v = cruise_v = math.sqrt(cruise_v2)
        self.end_v = end_v = math.sqrt(end_v2)
        # Determine time spent in each portion of move (time is the
        # distance divided by average velocity)
        self.accel_t = accel_r * self.move_d / ((start_v + cruise_v) * 0.5)
        self.cruise_t = cruise_r * self.move_d / cruise_v
        self.decel_t = decel_r * self.move_d / ((end_v + cruise_v) * 0.5)
    def move(self):
        # Generate step times for the move
        next_move_time = self.toolhead.get_next_move_time()
        if self.is_kinematic_move:
            self.toolhead.kin.move(next_move_time, self)
        if self.axes_d[3]:
            self.toolhead.extruder.move(next_move_time, self)
        self.toolhead.update_move_time(
            self.accel_t + self.cruise_t + self.decel_t)

LOOKAHEAD_FLUSH_TIME = 0.250

# Class to track a list of pending move requests and to facilitate
# "look-ahead" across moves to reduce acceleration between moves.
class MoveQueue:
    def __init__(self):
        self.extruder_lookahead = None
        self.queue = []
        self.leftover = 0
        self.junction_flush = LOOKAHEAD_FLUSH_TIME
    def reset(self):
        del self.queue[:]
        self.leftover = 0
        self.junction_flush = LOOKAHEAD_FLUSH_TIME
    def set_flush_time(self, flush_time):
        self.junction_flush = flush_time
    def set_extruder(self, extruder):
        self.extruder_lookahead = extruder.lookahead
    def flush(self, lazy=False):
        self.junction_flush = LOOKAHEAD_FLUSH_TIME
        update_flush_count = lazy
        queue = self.queue
        flush_count = len(queue)
        # Traverse queue from last to first move and determine maximum
        # junction speed assuming the robot comes to a complete stop
        # after the last move.
        delayed = []
        next_end_v2 = next_smoothed_v2 = peak_cruise_v2 = 0.
        for i in range(flush_count-1, self.leftover-1, -1):
            move = queue[i]
            reachable_start_v2 = next_end_v2 + move.delta_v2
            start_v2 = min(move.max_start_v2, reachable_start_v2)
            reachable_smoothed_v2 = next_smoothed_v2 + move.smooth_delta_v2
            smoothed_v2 = min(move.max_smoothed_v2, reachable_smoothed_v2)
            if smoothed_v2 < reachable_smoothed_v2:
                # It's possible for this move to accelerate
                if (smoothed_v2 + move.smooth_delta_v2 > next_smoothed_v2
                    or delayed):
                    # This move can decelerate or this is a full accel
                    # move after a full decel move
                    if update_flush_count and peak_cruise_v2:
                        flush_count = i
                        update_flush_count = False
                    peak_cruise_v2 = min(move.max_cruise_v2, (
                        smoothed_v2 + reachable_smoothed_v2) * .5)
                    if delayed:
                        # Propagate peak_cruise_v2 to any delayed moves
                        if not update_flush_count and i < flush_count:
                            for m, ms_v2, me_v2 in delayed:
                                mc_v2 = min(peak_cruise_v2, ms_v2)
                                m.set_junction(min(ms_v2, mc_v2), mc_v2
                                               , min(me_v2, mc_v2))
                        del delayed[:]
                if not update_flush_count and i < flush_count:
                    cruise_v2 = min((start_v2 + reachable_start_v2) * .5
                                    , move.max_cruise_v2, peak_cruise_v2)
                    move.set_junction(min(start_v2, cruise_v2), cruise_v2
                                      , min(next_end_v2, cruise_v2))
            else:
                # Delay calculating this move until peak_cruise_v2 is known
                delayed.append((move, start_v2, next_end_v2))
            next_end_v2 = start_v2
            next_smoothed_v2 = smoothed_v2
        if update_flush_count:
            return
        # Allow extruder to do its lookahead
        move_count = self.extruder_lookahead(queue, flush_count, lazy)
        # Generate step times for all moves ready to be flushed
        for move in queue[:move_count]:
            move.move()
        # Remove processed moves from the queue
        self.leftover = flush_count - move_count
        del queue[:move_count]
    def add_move(self, move):
        self.queue.append(move)
        if len(self.queue) == 1:
            return
        move.calc_junction(self.queue[-2])
        self.junction_flush -= move.min_move_t
        if self.junction_flush <= 0.:
            # There are enough queued moves to return to zero velocity
            # from the first move's maximum possible velocity, so at
            # least one move can be flushed.
            self.flush(lazy=True)

STALL_TIME = 0.100

# Main code to track events (and their timing) on the printer toolhead
class ToolHead:
    def __init__(self, printer, config):
        self.printer = printer
        self.reactor = printer.reactor
        self.extruder = extruder.DummyExtruder()
        kintypes = {'cartesian': cartesian.CartKinematics,
                    'corexy': corexy.CoreXYKinematics,
                    'delta': delta.DeltaKinematics}
        self.kin = config.getchoice('kinematics', kintypes)(printer, config)
        self.max_speed = config.getfloat('max_velocity', above=0.)
        self.max_accel = config.getfloat('max_accel', above=0.)
        self.max_accel_to_decel = config.getfloat(
            'max_accel_to_decel', self.max_accel * 0.5
            , above=0., maxval=self.max_accel)
        self.junction_deviation = config.getfloat(
            'junction_deviation', 0.02, above=0.)
        self.move_queue = MoveQueue()
        self.move_queue.set_extruder(self.extruder)
        self.commanded_pos = [0., 0., 0., 0.]
        # Print time tracking
        self.buffer_time_low = config.getfloat(
            'buffer_time_low', 1.000, above=0.)
        self.buffer_time_high = config.getfloat(
            'buffer_time_high', 2.000, above=self.buffer_time_low)
        self.buffer_time_start = config.getfloat(
            'buffer_time_start', 0.250, above=0.)
        self.move_flush_time = config.getfloat(
            'move_flush_time', 0.050, above=0.)
        self.print_time = 0.
        self.last_print_end_time = self.reactor.monotonic()
        self.need_check_stall = -1.
        self.print_stall = 0
        self.synch_print_time = True
        self.forced_synch = False
        self.flush_timer = self.reactor.register_timer(self._flush_handler)
        self.move_queue.set_flush_time(self.buffer_time_high)
        # Motor off tracking
        self.motor_off_time = config.getfloat(
            'motor_off_time', 600.000, minval=0.)
        self.motor_off_timer = self.reactor.register_timer(
            self._motor_off_handler)
        # Determine the maximum velocity a cartesian axis could have
        # before cornering.  The 8. was determined experimentally.
        xy_halt = math.sqrt(8. * self.junction_deviation * self.max_accel)
        self.kin.set_max_jerk(xy_halt, self.max_speed, self.max_accel)
        for e in extruder.get_printer_extruders(printer):
            e.set_max_jerk(xy_halt, self.max_speed, self.max_accel)
    # Print time tracking
    def update_move_time(self, movetime):
        self.print_time += movetime
        flush_to_time = self.print_time - self.move_flush_time
        self.printer.mcu.flush_moves(flush_to_time)
    def get_next_move_time(self):
        if self.synch_print_time:
            curtime = self.reactor.monotonic()
            if self.print_time:
                buffer_time = self.printer.mcu.get_print_buffer_time(
                    curtime, self.print_time)
                self.print_time += max(self.buffer_time_start - buffer_time, 0.)
                if self.forced_synch:
                    self.print_stall += 1
                    self.forced_synch = False
            else:
                self.printer.mcu.set_print_start_time(curtime)
                self.print_time = self.buffer_time_start
                self._reset_motor_off()
            self.reactor.update_timer(self.flush_timer, self.reactor.NOW)
            self.synch_print_time = False
        return self.print_time
    def _flush_lookahead(self, must_synch=False):
        synch_print_time = self.synch_print_time
        self.move_queue.flush()
        if synch_print_time or must_synch:
            self.synch_print_time = True
            self.move_queue.set_flush_time(self.buffer_time_high)
            self.printer.mcu.flush_moves(self.print_time)
    def get_last_move_time(self):
        self._flush_lookahead()
        return self.get_next_move_time()
    def reset_print_time(self):
        self._flush_lookahead(must_synch=True)
        self.print_time = 0.
        self.last_print_end_time = self.reactor.monotonic()
        self.need_check_stall = -1.
        self.forced_synch = False
        self._reset_motor_off()
    def _check_stall(self):
        eventtime = self.reactor.monotonic()
        if not self.print_time:
            # Building initial queue - make sure to flush on idle input
            self.reactor.update_timer(self.flush_timer, eventtime + 0.100)
            return
        # Check if there are lots of queued moves and stall if so
        while 1:
            buffer_time = self.printer.mcu.get_print_buffer_time(
                eventtime, self.print_time)
            stall_time = buffer_time - self.buffer_time_high
            if stall_time <= 0.:
                break
            eventtime = self.reactor.pause(eventtime + stall_time)
            if not self.print_time:
                return
        self.need_check_stall = self.print_time - stall_time + 0.100
    def _flush_handler(self, eventtime):
        try:
            if not self.print_time:
                # Input idled before filling lookahead queue - flush it
                self._flush_lookahead()
                if not self.print_time:
                    return self.reactor.NEVER
            print_time = self.print_time
            buffer_time = self.printer.mcu.get_print_buffer_time(
                eventtime, print_time)
            if buffer_time > self.buffer_time_low:
                # Running normally - reschedule check
                return eventtime + buffer_time - self.buffer_time_low
            # Under ran low buffer mark - flush lookahead queue
            self._flush_lookahead(must_synch=True)
            if print_time != self.print_time:
                # Flushed something - retry
                self.forced_synch = True
                return self.reactor.NOW
            if buffer_time > 0.:
                # Wait for buffer to fully empty
                return eventtime + buffer_time
            self.reset_print_time()
        except:
            logging.exception("Exception in flush_handler")
            self.force_shutdown()
        return self.reactor.NEVER
    # Motor off timer
    def _reset_motor_off(self):
        if not self.print_time:
            waketime = self.reactor.monotonic() + self.motor_off_time
        else:
            waketime = self.reactor.NEVER
        self.reactor.update_timer(self.motor_off_timer, waketime)
    def _motor_off_handler(self, eventtime):
        try:
            self.motor_off()
            self.reset_print_time()
        except:
            logging.exception("Exception in motor_off_handler")
            self.force_shutdown()
        return self.reactor.NEVER
    # Movement commands
    def get_position(self):
        return list(self.commanded_pos)
    def set_position(self, newpos):
        self._flush_lookahead()
        self.commanded_pos[:] = newpos
        self.kin.set_position(newpos)
    def reset_position(self):
        self.set_position(self.kin.get_position() + [self.commanded_pos[3]])
    def move(self, newpos, speed):
        speed = min(speed, self.max_speed)
        move = Move(self, self.commanded_pos, newpos, speed)
        if not move.move_d:
            return
        if move.is_kinematic_move:
            self.kin.check_move(move)
        if move.axes_d[3]:
            self.extruder.check_move(move)
        self.commanded_pos[:] = newpos
        self.move_queue.add_move(move)
        if self.print_time > self.need_check_stall:
            self._check_stall()
    def home(self, homing_state):
        self.kin.home(homing_state)
    def dwell(self, delay):
        self.get_last_move_time()
        self.update_move_time(delay)
        self._check_stall()
    def motor_off(self):
        self.dwell(STALL_TIME)
        last_move_time = self.get_last_move_time()
        self.kin.motor_off(last_move_time)
        self.extruder.motor_off(last_move_time)
        self.dwell(STALL_TIME)
        logging.debug('; Max time of %f' % (last_move_time,))
    def wait_moves(self):
        self._flush_lookahead()
        eventtime = self.reactor.monotonic()
        while self.print_time:
            eventtime = self.reactor.pause(eventtime + 0.100)
    def query_endstops(self):
        last_move_time = self.get_last_move_time()
        return self.kin.query_endstops(last_move_time)
    def set_extruder(self, extruder):
        last_move_time = self.get_last_move_time()
        self.extruder.set_active(last_move_time, False)
        extrude_pos = extruder.set_active(last_move_time, True)
        self.extruder = extruder
        self.move_queue.set_extruder(extruder)
        self.commanded_pos[3] = extrude_pos
    def get_z_steppers(self):
        return self.kin.get_z_steppers()
    # Misc commands
    def stats(self, eventtime):
        buffer_time = 0.
        print_time = self.print_time
        if print_time:
            is_active = True
            buffer_time = max(0., self.printer.mcu.get_print_buffer_time(
                eventtime, print_time))
        else:
            is_active = eventtime < self.last_print_end_time + 60.
        return is_active, "print_time=%.3f buffer_time=%.3f print_stall=%d" % (
            print_time, buffer_time, self.print_stall)
    def force_shutdown(self):
        try:
            self.printer.mcu.force_shutdown()
            self.move_queue.reset()
            self.reset_print_time()
        except:
            logging.exception("Exception in force_shutdown")

def add_printer_objects(printer, config):
    printer.add_object('toolhead', ToolHead(printer, config))

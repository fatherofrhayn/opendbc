import copy
import numpy as np
from opendbc.car import CanBusBase
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.hyundai.values import HyundaiFlags


class CanBus(CanBusBase):
  def __init__(self, CP, fingerprint=None, lka_steering=None) -> None:
    super().__init__(CP, fingerprint)

    if lka_steering is None:
      lka_steering = CP.flags & HyundaiFlags.CANFD_LKA_STEERING.value if CP is not None else False

    # On the CAN-FD platforms, the LKAS camera is on both A-CAN and E-CAN. LKA steering cars
    # have a different harness than the LFA steering variants in order to split
    # a different bus, since the steering is done by different ECUs.
    self._a, self._e = 1, 0
    if lka_steering:
      self._a, self._e = 0, 1

    self._a += self.offset
    self._e += self.offset
    self._cam = 2 + self.offset

  @property
  def ECAN(self):
    return self._e

  @property
  def ACAN(self):
    return self._a

  @property
  def CAM(self):
    return self._cam


def create_steering_messages(packer, CP, CAN, enabled, lat_active, apply_torque):
  common_values = {
    "LKA_MODE": 2,
    "LKA_ICON": 2 if enabled else 1,
    "TORQUE_REQUEST": apply_torque,
    "LKA_ASSIST": 0,
    "STEER_REQ": 1 if lat_active else 0,
    "STEER_MODE": 0,
    "HAS_LANE_SAFETY": 0,  # hide LKAS settings
    "NEW_SIGNAL_2": 0,
  }

  lkas_values = copy.copy(common_values)
  lkas_values["LKA_AVAILABLE"] = 0

  lfa_values = copy.copy(common_values)
  lfa_values["NEW_SIGNAL_1"] = 0

  ret = []
  if CP.flags & HyundaiFlags.CANFD_LKA_STEERING:
    lkas_msg = "LKAS_ALT" if CP.flags & HyundaiFlags.CANFD_LKA_STEERING_ALT else "LKAS"
    if CP.openpilotLongitudinalControl:
      ret.append(packer.make_can_msg("LFA", CAN.ECAN, lfa_values))
    ret.append(packer.make_can_msg(lkas_msg, CAN.ACAN, lkas_values))
  else:
    ret.append(packer.make_can_msg("LFA", CAN.ECAN, lfa_values))

  return ret

def create_suppress_lfa(packer, CAN, lfa_block_msg, lka_steering_alt):
  suppress_msg = "CAM_0x362" if lka_steering_alt else "CAM_0x2a4"
  msg_bytes = 32 if lka_steering_alt else 24

  values = {f"BYTE{i}": lfa_block_msg[f"BYTE{i}"] for i in range(3, msg_bytes) if i != 7}
  values["COUNTER"] = lfa_block_msg["COUNTER"]
  values["SET_ME_0"] = 0
  values["SET_ME_0_2"] = 0
  values["LEFT_LANE_LINE"] = 0
  values["RIGHT_LANE_LINE"] = 0
  return packer.make_can_msg(suppress_msg, CAN.ACAN, values)

def create_buttons(packer, CP, CAN, cnt, btn):
  values = {
    "COUNTER": cnt,
    "SET_ME_1": 1,
    "CRUISE_BUTTONS": btn,
  }

  bus = CAN.ECAN if CP.flags & HyundaiFlags.CANFD_LKA_STEERING else CAN.CAM
  return packer.make_can_msg("CRUISE_BUTTONS", bus, values)

def create_acc_cancel(packer, CP, CAN, cruise_info_copy):
  # TODO: why do we copy different values here?
  if CP.flags & HyundaiFlags.CANFD_CAMERA_SCC.value:
    values = {s: cruise_info_copy[s] for s in [
      "COUNTER",
      "CHECKSUM",
      "NEW_SIGNAL_1",
      "MainMode_ACC",
      "ACCMode",
      "ZEROS_9",
      "CRUISE_STANDSTILL",
      "ZEROS_5",
      "DISTANCE_SETTING",
      "VSetDis",
    ]}
  else:
    values = {s: cruise_info_copy[s] for s in [
      "COUNTER",
      "CHECKSUM",
      "ACCMode",
      "VSetDis",
      "CRUISE_STANDSTILL",
    ]}
  values.update({
    "ACCMode": 4,
    "aReqRaw": 0.0,
    "aReqValue": 0.0,
  })
  return packer.make_can_msg("SCC_CONTROL", CAN.ECAN, values)

def create_lfahda_cluster(packer, CAN, enabled):
  values = {
    "HDA_ICON": 1 if enabled else 0,
    "LFA_ICON": 2 if enabled else 0,
  }
  return packer.make_can_msg("LFAHDA_CLUSTER", CAN.ECAN, values)

def create_ccnc(packer, CAN, CP, CC, CS, lat_active):
  msg_161, msg_162 = CS.msg_161.copy(), CS.msg_162.copy()
  enabled, hud = CC.enabled, CC.hudControl

  for f in ("FAULT_LSS", "FAULT_HDA", "FAULT_DAS", "FAULT_LFA", "FAULT_DAW"):
    msg_162[f] = 0

  if msg_161.get("ALERTS_3") == 17:  # DRIVE_CAREFULLY
    msg_161["ALERTS_3"] = 0

  if msg_161.get("ALERTS_5") == 2:  # WATCH_FOR_SURROUNDING_VEHICLES
    msg_161["ALERTS_5"] = 0

  if msg_161.get("ALERTS_5") == 4:  # SMART_CRUISE_CONTROL_CONDITIONS_NOT_MET
    msg_161["ALERTS_5"] = 0

  if msg_161.get("ALERTS_5") == 5:  # USE_SWITCH_OR_PEDAL_TO_ACCELERATE
    msg_161["ALERTS_5"] = 0

  if msg_161.get("ALERTS_2") == 5:  # CONSIDER_TAKING_A_BREAK
    msg_161.update({"ALERTS_2": 0, "SOUNDS_2": 0})

  if msg_161.get("SOUNDS_4") == 2 and msg_161.get("LFA_ICON") in (3, 0,):  # LFA BEEPS
    msg_161["SOUNDS_4"] = 0

  msg_161.update({
    "DAW_ICON": 0,
    "LKA_ICON": 0,
    "LFA_ICON": 2 if lat_active or enabled else 1,
    "CENTERLINE": 1 if lat_active or enabled else 0,
    "LANELINE_LEFT": (
      1 if not hud.leftLaneVisible else
      4 if hud.leftLaneDepart else
      0 if not (lat_active or enabled) else
      2 if CS.out.leftBlindspot or CS.out.vEgo < 8.94 else 6
    ),
    "LANELINE_RIGHT": (
      1 if not hud.rightLaneVisible else
      4 if hud.rightLaneDepart else
      0 if not (lat_active or enabled) else
      2 if CS.out.rightBlindspot or CS.out.vEgo < 8.94 else 6
    ),
    "LCA_LEFT_ARROW": 2 if CC.leftBlinker else 0,
    "LCA_RIGHT_ARROW": 2 if CC.rightBlinker else 0,
    "LANE_LEFT": 1 if CC.leftBlinker else 0,
    "LANE_RIGHT": 1 if CC.rightBlinker else 0,
  })

  if hud.leftLaneDepart or hud.rightLaneDepart:
    msg_162["VIBRATE"] = 1

  if CP.openpilotLongitudinalControl:
    msg_161.update({
      "SETSPEED": 3 if enabled else 1,
      "SETSPEED_HUD": 2 if enabled else 1,
      "SETSPEED_SPEED": 25 if (s := round(CS.out.vCruiseCluster * (1 if CS.is_metric else CV.KPH_TO_MPH))) > 100 else s,
      "DISTANCE": hud.leadDistanceBars,
      "DISTANCE_SPACING": 1 if enabled else 0,
      "DISTANCE_LEAD": 2 if enabled and hud.leadVisible else 1 if enabled else 0,
      "DISTANCE_CAR": 2 if enabled else 1,
      "SLA_ICON": 0,
    })

    if msg_161.get("ALERTS_3") in (1 ,2, 3, 4, 7, 8, 9, 10):  # HIDE ISLA, DISTANCE MESSAGES
      msg_161["ALERTS_3"] = 0

    if msg_161.get("NAV_ICON") in (2, 4):  # DISABLE NAV IF AVAILABLE
      msg_161["NAV_ICON"] = 1

    msg_162["LEAD"] = 0

  return [packer.make_can_msg(msg, CAN.ECAN, data) for msg, data in [("CCNC_0x161", msg_161), ("CCNC_0x162", msg_162)]]

def create_acc_control(packer, CAN, enabled, accel_last, accel, stopping, gas_override, set_speed, hud_control, cruise_info=None):
  jerk = 5
  jn = jerk / 50
  a_raw, a_val = (0, 0) if not enabled or gas_override else (accel, np.clip(accel, accel_last - jn, accel_last + jn))

  values = {
    "ACCMode": 0 if not enabled else (2 if gas_override else 1),
    "MainMode_ACC": 1,
    "StopReq": 1 if stopping else 0,
    "aReqValue": a_val,
    "aReqRaw": a_raw,
    "VSetDis": set_speed,
    "JerkLowerLimit": jerk if enabled else 1,
    "JerkUpperLimit": 3.0,
    "ObjValid": 0,
    "OBJ_STATUS": 2,
    "SET_ME_2": 0x4,
    "SET_ME_3": 0x3,
    "SET_ME_TMP_64": 0x64,
    "DISTANCE_SETTING": hud_control.leadDistanceBars,
  }

  # fixes auto regen stuck on max for hybrids, should probably apply to all cars
  values.update({"ACC_ObjDist": 1} if cruise_info is None else {s: cruise_info[s] for s in ["ACC_ObjDist", "ACC_ObjRelSpd"]})

  return packer.make_can_msg("SCC_CONTROL", CAN.ECAN, values)


def create_spas_messages(packer, CAN, frame, left_blink, right_blink):
  ret = []

  values = {
  }
  ret.append(packer.make_can_msg("SPAS1", CAN.ECAN, values))

  blink = 0
  if left_blink:
    blink = 3
  elif right_blink:
    blink = 4
  values = {
    "BLINKER_CONTROL": blink,
  }
  ret.append(packer.make_can_msg("SPAS2", CAN.ECAN, values))

  return ret


def create_fca_warning_light(packer, CAN, frame):
  ret = []

  if frame % 2 == 0:
    values = {
      'AEB_SETTING': 0x1,  # show AEB disabled icon
      'SET_ME_2': 0x2,
      'SET_ME_FF': 0xff,
      'SET_ME_FC': 0xfc,
      'SET_ME_9': 0x9,
    }
    ret.append(packer.make_can_msg("ADRV_0x160", CAN.ECAN, values))
  return ret


def create_adrv_messages(packer, CAN, frame):
  # messages needed to car happy after disabling
  # the ADAS Driving ECU to do longitudinal control

  ret = []

  values = {
  }
  ret.append(packer.make_can_msg("ADRV_0x51", CAN.ACAN, values))

  ret.extend(create_fca_warning_light(packer, CAN, frame))

  if frame % 5 == 0:
    values = {
      'SET_ME_1C': 0x1c,
      'SET_ME_FF': 0xff,
      'SET_ME_TMP_F': 0xf,
      'SET_ME_TMP_F_2': 0xf,
    }
    ret.append(packer.make_can_msg("ADRV_0x1ea", CAN.ECAN, values))

    values = {
      'SET_ME_E1': 0xe1,
      'SET_ME_3A': 0x3a,
    }
    ret.append(packer.make_can_msg("ADRV_0x200", CAN.ECAN, values))

  if frame % 20 == 0:
    values = {
      'SET_ME_15': 0x15,
    }
    ret.append(packer.make_can_msg("ADRV_0x345", CAN.ECAN, values))

  if frame % 100 == 0:
    values = {
      'SET_ME_22': 0x22,
      'SET_ME_41': 0x41,
    }
    ret.append(packer.make_can_msg("ADRV_0x1da", CAN.ECAN, values))

  return ret
